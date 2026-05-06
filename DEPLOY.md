# Deploy

Three ways to run Randy. All assume you've populated `.env` with API keys + Telegram bot token (see `.env.example`).

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

## 2. Docker Compose (recommended for personal always-on)

```bash
docker compose up -d --build
docker compose logs -f randy
```

DB persists in `./data/randy.sqlite` (volume mounted at `/app/data` in the container). The container restarts unless explicitly stopped.

To upgrade after a `git pull`:

```bash
docker compose up -d --build
```

To wipe everything (including DB):

```bash
docker compose down
rm -rf ./data
```

## 3. systemd on a VPS

For a long-lived host without Docker, run under a user-level systemd unit. Save the snippet below as `~/.config/systemd/user/randy.service`, edit paths, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now randy
journalctl --user -u randy -f
```

```ini
[Unit]
Description=Randy advisory bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/youruser/Randy
EnvironmentFile=/home/youruser/Randy/.env
ExecStart=/home/youruser/Randy/.venv/bin/python -m randy
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

## Operational notes

- **One bot instance only.** Telegram returns 409 Conflict if two pollers fight for the same token. If you see this, kill the duplicate process.
- **Keys.** The bot will boot with no Telegram token and crash; with no LLM keys it'll boot but every consultation fails. Either way, populate `.env`.
- **Cost cap.** `SESSION_COST_CAP_USD=25` is the default hard stop per session; set it lower if you want a stricter ceiling for first runs.
- **Backups.** `randy.sqlite` is the only stateful file. Snapshot it however you like (rsync, restic, `litestream replicate`).
- **Logs.** The bot logs to stdout; under Docker that lands in `docker compose logs`, under systemd in `journalctl`.
