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

```bash
# PYTHONPATH must include ~/tmux-mainbrain so `import motor` resolves.
cd ~/tmux-mainbrain/journey-tui-web
PYTHONPATH=~/tmux-mainbrain python3 -m uvicorn app:app --host 127.0.0.1 --port 8080
```

Then open <http://127.0.0.1:8080/>.

### Configuration (env vars)

| var | default | meaning |
|-----|---------|---------|
| `JTW_ROOT` | `~/tmux-mainbrain` | where `journeys/*/meta.json` + the motor live |
| `JTW_BIND` | `127.0.0.1` | FastAPI/uvicorn bind (informational; the `--host` flag governs the socket) |
| `JTW_TTYD_PORT` | `7681` | ttyd listen port |
| `JTW_TTYD_BIND` | `127.0.0.1` | ttyd bind addr; `0.0.0.0` exposes the terminal on the LAN |
| `JTW_TTYD_CRED` | _(none)_ | ttyd HTTP basic-auth as `user:pass` (set this whenever the bind isn't localhost) |
| `JTW_PUBLIC_HOST` | `127.0.0.1` | host a BROWSER uses to reach ttyd — drives the iframe src (LAN IP or DNS name) |
| `JTW_TTYD_SCHEME` | `http` | scheme the browser uses for ttyd (`https` if fronted by TLS) |
| `JTW_BOOT_SESSION` | first live knight | the tmux session ttyd attaches to at boot |

> If no `JTW_BOOT_SESSION` is set and no live governed knight is found, ttyd will
> not start — set `JTW_BOOT_SESSION` to a live tmux session name.

> The terminal iframe src is **computed at runtime** from `GET /api/config`
> (`JTW_PUBLIC_HOST` + `JTW_TTYD_PORT` + `JTW_TTYD_SCHEME`), so it is correct for
> localhost, LAN, or a DNS deployment without editing the static page.

## Security

Localhost (`127.0.0.1`, the default) exposes nothing to the network. ttyd runs
`tmux attach -W` (writable) — a reachable port 7681 is a **fully interactive,
unauthenticated terminal** into the knights' sessions.

**Never bind beyond localhost without a credential.** When `JTW_TTYD_BIND` is
`0.0.0.0` (LAN/DNS), set `JTW_TTYD_CRED=user:pass` so ttyd enforces HTTP
basic-auth (verified: no/wrong creds → 401, correct → 200). Put the credential
in `~/tmux-mainbrain/.env.secrets` (`JTW_TTYD_CRED=...`) rather than the shell
history or the alias.

### Reaching it from the LAN

Two options:

1. **SSH tunnel (no exposure).** On the remote machine:
   ```bash
   ssh -L 8080:localhost:8080 -L 7681:localhost:7681 <user>@<this-host-ip>
   ```
   then browse `http://localhost:8080/`. No binds change.

2. **Bind to the LAN (with auth).** Export before launch:
   ```bash
   export JTW_BIND=0.0.0.0 JTW_TTYD_BIND=0.0.0.0
   export JTW_PUBLIC_HOST=<lan-ip-or-dns>      # e.g. 192.168.15.145
   export JTW_TTYD_CRED=<user>:<pass>
   # run uvicorn with --host 0.0.0.0
   ```
   Open the firewall for ports 8080 and 7681. The terminal will prompt for the
   basic-auth credentials in the browser.

### DNS deployment

Set `JTW_PUBLIC_HOST` to the DNS name and (if TLS-fronted) `JTW_TTYD_SCHEME=https`.
Put a reverse proxy (caddy/nginx) in front for TLS on both the app and ttyd;
keep `JTW_TTYD_CRED` set. The iframe follows `JTW_PUBLIC_HOST` automatically.

## Test

```bash
cd ~/tmux-mainbrain/journey-tui-web
.venv/bin/python -m pytest -q
```

Tests are deterministic: the cascade core is pure, and `TtydManager`/the motor
are monkeypatched so no real ttyd is ever spawned.
