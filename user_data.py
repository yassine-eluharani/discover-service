"""DB-backed accessors for per-user profile / search-config / resume.

The worker reads everything per-user from the shared Turso `users` table
(populated by the FastAPI backend through Clerk-authenticated /setup +
profile mutations). No filesystem fallback — the worker never runs in
single-user legacy mode.

`TAILORED_DIR` and `COVER_LETTER_DIR` are exposed as benign placeholders
so the scoring modules' imports succeed; the worker only ever takes the
DB-storage path (text fields on `user_jobs`) and never writes to disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from db import get_connection

log = logging.getLogger(__name__)

# Placeholders. The legacy filesystem-based tailor/cover code paths are
# only triggered when user_id is None — the worker always passes a real
# user_id, so these dirs are never written to.
TAILORED_DIR: Path = Path("/tmp/applypilot-discover-unused")
COVER_LETTER_DIR: Path = Path("/tmp/applypilot-discover-unused")


def _read_user_field(user_id: int | None, column: str) -> str | None:
    if user_id is None:
        return None
    conn = get_connection()
    row = conn.execute(
        f"SELECT {column} FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        return None
    return row[column] if hasattr(row, "keys") else row[0]


def load_profile(user_id: int | None = None) -> dict:
    """Return the user's parsed profile JSON, or empty dict if missing."""
    raw = _read_user_field(user_id, "profile_json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        log.warning("user %s: profile_json is not valid JSON", user_id)
        return {}


def load_search_config(user_id: int | None = None) -> dict:
    """Return the user's parsed searches JSON, or empty dict if missing."""
    raw = _read_user_field(user_id, "searches_json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        log.warning("user %s: searches_json is not valid JSON", user_id)
        return {}


def get_resume_text(user_id: int | None = None) -> str:
    """Return the user's master resume text, or empty string if missing."""
    return _read_user_field(user_id, "resume_text") or ""


def list_active_user_ids() -> list[int]:
    """All Clerk-bound users — i.e. real signed-in accounts.

    Filters out legacy / test rows that have no clerk_id, so the worker
    only spends LLM tokens on people who can actually sign in and see
    the results.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT id FROM users WHERE clerk_id IS NOT NULL ORDER BY id"
    ).fetchall()
    return [r["id"] if hasattr(r, "keys") else r[0] for r in rows]
