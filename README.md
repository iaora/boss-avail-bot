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
5. Show the host commands to hosts. `/host`, `/config`, `/player` and `/character` are
   hidden from everyone without Manage Server. Go to Server Settings > Integrations >
   Monkey Inc. > Manage, click each of the four, and allow the host role (see
   host_guide.txt, "Who can see the host commands"). Optionally copy the CQ
   channel ID too (`CQ_CHANNEL_ID`), or set it later with `/config reminders`.

## Deploy to EC2

The app is self-contained: the code, a `.env` file, and a `data/` folder holding the SQLite
database.

**Automatic (new instance):** launch Amazon Linux 2023 with
[deploy/ec2-user-data.sh](deploy/ec2-user-data.sh) as the User data. Edit `REPO_URL` first,
and store your `.env` contents in SSM Parameter Store as the SecureString
`/monkey-inc/env`. The instance clones the repo, writes `.env`, and installs and starts the
service. To seed the database, upload `bot_data/` to a **private** S3 folder and set
`SEED_S3_URI` in the script. Or copy your existing database instead (see below).

**Manual (existing instance):**

```bash
git clone <repo> ~/boss-avail-bot && cd ~/boss-avail-bot
cp .env.example .env && nano .env
# optional seed data, from your machine:
#   scp -r bot_data ec2-user@<host>:~/boss-avail-bot/
./deploy/install.sh
```

`install.sh` is safe to re-run after a `git pull`. It:
- creates the venv and installs the requirements,
- installs the `monkey-inc` systemd service, which restarts on crash and starts on boot,
- sets up a nightly database backup to `data/backups/`.

Logs: `sudo journalctl -u monkey-inc -f`.

**Moving an existing database:** stop the bot, copy `data/monkey_inc.db` to the same path
on the new machine, then start the service. Schema upgrades run automatically at startup.

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

```bash
sudo systemctl stop monkey-inc                          # stop the bot
sudo systemctl disable monkey-inc                       # don't start on boot
sudo rm /etc/systemd/system/monkey-inc.service && sudo systemctl daemon-reload
crontab -l | grep -v 'bot.backup' | crontab -           # remove the nightly backup job
rm -rf ~/boss-avail-bot/data                            # delete the database + backups
rm -rf ~/boss-avail-bot                                 # delete everything (code, .venv, .env)
```

(If you used `ec2-user-data.sh`, the app lives in `/opt/monkey-inc` instead of `~/boss-avail-bot`.)
To pause the bot without removing anything, use `sudo systemctl stop monkey-inc` and later
`sudo systemctl start monkey-inc`.

To remove the AWS resources as well: terminate the instance in the EC2 console, then delete
the `/monkey-inc/env` parameter in SSM Parameter Store and any seed files in S3.

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
deploy/            systemd unit, install script, EC2 user data
bot_data/          seed data (roster CSV, squad timings, optional class_icons/) -- git-ignored
tests/
```
