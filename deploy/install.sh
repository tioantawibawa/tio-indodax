#!/usr/bin/env bash
# Install / update the Indodax agent as an isolated systemd service.
# Run as root FROM THE REPO CHECKOUT:   sudo bash deploy/install.sh
# It does NOT start the service and does NOT change firewall or NTP settings.
set -euo pipefail

APP_DIR=/opt/indodax-agent
APP_USER=indodax
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY=${PYTHON:-python3.11}

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
command -v "$PY" >/dev/null || { echo "$PY not found (need Python 3.11+)"; exit 1; }

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  echo "created user $APP_USER"
fi

mkdir -p "$APP_DIR"
# copy code (keeps data/, logs/ and .env of an existing install)
rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'data/' --exclude 'logs/' \
      --exclude '.env' --exclude 'reports/' "$SRC_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

if [ ! -d "$APP_DIR/.venv" ]; then
  "$PY" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "created $APP_DIR/.env from template — EDIT IT before starting"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
chmod 750 "$APP_DIR"

install -m 644 "$SRC_DIR/deploy/indodax-agent.service" /etc/systemd/system/indodax-agent.service
systemctl daemon-reload

echo
echo "Time sync status (must say 'System clock synchronized: yes'):"
timedatectl 2>/dev/null | grep -Ei 'synchronized|NTP service' || echo "  timedatectl not available"
echo
echo "Installed. Next steps: see deploy/README_DEPLOY.md (edit .env, checks, then systemctl enable --now)."
