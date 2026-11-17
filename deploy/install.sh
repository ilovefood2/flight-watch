#!/usr/bin/env bash
# Install the fare monitor on a Raspberry Pi (or any systemd box).
# Run ON the Pi:   bash deploy/install.sh
# Idempotent - safe to re-run after a git pull.
set -euo pipefail

APP="$HOME/flight-watch"
REPO="${REPO:-https://github.com/ilovefood2/flight-watch.git}"

# This installs a systemd user timer, so it only works on the target machine.
# Running it on a Mac used to clone the repo, write a .env and then die at the
# first systemctl call, leaving a half-installed mess behind.
if ! command -v systemctl >/dev/null 2>&1; then
  echo "ERROR: no systemctl on this machine ($(uname -s))." >&2
  echo "       Run this ON the Raspberry Pi, not on your laptop:" >&2
  echo "         ssh kelvin@10.0.0.33" >&2
  echo "         bash ~/flight-watch/deploy/install.sh" >&2
  exit 1
fi

echo "==> syncing $APP"
if [ -d "$APP/.git" ]; then
  git -C "$APP" pull --rebase --autostash
else
  git clone "$REPO" "$APP"
fi

cd "$APP"
mkdir -p state
[ -f state/prices.json ] || echo '{"updated": null, "prices": {}}' > state/prices.json

if [ ! -f .env ]; then
  cat > .env <<'ENVEOF'
# Telegram credentials. Fill these in, then: chmod 600 .env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Scan window and scope
WINDOW_START=2027-01-01
WINDOW_END=2027-12-31
ROUTES=YYZ-PVG,PVG-YYZ
TRIP_LENGTHS=14,21,28
MAX_WEEKDAYS=20
SLICES=24

# Alert thresholds (CAD, round trip incl. tax, 1 adult)
ALERT_PREMIUM=3200
ALERT_BUSINESS=5000
ALERT_DROP=0
DROP_CEILING_PREMIUM=3800
DROP_CEILING_BUSINESS=6000

# Trip.com browser scan (slow; sees Air China / China Eastern)
JS_ROUTES=yto-sha,sha-yto,yto-bjs,bjs-yto
JS_PER_RUN=12
ENVEOF
  chmod 600 .env
  echo "==> created $APP/.env - fill in the Telegram values, then re-run this script"
fi
chmod 600 .env

# The Trip.com scanner needs a browser; the Google one is stdlib-only, so a
# missing venv must not stop the fast scan from being installed.
if [ ! -x .venv/bin/python ]; then
  echo "==> creating venv for the browser scanner"
  python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && \
    .venv/bin/pip install -q playwright || echo "!! playwright install failed"
fi
command -v chromium >/dev/null || command -v chromium-browser >/dev/null || \
  echo "!! no chromium found - install it: sudo apt install -y chromium"

echo "==> installing systemd units (user scope)"
mkdir -p "$HOME/.config/systemd/user"
cp deploy/flight-watch.service deploy/flight-watch.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now flight-watch.timer
if [ -x .venv/bin/python ]; then
  cp deploy/flight-watch-js.service deploy/flight-watch-js.timer "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable --now flight-watch-js.timer
fi

# Without lingering, user units stop when the SSH session ends.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  echo "==> enabling linger so the timer survives logout"
  sudo loginctl enable-linger "$USER" || \
    echo "!! could not enable linger; run: sudo loginctl enable-linger $USER"
fi

echo
echo "==> done"
systemctl --user list-timers flight-watch.timer --no-pager || true
echo
echo "run once now : systemctl --user start flight-watch.service"
echo "watch logs   : journalctl --user -u flight-watch.service -f"
