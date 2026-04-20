# applypilot-discovery

Standalone job discovery worker for [ApplyPilot](https://github.com/yassine-eluharani/vigilant-funicular). Scrapes job boards on a schedule and writes results to a shared Turso database.

## How it works

Runs a loop every `INTERVAL_HOURS`. Each cycle:
1. Loads popular searches from `popular_searches.yaml`
2. Loads per-user search configs from the database (`users.searches_json`)
3. Deduplicates query × location × boards combos
4. Skips combos scraped within `STALE_AFTER_HOURS`
5. Runs JobSpy for each stale combo, writes new jobs to the DB

The main ApplyPilot app reads from the same database — jobs appear automatically.

## Setup

```bash
cp .env.example .env
# Fill in DATABASE_URL and DATABASE_TOKEN (Turso)

pip install -r requirements.txt
python main.py
```

## LXC / homelab deployment

```bash
# Run as root on the LXC:
bash setup-lxc.sh

cp /opt/applypilot-discovery/.env.example /opt/applypilot-discovery/.env
# Fill in .env

sudo systemctl start applypilot-discovery
journalctl -u applypilot-discovery -f
```

CI/CD is handled by a self-hosted GitHub Actions runner on the LXC. Every push to `main` triggers a `git pull` + service restart automatically.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | — | Turso URL (`libsql://...`) |
| `DATABASE_TOKEN` | — | Turso auth token |
| `INTERVAL_HOURS` | `2` | How often to run a cycle |
| `STALE_AFTER_HOURS` | `2` | Re-scrape threshold |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
