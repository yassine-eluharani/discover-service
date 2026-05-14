"""Two-phase scoring: rule-based pre-filter + heuristic ranking + top-N LLM scoring.

Phase 3 of the multi-user plan. Replaces the O(jobs × users) LLM-everything
approach with:

  1. Rule-based pre-filter (zero LLM calls)
     Compares job_metadata_json against user profile:
       - Visa sponsorship mismatch → REJECT
       - Location/remote policy mismatch → REJECT
       - Experience gap (>5 years over max) → REJECT
       - Seniority mismatch → REJECT

  2. Heuristic score (zero LLM calls)
     Jaccard similarity of required_skills vs user skills + experience fit.

  3. Top-N LLM deep scoring (N=100 by default)
     Full LLM score only for the top-N heuristic candidates.
     Jobs below top-N get the heuristic score stored directly.
"""

from __future__ import annotations

import json
import logging

from user_data import load_profile
from db import get_connection, upsert_user_job
from scoring.scorer import run_scoring

log = logging.getLogger(__name__)

TOP_N_LLM = 100   # jobs that get full LLM scoring per user


# ---------------------------------------------------------------------------
# Rule-based pre-filter
# ---------------------------------------------------------------------------

def _passes_rules(meta: dict, profile: dict) -> tuple[bool, str]:
    """Check a job's metadata against the user's profile.

    Returns (passes: bool, reason: str).
    """
    wa = profile.get("work_authorization", {})
    exp = profile.get("experience", {})
    personal = profile.get("personal", {})

    needs_sponsorship = wa.get("require_sponsorship", False)
    user_country = personal.get("country", "").lower()
    # `target_countries` lets a user signal countries they're willing to
    # relocate to (or already work remotely for). Pre-filter accepts a job
    # if its location_country matches user_country OR any target country.
    # No-op when missing (back-compat with profiles set via /setup that
    # only have a single `country` field).
    target_countries = [c.lower() for c in personal.get("target_countries", []) if isinstance(c, str)]
    acceptable_countries = ([user_country] if user_country else []) + target_countries
    # Support both `years_of_experience` (legacy) and the field name the
    # /setup wizard actually writes today (`years_of_experience_total`).
    user_years = (
        exp.get("years_of_experience_total")
        or exp.get("years_of_experience")
        or 0
    )
    target_seniority = [s.lower() for s in exp.get("target_seniority", [])]

    def _country_matches(job_country: str) -> bool:
        if not job_country or not acceptable_countries:
            return True  # no info on either side → don't reject
        return any(job_country in c or c in job_country for c in acceptable_countries)

    # Visa check
    visa_offered = meta.get("visa_sponsorship")
    if needs_sponsorship and visa_offered is False:
        return False, "visa_not_offered"

    # Remote/location policy
    remote_policy = meta.get("remote_policy", "")
    location_country = (meta.get("location_country") or "").lower()

    if remote_policy == "worldwide":
        pass  # always accessible
    elif remote_policy == "us_only":
        # Only accept us_only jobs if the US is one of the user's
        # acceptable countries (rare for this app; keep the check simple).
        if acceptable_countries and not any(
            c in ("us", "usa", "united states") for c in acceptable_countries
        ):
            return False, "us_only_remote"
    elif remote_policy == "country_specific":
        if not _country_matches(location_country):
            return False, "country_specific_remote"
    elif remote_policy == "onsite":
        if not _country_matches(location_country):
            return False, "onsite_wrong_country"

    # Experience gap. Skip the check entirely when the user hasn't filled
    # their profile yet (user_years == 0) — otherwise every job with
    # experience_years_min >= 3 gets auto-rejected before the user has had
    # a chance to upload a CV, which makes the new-user experience look
    # broken (the /apply page shows nothing because nothing passed the
    # filter).
    exp_min = meta.get("experience_years_min")
    exp_max = meta.get("experience_years_max")
    if user_years > 0:
        if exp_min is not None and isinstance(exp_min, (int, float)):
            if user_years < exp_min - 2:  # allow 2-year grace
                return False, "experience_gap"
        if exp_max is not None and isinstance(exp_max, (int, float)):
            if user_years > exp_max + 5:  # overqualified by >5y
                return False, "overqualified"

    # Seniority
    job_seniority = (meta.get("seniority") or "").lower()
    if job_seniority and job_seniority != "unknown" and target_seniority:
        if job_seniority not in target_seniority:
            # Don't hard-reject on seniority alone — just note it
            pass

    return True, "ok"


# ---------------------------------------------------------------------------
# Heuristic score
# ---------------------------------------------------------------------------

def _heuristic_score(meta: dict, profile: dict) -> float:
    """Compute a fast 0–100 heuristic score without any LLM calls."""
    score = 0.0

    boundary = profile.get("skills_boundary", {})
    user_skills: set[str] = set()
    for items in boundary.values():
        if isinstance(items, list):
            user_skills.update(s.lower() for s in items)

    required_skills = [s.lower() for s in (meta.get("required_skills") or [])]
    if required_skills and user_skills:
        matched = sum(1 for s in required_skills if s in user_skills)
        jaccard = matched / len(set(required_skills) | user_skills)
        score += 60 * jaccard  # max 60 points from skills

    exp = profile.get("experience", {})
    # Support both `years_of_experience` (legacy) and the field name the
    # /setup wizard actually writes today (`years_of_experience_total`).
    user_years = (
        exp.get("years_of_experience_total")
        or exp.get("years_of_experience")
        or 0
    )
    exp_min = meta.get("experience_years_min")
    exp_max = meta.get("experience_years_max")

    if exp_min is not None and exp_max is not None:
        if exp_min <= user_years <= exp_max:
            score += 20  # perfect fit
        elif user_years >= exp_min:
            score += 10  # above minimum
    elif exp_min is not None:
        if user_years >= exp_min:
            score += 15

    # Visa/location bonus
    remote_policy = meta.get("remote_policy", "")
    wa = profile.get("work_authorization", {})
    needs_sponsorship = wa.get("require_sponsorship", False)

    if remote_policy == "worldwide":
        score += 20  # always accessible — big bonus
    elif not needs_sponsorship:
        score += 10

    return min(100.0, score)


# ---------------------------------------------------------------------------
# Two-phase scoring entry point
# ---------------------------------------------------------------------------

def run_two_phase_scoring(
    user_id: int,
    top_n: int = TOP_N_LLM,
    limit: int = 0,
) -> dict:
    """Run rule-based filter + heuristic + top-N LLM scoring for a user.

    Args:
        user_id: DB user ID.
        top_n:   Number of top-heuristic jobs to pass to LLM deep scoring.
        limit:   Max jobs to consider (0 = all enriched + filtered).

    Returns:
        {"pre_filtered": int, "heuristic_only": int, "llm_scored": int, "errors": int}
    """
    profile = load_profile(user_id)
    conn = get_connection()

    # Fetch all enriched jobs with metadata that this user hasn't scored yet
    query = (
        "SELECT j.url, j.title, j.location, j.full_description, j.job_metadata_json "
        "FROM jobs j "
        "WHERE j.full_description IS NOT NULL "
        "AND j.job_metadata_json IS NOT NULL "
        "AND j.filtered_at IS NOT NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM user_jobs uj "
        "  WHERE uj.job_url = j.url AND uj.user_id = ? AND uj.fit_score IS NOT NULL"
        ")"
    )
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = conn.execute(query, (user_id,)).fetchall()
    if not rows:
        log.info("No unscored jobs with metadata for user %s.", user_id)
        return {"pre_filtered": 0, "heuristic_only": 0, "llm_scored": 0, "errors": 0}

    jobs = [dict(r) for r in rows]
    # %s for user_id — Turso returns it as a string and %d would crash
    # the logger (PEP 282 swallows the exception, but it spams stderr).
    log.info("Two-phase scoring: %d candidate jobs for user %s", len(jobs), user_id)

    # Phase 1: Rule-based pre-filter
    passed: list[dict] = []
    pre_filtered = 0
    for job in jobs:
        try:
            meta = json.loads(job["job_metadata_json"]) if job.get("job_metadata_json") else {}
        except Exception:
            meta = {}
        ok, reason = _passes_rules(meta, profile)
        if ok:
            job["_meta"] = meta
            passed.append(job)
        else:
            pre_filtered += 1
            upsert_user_job(
                conn, user_id, job["url"],
                apply_status="location_filtered",
                score_reasoning=f"pre-filter: {reason}",
            )

    log.info("Pre-filter: %d rejected, %d passed", pre_filtered, len(passed))

    # Phase 2: Heuristic scoring + rank
    for job in passed:
        job["_heuristic"] = _heuristic_score(job.get("_meta", {}), profile)

    passed.sort(key=lambda j: j["_heuristic"], reverse=True)

    top = passed[:top_n]
    bottom = passed[top_n:]

    # Store heuristic scores for jobs below top-N
    for job in bottom:
        h_score = int(job["_heuristic"] / 10)  # normalize to 1-10 scale
        h_score = max(1, min(10, h_score))
        upsert_user_job(
            conn, user_id, job["url"],
            fit_score=h_score,
            score_reasoning="heuristic",
        )
    log.info("Heuristic-only: %d jobs scored without LLM", len(bottom))

    # Phase 3: LLM deep scoring for top-N
    llm_scored = 0
    errors = 0
    if top:
        log.info("LLM deep scoring: %d top jobs", len(top))
        result = run_scoring(user_id=user_id, limit=len(top))
        llm_scored = result.get("scored", 0)
        errors = result.get("errors", 0)

    return {
        "pre_filtered": pre_filtered,
        "heuristic_only": len(bottom),
        "llm_scored": llm_scored,
        "errors": errors,
    }
