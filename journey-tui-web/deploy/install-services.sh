#!/usr/bin/env bash
#
# install-services.sh — install + start the journey-tui-web systemd USER services
# (the FastAPI app + ttyd) and its Cloudflare tunnel, on ANY machine. Idempotent.
#
# PORTABLE BY DESIGN: nothing machine-specific is baked in. Per-machine values
# (tunnel name, public hostnames, boot tmux session) come from deploy.env — copy
# deploy.env.example to deploy.env and edit it. Each computer MUST use its OWN
# tunnel + its OWN hostnames so you reach THAT machine's terminal (not another's).
#
# Prereqs on the target host (see deploy/README.md for the one-time bootstrap):
#   * ~/tmux-mainbrain/journey-tui-web/.venv  (python deps installed)
#   * ttyd + cloudflared on PATH (e.g. ~/.local/bin)
#   * `cloudflared tunnel login` already done (writes ~/.cloudflared/cert.pem)
#   * a Cloudflare Access policy gating this machine's two hostnames
#
# Usage:   bash deploy/install-services.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${HERE}/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
USER_NAME="$(id -un)"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mXX %s\033[0m\n' "$*"; exit 1; }

# --- 0. load per-machine config ------------------------------------------
ENV_FILE="${HERE}/deploy.env"
if [ -f "$ENV_FILE" ]; then
  say "Loading config from ${ENV_FILE}"
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
else
  warn "no ${ENV_FILE} — copy deploy.env.example to deploy.env and edit it."
  warn "proceeding with environment / defaults where possible."
fi

# Required per-machine values (fail loud if unset).
TUNNEL_NAME="${JTW_TUNNEL_NAME:-}"
PUBLIC_HOST_APP="${JTW_PUBLIC_HOST_APP:-}"
PUBLIC_HOST_TERM="${JTW_PUBLIC_HOST:-}"       # the term hostname (iframe origin)
BOOT_SESSION="${JTW_BOOT_SESSION:-}"
TTYD_SCHEME="${JTW_TTYD_SCHEME:-https}"
PUBLIC_PORT="${JTW_PUBLIC_PORT-}"             # may be intentionally empty (443)

[ -n "$TUNNEL_NAME" ]      || die "JTW_TUNNEL_NAME is required (set it in deploy.env)"
[ -n "$PUBLIC_HOST_APP" ]  || die "JTW_PUBLIC_HOST_APP is required (the app hostname)"
[ -n "$PUBLIC_HOST_TERM" ] || die "JTW_PUBLIC_HOST is required (the terminal hostname)"
[ -n "$BOOT_SESSION" ]     || die "JTW_BOOT_SESSION is required (a live tmux session)"

APP_UNIT="journey-tui-web.service"
TUN_UNIT="journey-tui-web-tunnel.service"

# --- 1. sanity: required binaries + venv ---------------------------------
say "Checking prerequisites"
VENV_PY="${APP_DIR}/.venv/bin/python"
[ -x "$VENV_PY" ] || die "missing venv python: $VENV_PY — create the venv first"
command -v cloudflared >/dev/null || die "cloudflared not on PATH"
command -v ttyd >/dev/null || warn "ttyd not on PATH — the app will fail to start ttyd"
[ -f "${HOME}/.cloudflared/cert.pem" ] || warn "no ~/.cloudflared/cert.pem — run 'cloudflared tunnel login' first"

# --- 2. ensure the tunnel + DNS + config exist (bootstrap, idempotent) ---
say "Ensuring Cloudflare tunnel '${TUNNEL_NAME}' exists"
if cloudflared tunnel list 2>/dev/null | awk '{print $2}' | grep -qx "$TUNNEL_NAME"; then
  echo "tunnel '${TUNNEL_NAME}' already exists"
else
  cloudflared tunnel create "$TUNNEL_NAME"
fi
TUNNEL_ID="$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2==n {print $1}' | head -1)"
[ -n "$TUNNEL_ID" ] || die "could not resolve tunnel id for '${TUNNEL_NAME}'"
echo "tunnel id: ${TUNNEL_ID}"

say "Writing ~/.cloudflared/config.yml (ingress for both hostnames)"
cat > "${HOME}/.cloudflared/config.yml" <<CFG
tunnel: ${TUNNEL_ID}
credentials-file: ${HOME}/.cloudflared/${TUNNEL_ID}.json

ingress:
  - hostname: ${PUBLIC_HOST_APP}
    service: http://localhost:8080
  - hostname: ${PUBLIC_HOST_TERM}
    service: http://localhost:7681
  - service: http_status:404
CFG

say "Ensuring DNS routes"
cloudflared tunnel route dns "$TUNNEL_NAME" "$PUBLIC_HOST_APP"  2>&1 || true
cloudflared tunnel route dns "$TUNNEL_NAME" "$PUBLIC_HOST_TERM" 2>&1 || true

# --- 3. write the systemd unit files -------------------------------------
say "Writing unit files to ${UNIT_DIR}"
mkdir -p "$UNIT_DIR"

cat > "${UNIT_DIR}/${APP_UNIT}" <<UNIT_APP
[Unit]
Description=journey-tui-web — FastAPI subagent-cascade observatory (app + ttyd)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
WorkingDirectory=%h/tmux-mainbrain/journey-tui-web
Environment=PYTHONPATH=%h/tmux-mainbrain
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=TMUX_TMPDIR=/tmp
Environment=JTW_BOOT_SESSION=${BOOT_SESSION}
Environment=JTW_PUBLIC_HOST=${PUBLIC_HOST_TERM}
Environment=JTW_TTYD_SCHEME=${TTYD_SCHEME}
Environment=JTW_PUBLIC_PORT=${PUBLIC_PORT}
ExecStart=%h/tmux-mainbrain/journey-tui-web/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
KillMode=mixed

[Install]
WantedBy=default.target
UNIT_APP

cat > "${UNIT_DIR}/${TUN_UNIT}" <<UNIT_TUN
[Unit]
Description=Cloudflare Tunnel for journey-tui-web (${PUBLIC_HOST_APP})
After=network-online.target journey-tui-web.service
Wants=network-online.target journey-tui-web.service

[Service]
Type=exec
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/cloudflared --no-autoupdate tunnel run ${TUNNEL_NAME}
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=default.target
UNIT_TUN

# --- 4. lingering --------------------------------------------------------
say "Ensuring user lingering (needs sudo once)"
if loginctl show-user "$USER_NAME" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo "lingering already enabled"
elif sudo -n true 2>/dev/null; then
  sudo loginctl enable-linger "$USER_NAME" && echo "lingering enabled"
else
  warn "no passwordless sudo. Run ONCE yourself, then re-run this script:"
  warn "    sudo loginctl enable-linger $USER_NAME"
fi

# --- 5. stop ad-hoc cloudflared, reload, enable, start -------------------
say "Stopping any ad-hoc 'cloudflared tunnel run'"
pkill -f "cloudflared.*tunnel run" 2>/dev/null || true
sleep 1

say "Reloading systemd and enabling services"
systemctl --user daemon-reload
systemctl --user enable --now "$APP_UNIT"
systemctl --user enable --now "$TUN_UNIT"

# --- 6. verify -----------------------------------------------------------
say "Status"
systemctl --user --no-pager --lines=0 status "$APP_UNIT" "$TUN_UNIT" || true
say "Local app check"
sleep 2
curl -fsS http://127.0.0.1:8080/api/config 2>/dev/null && echo || \
  warn "app not answering on :8080 yet — check: journalctl --user -u $APP_UNIT -e"

cat <<EOF

$(printf '\033[1;32m')Done.$(printf '\033[0m')
  Open:    https://${PUBLIC_HOST_APP}   (Cloudflare Access login, then the app)
  Logs:    journalctl --user -u ${APP_UNIT} -f
           journalctl --user -u ${TUN_UNIT} -f
  Restart: systemctl --user restart ${APP_UNIT}
  Stop:    systemctl --user disable --now ${TUN_UNIT} ${APP_UNIT}
EOF
