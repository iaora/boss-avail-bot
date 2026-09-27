#!/usr/bin/env bash
# EC2 "User data" script for a brand-new Amazon Linux 2023 instance.
# Paste this into Advanced details -> User data when launching the instance.
#
# Before launching:
#   1. Set REPO_URL below (for a private repo, use a deploy key or a token URL).
#   2. Store the full contents of your .env in AWS Systems Manager Parameter Store as a
#      SecureString named /monkey-inc/env, and give the instance an IAM role allowing
#      ssm:GetParameter on it (plus kms:Decrypt for the default key).
#   3. Optional: bot_data/ is not in the public repo. To seed a brand-new database, upload
#      the roster CSV and squad_timings.txt to a private S3 folder, set SEED_S3_URI, and allow
#      the IAM role s3:GetObject/s3:ListBucket on it. Without seed files, the bot starts
#      with an empty database; hosts can then use /host import and /host squad_time.
set -euxo pipefail

REPO_URL="https://github.com/YOUR_USER/boss-avail-bot.git"
SEED_S3_URI=""   # e.g. s3://my-private-bucket/monkey-inc/bot_data/
APP_DIR=/opt/monkey-inc
APP_USER=ec2-user

dnf install -y git python3.11 cronie
systemctl enable --now crond

if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER" "$APP_DIR"

TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
aws ssm get-parameter --region "$REGION" --name /monkey-inc/env --with-decryption \
  --query Parameter.Value --output text > "$APP_DIR/.env"
chown "$APP_USER" "$APP_DIR/.env"

if [[ -n "$SEED_S3_URI" ]]; then
  aws s3 cp --recursive "$SEED_S3_URI" "$APP_DIR/bot_data/"
  chown -R "$APP_USER" "$APP_DIR/bot_data"
fi
chmod 600 "$APP_DIR/.env"

sudo -u "$APP_USER" SERVICE_USER="$APP_USER" bash "$APP_DIR/deploy/install.sh"
