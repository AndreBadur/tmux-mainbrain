#!/usr/bin/env bash
#
# install.sh — install + start the localhost-only journey web app as a systemd
# USER service. Self-contained, WR-internal, NO cloud. Idempotent, fail-loud.
#
# ROOT_DIR is derived from THIS script's own location, so the same script works
# in any tree (e.g. ~/tmux-mainbrain or a future copy) with no hardcoded path.
#
# Usage:  bash install.sh
# Stop/remove:  bash uninstall.sh   (see --help there)
#
set -euo pipefail

# --- knobs (Phase 3 flips only UNIT) ----------------------------------------
UNIT="journey-tui-web.service"     # systemd user unit name
APP_PORT="${JTW_APP_PORT:-8787}"   # app listen port (localhost only)
TTYD_VERSION="1.7.7"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$ROOT_DIR/journey-tui-web"
UNIT_DIR="$HOME/.config/systemd/user"
VENV_PY="$APP_DIR/.venv/bin/python"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mXX %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0. prerequisites -------------------------------------------------------
say "Checking prerequisites (ROOT_DIR=$ROOT_DIR)"
[ -d "$APP_DIR" ] || die "app dir not found: $APP_DIR"
[ -f "$APP_DIR/requirements.txt" ] || die "missing $APP_DIR/requirements.txt"

command -v python3 >/dev/null || die "python3 not found"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' \
  || die "Python 3.11+ required (found $PYV)"
echo "python $PYV OK"

command -v tmux >/dev/null || die "tmux not found on PATH (required at runtime)"
echo "tmux OK"

# --- 1. ttyd (ensure the static binary in ~/.local/bin; NO cloudflared) -----
say "Ensuring ttyd"
mkdir -p "$HOME/.local/bin"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
if command -v ttyd >/dev/null; then
  echo "ttyd present: $(command -v ttyd)"
else
  arch="$(uname -m)"
  url="https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${arch}"
  echo "fetching ttyd ${TTYD_VERSION} for ${arch}"
  curl -fsSL -o "$HOME/.local/bin/ttyd" "$url" || die "failed to download ttyd from $url"
  chmod +x "$HOME/.local/bin/ttyd"
  "$HOME/.local/bin/ttyd" --version >/dev/null 2>&1 || die "downloaded ttyd is not runnable"
  echo "installed ttyd -> $HOME/.local/bin/ttyd"
fi

# --- 1b. motor launcher on PATH (idempotent symlink into ~/.local/bin) ------
# ~/.local/bin already exists and is on PATH (ensured above). Symlink the
# repo's bin/motor so agents can run `motor <tool>` from any cwd.
say "Ensuring motor launcher on PATH"
[ -x "$ROOT_DIR/bin/motor" ] || die "launcher not found/executable: $ROOT_DIR/bin/motor"
ln -sf "$ROOT_DIR/bin/motor" "$HOME/.local/bin/motor"
echo "linked motor -> $HOME/.local/bin/motor ($ROOT_DIR/bin/motor)"

# --- 2. python venv + deps --------------------------------------------------
say "Setting up the Python venv"
if [ ! -x "$VENV_PY" ]; then
  python3 -m venv "$APP_DIR/.venv" || die "venv creation failed"
fi
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r "$APP_DIR/requirements.txt" || die "pip install failed"
echo "deps installed into $APP_DIR/.venv"

# --- 3. systemd user unit (localhost only, no tunnel, no cloud host) --------
say "Writing systemd user unit: $UNIT"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$UNIT" <<UNIT_EOF
[Unit]
Description=journey web app (localhost cascade + ttyd terminal)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
WorkingDirectory=$APP_DIR
Environment=JTW_ROOT=$ROOT_DIR
Environment=PYTHONPATH=$ROOT_DIR
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=TMUX_TMPDIR=/tmp
Environment=JTW_PUBLIC_HOST=127.0.0.1
Environment=JTW_TTYD_SCHEME=http
Environment=JTW_PUBLIC_PORT=7681
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port $APP_PORT
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
KillMode=mixed

[Install]
WantedBy=default.target
UNIT_EOF

# --- 4. optional lingering (run without an active login; sudo-gated) --------
if loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo "lingering already enabled"
elif sudo -n true 2>/dev/null; then
  sudo loginctl enable-linger "$(id -un)" && echo "lingering enabled" || warn "could not enable lingering"
else
  warn "no passwordless sudo — to run without being logged in, run once:"
  warn "    sudo loginctl enable-linger $(id -un)"
fi

# --- 5. reload + enable + start ---------------------------------------------
say "Starting the service"
systemctl --user daemon-reload
systemctl --user enable "$UNIT"
# restart (not just start) so an updated unit file always takes effect, even
# when the service was already running from a previous install.
systemctl --user restart "$UNIT"

# --- 6. smoke (warn, never die) ---------------------------------------------
say "Smoke check"
sleep 2
if curl -fsS "http://127.0.0.1:${APP_PORT}/api/config" >/dev/null 2>&1; then
  echo "app responding on 127.0.0.1:${APP_PORT}"
else
  warn "app not answering yet on :${APP_PORT} — check: journalctl --user -u $UNIT -e"
fi

printf '\n\033[1;32mDone.\033[0m  Open: http://127.0.0.1:%s\n' "$APP_PORT"
printf '  Logs:    journalctl --user -u %s -f\n' "$UNIT"
printf '  Restart: systemctl --user restart %s\n' "$UNIT"
printf '  Remove:  bash %s/uninstall.sh\n' "$ROOT_DIR"
printf '  CLI:     motor <tool> [args]   (now on PATH via ~/.local/bin/motor)\n'
