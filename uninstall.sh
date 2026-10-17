#!/usr/bin/env bash
#
# uninstall.sh — stop + remove the journey web app's systemd USER service.
# Idempotent and safe. By DEFAULT it keeps the venv and NEVER touches user data
# (journeys/). It leaves the shared ttyd/tmux binaries alone.
#
# Usage:
#   bash uninstall.sh                 stop+disable the unit, remove the unit file
#   bash uninstall.sh --purge-venv    also delete journey-tui-web/.venv
#   bash uninstall.sh --purge-data    ALSO delete journeys/  (DANGER; confirms)
#   bash uninstall.sh --help
#
set -euo pipefail

# --- knobs (match install.sh) -----------------------------------------------
UNIT="journey-tui-web.service"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$ROOT_DIR/journey-tui-web"
UNIT_DIR="$HOME/.config/systemd/user"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

PURGE_VENV=0
PURGE_DATA=0
for arg in "$@"; do
  case "$arg" in
    --purge-venv) PURGE_VENV=1 ;;
    --purge-data) PURGE_DATA=1 ;;
    --help|-h)
      sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) warn "unknown flag: $arg (ignored)" ;;
  esac
done

removed=()
kept=()

# --- 1. stop + disable the unit (ignore if not loaded) ----------------------
say "Stopping + disabling $UNIT"
systemctl --user disable --now "$UNIT" 2>/dev/null && removed+=("service stopped+disabled") \
  || echo "unit not loaded (already stopped)"

# --- 2. remove the unit file ------------------------------------------------
if [ -f "$UNIT_DIR/$UNIT" ]; then
  rm -f "$UNIT_DIR/$UNIT"
  removed+=("unit file $UNIT_DIR/$UNIT")
fi
systemctl --user daemon-reload 2>/dev/null || true

# --- 2b. motor launcher symlink (remove only if it points into this repo) ---
MOTOR_LINK="$HOME/.local/bin/motor"
if [ -L "$MOTOR_LINK" ]; then
  link_target="$(readlink -f "$MOTOR_LINK" 2>/dev/null || true)"
  if [ "$link_target" = "$ROOT_DIR/bin/motor" ]; then
    rm -f "$MOTOR_LINK"
    removed+=("motor launcher symlink $MOTOR_LINK")
  else
    kept+=("$MOTOR_LINK (points elsewhere: ${link_target:-unknown} — left alone)")
  fi
elif [ -e "$MOTOR_LINK" ]; then
  kept+=("$MOTOR_LINK (not a symlink — left alone)")
fi

# --- 3. venv (kept unless --purge-venv) -------------------------------------
if [ "$PURGE_VENV" -eq 1 ]; then
  if [ -d "$APP_DIR/.venv" ]; then
    rm -rf "$APP_DIR/.venv"
    removed+=("venv $APP_DIR/.venv")
  fi
else
  [ -d "$APP_DIR/.venv" ] && kept+=("venv $APP_DIR/.venv (use --purge-venv to remove)")
fi

# --- 4. user data (journeys/) — NEVER by default ----------------------------
if [ "$PURGE_DATA" -eq 1 ]; then
  warn "You asked to PURGE user data: $ROOT_DIR/journeys"
  read -r -p "Type the word DELETE to confirm: " ans
  if [ "$ans" = "DELETE" ] && [ -d "$ROOT_DIR/journeys" ]; then
    rm -rf "$ROOT_DIR/journeys"
    removed+=("journeys/ (user data)")
  else
    kept+=("journeys/ (purge NOT confirmed)")
  fi
else
  [ -d "$ROOT_DIR/journeys" ] && kept+=("journeys/ (user data — never deleted by default)")
fi

# --- report -----------------------------------------------------------------
say "Uninstall summary"
if [ "${#removed[@]}" -gt 0 ]; then
  printf 'removed:\n'; for r in "${removed[@]}"; do printf '  - %s\n' "$r"; done
else
  printf 'removed: nothing (already clean)\n'
fi
if [ "${#kept[@]}" -gt 0 ]; then
  printf 'kept:\n'; for k in "${kept[@]}"; do printf '  - %s\n' "$k"; done
fi
printf 'left alone: ttyd + tmux binaries (shared)\n'
