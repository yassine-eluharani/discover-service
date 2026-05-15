"""Per-user application orchestration.

Two entry points:

  prepare_apply_for_user(conn, user_id, max_jobs)
    - Finds fit_score>=9 jobs with both docs ready and no apply_status.
    - For each, dispatches to a registered handler in dry_run mode.
    - Persists screenshot path + apply_status='ready_to_submit', or
      'failed' / 'manual_only' on the relevant misses.
    - Capped at max_jobs per call to bound LLM-PDF + browser-launch cost.

  submit_prepared_for_user(conn, user_id)
    - Drains rows where apply_status='submitting' (set by the backend
      when the user clicks "Approve & Submit" on /apply).
    - Re-runs the same handler with submit=True, captures the success
      page, marks 'applied' or 'failed'.

Both share `_run_handler()` which manages browser lifecycle + screenshot
naming + DB persistence. Each call launches and tears down its own
Chromium context to keep failure blast radius small.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from apply_handlers import dispatch
from db import get_connection, upsert_user_job
from user_data import get_resume_text, load_profile

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Where to save screenshots. The Docker compose mounts a `data` volume at
# /data; reuse it so the screenshots survive container recreations.
SCREENSHOT_DIR = Path(os.environ.get("APPLY_SCREENSHOT_DIR", "/data/apply_screenshots"))


def _screenshot_path(user_id: int, job_url: str, kind: str) -> Path:
    """Stable, dedupable filename per (user, job, kind)."""
    h = hashlib.sha1(job_url.encode("utf-8")).hexdigest()[:12]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return SCREENSHOT_DIR / str(user_id) / f"{h}_{kind}_{ts}.png"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_handler(
    handler,
    job: dict,
    profile: dict,
    resume_text: str,
    cover_text: str,
    user_id: int,
    submit: bool,
) -> dict:
    """Launch a fresh browser context, run handler.fill, return its result."""
    kind = "submit" if submit else "prepared"
    screenshot_path = _screenshot_path(user_id, job["url"], kind)

    t0 = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=UA)
                page = context.new_page()
                result = handler.fill(
                    page=page,
                    job=job,
                    profile=profile,
                    resume_text=resume_text,
                    cover_text=cover_text,
                    submit=submit,
                    screenshot_path=screenshot_path,
                    resume_pdf_path=None,  # Phase B: text-only; handlers skip resume upload if None
                )
            finally:
                browser.close()
    except Exception as e:
        log.exception("apply_runner: handler raised for %s", job["url"][:80])
        return {
            "status": "failed",
            "error": f"handler crashed: {type(e).__name__}: {e}",
            "screenshot_url": None,
        }

    result["elapsed_ms"] = int((time.time() - t0) * 1000)
    return result


def _persist(conn, user_id: int, job_url: str, result: dict) -> None:
    """Write the handler's result back to user_jobs."""
    fields: dict = {
        "apply_status": result["status"],
        "last_attempted_at": _now_iso(),
        "apply_duration_ms": result.get("elapsed_ms"),
    }
    if result.get("screenshot_url"):
        fields["apply_screenshot_url"] = result["screenshot_url"]
    if result.get("error"):
        fields["apply_error"] = result["error"][:1000]  # bound size
    if result["status"] == "applied":
        fields["applied_at"] = _now_iso()
        fields["apply_error"] = None  # clear any prior failure
    # Bump apply_attempts atomically via a separate read.
    cur_attempts = (conn.execute(
        "SELECT COALESCE(apply_attempts, 0) FROM user_jobs WHERE user_id = ? AND job_url = ?",
        (user_id, job_url),
    ).fetchone() or [0])[0]
    fields["apply_attempts"] = int(cur_attempts or 0) + 1

    upsert_user_job(conn, user_id, job_url, **fields)


# ---------------------------------------------------------------------------
# Public stages — called by worker.run_cycle()
# ---------------------------------------------------------------------------

def prepare_apply_for_user(conn, user_id: int, max_jobs: int) -> None:
    """Prepare (dry-run fill) up to max_jobs eligible applications."""
    if not int(os.environ.get("AUTO_APPLY_ENABLED", "1")):
        log.info("apply: disabled via AUTO_APPLY_ENABLED=0")
        return

    rows = conn.execute(
        """
        SELECT j.url AS url,
               j.title AS title,
               j.application_url AS application_url,
               j.site AS site,
               uj.fit_score AS fit_score,
               uj.tailored_resume_text AS tailored,
               uj.cover_letter_text    AS cover
        FROM jobs j
        JOIN user_jobs uj ON uj.job_url = j.url AND uj.user_id = ?
        WHERE uj.fit_score >= 9
          AND uj.tailored_resume_text IS NOT NULL
          AND uj.cover_letter_text    IS NOT NULL
          AND uj.apply_status IS NULL
          AND uj.dismissed_at IS NULL
          AND j.closed_at IS NULL
          AND j.application_url IS NOT NULL
        ORDER BY uj.fit_score DESC, uj.scored_at DESC
        LIMIT ?
        """,
        (user_id, max_jobs),
    ).fetchall()

    if not rows:
        log.info("prepare-apply: user_id=%s — nothing to prepare", user_id)
        return

    profile = load_profile(user_id)
    if not profile:
        log.warning("prepare-apply: user_id=%s — empty profile, skipping", user_id)
        return
    resume_text = get_resume_text(user_id) or ""

    log.info("prepare-apply: user_id=%s — %d candidate(s)", user_id, len(rows))
    for row in rows:
        job = dict(row) if hasattr(row, "keys") else {}
        url = job["application_url"]
        handler = dispatch(url)
        if handler is None:
            log.info(
                "prepare-apply: user_id=%s no handler for %s — marking manual_only",
                user_id, url[:80],
            )
            upsert_user_job(
                conn, user_id, job["url"],
                apply_status="manual_only",
                last_attempted_at=_now_iso(),
            )
            continue

        # Mark "preparing" up front so a UI poll mid-flight sees progress.
        upsert_user_job(conn, user_id, job["url"],
                        apply_status="preparing", last_attempted_at=_now_iso())

        log.info("prepare-apply: user_id=%s %s handler matched %s",
                 user_id, handler.__name__.rsplit('.', 1)[-1], url[:80])

        result = _run_handler(
            handler=handler,
            job=job,
            profile=profile,
            resume_text=job.get("tailored") or resume_text,
            cover_text=job.get("cover") or "",
            user_id=user_id,
            submit=False,
        )
        log.info("prepare-apply: user_id=%s status=%s screenshot=%s",
                 user_id, result["status"], result.get("screenshot_url"))
        _persist(conn, user_id, job["url"], result)


def submit_prepared_for_user(conn, user_id: int) -> None:
    """Drain rows the backend marked as 'submitting' for this user."""
    rows = conn.execute(
        """
        SELECT j.url AS url,
               j.title AS title,
               j.application_url AS application_url,
               j.site AS site,
               uj.tailored_resume_text AS tailored,
               uj.cover_letter_text    AS cover
        FROM jobs j
        JOIN user_jobs uj ON uj.job_url = j.url AND uj.user_id = ?
        WHERE uj.apply_status = 'submitting'
          AND j.closed_at IS NULL
          AND j.application_url IS NOT NULL
        ORDER BY uj.last_attempted_at ASC NULLS FIRST
        """,
        (user_id,),
    ).fetchall()

    if not rows:
        return

    profile = load_profile(user_id)
    if not profile:
        log.warning("submit: user_id=%s — empty profile, refusing to submit", user_id)
        return
    resume_text = get_resume_text(user_id) or ""

    log.info("submit: user_id=%s — %d to submit", user_id, len(rows))
    for row in rows:
        job = dict(row) if hasattr(row, "keys") else {}
        url = job["application_url"]
        handler = dispatch(url)
        if handler is None:
            log.warning(
                "submit: user_id=%s no handler for %s — flipping back to manual_only",
                user_id, url[:80],
            )
            upsert_user_job(
                conn, user_id, job["url"],
                apply_status="manual_only", last_attempted_at=_now_iso(),
                apply_error="handler disappeared between prepare and submit",
            )
            continue

        result = _run_handler(
            handler=handler,
            job=job,
            profile=profile,
            resume_text=job.get("tailored") or resume_text,
            cover_text=job.get("cover") or "",
            user_id=user_id,
            submit=True,
        )
        log.info("submit: user_id=%s status=%s url=%s",
                 user_id, result["status"], url[:80])
        _persist(conn, user_id, job["url"], result)
