"""Detail page enrichment: inlined from applypilot.enrichment.detail.

No dependency on the main applypilot package — uses local db.py and llm.py.
Three-tier extraction cascade:
  Tier 1: JSON-LD JobPosting structured data
  Tier 2: Deterministic CSS pattern matching
  Tier 3: LLM-assisted extraction
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from db import get_connection, init_db

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}

_PROXY_CONFIG: dict | None = None


def set_proxy(proxy_str: str | None):
    global _PROXY_CONFIG
    if proxy_str:
        from discovery import parse_proxy
        _PROXY_CONFIG = parse_proxy(proxy_str)


# -- LLM client (inlined) ----------------------------------------------------

def _get_llm_client():
    """Return an LLM client if one is configured, else None."""
    try:
        from llm import get_client
        return get_client()
    except Exception:
        return None


# -- JSON extraction helper --------------------------------------------------

def _extract_json(text: str) -> dict:
    if "<think>" in text:
        after = text.split("</think>")[-1].strip()
        if after:
            text = after
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    text = text.strip()
    text = re.sub(r'\\([^"\\\/bfnrtu])', r'\1', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {}


# -- URL resolution ----------------------------------------------------------

# Base URLs for sites that use relative URLs
_BASE_URLS: dict[str, str | None] = {}


def resolve_url(raw_url: str, site: str) -> str | None:
    if not raw_url:
        return None
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url
    if site == "WelcomeToTheJungle":
        return None
    if site == "Randstad Canada" and "/" not in raw_url:
        return f"https://www.randstad.ca/jobs/search/{raw_url}"
    if site == "4DayWeek" and raw_url in ("/", "/jobs"):
        return None
    base = _BASE_URLS.get(site)
    if not base:
        return None
    if ";jsessionid=" in raw_url:
        raw_url = raw_url.split(";jsessionid=")[0]
    return urljoin(base, raw_url)


def resolve_all_urls(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT url, site FROM jobs").fetchall()
    resolved = 0
    failed = 0
    already_absolute = 0

    for row in rows:
        url, site = row[0], row[1]
        if url.startswith("http://") or url.startswith("https://"):
            already_absolute += 1
            continue
        new_url = resolve_url(url, site)
        if new_url and new_url != url:
            try:
                conn.execute("UPDATE jobs SET url = ? WHERE url = ?", (new_url, url))
                resolved += 1
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
                resolved += 1
        else:
            failed += 1

    app_resolved = 0
    rows = conn.execute(
        "SELECT url, site, application_url FROM jobs "
        "WHERE application_url IS NOT NULL AND application_url != '' "
        "AND application_url NOT LIKE 'http%'"
    ).fetchall()
    for row in rows:
        url, site, app_url = row[0], row[1], row[2]
        new_app = resolve_url(app_url, site)
        if new_app and new_app != app_url:
            conn.execute("UPDATE jobs SET application_url = ? WHERE url = ?", (new_app, url))
            app_resolved += 1

    conn.commit()
    return {"resolved": resolved, "failed": failed, "already_absolute": already_absolute,
            "app_resolved": app_resolved}


# -- Description cleaning ---------------------------------------------------

def clean_description(text: str) -> str:
    if not text:
        return ""
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        text = soup.get_text()
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# -- Detail page intelligence ------------------------------------------------

def collect_detail_intelligence(page) -> dict:
    intel: dict = {"json_ld": [], "page_title": "", "final_url": ""}
    intel["page_title"] = page.title()
    intel["final_url"] = page.url
    for el in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(el.inner_text())
            intel["json_ld"].append(data)
        except Exception:
            pass
    return intel


# -- Tier 1: JSON-LD extraction -----------------------------------------------

def extract_from_json_ld(intel: dict) -> dict | None:
    def find_job_posting(data):
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                return data
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    result = find_job_posting(item)
                    if result:
                        return result
        elif isinstance(data, list):
            for item in data:
                result = find_job_posting(item)
                if result:
                    return result
        return None

    for ld in intel.get("json_ld", []):
        posting = find_job_posting(ld)
        if not posting:
            continue
        desc = posting.get("description", "")
        if not desc:
            continue
        desc_clean = clean_description(desc)
        if len(desc_clean) < 50:
            continue
        apply_url = None
        if posting.get("directApply"):
            apply_url = posting.get("url")
        if not apply_url:
            contact = posting.get("applicationContact")
            if isinstance(contact, dict):
                apply_url = contact.get("url")
        if not apply_url:
            apply_url = posting.get("url")
        return {"full_description": desc_clean, "application_url": apply_url}
    return None


# -- Tier 2: Deterministic CSS -----------------------------------------------

APPLY_SELECTORS = [
    'a[href*="apply"]', 'a[data-testid*="apply"]', 'a[class*="apply"]',
    'a[aria-label*="pply"]', 'button[data-testid*="apply"]', 'a#apply_button',
    '.postings-btn-wrapper a', 'a.ashby-job-posting-apply-button',
    '#grnhse_app a[href*="apply"]', 'a[data-qa="btn-apply"]',
    'a[class*="btn-apply"]', 'a[class*="apply-btn"]', 'a[class*="apply-button"]',
]

DESCRIPTION_SELECTORS = [
    '#job-description', '#job_description', '#jobDescriptionText',
    '.job-description', '.job_description', '[class*="job-description"]',
    '[class*="jobDescription"]', '[data-testid*="description"]',
    '[data-testid="job-description"]', '.posting-page .posting-categories + div',
    '#content .posting-page', '#app_body .content', '#grnhse_app .content',
    '.ashby-job-posting-description', '[class*="posting-description"]',
    '[class*="job-detail"]', '[class*="jobDetail"]', '[class*="job-content"]',
    '[class*="job-body"]', '[role="main"] article', 'main article',
    'article[class*="job"]', '.job-posting-content',
]


def extract_apply_url_deterministic(page) -> str | None:
    for sel in APPLY_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                href = el.get_attribute("href")
                if href and href != "#":
                    return href
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "button":
                    parent_href = el.evaluate("el => el.parentElement?.querySelector('a')?.href || null")
                    if parent_href:
                        return parent_href
                    return page.url
        except Exception:
            continue
    try:
        links = page.query_selector_all("a")
        for link in links:
            text = link.inner_text().strip().lower()
            if "apply" in text and len(text) < 50:
                href = link.get_attribute("href")
                if href and href != "#" and "javascript:" not in href:
                    return href
    except Exception:
        pass
    return None


def extract_description_deterministic(page) -> str | None:
    for sel in DESCRIPTION_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if len(text) >= 100:
                    return clean_description(text)
        except Exception:
            continue
    return None


# -- Tier 3: LLM extraction -------------------------------------------------

DETAIL_EXTRACT_PROMPT = """You are extracting job details from a single job posting page.

PAGE URL: {url}
PAGE TITLE: {title}

Find TWO things in the HTML below:
1. The full job description text (responsibilities, requirements, etc.)
2. The URL of the "Apply" button/link

Rules:
- For description: extract the FULL text. Include all sections (About, Responsibilities, Requirements, etc.)
- For apply URL: find the href of the link/button that starts the application process
- If you cannot find one, set it to null

Return ONLY valid JSON:
{{"full_description": "the complete job description text here", "application_url": "https://..." or null}}

No explanation, no markdown.

HTML:
{content}"""


def clean_content_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script, style, noscript, svg, iframe, nav, header, footer"):
        tag.decompose()
    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in ("id", "href", "class", "role", "aria-label", "data-testid", "name", "for", "type"):
                if attr == "class":
                    classes = val if isinstance(val, list) else val.split()
                    kept = [c for c in classes if len(c) < 30 and not re.match(r"^[a-z]{1,2}-\d+$", c)]
                    if kept:
                        new_attrs["class"] = " ".join(kept[:3])
                else:
                    new_attrs[attr] = val
            elif attr.startswith("data-") or attr.startswith("aria-"):
                new_attrs[attr] = val
        tag.attrs = new_attrs
    return str(soup)


def extract_main_content(page) -> str:
    for sel in ["main", "article", '[role="main"]', "#content", ".content"]:
        try:
            el = page.query_selector(sel)
            if el:
                text_len = len(el.inner_text().strip())
                if text_len > 200:
                    html = el.inner_html()
                    if len(html) < 50000:
                        return clean_content_html(html)
        except Exception:
            continue
    try:
        html = page.evaluate("""
            () => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('nav, header, footer, script, style, noscript, svg, iframe').forEach(el => el.remove());
                return clone.innerHTML;
            }
        """)
        return clean_content_html(html[:50000])
    except Exception:
        return ""


def extract_with_llm(page, url: str) -> dict:
    client = _get_llm_client()
    if not client:
        return {"full_description": None, "application_url": None}

    content = extract_main_content(page)
    if not content:
        return {"full_description": None, "application_url": None}

    title = ""
    try:
        title = page.title()
    except Exception:
        pass

    prompt = DETAIL_EXTRACT_PROMPT.format(url=url, title=title, content=content[:30000])

    try:
        t0 = time.time()
        raw = client.ask(prompt, temperature=0.0, max_tokens=4096)
        log.info("LLM: %d chars in, %.1fs", len(prompt), time.time() - t0)
        result = _extract_json(raw)
        desc = result.get("full_description")
        apply_url = result.get("application_url")
        if desc:
            desc = clean_description(desc)
        return {"full_description": desc, "application_url": apply_url}
    except Exception as e:
        log.error("LLM ERROR: %s", e)
        return {"full_description": None, "application_url": None}


# -- Orchestration -----------------------------------------------------------

SITE_DELAYS = {
    "RemoteOK": 3.0,
    "WelcomeToTheJungle": 2.0,
    "Job Bank Canada": 1.5,
    "CareerJet Canada": 3.0,
    "Hacker News Jobs": 1.0,
    "BuiltIn Remote": 2.0,
}

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PERMANENT_FAILURES = {404, 410, 451}


def scrape_detail_page(page, url: str) -> dict:
    result: dict = {
        "full_description": None, "application_url": None,
        "status": "error", "tier_used": None, "error": None,
    }
    t0 = time.time()

    try:
        resp = page.goto(url, timeout=45000)
        if resp and resp.status in PERMANENT_FAILURES:
            result["error"] = f"HTTP {resp.status}"
            result["elapsed"] = time.time() - t0
            return result
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    except Exception as e:
        err_str = str(e)
        result["error"] = "timeout" if "timeout" in err_str.lower() else err_str[:200]
        result["elapsed"] = time.time() - t0
        return result

    intel = collect_detail_intelligence(page)

    json_ld_result = extract_from_json_ld(intel)
    if json_ld_result and json_ld_result.get("full_description"):
        result.update(json_ld_result)
        result["tier_used"] = 1
        if not result.get("application_url"):
            apply = extract_apply_url_deterministic(page)
            if apply:
                result["application_url"] = apply
        result["status"] = "ok" if result.get("application_url") else "partial"
        result["elapsed"] = time.time() - t0
        return result

    desc = extract_description_deterministic(page)
    apply = extract_apply_url_deterministic(page)

    if desc:
        result["full_description"] = desc
        result["application_url"] = apply
        result["tier_used"] = 2
        result["status"] = "ok" if apply else "partial"
        result["elapsed"] = time.time() - t0
        return result

    tier2_apply = apply

    llm_result = extract_with_llm(page, url)
    result["full_description"] = llm_result.get("full_description")
    result["application_url"] = llm_result.get("application_url") or tier2_apply
    result["tier_used"] = 3

    if result.get("full_description"):
        result["status"] = "ok" if result.get("application_url") else "partial"
    elif result.get("application_url"):
        result["status"] = "partial"
    else:
        result["status"] = "error"
        result["error"] = "no data extracted"

    result["elapsed"] = time.time() - t0
    return result


def scrape_site_batch(
    conn: sqlite3.Connection | None,
    site: str,
    jobs: list[tuple],
    delay: float = 2.0,
    max_jobs: int | None = None,
) -> dict:
    stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    if max_jobs:
        jobs = jobs[:max_jobs]
    if not jobs:
        return stats

    own_conn = conn is None
    if own_conn:
        init_db()
        conn = get_connection()

    now = datetime.now(timezone.utc).isoformat()

    try:
        with sync_playwright() as p:
            launch_opts: dict = {"headless": True}
            if _PROXY_CONFIG:
                launch_opts["proxy"] = _PROXY_CONFIG["playwright"]
            browser = p.chromium.launch(**launch_opts)
            context = browser.new_context(user_agent=UA)
            page = context.new_page()

            for i, (url, title) in enumerate(jobs):
                log.info("[%d/%d] %s", i + 1, len(jobs), title[:50] if title else url[:50])

                result = scrape_detail_page(page, url)
                stats["processed"] += 1

                tier = result.get("tier_used")
                status = result["status"]

                if tier:
                    stats["tiers"][tier] = stats["tiers"].get(tier, 0) + 1

                if status in ("ok", "partial"):
                    stats[status] += 1
                    conn.execute(
                        "UPDATE jobs SET full_description = ?, application_url = ?, "
                        "detail_scraped_at = ?, detail_error = NULL WHERE url = ?",
                        (result.get("full_description"), result.get("application_url"), now, url),
                    )
                else:
                    stats["error"] += 1
                    conn.execute(
                        "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
                        (result.get("error", "unknown"), now, url),
                    )

                conn.commit()

                if i < len(jobs) - 1:
                    time.sleep(delay)

            browser.close()
    finally:
        if own_conn:
            conn.close()

    return stats


def _run_detail_scraper(
    conn: sqlite3.Connection,
    sites: list[str] | None = None,
    max_per_site: int | None = None,
    workers: int = 1,
) -> dict:
    skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
    where = f"WHERE detail_scraped_at IS NULL AND {skip_filter}"
    rows = conn.execute(
        f"SELECT url, title, site FROM jobs {where} ORDER BY site"
    ).fetchall()

    if not rows:
        log.info("No pending jobs to scrape.")
        return {"processed": 0, "ok": 0, "partial": 0, "error": 0}

    site_jobs: dict[str, list[tuple]] = {}
    for row in rows:
        url, title, site = row[0], row[1], row[2]
        if sites and site not in sites:
            continue
        site_jobs.setdefault(site, []).append((url, title))

    log.info("Pending: %d jobs across %d sites (workers=%d)", len(rows), len(site_jobs), workers)

    known_order = ["RemoteOK", "Job Bank Canada", "BuiltIn Remote", "WelcomeToTheJungle"]
    order = [s for s in known_order if s in site_jobs]
    order += [s for s in sorted(site_jobs.keys()) if s not in order]

    total_stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    def _merge(s: dict) -> None:
        for k in ("processed", "ok", "partial", "error"):
            total_stats[k] += s[k]
        for t, count in s["tiers"].items():
            total_stats["tiers"][t] = total_stats["tiers"].get(t, 0) + count

    if workers > 1 and len(order) > 1:
        def _scrape_site(site: str) -> dict:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            return scrape_site_batch(None, site, jobs, delay=delay, max_jobs=max_per_site)
        with ThreadPoolExecutor(max_workers=min(workers, len(order))) as pool:
            futures = {pool.submit(_scrape_site, site): site for site in order}
            for future in as_completed(futures):
                _merge(future.result())
    else:
        for site in order:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)
            stats = scrape_site_batch(conn, site, jobs, delay=delay, max_jobs=max_per_site)
            _merge(stats)

    log.info("TOTAL: %d processed | %d ok | %d partial | %d error",
             total_stats["processed"], total_stats["ok"], total_stats["partial"], total_stats["error"])

    return total_stats


# -- Public entry point ------------------------------------------------------

def run_enrichment(limit: int = 100, workers: int = 1) -> dict:
    """Enrich pending jobs: resolve URLs then run three-tier extraction cascade."""
    init_db()
    conn = get_connection()

    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute, %d failed",
             url_stats["resolved"], url_stats["already_absolute"], url_stats["failed"])

    stats = _run_detail_scraper(conn, max_per_site=limit, workers=workers)
    return stats
