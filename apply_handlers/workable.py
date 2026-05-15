"""Workable application form handler.

Hosts:
  - apply.workable.com/<company>/j/<id>           (application form)
  - apply.workable.com/<company>/j/<id>/apply     (alt apply path)

Workable's apply form is a single-page React app. Inputs are usually
labelled in plain English ("First name", "Last name", "Email", etc.)
and exposed via name= attributes that match the label.

Test recipe:
  url = "https://apply.workable.com/<company>/j/<id>"
  Run with submit=False; verify the screenshot shows the contact fields
  populated. Submit success page contains "Thank you for applying".
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
    return bool(url) and "workable.com" in url.lower()


_FIRST_NAME_SELECTORS = [
    'input[name="firstname"]',
    'input[name="first_name"]',
    'input[id*="firstname" i]',
    'input[autocomplete="given-name"]',
    'input[aria-label="First name"]',
]
_LAST_NAME_SELECTORS = [
    'input[name="lastname"]',
    'input[name="last_name"]',
    'input[id*="lastname" i]',
    'input[autocomplete="family-name"]',
    'input[aria-label="Last name"]',
]
_EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="email"]',
]
_PHONE_SELECTORS = [
    'input[type="tel"]',
    'input[name="phone"]',
    'input[autocomplete="tel"]',
]
_LOCATION_SELECTORS = [
    'input[name*="address" i]',
    'input[name*="location" i]',
    'input[autocomplete="address-line1"]',
]
_LINKEDIN_SELECTORS = [
    'input[name*="linkedin" i]',
    'input[placeholder*="linkedin" i]',
]
_RESUME_FILE_SELECTORS = [
    'input[type="file"][accept*="pdf" i]',
    'input[type="file"]',  # Workable usually has only one file input
]
_COVER_TEXT_SELECTORS = [
    'textarea[name*="cover" i]',
    'textarea[placeholder*="cover" i]',
    'textarea[name="summary"]',  # some Workable themes label cover as summary
]
_SUBMIT_SELECTORS = [
    'button[type="submit"]:has-text("Submit")',
    'button:has-text("Submit application")',
    'button:has-text("Send application")',
    'button[type="submit"]',
]
_FORM_READY_ANCHORS = _EMAIL_SELECTORS + _FIRST_NAME_SELECTORS


def _split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


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
    first, last = _split_name(personal.get("full_name", ""))
    email = personal.get("email", "").strip()
    phone = personal.get("phone", "").strip()
    city = personal.get("city", "").strip()
    country = personal.get("country", "").strip()
    location = ", ".join(p for p in (city, country) if p)
    linkedin = personal.get("linkedin_url", "").strip()

    log.info("workable: navigating to %s", url[:120])
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        return {"status": "failed", "error": f"navigation failed: {e}", "screenshot_url": None}

    if not wait_for_form_ready(page, _FORM_READY_ANCHORS, timeout_ms=10_000):
        try:
            take_full_page_screenshot(page, screenshot_path)
        except Exception:
            pass
        return {
            "status": "failed",
            "error": "form did not hydrate (no email/first-name field within 10s)",
            "screenshot_url": str(screenshot_path) if screenshot_path.exists() else None,
        }

    filled, skipped = [], []
    for label, selectors, value in [
        ("first_name", _FIRST_NAME_SELECTORS, first),
        ("last_name",  _LAST_NAME_SELECTORS,  last),
        ("email",      _EMAIL_SELECTORS,      email),
        ("phone",      _PHONE_SELECTORS,      phone),
        ("location",   _LOCATION_SELECTORS,   location),
        ("linkedin",   _LINKEDIN_SELECTORS,   linkedin),
        ("cover",      _COVER_TEXT_SELECTORS, cover_text),
    ]:
        ok = fill_text(page, selectors, value)
        (filled if ok else skipped).append(label)

    if resume_pdf_path and upload_file(page, _RESUME_FILE_SELECTORS, resume_pdf_path):
        filled.append("resume")
    else:
        skipped.append("resume")

    log.info("workable: filled=%s skipped=%s", filled, skipped)

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
        "thank you for applying",
        "thanks for applying",
        "application submitted",
        "we've received your application",
        "your application has been sent",
    ]
    body_lower = body.lower()
    success = any(s in body_lower for s in success_signals)
    if not success:
        success = bool(re.search(r"/(thanks|thank-you|confirmation|success|submitted)", confirmation_url, re.I))

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
