"""Lever application form handler.

Hosts:
  - jobs.lever.co/<company>/<id>
  - jobs.eu.lever.co/<company>/<id>

Lever job pages have a button labelled "Apply for this job" that scrolls
to (or navigates to) the actual form at /<id>/apply. The application_url
captured by the discovery worker may be either, so this handler tries
the apply suffix variant first if not already on it.

Form is a standard HTML form (`form#application-form`) with a single
`name` field, then email/phone/links section, then resume + additional
file uploads, then optional textarea for "additional information".

Test recipe:
  url = "https://jobs.lever.co/<company>/<id>"
  Run with submit=False; verify screenshot shows name/email/phone filled.
  Submit success page contains "Application submitted" or routes to
  /thanks.
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
    return bool(url) and "lever.co" in url.lower()


_NAME_SELECTORS = [
    'input[name="name"]',
    'input[id="name"]',
    'input[placeholder*="full name" i]',
    'input[autocomplete="name"]',
]
_EMAIL_SELECTORS = [
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="email"]',
]
_PHONE_SELECTORS = [
    'input[name="phone"]',
    'input[type="tel"]',
    'input[autocomplete="tel"]',
]
_CURRENT_COMPANY_SELECTORS = [
    'input[name="org"]',
    'input[name*="company" i]',
    'input[placeholder*="current company" i]',
]
_LINKEDIN_SELECTORS = [
    'input[name*="linkedin" i]',
    'input[placeholder*="linkedin" i]',
]
_GITHUB_SELECTORS = [
    'input[name*="github" i]',
    'input[placeholder*="github" i]',
]
_RESUME_FILE_SELECTORS = [
    'input[type="file"][name="resume"]',
    'input[type="file"][name*="resume" i]',
    'input[type="file"]',  # last-ditch
]
_COVER_TEXT_SELECTORS = [
    'textarea[name="comments"]',
    'textarea[name*="cover" i]',
    'textarea[placeholder*="cover" i]',
    'textarea[placeholder*="additional" i]',
]
_SUBMIT_SELECTORS = [
    'button[data-qa="btn-submit"]',
    'button[type="submit"]:has-text("Submit")',
    'button:has-text("Submit application")',
    'button[type="submit"]',
]
_FORM_READY_ANCHORS = _EMAIL_SELECTORS + _NAME_SELECTORS

# Used when the URL is the job page rather than the apply form.
_APPLY_BUTTON_SELECTORS = [
    'a[href*="/apply" i]:has-text("Apply")',
    'a:has-text("Apply for this job")',
    'a.postings-btn:has-text("Apply")',
]


def _ensure_on_apply_page(page: Page, url: str) -> None:
    """If the URL looks like the job page (no /apply suffix), navigate to
    the apply form directly. Lever's job page links are structured as
    /<company>/<id> and the apply form lives at /<company>/<id>/apply."""
    if "/apply" in url:
        return
    # Naive but reliable: append /apply if not present
    apply_url = url.rstrip("/") + "/apply"
    log.info("lever: redirecting to apply form: %s", apply_url[:120])
    try:
        page.goto(apply_url, wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        # Fall back to clicking the in-page apply button
        for sel in _APPLY_BUTTON_SELECTORS:
            try:
                page.locator(sel).first.click(timeout=2000)
                break
            except Exception:
                continue


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
    url = job.get("application_url") or job.get("url") or ""
    if not url:
        return {"status": "failed", "error": "no application_url", "screenshot_url": None}

    personal = profile.get("personal", {})
    full_name = personal.get("full_name", "").strip()
    email = personal.get("email", "").strip()
    phone = personal.get("phone", "").strip()
    linkedin = personal.get("linkedin_url", "").strip()
    github = personal.get("github_url", "").strip()

    log.info("lever: navigating to %s", url[:120])
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        return {"status": "failed", "error": f"navigation failed: {e}", "screenshot_url": None}

    # If we landed on the job description rather than the form, hop to /apply.
    _ensure_on_apply_page(page, page.url)

    if not wait_for_form_ready(page, _FORM_READY_ANCHORS, timeout_ms=10_000):
        try:
            take_full_page_screenshot(page, screenshot_path)
        except Exception:
            pass
        return {
            "status": "failed",
            "error": "form did not hydrate (no email/name field within 10s)",
            "screenshot_url": str(screenshot_path) if screenshot_path.exists() else None,
        }

    filled, skipped = [], []
    for label, selectors, value in [
        ("name",     _NAME_SELECTORS,            full_name),
        ("email",    _EMAIL_SELECTORS,           email),
        ("phone",    _PHONE_SELECTORS,           phone),
        ("linkedin", _LINKEDIN_SELECTORS,        linkedin),
        ("github",   _GITHUB_SELECTORS,          github),
        ("cover",    _COVER_TEXT_SELECTORS,      cover_text),
    ]:
        ok = fill_text(page, selectors, value)
        (filled if ok else skipped).append(label)

    if resume_pdf_path and upload_file(page, _RESUME_FILE_SELECTORS, resume_pdf_path):
        filled.append("resume")
    else:
        skipped.append("resume")

    log.info("lever: filled=%s skipped=%s", filled, skipped)

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
        take_full_page_screenshot(page, screenshot_path)
        return {"status": "ready_to_submit", "screenshot_url": str(screenshot_path)}

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
        "we've received your application",
        "thanks for your application",
    ]
    body_lower = body.lower()
    success = any(s in body_lower for s in success_signals)
    if not success:
        success = bool(re.search(r"/(thanks|thank-you|confirmation|complete)", confirmation_url, re.I))

    take_full_page_screenshot(page, screenshot_path)

    if success:
        return {
            "status": "applied",
            "confirmation_url": confirmation_url,
            "screenshot_url": str(screenshot_path),
        }
    return {
        "status": "failed",
        "error": "submit clicked but no success signal detected",
        "screenshot_url": str(screenshot_path),
    }
