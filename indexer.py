"""Job indexer: extract structured metadata from each job once.

Stores job_metadata_json on the jobs table — shared across all users,
computed one time total.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger(__name__)

_INDEX_PROMPT = """You are a job metadata extractor. Given a job posting, extract structured metadata.

Return ONLY a valid JSON object with these exact keys (use null when unknown):
{
  "required_skills": ["skill1", "skill2"],
  "experience_years_min": 3,
  "experience_years_max": 7,
  "visa_sponsorship": false,
  "remote_policy": "us_only",
  "seniority": "senior",
  "location_country": "US",
  "salary_min": 120000,
  "salary_max": 180000
}

remote_policy values: "worldwide" | "us_only" | "country_specific" | "hybrid" | "onsite"
  - worldwide: explicitly "work from anywhere", no country restriction
  - us_only: remote but US residents only
  - country_specific: remote but restricted to a specific country (not US)
  - hybrid: partially remote, requires physical presence
  - onsite: no remote option

seniority values: "junior" | "mid" | "senior" | "lead" | "staff" | "principal" | "unknown"

visa_sponsorship: true = company will sponsor visas, false = no sponsorship, null = not mentioned

No explanation, no markdown fences. Return the JSON object only."""


def _extract_metadata(job: dict) -> dict | None:
    text = f"TITLE: {job.get('title', '')}\n"
    text += f"LOCATION: {job.get('location', '')}\n\n"
    text += f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"

    try:
        from llm import get_client
        client = get_client()
        raw = client.chat(
            [{"role": "system", "content": _INDEX_PROMPT},
             {"role": "user", "content": text}],
            max_tokens=2048,
            temperature=0.0,
            json_mode=True,
        )
        raw = raw.strip()
        if "```" in raw:
            for part in raw.split("```")[1::2]:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            return json.loads(raw[start:end + 1])
    except Exception as e:
        log.error("Indexer LLM error for '%s': %s", job.get("title", "?"), e)
    return None


def run_indexing(conn=None, limit: int = 0) -> dict:
    """Index jobs that have a full description but no metadata yet.

    Returns {"indexed": int, "errors": int, "elapsed": float}
    """
    if conn is None:
        from db import get_connection
        conn = get_connection()

    query = (
        "SELECT url, title, location, full_description FROM jobs "
        "WHERE full_description IS NOT NULL AND job_metadata_json IS NULL"
    )
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = conn.execute(query).fetchall()
    if not rows:
        log.info("No jobs pending indexing.")
        return {"indexed": 0, "errors": 0, "elapsed": 0.0}

    jobs = [dict(r) for r in rows]
    log.info("Indexing metadata for %d jobs...", len(jobs))
    t0 = time.time()
    indexed = errors = 0

    for i, job in enumerate(jobs):
        meta = _extract_metadata(job)
        if meta is not None:
            conn.execute(
                "UPDATE jobs SET job_metadata_json = ? WHERE url = ?",
                (json.dumps(meta), job["url"]),
            )
            conn.commit()
            indexed += 1
            log.info("[%d/%d] indexed  %s", i + 1, len(jobs), job.get("title", "?")[:60])
        else:
            errors += 1
            log.warning("[%d/%d] error    %s", i + 1, len(jobs), job.get("title", "?")[:60])

    elapsed = time.time() - t0
    log.info("Indexing done: %d indexed, %d errors in %.1fs", indexed, errors, elapsed)
    return {"indexed": indexed, "errors": errors, "elapsed": elapsed}
