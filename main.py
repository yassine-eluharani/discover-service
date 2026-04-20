#!/usr/bin/env python3
"""ApplyPilot Discovery Service — standalone worker.

Runs the discovery scheduler loop: reads all user search configs + the
built-in popular_searches.yaml, deduplicates combos, and scrapes stale ones.

Environment variables:
  DATABASE_URL       libsql://your-db.turso.io  (or leave blank for local SQLite)
  DATABASE_TOKEN     Auth token for Turso
  INTERVAL_HOURS     How often to run a cycle (default: 2)
  STALE_AFTER_HOURS  How old a combo must be to re-scrape (default: 2)
  LOG_LEVEL          Logging level (default: INFO)
"""

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("discovery")

INTERVAL_HOURS = float(os.environ.get("INTERVAL_HOURS", "2"))


def main() -> None:
    from worker import run_cycle

    db_url = os.environ.get("DATABASE_URL", "")
    log.info("ApplyPilot Discovery Service starting")
    log.info("DB: %s", db_url or "SQLite (local)")
    log.info("Cycle: every %.1fh | Stale threshold: %sh",
             INTERVAL_HOURS, os.environ.get("STALE_AFTER_HOURS", "2"))

    while True:
        try:
            log.info("── Starting discovery cycle ──")
            run_cycle()
        except Exception as e:
            log.error("Cycle failed: %s", e, exc_info=True)

        log.info("Sleeping %.1fh until next cycle…", INTERVAL_HOURS)
        time.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    main()
