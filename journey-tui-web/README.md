# journey-tui-web

A thin FastAPI app that visualizes the subagent cascade (left sidebar) and
embeds ONE `ttyd` terminal (main `<iframe>`), switching which knight it shows via
`tmux switch-client` — the iframe never reloads.

## Architecture (locked)

- **Backend:** Python FastAPI + uvicorn. The tmux-mainbrain **motor is imported
  in-process** (`from motor import scan_knights`) — zero process boundary.
- **Terminal:** a single long-lived `ttyd` running `tmux attach`. Clicking a
  knight runs `tmux switch-client -c <ttyd_client> -t <target>`, re-pointing
  ttyd's own tmux client underneath the still-open websocket. **No new attach,
  no iframe reload, scrollback intact.** (Risk proven in the lab: the `-c
  <client>` flag is REQUIRED.)
- **Front-end:** one static page — a `<nav>` sidebar (polls `/api/cascade` every
  3s) + one static `<iframe>` pointing at the single ttyd port.

## Files

| file | responsibility |
|------|----------------|
| `app.py` | FastAPI app, lifespan (owns the ttyd child), routes |
| `cascade.py` | governed tree from `journeys/*/meta.json` + motor enrichment |
| `ttyd_manager.py` | the one ttyd child + `tmux switch-client` driver |
| `models.py` | pydantic response models |
| `static/index.html`, `static/app.js` | the single page |

## Routes

- `GET  /`                        → `static/index.html`
- `GET  /api/health`              → `{ok, ttyd_up}`
- `GET  /api/cascade`             → the governed tree JSON
- `POST /api/switch/{session_id}` → `{ok, target}` or `{ok:false, error}` (404 unknown sid)

## Run

Requires **Python 3.11+** and `ttyd` + `tmux` on `PATH`.

**Preferred — one-shot installer** (builds the venv, ensures ttyd, installs a
localhost systemd user service on `127.0.0.1:8787`):

```bash
bash ~/tmux-mainbrain/install.sh
# then open http://127.0.0.1:8787/   (stop/remove with uninstall.sh)
```

**Manual** (foreground, for development):

```bash
# Use the project venv's python (the system python3 lacks fastapi).
# PYTHONPATH must include ~/tmux-mainbrain so `import motor` resolves.
# No JTW_BOOT_SESSION needed — ttyd boots an auto-created `jtw-console` shell.
cd ~/tmux-mainbrain/journey-tui-web
PYTHONPATH=~/tmux-mainbrain TMUX_TMPDIR=/tmp PATH="$HOME/.local/bin:$PATH" \
  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8787
```

Then open <http://127.0.0.1:8787/>.

> Running as a systemd user service instead? `systemctl --user restart journey-tui-web.service`
> (the unit no longer pins a knight at boot).

### Configuration (env vars)

| var | default | meaning |
|-----|---------|---------|
| `JTW_ROOT` | `~/tmux-mainbrain` | where `journeys/*/meta.json` + the motor live |
| `JTW_BIND` | `127.0.0.1` | FastAPI/uvicorn bind (informational; the `--host` flag governs the socket) |
| `JTW_TTYD_PORT` | `7681` | ttyd listen port |
| `JTW_TTYD_BIND` | `127.0.0.1` | ttyd bind addr; `0.0.0.0` exposes the terminal on the LAN |
| `JTW_TTYD_CRED` | _(none)_ | ttyd HTTP basic-auth as `user:pass` (set this whenever the bind isn't localhost) |
| `JTW_PUBLIC_HOST` | `127.0.0.1` | host a BROWSER uses to reach ttyd — drives the iframe src (set to the LAN IP for LAN access) |
| `JTW_TTYD_SCHEME` | `http` | scheme the browser uses for ttyd |
| `JTW_BOOT_SESSION` | _(empty → auto console)_ | OPTIONAL override to pin ttyd to a specific live session at boot |

> By default (no `JTW_BOOT_SESSION`) the app **auto-creates and boots a console
> shell** (`jtw-console`), so startup never depends on any knight being alive.
> Set `JTW_BOOT_SESSION` only to pin ttyd to a specific live session; if that
> session isn't live, startup fails loud rather than silently falling back.

> The terminal iframe src is **computed at runtime** from `GET /api/config`
> (`JTW_PUBLIC_HOST` + `JTW_TTYD_PORT` + `JTW_TTYD_SCHEME`), so it is correct for
> localhost or LAN without editing the static page.

## Security

This is a **localhost-only, WR-internal tool**. The app binds `127.0.0.1:8787`
and ttyd binds `127.0.0.1:7681` — nothing is exposed to the network by default.
ttyd runs `tmux attach -W` (writable): a reachable port 7681 is a **fully
interactive terminal** into the knights' sessions, so keep it on localhost.

**Never bind beyond localhost without a credential.** If you set `JTW_TTYD_BIND`
to `0.0.0.0` (LAN), also set `JTW_TTYD_CRED=user:pass` so ttyd enforces HTTP
basic-auth (verified: no/wrong creds → 401, correct → 200). Put the credential
in `~/tmux-mainbrain/.env.secrets` (`JTW_TTYD_CRED=...`) rather than the shell
history.

### Reaching it from another machine on the LAN

Preferred — an **SSH tunnel** (no network exposure, no binds changed). On the
remote machine:
```bash
ssh -L 8787:localhost:8787 -L 7681:localhost:7681 <user>@<this-host-ip>
```
then browse `http://localhost:8787/`.

## Test

```bash
cd ~/tmux-mainbrain/journey-tui-web
.venv/bin/python -m pytest -q
```

Tests are deterministic: the cascade core is pure, and `TtydManager`/the motor
are monkeypatched so no real ttyd is ever spawned.
