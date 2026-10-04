#!/usr/bin/env bash
# Production deploy step on the EC2 instance (run as root). Called by the first-boot user
# data and by GitHub Actions (over SSM) after it checks out the new commit:
#
#   sudo bash /opt/monkey-inc/deploy/deploy.sh
#
# Refreshes .env from SSM Parameter Store, backs up the database, then reinstalls and
# restarts the service with deploy/install.sh. Fails if the bot doesn't stay up.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER=ec2-user
SERVICE_NAME=monkey-inc

# Written by the user data: BUCKET, AWS_REGION, ENV_PARAMETER
source /etc/monkey-inc.env

echo "==> Deploying $(sudo -u "$APP_USER" git -C "$APP_DIR" log -1 --format='%h %s')"

echo "==> Refreshing .env from SSM $ENV_PARAMETER"
aws ssm get-parameter --region "$AWS_REGION" --name "$ENV_PARAMETER" --with-decryption \
  --query Parameter.Value --output text > "$APP_DIR/.env.new"
chmod 600 "$APP_DIR/.env.new"
chown "$APP_USER" "$APP_DIR/.env.new"
mv "$APP_DIR/.env.new" "$APP_DIR/.env"

if [[ -f "$APP_DIR/data/monkey_inc.db" && -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "==> Pre-deploy database backup"
  (cd "$APP_DIR" && sudo -u "$APP_USER" .venv/bin/python -m bot.backup)
fi

sudo -u "$APP_USER" SERVICE_USER="$APP_USER" BACKUP_S3_URI="s3://$BUCKET/backups/" \
  bash "$APP_DIR/deploy/install.sh"

echo "==> Checking that the bot stays up"
sleep 15
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  journalctl -u "$SERVICE_NAME" -n 50 --no-pager
  echo "!! $SERVICE_NAME is not running" >&2
  exit 1
fi
journalctl -u "$SERVICE_NAME" -n 15 --no-pager
echo "==> Deploy OK"
