#!/usr/bin/env python3
"""ApplyPilot Discovery Service — standalone worker.

Runs the discovery scheduler loop: reads all user search configs + the
built-in popular_searches.yaml, deduplicates combos, and scrapes stale ones.

Environment variables:
  DATABASE_URL       libsql://your-db.turso.io  (or leave blank for local SQLite)
  DATABASE_TOKEN     Auth token for Turso
  GEMINI_API_KEY     Gemini API key (for Tier 3 enrichment + indexing)
  OPENAI_API_KEY     OpenAI API key (alternative LLM provider)
  LLM_URL            Local LLM endpoint (alternative to cloud providers)
  LLM_MODEL          Override LLM model name
  INTERVAL_HOURS     How often to run a cycle (default: 2)
  STALE_AFTER_HOURS  How old a combo must be to re-scrape (default: 2)
  LOG_LEVEL          Logging level (default: INFO)
"""

import logging
import os
import signal
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

# Shutdown flag — set by SIGTERM/SIGINT so the current cycle finishes cleanly
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %d — will shut down after current cycle completes", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def main() -> None:
    from worker import run_cycle

    db_url = os.environ.get("DATABASE_URL", "")
    log.info("ApplyPilot Discovery Service starting")
    log.info("DB: %s", db_url or "SQLite (local)")
    log.info("Cycle: every %.1fh | Stale threshold: %sh",
             INTERVAL_HOURS, os.environ.get("STALE_AFTER_HOURS", "2"))

    while not _shutdown:
        try:
            log.info("── Starting discovery cycle ──")
            run_cycle()
        except Exception as e:
            log.error("Cycle failed: %s", e, exc_info=True)

        if _shutdown:
            break

        log.info("Sleeping %.1fh until next cycle…", INTERVAL_HOURS)
        # Sleep in short increments so we can respond to shutdown signal
        deadline = time.time() + INTERVAL_HOURS * 3600
        while time.time() < deadline and not _shutdown:
            time.sleep(5)

    log.info("Discovery service stopped cleanly")


if __name__ == "__main__":
    main()
