# Monkey Inc (boss-avail-bot)

Discord bot for CQ bossing availability. Players confirm or update their weekly squad
availability with slash commands and buttons. Hosts check who has confirmed, view
availability, and manage squad times and reminders.

- Player guide: [user_guide.txt](user_guide.txt)
- Host guide: [host_guide.txt](host_guide.txt)

## Run locally

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # then fill in DISCORD_TOKEN, GUILD_ID, HOST_ROLE_ID
.venv/bin/python -m bot
```

On first start, the bot creates `data/monkey_inc.db` and fills it from
`bot_data/squad_timings.txt` and `bot_data/Monkey, Inc.  - CQ Roster.csv`. No manual
database setup is needed. To start over, stop the bot and delete `data/`.

> **`bot_data/` is git-ignored** because the roster contains players' Discord handles and
> IDs. It never goes to GitHub. Keep your copy locally. Without it, the bot starts with an
> empty database, and hosts can load the roster with `/host import` and add squads with
> `/config squad_time ... permanent:True`. The seed-based tests are skipped when the files
> are missing.

Run the tests with `.venv/bin/python -m pytest`.

### Discord application setup (one time)

1. In https://discord.com/developers/applications, create an application, then open
   **Bot** and **Reset Token**. Put the token in `.env` as `DISCORD_TOKEN`.
2. Still on the **Bot** page, under **Privileged Gateway Intents**, turn on **Message Content
   Intent**. The bot needs it to read damage log files posted in #queen-logs. If it's
   off, the bot fails to start with a `PrivilegedIntentsRequired` error.
3. Open **OAuth2 > URL Generator** and select scopes `bot` and `applications.commands`, plus
   bot permissions **View Channels**, **Send Messages**, **Embed Links**, **Attach Files**,
   **Read Message History** and **Add Reactions**. Open the generated URL and invite the
   bot to your server.
4. In Discord, turn on Developer Mode (Settings > Advanced). Right-click to copy the
   server ID (`GUILD_ID`), the host role ID (`HOST_ROLE_ID`), and the player role to ping
   in reminders (`PLAYER_ROLE_ID`). For the ping to notify people, the player role needs
   **Allow anyone to @mention this role** turned on (Server Settings > Roles).
   Optionally set `ROSTER_CONTACT_ID` to who players should message about new characters: their
   **user ID, just the digits** (right-click them > Copy User ID), which shows as a clickable
   @mention, or a plain name. Don't add `<@ >`. It appears in `/cq characters` and defaults
   to "a host".
5. Show the host commands to hosts. `/host`, `/config`, `/player` and `/character` are
   hidden from everyone without Manage Server. Go to Server Settings > Integrations >
   Monkey Inc. > Manage, click each of the four, and allow the host role (see
   host_guide.txt, "Who can see the host commands"). Optionally copy the CQ
   channel ID too (`CQ_CHANNEL_ID`), or set it later with `/config reminders`.

## Development vs production

| | Production | Development |
|---|---|---|
| Runs on | EC2, built by Terraform ([infra/](infra/)) | your computer, `python -m bot` |
| Discord bot | Monkey Inc, in the official server | a separate test bot, in a test server |
| Config | SSM parameter `/monkey-inc/env` | your local `.env` |
| Database | `/opt/monkey-inc/data/monkey_inc.db` on the instance | `data/monkey_inc.db` in this folder |
| Updated by | merging into `main` (GitHub Actions deploys) | `git checkout` + restart |

Never run the production bot's token in two places. If two programs log in with the same token,
both answer every button press, and players get "interaction failed" or duplicate replies. Your
local `.env` must always hold the **test** bot's token.

**One-time dev setup:**

1. **Create a second Discord application** (e.g. "Monkey Inc Test") in the Developer Portal and
   follow the one-time setup above: reset and copy its token, turn on Message Content Intent, and
   invite it to your **test server** with the same scopes and permissions.
2. **Point your local `.env` at the test server:**
   - `DISCORD_TOKEN` is the **test bot's** token.
   - `GUILD_ID`, `HOST_ROLE_ID`, `PLAYER_ROLE_ID`, `CQ_CHANNEL_ID` and `QUEEN_LOGS_CHANNEL_ID` are
     the test server's IDs. Every server has its own.
3. **Choose the test data**, before the next start:
   - **Fresh:** `rm -rf data/`. The database is rebuilt from `bot_data/`.
   - **A copy of production:** download a nightly backup:
     `aws s3 cp s3://<bucket>/backups/<file>.db data/monkey_inc.db` (stop the dev bot first).
   Nothing you do in dev ever touches the production database.
4. **Run it:** `.venv/bin/python -m bot`.

Notes:
- Class icons are per bot, so the test bot uploads its own set from `bot_data/class_icons/` on
  startup.
- To use the player commands in the test server, your Discord username must be on the roster, or
  add yourself there with `/player add`.
- To try a branch: `git checkout <branch>`, then restart the dev bot.

## Deploy to EC2 (production)

Production is one small EC2 instance (Amazon Linux 2023, t4g.micro, us-east-1) described in
[infra/](infra/). It has no open ports; you get a shell with SSM Session Manager. Pieces:

- **The instance** runs the `monkey-inc` systemd service from `/opt/monkey-inc` (a clone of
  `main`), restarting on crash and starting on boot. The SQLite database is
  `/opt/monkey-inc/data/monkey_inc.db`.
- **Config** is the production `.env`, stored in SSM Parameter Store as the SecureString
  `/monkey-inc/env`. It is never in Terraform or GitHub.
- **A private S3 bucket** (`monkey-inc-<account id>`) holds `bot_data/` (roster, squad timings,
  class icons), `migration/` (the one-time database upload), and `backups/` (nightly copies).
- **GitHub Actions** ([.github/workflows/deploy.yml](.github/workflows/deploy.yml)) runs the tests
  on every PR and every push to `main`. After a push to `main` passes, it logs in to AWS with
  GitHub OIDC (no AWS keys are stored in GitHub) and tells the instance, through SSM, to check out
  that commit and run [deploy/deploy.sh](deploy/deploy.sh). That script refreshes `.env` from SSM,
  backs up the database, runs `deploy/install.sh` (pip install and restart), and fails the run
  if the bot doesn't stay up.

### Tools (one time)

```bash
brew install hashicorp/tap/terraform awscli
aws configure          # an IAM user (or SSO profile) with admin rights, region us-east-1
gh auth login
```

For SSM shell access, also install the Session Manager plugin:
`brew install --cask session-manager-plugin`.

### First setup and moving the data from your computer

The production bot moves from your computer to EC2. Run these in order:

1. **Create the bucket and IAM roles, without the instance yet:**
   ```bash
   cd infra
   terraform init
   terraform apply -var instance_enabled=false
   terraform output        # note bucket and deploy_role_arn
   cd ..
   ```
   This also creates the AWS-side GitHub OIDC provider. If your account already has one for
   `token.actions.githubusercontent.com`, import it first:
   `terraform import aws_iam_openid_connect_provider.github arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com`.
2. **Upload the seed data and class icons** (the bucket is private):
   `aws s3 sync bot_data/ s3://<bucket>/bot_data/`
3. **Store the production `.env` in SSM** (your current `.env`, which holds the production token):
   `aws ssm put-parameter --name /monkey-inc/env --type SecureString --value file://.env`
4. **Merge the PR that adds `infra/` and `deploy/deploy.sh` into `main`.** The instance clones
   `main`. The deploy job is skipped because `AWS_DEPLOY_ROLE_ARN` isn't set yet. Also make sure
   `main` has every database migration your local database has.
5. **Stop the local bot** (Ctrl+C), then upload a consistent copy of its database:
   ```bash
   .venv/bin/python -m bot.backup          # prints data/backups/monkey_inc-<time>.db
   aws s3 cp data/backups/monkey_inc-<time>.db s3://<bucket>/migration/monkey_inc.db
   ```
   From here until step 6 finishes, the bot is offline.
6. **Create the instance:** `cd infra && terraform apply`. On first boot it installs Python,
   clones the repo, downloads `bot_data/`, restores `migration/monkey_inc.db`, and starts the
   bot (about 3 to 5 minutes). Check it (see "Logs and shell" below).
7. **Turn on automatic deploys:**
   `gh variable set AWS_DEPLOY_ROLE_ARN --body "$(terraform -chdir=infra output -raw deploy_role_arn)"`
   Then re-run the latest workflow on `main` (Actions tab > Test and deploy > Run workflow) to
   check that deploys work.
8. **Switch your computer to dev:** put the test bot's token and test server IDs in your local
   `.env` (see "Development vs production").

### Day to day

- **Ship a change:** merge a PR into `main`. Actions runs the tests, then deploys. Watch it in the
  Actions tab. A failed test or a bot that doesn't start fails the run.
- **Change production settings** (`.env` values):
  `aws ssm put-parameter --name /monkey-inc/env --type SecureString --overwrite --value file://prod.env`,
  then re-run the workflow on `main` (or run `sudo bash /opt/monkey-inc/deploy/deploy.sh` on the
  instance). Keep `prod.env` outside the repo, or delete it afterwards.
- **Update roster seed files or class icons:** `aws s3 sync bot_data/ s3://<bucket>/bot_data/`,
  then on the instance: `sudo -u ec2-user aws s3 sync s3://<bucket>/bot_data/ /opt/monkey-inc/bot_data/`.
  The icon sync picks up changes within 10 minutes.

### Logs and shell

```bash
aws ssm start-session --target "$(terraform -chdir=infra output -raw instance_id)"
sudo journalctl -u monkey-inc -f        # live logs
sudo systemctl status monkey-inc
```

### Backups and restore

Every night at 04:00 (UTC on the instance) the bot copies the database to
`/opt/monkey-inc/data/backups/` (newest 14 kept) and syncs that folder to `s3://<bucket>/backups/`.
Every deploy also takes a backup first.

To restore, on the instance:

```bash
sudo systemctl stop monkey-inc
cd /opt/monkey-inc
sudo -u ec2-user cp data/backups/<file>.db data/monkey_inc.db   # or: aws s3 cp s3://<bucket>/backups/<file>.db ...
sudo rm -f data/monkey_inc.db-wal data/monkey_inc.db-shm
sudo systemctl start monkey-inc
```

To rebuild the instance from scratch: upload the backup to `s3://<bucket>/migration/monkey_inc.db`,
then `terraform apply -replace='aws_instance.bot[0]'`. The new instance restores it on first boot.

`terraform apply` never replaces the instance on its own when a newer AMI comes out or the
bootstrap script changes. Patch it in place with `sudo dnf upgrade -y` over SSM.

### Without Terraform

`deploy/install.sh` still works on any Linux box with systemd: clone the repo, create `.env`,
optionally copy `bot_data/`, and run `./deploy/install.sh`. It is safe to re-run after a
`git pull`.

## Taking the bot down

Back up first if you might want the data again: `.venv/bin/python -m bot.backup` writes a
copy to `data/backups/`. Copy that file somewhere safe **outside** the project folder.

### Local (your Mac)

1. **Stop the bot:** press `Ctrl+C` in the terminal where `python -m bot` is running.
   If you can't find that terminal: `pkill -f "python -m bot"`.
2. **Leave the virtualenv** (only if you ran `source .venv/bin/activate`): `deactivate`
3. **Delete the database** (all players, availability and settings): `rm -rf data/`
   The next start creates a fresh one from `bot_data/`.
4. **Delete the virtualenv** (optional; recreate with the setup steps above): `rm -rf .venv`

### EC2

To pause the bot without removing anything: `sudo systemctl stop monkey-inc` on the instance,
and later `sudo systemctl start monkey-inc`.

To remove production entirely, first copy the latest backup somewhere safe
(`aws s3 sync s3://<bucket>/backups/ ~/monkey-inc-backups/`), then:

```bash
gh variable delete AWS_DEPLOY_ROLE_ARN                 # stop deploys
aws s3 rm --recursive s3://<bucket>                    # Terraform won't delete a non-empty bucket
aws s3api delete-objects --bucket <bucket> --delete "$(aws s3api list-object-versions --bucket <bucket> \
  --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json)"   # old versions too
cd infra && terraform destroy
aws ssm delete-parameter --name /monkey-inc/env
```

### Discord

- The slash commands stay visible in the server after the bot stops. To remove them, kick
  the bot from the server (Server Settings > Members, or Integrations), which also removes
  its guild commands.
- To disable the bot for good, reset its token (Developer Portal > Bot > Reset Token) or
  delete the application. The old token stops working immediately. Do this right away if
  the token is ever leaked, e.g. committed to GitHub.

## Layout

```
bot/
  __main__.py      entry point (python -m bot)
  config.py        .env / environment settings
  app.py           bot class, command sync, player linking, host check
  db.py            SQLite schema (auto-migrating) and queries
  importer.py      roster CSV + squad timings import, first-run seeding
  reminders.py     reminder/deadline settings
  timeutil.py      week/timezone helpers
  views.py         embeds, reminder buttons, availability editor
  backup.py        python -m bot.backup
  cogs/player.py   player commands
  cogs/host.py     host-only commands: /host, /config, /player, /character
  cogs/reminder.py weekly reminder scheduler
  damage.py        damage log parsing and averaging
  squad_breakdown.py  /host availability per-squad player view
  class_icons.py   class icons (the bot's application emojis), synced from bot_data/class_icons/
  cogs/class_icons.py  background sync of the class icon folder
  cogs/damage_logs.py  watches #queen-logs for damage log uploads
deploy/            systemd unit, install script, production deploy script (deploy.sh)
infra/            Terraform for production (EC2, S3, IAM, GitHub OIDC) and the first-boot script
.github/workflows/  tests on every PR; deploy to EC2 on every push to main
bot_data/          seed data (roster CSV, squad timings, optional class_icons/) -- git-ignored
tests/
```
