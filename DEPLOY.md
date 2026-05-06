# Deploy

Three runtime modes. Most personal users will want **systemd on a Linux home server** (the third one) — that's where Randy is designed to live.

## 1. Local Python (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# In one terminal — Telegram bot:
python -m randy

# In another — web dashboard at http://127.0.0.1:8000:
python -m randy.web
```

DB lands at `./randy.sqlite`. Both processes use it; SQLite is in WAL mode so they don't fight.

## 2. Docker Compose (alternative — if you prefer containers)

```bash
docker compose up -d --build
docker compose logs -f randy
```

DB persists in `./data/randy.sqlite`. Note: the Docker setup currently runs only the bot. To add the web dashboard, append a second service to `docker-compose.yml` running `python -m randy.web`. Most folks prefer systemd for personal home-server use; see #3.

## 3. Linux home server with systemd (recommended)

Designed for a small always-on Linux box (NUC, mini-PC, Raspberry Pi 4/5) on your home LAN. Two systemd user units, manual git-pull updates over SSH.

### What you'll set up

- `~/randy/` — git checkout
- `~/randy/.venv/` — Python virtualenv
- `~/randy/.env` — secrets (mode 600)
- `~/randy/data/randy.sqlite` — persistent DB (auto-created on first run)
- `~/.config/systemd/user/randy.service` — Telegram bot
- `~/.config/systemd/user/randy-web.service` — FastAPI web app on `0.0.0.0:8000`

The web binds to `0.0.0.0` so phones on the LAN can reach it (e.g. `http://192.168.86.41:8000`). The bot uses outbound polling — no inbound ports needed.

**Trust model**: LAN-only means anyone on your home network can hit the dashboard. No auth. Don't put it on a network you don't trust.

### One-time setup

On the server (assumes a sudoer user, e.g. `bruceli`):

```bash
# Prereqs
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev git build-essential

# Clone
git clone https://github.com/zbruceli/Randy.git ~/randy
cd ~/randy

# Virtualenv + install
python3.11 -m venv .venv
.venv/bin/pip install -q -e '.[dev]'

# Secrets
cp .env.example .env
vim .env   # paste API keys, telegram bot token, etc.

# Server-specific overrides in .env:
#   WEB_HOST=0.0.0.0          # listen on all interfaces (LAN-accessible)
#   WEB_PORT=8000
#   DB_PATH=/home/<user>/randy/data/randy.sqlite
#   TELEGRAM_ALLOWED_USER_IDS=<your numeric Telegram user id>
chmod 600 .env

mkdir -p data

# systemd user units
mkdir -p ~/.config/systemd/user
cp deploy/systemd/randy.service       ~/.config/systemd/user/
cp deploy/systemd/randy-web.service   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now randy.service randy-web.service

# So the units keep running after you log out:
loginctl enable-linger "$USER"

# Verify
systemctl --user status randy.service randy-web.service
```

The web should now be reachable at `http://<server-ip>:8000` from any device on the LAN, and the Telegram bot should respond to DMs.

### Updates (everyday)

```bash
ssh user@server
cd ~/randy && ./scripts/deploy.sh
```

What `deploy.sh` does:
1. `git pull --ff-only` — refuses to clobber local changes.
2. `pip install -q -e '.[dev]'` — picks up new deps.
3. `systemctl --user restart randy randy-web`.
4. Sleeps 3s, then verifies both services are `active`.
5. Prints the last 5 log lines per service and the LAN URL.

If anything fails the script exits non-zero and prints `systemctl status` output. The previously-running version stays up — there's no rollback automation, but a `git revert HEAD && ./scripts/deploy.sh` reverts cleanly.

### Logs

```bash
journalctl --user -u randy -f          # bot, follow
journalctl --user -u randy-web -f      # web, follow
journalctl --user -u randy --since '1 hour ago'
```

### Cutover from your dev machine

Telegram allows only one poller per bot token at a time — both pollers will get HTTP 409 and fight.

**Option A (recommended): separate bots**. Create a new bot with `@BotFather` on Telegram, give it a different name (e.g. `@RandyHome_bot`). Use that token in the server `.env`. Your dev Mac keeps using the original token.

**Option B: same bot, single poller**. Stop the dev bot before starting the server: `kill <pid>` on the Mac. After that, only the server polls.

### Backups

The SQLite file is your only state. A nightly cron backup is enough for personal use:

```bash
mkdir -p ~/backups
crontab -e
# Add:
@daily cp ~/randy/data/randy.sqlite ~/backups/randy-$(date +\%Y\%m\%d).sqlite && find ~/backups -name 'randy-*.sqlite' -mtime +30 -delete
```

For continuous replication to S3/B2, install [Litestream](https://litestream.io) — overkill for most personal setups.

### Troubleshooting

- **`409 Conflict` in bot logs**: another instance is polling the same token. Kill it (likely your Mac dev bot).
- **Web reachable from server but not LAN**: `WEB_HOST` is still `127.0.0.1`. Set it to `0.0.0.0` in `.env` and `systemctl --user restart randy-web`.
- **`Bind for 0.0.0.0:8000 failed`**: another process owns port 8000. `sudo ss -lntp | grep 8000` to find it.
- **Service stuck restarting**: `journalctl --user -u randy -n 50` for the actual error. Common: missing API key in `.env`, or `.env` not readable.
- **Web is up but the bot isn't**: most often a Telegram-side issue (token revoked, bot deleted). `curl https://api.telegram.org/bot<TOKEN>/getMe` to verify.

### Optional: systemd timer for daily backups instead of cron

```bash
# ~/.config/systemd/user/randy-backup.service
[Unit]
Description=Randy SQLite snapshot

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'mkdir -p %h/backups && cp %h/randy/data/randy.sqlite %h/backups/randy-$(date +%%Y%%m%%d-%%H%%M).sqlite && find %h/backups -name "randy-*.sqlite" -mtime +30 -delete'

# ~/.config/systemd/user/randy-backup.timer
[Unit]
Description=Daily Randy backup

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now randy-backup.timer
```

## Operational notes (all modes)

- **One bot instance only.** Telegram returns 409 Conflict if two pollers fight for the same token.
- **Cost cap.** `SESSION_COST_CAP_USD=25` is the default hard stop per session; set it lower if you want a stricter ceiling.
- **Logs.** Bot logs to stdout — under systemd that's `journalctl`, under Docker it's `docker compose logs`.
- **Secrets.** `.env` is in `.gitignore`. `chmod 600 ~/randy/.env`. Don't commit it.
