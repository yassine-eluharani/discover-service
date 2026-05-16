"""JobSpy-based discovery: inlined from applypilot.discovery.jobspy.

No dependency on the main applypilot package — uses local db.py for storage.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


# -- Proxy parsing -----------------------------------------------------------

def parse_proxy(proxy_str: str) -> dict:
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return {
            "host": host, "port": port, "user": user, "pass": passwd,
            "jobspy": f"{user}:{passwd}@{host}:{port}",
            "playwright": {"server": f"http://{host}:{port}", "username": user, "password": passwd},
        }
    elif len(parts) == 2:
        host, port = parts
        return {
            "host": host, "port": port, "user": None, "pass": None,
            "jobspy": f"{host}:{port}",
            "playwright": {"server": f"http://{host}:{port}"},
        }
    else:
        raise ValueError(f"Proxy format not recognized: {proxy_str}. Expected: host:port:user:pass or host:port")


# -- Retry wrapper -----------------------------------------------------------

def _scrape_with_retry(kwargs: dict, max_retries: int = 2, backoff: float = 5.0):
    from jobspy import scrape_jobs
    for attempt in range(max_retries + 1):
        try:
            return scrape_jobs(**kwargs)
        except Exception as e:
            err = str(e).lower()
            transient = any(k in err for k in ("timeout", "429", "proxy", "connection", "reset", "refused"))
            if transient and attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Retry %d/%d in %.0fs: %s", attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                raise


# -- Location filtering ------------------------------------------------------

# Map a search location string to JobSpy's `country_indeed` value so Indeed
# queries hit the right country site (sa.indeed.com, ae.indeed.com, etc).
# JobSpy retries with country_indeed="worldwide" if the value is rejected,
# so unknown locations still produce results — just slightly slower.
_COUNTRY_INDEED_MAP = {
    "saudi arabia": "saudi arabia",
    "ksa": "saudi arabia",
    "united arab emirates": "united arab emirates",
    "uae": "united arab emirates",
    "qatar": "qatar",
    "bahrain": "bahrain",
    "kuwait": "kuwait",
    "oman": "oman",
    "egypt": "egypt",
    "morocco": "morocco",
    "remote": "worldwide",
    "worldwide": "worldwide",
}


def _country_indeed_for(location: str, defaults: dict) -> str:
    """Pick the JobSpy `country_indeed` value for a given location.

    Per-location override beats the global default beats "usa". This is
    what lets one search config target the Gulf without leaking US-remote
    listings into every result.
    """
    loc_lower = (location or "").lower().strip()
    # Try exact match first, then check if any mapping key is a substring
    if loc_lower in _COUNTRY_INDEED_MAP:
        return _COUNTRY_INDEED_MAP[loc_lower]
    for key, val in _COUNTRY_INDEED_MAP.items():
        if key in loc_lower:
            return val
    return defaults.get("country_indeed", "usa")


def _load_location_config(search_cfg: dict) -> tuple[list[str], list[str]]:
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Decide whether to keep a JobSpy result based on its location string.

    The SEARCH already constrains location (we called JobSpy with
    location="Saudi Arabia" / "UAE" / etc), so JobSpy's results are
    already roughly on-target. This filter is a belt-and-suspenders
    safety net — it should only reject when there's clear evidence
    of a wrong-country match.

    Rules:
      - Missing location → accept (let scoring decide).
      - Remote/anywhere keyword → accept.
      - Matches an explicit reject pattern ("us only", "uk only", ...) → reject.
      - Default → ACCEPT. The previous default-reject silently dropped
        most on-site Gulf jobs because LinkedIn location formats
        ("Dubai, Dubayy, United Arab Emirates") don't always substring-match
        the accept patterns ("dubai", "uae"). The LLM scorer is the
        actual relevance gate.
    """
    if not location:
        return True
    loc = location.lower()
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True
    for r in reject:
        if r.lower() in loc:
            return False
    return True


def _title_ok(title: str | None, include_any: list[str], exclude_any: list[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    for bad in exclude_any:
        if bad and bad.lower() in t:
            return False
    if not include_any:
        return True
    return any(k and k.lower() in t for k in include_any)


# -- DB storage (JobSpy DataFrame -> SQLite) ---------------------------------

def store_jobspy_results(conn: sqlite3.Connection, df, source_label: str) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for _, row in df.iterrows():
        url = str(row.get("job_url", ""))
        if not url or url == "nan":
            continue

        title = str(row.get("title", "")) if str(row.get("title", "")) != "nan" else None
        company = str(row.get("company", "")) if str(row.get("company", "")) != "nan" else None
        location_str = str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None

        salary = None
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        interval = str(row.get("interval", "")) if str(row.get("interval", "")) != "nan" else ""
        currency = str(row.get("currency", "")) if str(row.get("currency", "")) != "nan" else ""
        if min_amt and str(min_amt) != "nan":
            if max_amt and str(max_amt) != "nan":
                salary = f"{currency}{int(float(min_amt)):,}-{currency}{int(float(max_amt)):,}"
            else:
                salary = f"{currency}{int(float(min_amt)):,}"
            if interval:
                salary += f"/{interval}"

        description = str(row.get("description", "")) if str(row.get("description", "")) != "nan" else None
        site_name = str(row.get("site", source_label))
        is_remote = row.get("is_remote", False)

        if is_remote:
            location_str = f"{location_str} (Remote)" if location_str else "Remote"

        strategy = "jobspy"

        full_description = None
        detail_scraped_at = None
        if description and len(description) > 200:
            full_description = description
            detail_scraped_at = now

        apply_url = str(row.get("job_url_direct", "")) if str(row.get("job_url_direct", "")) != "nan" else None

        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs (url, title, company, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, title, company, salary, description, location_str, site_name, strategy, now,
                 full_description, apply_url, detail_scraped_at),
            )
            # rowcount=1 means inserted; 0 means skipped (duplicate URL)
            if getattr(cur, "rowcount", 1) == 0:
                existing += 1
            else:
                new += 1
        except Exception:
            existing += 1

    conn.commit()
    return new, existing


# -- Single search execution -------------------------------------------------

def _run_one_search(
    search: dict,
    sites: list[str],
    results_per_site: int,
    hours_old: int,
    proxy_config: dict | None,
    defaults: dict,
    max_retries: int,
    accept_locs: list[str],
    reject_locs: list[str],
    include_titles: list[str],
    exclude_titles: list[str],
    glassdoor_map: dict,
) -> dict:
    from db import get_connection
    s = search
    label = f"\"{s['query']}\" in {s['location']} {'(remote)' if s.get('remote') else ''}"

    gd_location = glassdoor_map.get(s["location"], s["location"].split(",")[0])
    has_glassdoor = "glassdoor" in sites
    other_sites = [si for si in sites if si != "glassdoor"]

    all_dfs = []

    if other_sites:
        kwargs = {
            "site_name": other_sites,
            "search_term": s["query"],
            "location": s["location"],
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "country_indeed": _country_indeed_for(s["location"], defaults),
            "verbose": 0,
        }
        if s.get("remote"):
            kwargs["is_remote"] = True
        if proxy_config:
            kwargs["proxies"] = [proxy_config["jobspy"]]
        if "linkedin" in other_sites:
            kwargs["linkedin_fetch_description"] = True
        try:
            df = _scrape_with_retry(kwargs, max_retries=max_retries)
            all_dfs.append(df)
        except Exception as e:
            msg = str(e).lower()
            if "invalid country string" in msg and kwargs.get("country_indeed") != "worldwide":
                try:
                    kwargs["country_indeed"] = "worldwide"
                    df = _scrape_with_retry(kwargs, max_retries=max_retries)
                    all_dfs.append(df)
                except Exception as retry_err:
                    log.error("[%s] (non-gd retry worldwide): %s", label, retry_err)
            else:
                log.error("[%s] (non-gd): %s", label, e)

    if has_glassdoor:
        gd_kwargs = {
            "site_name": ["glassdoor"],
            "search_term": s["query"],
            "location": gd_location,
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "verbose": 0,
        }
        if s.get("remote"):
            gd_kwargs["is_remote"] = True
        if proxy_config:
            gd_kwargs["proxies"] = [proxy_config["jobspy"]]
        try:
            gd_df = _scrape_with_retry(gd_kwargs, max_retries=max_retries)
            all_dfs.append(gd_df)
        except Exception as e:
            log.error("[%s] (glassdoor): %s", label, e)

    if not all_dfs:
        log.warning("[%s]: all sites failed", label)
        return {"new": 0, "existing": 0, "errors": 1, "filtered": 0, "total": 0, "label": label}

    import pandas as pd
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(all_dfs, ignore_index=True) if len(all_dfs) > 1 else all_dfs[0]

    if len(df) == 0:
        log.info("[%s] 0 results", label)
        return {"new": 0, "existing": 0, "errors": 0, "filtered": 0, "total": 0, "label": label}

    before = len(df)
    df = df[df.apply(lambda row: _title_ok(
        str(row.get("title", "")) if str(row.get("title", "")) != "nan" else None,
        include_titles, exclude_titles,
    ), axis=1)]
    title_filtered = before - len(df)
    before_loc = len(df)
    df = df[df.apply(lambda row: _location_ok(
        str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None,
        accept_locs, reject_locs,
    ), axis=1)]
    loc_filtered = before_loc - len(df)
    filtered = title_filtered + loc_filtered

    conn = get_connection()
    new, existing = store_jobspy_results(conn, df, s["query"])

    msg = f"[{label}] {before} results -> {new} new, {existing} dupes"
    if filtered:
        msg += f", {filtered} filtered"
    log.info(msg)

    return {"new": new, "existing": existing, "errors": 0, "filtered": filtered, "total": before, "label": label}
