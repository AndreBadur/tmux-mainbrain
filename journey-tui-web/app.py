"""journey-tui-web — the thin FastAPI app.

Serves a static sidebar+iframe page, binds the tmux-mainbrain motor in-process,
and owns ONE ttyd child whose tmux client is re-pointed via ``switch-client``.

Run (from this directory):
    PYTHONPATH=~/tmux-mainbrain python3 -m uvicorn app:app \
        --host 127.0.0.1 --port 8080

Requirements at runtime:
    * ``ttyd`` and ``tmux`` on PATH.
    * ``PYTHONPATH`` must include ~/tmux-mainbrain so ``import motor`` resolves.
    * The motor's journeys live under ~/tmux-mainbrain/journeys/*/meta.json.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import cascade as cascade_mod
from models import CascadeNode, ClientConfig, Health, JourneyTree, SwitchResult
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
#   * localhost dev   -> 127.0.0.1
#   * LAN             -> this machine's LAN IP (e.g. 192.168.15.145)
#   * DNS deployment  -> terminal.your-domain
PUBLIC_HOST = os.environ.get("JTW_PUBLIC_HOST", "127.0.0.1").strip()
# URL scheme the browser uses for ttyd (http unless you front it with TLS).
TTYD_SCHEME = os.environ.get("JTW_TTYD_SCHEME", "http").strip()
# The PUBLIC port the browser uses to reach ttyd. Defaults to the local ttyd
# port (direct/LAN access). Behind a reverse proxy or Cloudflare tunnel the
# public port is the standard 443/80, so set JTW_PUBLIC_PORT="" (empty) to omit
# the port from the iframe URL entirely (e.g. https://console-term.domain/).
PUBLIC_PORT = os.environ.get("JTW_PUBLIC_PORT", os.environ.get("JTW_TTYD_PORT", "7681")).strip()
# The tmux session ttyd attaches to at boot. Must be a LIVE session; the UI then
# switches away from it on click. Overridable; defaults to an env or the first
# alive governed knight discovered at startup.
BOOT_SESSION_ENV = os.environ.get("JTW_BOOT_SESSION")

# A dedicated plain-shell tmux session for the operator's own direct terminal
# access (the "Console" button) — NOT a knight. Created on demand.
CONSOLE_SESSION = os.environ.get("JTW_CONSOLE_SESSION", "jtw-console")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _terminal_url() -> str:
    """The URL a browser should use for the embedded ttyd terminal.

    Omits the port when ``JTW_PUBLIC_PORT`` is empty (reverse proxy / Cloudflare
    tunnel serving on the standard 443/80).
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


def _first_alive_target(trees: list[JourneyTree]) -> Optional[str]:
    """First clickable (alive + has tmux) node's tmux session, DFS order."""

    def walk(node: CascadeNode) -> Optional[str]:
        if node.clickable and node.tmux_session:
            return node.tmux_session
        for child in node.children:
            found = walk(child)
            if found is not None:
                return found
        return None

    for tree in trees:
        found = walk(tree.root)
        if found is not None:
            return found
    return None


def _load_trees() -> tuple[list[JourneyTree], list[str]]:
    scan = _import_motor_scan()
    return cascade_mod.load_cascade(ROOT_DIR, scan)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the ONE ttyd child here (never at import time)."""
    boot_session = BOOT_SESSION_ENV
    if not boot_session:
        try:
            trees, _errors = await run_in_threadpool(_load_trees)
            boot_session = _first_alive_target(trees)
        except Exception as exc:  # noqa: BLE001 - startup must surface, not crash import
            app.state.startup_error = f"cascade load at startup failed: {exc}"
            boot_session = None
    if not boot_session:
        # No live knight to attach to. Fall back to a self-session so ttyd can
        # still start; the operator can set JTW_BOOT_SESSION explicitly.
        app.state.startup_error = (
            "no live governed knight with a tmux session found; "
            "set JTW_BOOT_SESSION to a live tmux session"
        )
        ttyd = None
    else:
        ttyd = TtydManager(
            boot_session=boot_session,
            port=TTYD_PORT,
            bind=TTYD_BIND,
            credential=TTYD_CRED,
        )
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
