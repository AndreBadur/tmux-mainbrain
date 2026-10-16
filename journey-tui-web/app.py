"""journey-tui-web — the thin FastAPI app.

Serves a static sidebar+iframe page, binds the tmux-mainbrain motor in-process,
and owns ONE ttyd child whose tmux client is re-pointed via ``switch-client``.

Run (from this directory):
    PYTHONPATH=~/tmux-mainbrain python3 -m uvicorn app:app \
        --host 127.0.0.1 --port 8787

Requirements at runtime:
    * ``ttyd`` and ``tmux`` on PATH.
    * ``PYTHONPATH`` must include ~/tmux-mainbrain so ``import motor`` resolves.
    * The motor's journeys live under ~/tmux-mainbrain/journeys/*/meta.json.
"""
from __future__ import annotations

import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import cascade as cascade_mod
import journey as journey_mod
from models import (
    CascadeNode,
    ClientConfig,
    Health,
    JourneyResult,
    JourneyTree,
    NewJourney,
    SwitchResult,
)
from ttyd_manager import DEFAULT_BIND, TtydManager

# --- configuration (env-overridable) -----------------------------------------

# The Realm's stable ground. meta.json + the motor index live here.
ROOT_DIR = Path(os.environ.get("JTW_ROOT", str(Path.home() / "tmux-mainbrain")))

# FastAPI bind. 127.0.0.1 (default) = localhost only; 0.0.0.0 = reachable on the
# LAN. The uvicorn --host flag still governs the actual socket; this value is
# informational + used when the app launches uvicorn itself.
APP_BIND = os.environ.get("JTW_BIND", DEFAULT_BIND)

TTYD_PORT = int(os.environ.get("JTW_TTYD_PORT", "7681"))
# ttyd bind. 0.0.0.0 exposes the terminal on the LAN — pair it with a credential.
TTYD_BIND = os.environ.get("JTW_TTYD_BIND", DEFAULT_BIND)
# ttyd HTTP basic-auth as "user:pass". Empty = no auth (safe only on localhost).
# Prefer providing this via ~/tmux-mainbrain/.env.secrets (JTW_TTYD_CRED=...) so
# it never lives in the shell history or the alias.
TTYD_CRED = os.environ.get("JTW_TTYD_CRED", "").strip() or None
# The host a BROWSER uses to reach ttyd. The iframe is built from this at
# runtime (NOT hardcoded), so a remote browser points at the right place:
#   * localhost       -> 127.0.0.1 (default)
#   * LAN             -> this machine's LAN IP (e.g. 192.168.1.50)
PUBLIC_HOST = os.environ.get("JTW_PUBLIC_HOST", "127.0.0.1").strip()
# URL scheme the browser uses for ttyd (http for localhost/LAN).
TTYD_SCHEME = os.environ.get("JTW_TTYD_SCHEME", "http").strip()
# The PUBLIC port the browser uses to reach ttyd. Defaults to the local ttyd
# port (direct localhost/LAN access). Set JTW_PUBLIC_PORT="" (empty) to omit the
# port from the iframe URL (only needed if a reverse proxy serves ttyd on 80/443).
PUBLIC_PORT = os.environ.get("JTW_PUBLIC_PORT", os.environ.get("JTW_TTYD_PORT", "7681")).strip()
# OPTIONAL operator override for the boot session. When set AND live, ttyd
# attaches to it; otherwise (the default) the app auto-creates and boots the
# console shell (CONSOLE_SESSION) — boot is knight-independent and deterministic.
BOOT_SESSION_ENV = os.environ.get("JTW_BOOT_SESSION")

# A dedicated plain-shell tmux session for the operator's own direct terminal
# access (the "Console" button) — NOT a knight. Created on demand.
CONSOLE_SESSION = os.environ.get("JTW_CONSOLE_SESSION", "jtw-console")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _terminal_url() -> str:
    """The URL a browser should use for the embedded ttyd terminal.

    Omits the port when ``JTW_PUBLIC_PORT`` is empty (a reverse proxy serving
    ttyd on the standard 80/443).
    """
    host = f"{PUBLIC_HOST}:{PUBLIC_PORT}" if PUBLIC_PORT else PUBLIC_HOST
    return f"{TTYD_SCHEME}://{host}/"

# The motor is imported in-process (ZERO process boundary). PYTHONPATH must
# include ROOT_DIR. We import lazily inside the loader to keep import-time clean
# and to let tests inject a stub without the real motor present.


def _import_motor_scan():
    from motor import scan_knights  # noqa: PLC0415 - lazy, in-process binding
    return scan_knights


def _import_motor_resume_clean():
    from motor import resume_clean  # noqa: PLC0415 - lazy, in-process binding
    return resume_clean


def _import_motor_spawn():
    from motor import spawn  # noqa: PLC0415 - lazy, in-process binding
    return spawn


# --- thin tmux helpers for journey bootstrap (fixed argv, bounded timeout) ---

def _tmux_session_exists(name: str) -> bool:
    try:
        r = subprocess.run(["tmux", "has-session", "-t", name],  # noqa: S603/S607
                           capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _tmux_capture_pane(name: str) -> str:
    try:
        r = subprocess.run(["tmux", "capture-pane", "-t", name, "-p", "-S", "-80"],
                           capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _tmux_kill_session(name: str) -> None:
    # Best-effort cleanup of a half-spawned king. Bounded; never raises up.
    try:
        subprocess.run(["tmux", "kill-session", "-t", name],  # noqa: S603/S607
                       capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _load_trees() -> tuple[list[JourneyTree], list[str]]:
    scan = _import_motor_scan()
    return cascade_mod.load_cascade(ROOT_DIR, scan)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the ONE ttyd child here (never at import time).

    Boot order (deterministic, knight-independent):
      a. JTW_BOOT_SESSION set AND live  -> use it (explicit operator override).
      b. otherwise                      -> the dedicated console shell session
         (CONSOLE_SESSION), auto-created if missing. ttyd ALWAYS has a session
         to attach to, so startup never fails because a knight is dead.
    Knights are reached only by clicking (switch-client), never at boot.
    """
    ttyd = TtydManager(
        boot_session=CONSOLE_SESSION,  # provisional; finalized below
        port=TTYD_PORT,
        bind=TTYD_BIND,
        credential=TTYD_CRED,
    )

    boot_session = CONSOLE_SESSION
    override = (BOOT_SESSION_ENV or "").strip()
    if override:
        # Explicit override: honor it ONLY if it is actually live, else fail
        # loud (the operator asked for a specific session that isn't there).
        if ttyd.session_exists(override):
            boot_session = override
        else:
            app.state.startup_error = (
                f"JTW_BOOT_SESSION='{override}' is not a live tmux session"
            )
            raise RuntimeError(app.state.startup_error)
    else:
        # Default: always boot to the auto-created console shell. Create it
        # BEFORE start() so `tmux attach -t <console>` never races a missing
        # session.
        if not ttyd.ensure_console_session(CONSOLE_SESSION, cwd=str(ROOT_DIR)):
            app.state.startup_error = (
                f"could not create console session '{CONSOLE_SESSION}'"
            )
            raise RuntimeError(app.state.startup_error)
        boot_session = CONSOLE_SESSION

    ttyd.boot_session = boot_session
    ttyd.start()
    app.state.ttyd = ttyd
    try:
        yield
    finally:
        if ttyd is not None:
            ttyd.stop()


app = FastAPI(title="journey-tui-web", lifespan=lifespan)
app.state.ttyd = None
app.state.startup_error = None


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Tell browsers to always revalidate the page + static assets.

    The ETag still allows a cheap 304, but ``no-cache`` forbids serving a stale
    copy WITHOUT revalidating — so a UI update is never masked by a cached
    index.html / app.js on a phone or a proxy.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    """Serve the single static page."""
    index_html = STATIC_DIR / "index.html"
    if not index_html.is_file():
        raise HTTPException(status_code=500, detail="static/index.html missing")
    return FileResponse(str(index_html))


@app.get("/api/health", response_model=Health)
def health() -> Health:
    ttyd = app.state.ttyd
    return Health(ok=True, ttyd_up=bool(ttyd and ttyd.is_running()))


@app.get("/api/config", response_model=ClientConfig)
def client_config() -> ClientConfig:
    """Runtime hints for the front-end: where to point the terminal iframe and
    whether the terminal will prompt for basic-auth."""
    return ClientConfig(terminal_url=_terminal_url(), auth_required=bool(TTYD_CRED))


# Blocking I/O (meta.json reads + motor scan) -> plain def runs in the
# threadpool, never stalling the event loop.
@app.get("/api/cascade", response_model=list[JourneyTree])
def get_cascade() -> list[JourneyTree]:
    trees, _errors = _load_trees()
    return trees


def _derive_tmux_name(node: CascadeNode) -> str:
    """A deterministic tmux session name to (re)start a dead knight under.

    Prefer the name stored in meta.json; otherwise derive ``<role>-<idsuffix>``
    (e.g. the king -> ``king-354dd79f``), matching the motor's own convention so
    a revived king lands on the expected session name.
    """
    if node.tmux_session:
        return node.tmux_session
    suffix = cascade_mod._uuid_suffix(node.session_id)
    role = (node.role or "knight").strip().lower()
    return f"{role}-{suffix}" if suffix else role


@app.post("/api/switch/{session_id}", response_model=SwitchResult)
def switch(session_id: str) -> SwitchResult:
    ttyd = app.state.ttyd
    if ttyd is None:
        raise HTTPException(status_code=503, detail="ttyd is not running")

    trees, _errors = _load_trees()
    node = cascade_mod.resolve_node(trees, session_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"unknown session_id: {session_id}")

    # Live tmux session -> straight switch (no side effects).
    if node.clickable and node.tmux_session:
        return ttyd.switch_client(node.tmux_session)

    # No live tmux pane. If the knight is revivable (has a resumable kiro
    # session id), AUTO-START a fresh tmux session via the motor's safe
    # resume_clean (clears a DEAD-owner stale .lock first; refuses if the lock
    # owner is alive; never kills anything), then switch to it.
    if node.revivable and node.session_id:
        return _revive_and_switch(ttyd, node)

    if not node.session_id:
        return SwitchResult(ok=False, error="knight has no resumable session to attach")
    return SwitchResult(ok=False, error="knight is not attachable")


def _revive_and_switch(ttyd, node: CascadeNode) -> SwitchResult:
    """Rebuild a dead knight's tmux runtime, then point the terminal at it.

    Uses motor ``resume_clean`` (stale-lock-aware). Bounded by the motor's own
    ready-timeout; any motor failure is surfaced, never swallowed.
    """
    target = _derive_tmux_name(node)
    try:
        resume_clean = _import_motor_resume_clean()
        result = resume_clean(node.session_id, tmux_session=target, cwd=str(ROOT_DIR))
    except Exception as exc:  # noqa: BLE001 - surface the motor's failure to the UI
        return SwitchResult(ok=False, target=target, error=f"revive failed: {exc}")

    revived_session = str(result.get("tmux_session") or target)
    return ttyd.switch_client(revived_session)


@app.post("/api/console", response_model=SwitchResult)
def console() -> SwitchResult:
    """Open the operator's own plain-shell terminal in the embedded view.

    Ensures a dedicated tmux session (``JTW_CONSOLE_SESSION``) running a normal
    interactive shell exists, then re-points ttyd to it — so you can type
    arbitrary commands directly from the web app, independent of any knight.
    """
    ttyd = app.state.ttyd
    if ttyd is None:
        raise HTTPException(status_code=503, detail="ttyd is not running")
    if not ttyd.ensure_console_session(CONSOLE_SESSION, cwd=str(ROOT_DIR)):
        return SwitchResult(
            ok=False, target=CONSOLE_SESSION,
            error=f"could not create console session '{CONSOLE_SESSION}'",
        )
    return ttyd.switch_client(CONSOLE_SESSION)


@app.post("/api/scroll/{direction}", response_model=SwitchResult)
def scroll(direction: str) -> SwitchResult:
    """Scroll the embedded kiro-cli terminal's tmux scrollback.

    ``direction`` is ``up`` or ``down``. This drives tmux copy-mode server-side
    (the terminal is a cross-origin iframe the page cannot scroll directly), so
    the arrow buttons move the ACTUAL terminal history.
    """
    ttyd = app.state.ttyd
    if ttyd is None:
        raise HTTPException(status_code=503, detail="ttyd is not running")
    step = 10  # lines per tap
    if direction == "up":
        return ttyd.scroll(step)
    if direction == "down":
        return ttyd.scroll(-step)
    raise HTTPException(status_code=400, detail="direction must be 'up' or 'down'")


@app.post("/api/journey", response_model=JourneyResult)
def new_journey(body: NewJourney) -> JourneyResult:
    """Bootstrap a fresh wise-king journey: create dirs, spawn the king (named
    after the journey, at ROOT_DIR), capture its session id, write meta.json.

    The new journey appears in the cascade on the next 3s poll (no extra wiring)
    because cascade.py globs ``journeys/*/meta.json``.
    """
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="journey name must not be empty")

    spawn = _import_motor_spawn()
    scan = _import_motor_scan()

    def capture_with_settle(session: str) -> str:
        # Give kiro a moment to render the startup banner before reading it.
        time.sleep(1.5)
        return _tmux_capture_pane(session)

    try:
        result = journey_mod.create_journey(
            name,
            ROOT_DIR,
            spawn=spawn,
            session_exists=_tmux_session_exists,
            capture_pane=capture_with_settle,
            scan_knights=scan,
            kill_session=_tmux_kill_session,
        )
    except journey_mod.JourneyError as exc:
        msg = str(exc)
        # Collision -> 409; invalid name -> 400; everything else -> 500.
        if "already exists" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "must not be empty" in msg or "no usable characters" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=500, detail=msg)

    return JourneyResult(ok=True, **result)
