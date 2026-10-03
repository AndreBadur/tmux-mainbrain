# Deploying journey-tui-web on another machine

The app visualizes the subagent cascade and embeds ONE ttyd terminal, exposed
to the internet through a **Cloudflare Tunnel** gated by **Cloudflare Access**.

> **Each machine is its own deployment.** Reaching "the other computer's
> terminal" means that computer runs its OWN tunnel with its OWN hostnames.
> Never copy another machine's tunnel credentials — they are per-host secrets
> and are not in git.

## One-time bootstrap on a fresh machine

1. **Clone the repo** and enter it:
   ```bash
   git clone <your-remote> ~/tmux-mainbrain && cd ~/tmux-mainbrain
   ```

2. **Install the runtime bits** (static binaries, no sudo):
   ```bash
   mkdir -p ~/.local/bin
   # ttyd (terminal) and cloudflared (tunnel):
   curl -fsSL -o ~/.local/bin/ttyd        https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.$(uname -m)
   curl -fsSL -o ~/.local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
   chmod +x ~/.local/bin/ttyd ~/.local/bin/cloudflared
   ```

3. **Create the Python venv** for the app:
   ```bash
   cd ~/tmux-mainbrain/journey-tui-web
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

4. **Authorize Cloudflare** (opens a browser; pick your domain, Authorize):
   ```bash
   cloudflared tunnel login
   ```

5. **Configure THIS machine**:
   ```bash
   cp deploy/deploy.env.example deploy/deploy.env
   # edit deploy/deploy.env — set a unique JTW_TUNNEL_NAME and this machine's
   # two hostnames (app + terminal), plus a live JTW_BOOT_SESSION.
   ```

6. **Create the Cloudflare Access policy** (dashboard → Zero Trust → Access →
   Applications → Add self-hosted app). Add BOTH hostnames to one app and an
   Allow policy locked to your email. Do this BEFORE first run so the terminal
   is never public.

7. **Install + start the services**:
   ```bash
   bash deploy/install-services.sh
   ```
   The script creates the tunnel (if missing), writes `~/.cloudflared/config.yml`,
   adds DNS routes, installs the systemd user services, enables lingering, and
   starts everything. It is idempotent — safe to re-run.

8. **Open** `https://<your app hostname>` → Cloudflare Access login → the app.

## Day-to-day

```bash
journalctl --user -u journey-tui-web.service -f          # app logs
journalctl --user -u journey-tui-web-tunnel.service -f   # tunnel logs
systemctl --user restart journey-tui-web.service         # after a code change
systemctl --user disable --now journey-tui-web-tunnel.service journey-tui-web.service  # offline
```

## Security notes

- The app binds `127.0.0.1` only; the sole public path is the Cloudflare tunnel.
- Both hostnames must be behind a Cloudflare Access policy — the embedded
  terminal is a **writable shell into live agent sessions**.
- `deploy/deploy.env` and `~/.cloudflared/*` (cert + tunnel creds) are secrets:
  gitignored / outside the repo. Never commit them.
