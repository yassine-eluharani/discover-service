"""Discovery worker — core cycle logic."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

STALE_AFTER_HOURS: float = float(os.environ.get("STALE_AFTER_HOURS", "2"))
POPULAR_SEARCHES_PATH = Path(__file__).parent / "popular_searches.yaml"


# ── Freshness tracking ────────────────────────────────────────────────────────

def _is_stale(conn, query: str, location: str, boards: list[str]) -> bool:
    boards_json = json.dumps(sorted(boards))
    row = conn.execute(
        "SELECT completed_at FROM discovery_runs "
        "WHERE query = ? AND location = ? AND boards_json = ? AND status = 'done' "
        "ORDER BY completed_at DESC LIMIT 1",
        (query, location, boards_json),
    ).fetchone()
    if not row or not row["completed_at"]:
        return True
    completed = datetime.fromisoformat(row["completed_at"])
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - completed > timedelta(hours=STALE_AFTER_HOURS)


def _record_start(conn, query: str, location: str, boards: list[str]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO discovery_runs (query, location, boards_json, started_at, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (query, location, json.dumps(sorted(boards)), now),
    )
    conn.commit()
    return cur.lastrowid


def _record_done(conn, run_id: int, jobs_found: int, status: str = "done") -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE discovery_runs SET completed_at = ?, status = ?, jobs_found = ? WHERE id = ?",
        (now, status, jobs_found, run_id),
    )
    conn.commit()


# ── Config loading ────────────────────────────────────────────────────────────

def _load_popular() -> dict:
    if not POPULAR_SEARCHES_PATH.exists():
        return {}
    return yaml.safe_load(POPULAR_SEARCHES_PATH.read_text()) or {}


def _load_user_configs(conn) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, searches_json FROM users WHERE searches_json IS NOT NULL"
        ).fetchall()
    except Exception:
        return []
    configs = []
    for row in rows:
        try:
            configs.append({"user_id": row["id"], "config": json.loads(row["searches_json"])})
        except Exception:
            pass
    return configs


def _unique_combos(configs: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    combos: list[dict] = []
    for entry in configs:
        cfg = entry.get("config", entry)
        queries  = cfg.get("queries", [])
        locs     = cfg.get("locations", [])
        boards   = sorted(cfg.get("boards", ["indeed", "linkedin"]))
        defaults = cfg.get("defaults", {})
        exclude  = cfg.get("exclude_titles", [])
        for q in queries:
            query_str = q["query"] if isinstance(q, dict) else str(q)
            for loc in locs:
                loc_str = loc["location"] if isinstance(loc, dict) else str(loc)
                remote  = loc.get("remote", False) if isinstance(loc, dict) else False
                key = (query_str.lower(), loc_str.lower(), tuple(boards))
                if key not in seen:
                    seen.add(key)
                    combos.append({
                        "query":         query_str,
                        "location":      loc_str,
                        "remote":        remote,
                        "boards":        boards,
                        "defaults":      defaults,
                        "config":        cfg,
                        "exclude_titles": exclude,
                    })
    return combos


# ── Discovery ─────────────────────────────────────────────────────────────────

def _discover_combo(conn, combo: dict) -> None:
    query    = combo["query"]
    location = combo["location"]
    boards   = combo["boards"]
    cfg      = combo["config"]
    defaults = combo.get("defaults", {})

    run_id = _record_start(conn, query, location, boards)
    try:
        from discovery import _run_one_search, _load_location_config

        accept_locs, reject_locs = _load_location_config(cfg)
        result = _run_one_search(
            search={
                "query":    query,
                "location": location,
                "remote":   combo.get("remote", False),
                "tier":     0,
            },
            sites=boards,
            results_per_site=defaults.get("results_per_site", 50),
            hours_old=defaults.get("hours_old", 48),
            proxy_config=None,
            defaults=defaults,
            max_retries=2,
            accept_locs=accept_locs,
            reject_locs=reject_locs,
            include_titles=cfg.get("include_title_any", []),
            exclude_titles=combo.get("exclude_titles", []),
            glassdoor_map=cfg.get("glassdoor_location_map", {}),
        )
        new_jobs = result.get("new", 0)
        log.info("'%s' @ %s → %d new", query, location, new_jobs)
        _record_done(conn, run_id, new_jobs, "done")
    except Exception as e:
        log.error("'%s' @ %s failed: %s", query, location, e)
        _record_done(conn, run_id, 0, "error")


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle() -> None:
    from db import get_connection, init_db

    init_db()
    conn = get_connection()

    popular_cfg  = _load_popular()
    user_configs = _load_user_configs(conn)

    all_configs: list[dict] = []
    if popular_cfg:
        all_configs.append({"config": popular_cfg})
    all_configs.extend(user_configs)

    combos = _unique_combos(all_configs)
    stale  = [c for c in combos if _is_stale(conn, c["query"], c["location"], c["boards"])]

    log.info("%d unique combos, %d stale → scraping", len(combos), len(stale))
    for combo in stale:
        _discover_combo(conn, combo)
    log.info("Discovery done — %d combos scraped", len(stale))

    # Enrich any jobs without a full description (new ones + any leftovers)
    pending = int(conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0])

    if pending > 0:
        from enrichment import run_enrichment
        enrich_limit = int(os.environ.get("ENRICH_LIMIT", "200"))
        log.info("Enriching up to %d pending jobs (total pending: %d)...", enrich_limit, pending)
        stats = run_enrichment(limit=enrich_limit, workers=1)
        log.info("Enrich done: %d ok, %d partial, %d error",
                 stats.get("ok", 0), stats.get("partial", 0), stats.get("error", 0))
    else:
        log.info("No pending jobs to enrich")

    # Filter: mark country-restricted jobs before scoring
    unfiltered = int(conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND filtered_at IS NULL"
    ).fetchone()[0])
    if unfiltered > 0:
        from filter import run_location_filter
        # Collect extra reject patterns from all user configs (union, deduped)
        extra_patterns: list[str] = []
        seen_patterns: set[str] = set()
        for entry in user_configs:
            cfg = entry.get("config", {})
            for p in cfg.get("description_reject_patterns", []):
                if isinstance(p, str) and p.strip() and p.strip().lower() not in seen_patterns:
                    extra_patterns.append(p.strip())
                    seen_patterns.add(p.strip().lower())
        log.info("Running location filter on %d unfiltered jobs (%d extra user patterns)...",
                 unfiltered, len(extra_patterns))
        fstats = run_location_filter(conn, extra_patterns=extra_patterns or None)
        log.info("Filter done: checked %d, filtered %d",
                 fstats.get("checked", 0), fstats.get("filtered", 0))
    else:
        log.info("No unfiltered jobs")

    # Index: extract structured metadata once per job
    unindexed = int(conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND job_metadata_json IS NULL"
    ).fetchone()[0])
    if unindexed > 0:
        from indexer import run_indexing
        index_limit = int(os.environ.get("INDEX_LIMIT", "100"))
        log.info("Indexing metadata for up to %d jobs (total unindexed: %d)...", index_limit, unindexed)
        istats = run_indexing(conn, limit=index_limit)
        log.info("Index done: %d indexed, %d errors", istats.get("indexed", 0), istats.get("errors", 0))
    else:
        log.info("No unindexed jobs")

    # Cleanup: remove old unengaged jobs to prevent unbounded DB growth
    from db import cleanup_old_jobs
    cleanup_days = int(os.environ.get("CLEANUP_DAYS", "7"))
    cleanup_old_jobs(days=cleanup_days, conn=conn)

    log.info("Cycle complete")
