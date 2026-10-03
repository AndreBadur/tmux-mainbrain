---
name: journey-tui-web-fastapi
description: >
  Build/edit the journey-tui-web backend — a tiny Python FastAPI app that serves a static
  sidebar+iframe page, binds the tmux-mainbrain motor in-process, and drives ONE ttyd child
  (tmux attach) switched via `tmux switch-client`. Activates on fastapi / ttyd / tmux switch-client
  / journey-tui-web / uvicorn / staticfiles / lifespan work.
provenance: >
  Distilled from authoritative web sources (FastAPI official "Lifespan Events" docs +
  zhanymkanov/fastapi-best-practices) and grounded in the LOCKED round-6 sketch
  (journeys/journey-tui/artifacts/arch-round6-fastapi-sketch.md). Hypothesis-until-battle — the v1
  battle-risks in §7 must be proven in the lab before this is curated as PROVEN.
sources:
  - "FastAPI official — Lifespan Events: https://fastapi.tiangolo.com/advanced/events/"
  - "zhanymkanov/fastapi-best-practices: https://github.com/zhanymkanov/fastapi-best-practices"
---

# journey-tui-web — FastAPI + static page + ONE ttyd (PROVEN patterns, app is v1-hypothesis)

A LEAN build guide. The stack is LOCKED (do not relitigate). Project root: `~/tmux-mainbrain/journey-tui-web/`.
Content was rephrased for compliance with source licensing.

## 1. THE STACK (locked by the dev)
- **Backend:** Python **FastAPI** + **uvicorn**, serving a **tiny static HTML/JS page** (no React/Angular/ReactPy).
- **Motor binding:** import the motor **in-process** (`from motor import scan_knights, context_of`) — ZERO
  process boundary where a verb is importable; shell `python3 -m motor <verb>` only if a verb isn't importable.
- **Terminal:** **ONE** `ttyd` process running `tmux attach`. Switching knights = `tmux switch-client -t <target>`
  (NOT a new attach). The `<iframe>` src is **static** (one ttyd port) and NEVER reloads — only tmux switches underneath.
- **Front-end:** one static page = left cascade sidebar (polls the API ~3s) + main `<iframe>` → the single ttyd port.
- **Security:** solo/isolated user. Default bind localhost (`127.0.0.1`); LAN exposure (`0.0.0.0`) is the dev's accepted call — do not block on it.

## 2. APP LAYOUT (from the locked sketch — one line each)
```
journey-tui-web/
  app.py            # FastAPI app, route table, lifespan (ttyd start/stop), uvicorn entry
  cascade.py        # governed tree: parse journeys/*/meta.json + enrich via motor scan_knights/context_of
  ttyd_manager.py   # launch/stop the ONE ttyd child; run `tmux switch-client`
  models.py         # pydantic response models (CascadeNode, SwitchResult, Health)
  static/index.html # sidebar + single <iframe>
  static/app.js     # render tree, onclick -> POST /api/switch/{sid}, setInterval poll 3s
  requirements.txt  # fastapi, uvicorn[standard]  (jinja2 only if templating is truly needed)
```

## 3. FASTAPI PATTERNS — the authoritative conventions, applied

### 3a. Lifespan (official docs pattern) — own the ttyd child here, NOT at import time
Resources that must start/stop with the app belong in a `lifespan` async context manager, never at module
top level (so tests and imports don't spawn ttyd). Pattern:
```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    ttyd.start()          # launch the ONE ttyd child (see §4)
    try:
        yield
    finally:
        ttyd.stop()       # terminate + reap the child on shutdown (always runs)

app = FastAPI(lifespan=lifespan)
```

### 3b. StaticFiles — serve the page
```python
from fastapi.staticfiles import StaticFiles
# mount the sidebar/app assets; serve index.html at "/"
app.mount("/static", StaticFiles(directory="static"), name="static")
# GET "/" -> return FileResponse("static/index.html")
```

### 3c. async vs sync routes (zhanymkanov best-practice — the key rule for THIS app)
**Do NOT declare a route `async def` if its body does only BLOCKING I/O.** `tmux switch-client` and the
motor calls are blocking subprocess/CPU work. Two safe choices:
- Declare the route as a **plain `def`** → FastAPI runs it in a worker threadpool (never blocks the event loop); OR
- Keep it `async def` but offload the blocking call via `from starlette.concurrency import run_in_threadpool`.
Never call blocking `subprocess.run(...)` directly inside a bare `async def` — it stalls the whole loop.

### 3d. pydantic response shapes (models.py)
```python
from pydantic import BaseModel

class CascadeNode(BaseModel):
    session_id: str
    role: str
    agent: str | None = None
    tmux_session: str | None = None
    parent: str | None = None
    level: int
    alive: bool = False
    context_pct: float | None = None
    children: list["CascadeNode"] = []

class SwitchResult(BaseModel):
    ok: bool
    target: str | None = None
    error: str | None = None

class Health(BaseModel):
    ok: bool
    ttyd_running: bool
```

## 4. THE ONE ttyd CHILD — safe subprocess/Popen management (ttyd_manager.py)
- **Launch once** at lifespan startup with pinned flags (from the sketch):
  `ttyd -p <PORT> -i 127.0.0.1 -W -t disableLeaveAlert=true tmux attach`
  (`-i 0.0.0.0` only on the dev's explicit LAN call; `-W` = writable).
- Hold the child in a single owner object:
  ```python
  import subprocess, signal
  class TtydManager:
      def __init__(self, port: int, bind: str = "127.0.0.1"):
          self.port, self.bind, self._proc = port, bind, None
      def start(self) -> None:
          if self._proc and self._proc.poll() is None:
              return  # already running — idempotent
          self._proc = subprocess.Popen(
              ["ttyd", "-p", str(self.port), "-i", self.bind, "-W",
               "-t", "disableLeaveAlert=true", "tmux", "attach"],
          )
      def stop(self, timeout: float = 5.0) -> None:
          if not self._proc:
              return
          self._proc.terminate()                 # SIGTERM first
          try:
              self._proc.wait(timeout=timeout)    # BOUNDED wait — never block forever
          except subprocess.TimeoutExpired:
              self._proc.kill()                   # escalate; then reap
              self._proc.wait(timeout=timeout)
          self._proc = None
      def is_running(self) -> bool:
          return bool(self._proc and self._proc.poll() is None)
  ```
- **switch-client** is a short, bounded command (NOT the long-lived child):
  ```python
  def switch_client(target: str) -> SwitchResult:
      try:
          r = subprocess.run(["tmux", "switch-client", "-t", target],
                             capture_output=True, text=True, timeout=5)
      except subprocess.TimeoutExpired:
          return SwitchResult(ok=False, error="tmux switch-client timed out")
      if r.returncode != 0:
          return SwitchResult(ok=False, target=target, error=r.stderr.strip() or "switch-client failed")
      return SwitchResult(ok=True, target=target)
  ```
- **Rules:** always pass `timeout=` to every `subprocess.run`; always `terminate()`→bounded-`wait()`→`kill()` the
  child on shutdown (no zombies, no hang); keep the child in ONE owner so start is idempotent; NEVER `kill-session`.

## 5. ROUTES (method, path, response)
- `GET  /`                          → `FileResponse("static/index.html")`.
- `GET  /api/cascade`               → `list[CascadeNode]` — the governed tree (see §6).
- `POST /api/switch/{session_id}`   → `SwitchResult` — resolve `session_id` → tmux target, `tmux switch-client -t <target>`.
- `GET  /api/health`                → `Health` — `{ok, ttyd_running}`.
Make the switch/cascade routes plain `def` (or offload with `run_in_threadpool`) per §3c.

## 6. CASCADE DATA — in-process (cascade.py)
- The governed TREE is authoritative from **`journeys/*/meta.json`** (reuse the proven journey-tui rules:
  nest by `parent`, honor `level`, filter by `tui:` — `ENABLED`/missing = shown, any other value hides the subtree).
- ENRICH each node with live state via the **imported motor**:
  `from motor import scan_knights, context_of` → join on `session_id` for `alive`/`context_pct`.
  `scan_knights()` returns `{count, knights:[{agent, alive, context_pct, parent, purpose, session_id, ...}]}`.
  NOTE: `scan_knights`'s own `parent` is SPARSE — the tree edges come from meta.json, never from this field.
- Resolve `session_id → tmux target` from the meta.json `tmux_session` of that knight (fall back to a derived name
  only if needed), then feed `switch_client(target)`.

## 7. V1 BATTLE-RISKS TO PROVE (hypothesis until the lab confirms)
1. `tmux switch-client` must change what the ALREADY-ATTACHED ttyd client shows — verify the client the ttyd
   `tmux attach` created is the one being switched (same tmux server/socket).
2. The `<iframe>` must show the switched session WITHOUT reloading (static src, one port) — confirm no flicker/reload.
3. Motor **import** vs shell boundary — confirm `from motor import scan_knights, context_of` resolves in-process
   from cwd `~/tmux-mainbrain` (only `python3` exists on host; no `python` shim).

## 8. BUILD / RUN / TEST
- Python 3.10+ (pydantic `X | None` syntax, lifespan). Deps pinned in `requirements.txt`: `fastapi`, `uvicorn[standard]`.
- System binaries required at runtime: `ttyd` and `tmux` on PATH.
- Run: `python3 -m uvicorn app:app --host 127.0.0.1 --port <PORT>` (from `journey-tui-web/`).
- Test: `python3 -m pytest`. Use FastAPI's `TestClient` for routes; monkeypatch `subprocess`/`TtydManager`
  so tests never spawn a real ttyd (keep tests deterministic & isolated — no real child processes).
- After changes: run the tests + a lint pass; fix before declaring done.
