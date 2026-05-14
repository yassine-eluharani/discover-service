"""Database connection — SQLite or Turso (libSQL)."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_local = threading.local()


def get_connection() -> sqlite3.Connection:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("libsql://") or url.startswith("wss://"):
        return _turso_connection(url, os.environ.get("DATABASE_TOKEN", ""))
    return _sqlite_connection(url or _default_sqlite_path())


def _default_sqlite_path() -> str:
    app_dir = os.environ.get("APPLYPILOT_DIR", str(Path.home() / ".applypilot"))
    return str(Path(app_dir) / "applypilot.db")


def _sqlite_connection(path: str) -> sqlite3.Connection:
    if not hasattr(_local, "sqlite_conns"):
        _local.sqlite_conns = {}
    conn = _local.sqlite_conns.get(path)
    if conn:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.sqlite_conns[path] = conn
    return conn


def _turso_connection(url: str, token: str):
    if not hasattr(_local, "turso_conn"):
        from turso import TursoConnection
        _local.turso_conn = TursoConnection(url, token)
    return _local.turso_conn


def init_db() -> None:
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            company               TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,
            filtered_at           TEXT,
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT,
            job_metadata_json     TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovery_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            query        TEXT NOT NULL,
            location     TEXT NOT NULL,
            boards_json  TEXT NOT NULL,
            started_at   TEXT,
            completed_at TEXT,
            status       TEXT DEFAULT 'pending',
            jobs_found   INTEGER DEFAULT 0
        )
    """)
    # Indexes — added to avoid full-table scans on every cycle
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovery_runs_lookup "
        "ON discovery_runs(query, location, boards_json, status, completed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_detail_scraped_at ON jobs(detail_scraped_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_filtered_at ON jobs(filtered_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_metadata_json ON jobs(job_metadata_json)"
    )
    conn.commit()


_ENGAGED_USER_JOBS_FILTER = """
    SELECT DISTINCT job_url FROM user_jobs
    WHERE fit_score             IS NOT NULL
       OR tailored_resume_path  IS NOT NULL
       OR tailored_resume_text  IS NOT NULL
       OR cover_letter_path     IS NOT NULL
       OR cover_letter_text     IS NOT NULL
       OR favorited = 1
       OR applied_at            IS NOT NULL
"""


def cleanup_old_jobs(days: int = 3, conn=None, batch_size: int = 500) -> int:
    """Delete stale jobs that no user has engaged with.

    A job is safe to delete when:
      - It was discovered more than `days` days ago, AND
      - No user has scored, tailored, covered, favorited, or applied to it
        (checked via the user_jobs table — written by the main applypilot
        platform).

    Pre-filter reject rows in `user_jobs` (e.g. ``apply_status='location_filtered'``
    with no fit_score/tailor/cover/applied_at) are not engagement; they are
    cascade-deleted alongside the parent job to satisfy the FK constraint.

    Batched in chunks of `batch_size` URLs so multi-thousand-row purges fit
    within Turso's HTTP request budget. If `user_jobs` does not exist (worker
    running before the main app), all unengaged old jobs are deleted.

    Returns the total number of `jobs` rows deleted.
    """
    if conn is None:
        conn = get_connection()

    cutoff = f"-{days} days"

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_jobs'"
    ).fetchone()
    has_user_jobs = row is not None

    total_deleted = 0
    while True:
        if has_user_jobs:
            rows = conn.execute(
                f"""
                SELECT url FROM jobs
                WHERE discovered_at < datetime('now', ?)
                  AND url NOT IN ({_ENGAGED_USER_JOBS_FILTER})
                LIMIT ?
                """,
                (cutoff, batch_size),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT url FROM jobs WHERE discovered_at < datetime('now', ?) LIMIT ?",
                (cutoff, batch_size),
            ).fetchall()
        if not rows:
            break
        urls = [r["url"] if hasattr(r, "keys") else r[0] for r in rows]
        placeholders = ",".join("?" for _ in urls)
        if has_user_jobs:
            conn.execute(
                f"DELETE FROM user_jobs WHERE job_url IN ({placeholders})", urls
            )
        cur = conn.execute(
            f"DELETE FROM jobs WHERE url IN ({placeholders})", urls
        )
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(urls)
        total_deleted += n

    conn.commit()

    if total_deleted:
        log.info("Cleanup: deleted %d jobs older than %d days", total_deleted, days)
    else:
        log.debug("Cleanup: no jobs older than %d days to remove", days)

    return total_deleted
