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
    conn.commit()


def cleanup_old_jobs(days: int = 7, conn=None) -> int:
    """Delete stale jobs that no user has engaged with.

    A job is safe to delete when:
      - It was discovered more than `days` days ago, AND
      - No user has scored, tailored, covered, or applied to it (checked via
        the user_jobs table — written by the main applypilot platform).

    If the user_jobs table does not exist yet (discovery worker running before
    the main app), all unengaged old jobs are deleted.

    Returns the number of rows deleted.
    """
    if conn is None:
        conn = get_connection()

    cutoff = f"-{days} days"

    # Check whether the user_jobs table exists in this DB
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_jobs'"
    ).fetchone()
    has_user_jobs = row is not None

    if has_user_jobs:
        cursor = conn.execute(
            """
            DELETE FROM jobs
            WHERE discovered_at < datetime('now', ?)
            AND url NOT IN (
                SELECT DISTINCT job_url FROM user_jobs
                WHERE fit_score             IS NOT NULL
                   OR tailored_resume_path  IS NOT NULL
                   OR cover_letter_path     IS NOT NULL
                   OR applied_at            IS NOT NULL
            )
            """,
            (cutoff,),
        )
    else:
        # user_jobs doesn't exist — safe to delete anything old
        cursor = conn.execute(
            "DELETE FROM jobs WHERE discovered_at < datetime('now', ?)",
            (cutoff,),
        )

    deleted = cursor.rowcount
    conn.commit()

    if deleted:
        log.info("Cleanup: deleted %d jobs older than %d days", deleted, days)
    else:
        log.debug("Cleanup: no jobs older than %d days to remove", days)

    return deleted
