"""Ashby (jobs.ashbyhq.com) application form handler.

Ashby renders applications as a single-page React form keyed mostly by
human-readable labels, with `data-testid` attributes on some controls.
The form is straightforward — name, email, phone, location, optional
links, resume upload, optional cover letter, EEO. Submit is a plain
button with text "Submit Application".

Test recipe:
  url = "https://jobs.ashbyhq.com/<company>/<job-id>"
  Pass `submit=False` first; verify the screenshot shows every required
  field populated. Then re-run with `submit=True` and watch for a
  confirmation page or success banner.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.sync_api import Page

from apply_handlers._base import (
    fill_text,
    safe_click_submit,
    take_full_page_screenshot,
    upload_file,
    wait_for_form_ready,
)

log = logging.getLogger(__name__)


def matches(url: str) -> bool:
    return bool(url) and "ashbyhq.com" in url.lower()


# Selector candidates — Ashby's React app has been stable enough that
# these labels work across most postings. Put `data-testid` first when
# we know the testid; fall back to label-based queries.
_NAME_SELECTORS = [
    'input[name="_systemfield_name"]',
    'input[id*="name"]',
    'input[aria-label*="Name"]',
    'input[placeholder*="name" i]',
]
_EMAIL_SELECTORS = [
    'input[name="_systemfield_email"]',
    'input[type="email"]',
    'input[aria-label*="Email"]',
]
_PHONE_SELECTORS = [
    'input[name="_systemfield_phone"]',
    'input[type="tel"]',
    'input[aria-label*="Phone"]',
]
_LOCATION_SELECTORS = [
    'input[name*="location" i]',
    'input[aria-label*="Location"]',
    'input[placeholder*="city" i]',
]
_LINKEDIN_SELECTORS = [
    'input[name*="linkedin" i]',
    'input[aria-label*="LinkedIn"]',
    'input[placeholder*="linkedin" i]',
]
_GITHUB_SELECTORS = [
    'input[name*="github" i]',
    'input[aria-label*="GitHub"]',
    'input[placeholder*="github" i]',
]
_RESUME_SELECTORS = [
    'input[type="file"][name*="resume" i]',
    'input[type="file"][aria-label*="Resume" i]',
    'input[type="file"]',  # last-ditch — Ashby usually has only one file input
]
_COVER_SELECTORS = [
    'textarea[name*="cover" i]',
    'textarea[aria-label*="Cover" i]',
    'textarea[placeholder*="cover" i]',
]
_SUBMIT_SELECTORS = [
    'button[type="submit"]:has-text("Submit")',
    'button:has-text("Submit Application")',
    'button:has-text("Submit application")',
    'button[type="submit"]',
]
# Anchor — wait for at least one of these to appear before declaring the
# form hydrated. Email field is the most reliable signal.
_FORM_READY_ANCHORS = _EMAIL_SELECTORS + _NAME_SELECTORS


def fill(
    page: Page,
    job: dict,
    profile: dict,
    resume_text: str,
    cover_text: str,
    submit: bool,
    *,
    screenshot_path: Path,
    resume_pdf_path: str | None = None,
) -> dict:
    """Fill (and optionally submit) an Ashby application form.

    Returns one of:
      {"status": "ready_to_submit", "screenshot_url": "<path>"}
      {"status": "applied", "confirmation_url": "<url>", "screenshot_url": "<path>"}
      {"status": "failed", "error": "<msg>", "screenshot_url": "<path or None>"}
    """
    url = job.get("application_url") or job.get("url") or ""
    if not url:
        return {"status": "failed", "error": "no application_url", "screenshot_url": None}

    personal = profile.get("personal", {})
    full_name = personal.get("full_name", "").strip()
    email = personal.get("email", "").strip()
    phone = personal.get("phone", "").strip()
    city = personal.get("city", "").strip()
    country = personal.get("country", "").strip()
    location = ", ".join(p for p in (city, country) if p)
    linkedin = personal.get("linkedin_url", "").strip()
    github = personal.get("github_url", "").strip()

    log.info("ashby: navigating to %s", url[:120])
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        return {"status": "failed", "error": f"navigation failed: {e}", "screenshot_url": None}

    if not wait_for_form_ready(page, _FORM_READY_ANCHORS, timeout_ms=10_000):
        # Save what we have so the user can debug
        try:
            take_full_page_screenshot(page, screenshot_path)
        except Exception:
            pass
        return {
            "status": "failed",
            "error": "form did not hydrate (no name/email field appeared within 10s)",
            "screenshot_url": str(screenshot_path) if screenshot_path.exists() else None,
        }

    # Fill core identity fields. Each is best-effort; missing fields just log.
    filled: list[str] = []
    skipped: list[str] = []
    for label, selectors, value in [
        ("name",     _NAME_SELECTORS,     full_name),
        ("email",    _EMAIL_SELECTORS,    email),
        ("phone",    _PHONE_SELECTORS,    phone),
        ("location", _LOCATION_SELECTORS, location),
        ("linkedin", _LINKEDIN_SELECTORS, linkedin),
        ("github",   _GITHUB_SELECTORS,   github),
        ("cover",    _COVER_SELECTORS,    cover_text),
    ]:
        ok = fill_text(page, selectors, value)
        (filled if ok else skipped).append(label)

    # Resume upload (only if we have a PDF on disk)
    if resume_pdf_path:
        ok = upload_file(page, _RESUME_SELECTORS, resume_pdf_path)
        (filled if ok else skipped).append("resume")
    else:
        skipped.append("resume(no_pdf)")

    log.info("ashby: filled=%s, skipped=%s", filled, skipped)

    # Find submit button. Don't actually click in dry-run.
    submit_present = safe_click_submit(page, _SUBMIT_SELECTORS, dry_run=not submit)
    if not submit_present:
        try:
            take_full_page_screenshot(page, screenshot_path)
        except Exception:
            pass
        return {
            "status": "failed",
            "error": "no submit button found",
            "screenshot_url": str(screenshot_path) if screenshot_path.exists() else None,
        }

    if not submit:
        # Dry run — capture the filled form for human review
        take_full_page_screenshot(page, screenshot_path)
        return {
            "status": "ready_to_submit",
            "screenshot_url": str(screenshot_path),
        }

    # Real submit — wait for confirmation. Ashby usually navigates to a
    # success page or shows a banner with text like "Application submitted".
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    confirmation_url = page.url
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=2000)[:2000]
    except Exception:
        pass

    success_signals = [
        "application submitted",
        "thank you for applying",
        "successfully submitted",
        "we received your application",
    ]
    body_lower = body.lower()
    success = any(s in body_lower for s in success_signals)
    # Ashby also routes to a /confirmation or /success path on submit
    if not success:
        success = bool(re.search(r"/(confirmation|success|thank-you|submitted)", confirmation_url, re.I))

    take_full_page_screenshot(page, screenshot_path)

    if success:
        return {
            "status": "applied",
            "confirmation_url": confirmation_url,
            "screenshot_url": str(screenshot_path),
        }
    return {
        "status": "failed",
        "error": "submit clicked but no success signal detected on the resulting page",
        "screenshot_url": str(screenshot_path),
    }
