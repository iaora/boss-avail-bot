#!/usr/bin/env bash
# Install or update Monkey Inc as a systemd service (Linux, e.g. an EC2 instance).
# Safe to re-run: it rebuilds the virtualenv dependencies and restarts the service.
#
#   ./deploy/install.sh            # install + start
#   SERVICE_USER=ec2-user ./deploy/install.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="monkey-inc"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PYTHON="$(command -v "$candidate")"; break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "Python 3.11+ is required (Amazon Linux 2023: sudo dnf install -y python3.11)" >&2
  exit 1
fi

echo "==> Using $PYTHON in $PROJECT_DIR"
[[ -d "$PROJECT_DIR/.venv" ]] || "$PYTHON" -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
mkdir -p "$PROJECT_DIR/data"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  chmod 600 "$PROJECT_DIR/.env"
  echo "!! Created $PROJECT_DIR/.env from the example. Fill in DISCORD_TOKEN (and IDs), then re-run this script."
  exit 1
fi

echo "==> Installing systemd service $SERVICE_NAME (runs as $SERVICE_USER)"
sed -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
  "$PROJECT_DIR/deploy/monkey-inc.service" | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null
sudo chown -R "$SERVICE_USER" "$PROJECT_DIR/data"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null
sudo systemctl restart "$SERVICE_NAME"

# Nightly database backup at 04:00 server time (keeps the newest 14).
CRON_LINE="0 4 * * * cd $PROJECT_DIR && .venv/bin/python -m bot.backup >> data/backup.log 2>&1"
( sudo crontab -u "$SERVICE_USER" -l 2>/dev/null | grep -v 'bot.backup' || true; echo "$CRON_LINE" ) \
  | sudo crontab -u "$SERVICE_USER" - || echo "(cron not available; skipping nightly backups)"

echo "==> Done. Logs: sudo journalctl -u $SERVICE_NAME -f"
