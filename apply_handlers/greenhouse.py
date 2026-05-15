"""Greenhouse application form handler.

Hosts:
  - boards.greenhouse.io/<company>/jobs/<id>
  - job-boards.greenhouse.io/<company>/jobs/<id>
  - grnh.se/<id>            (shortlinks — Playwright follows the redirect)

Greenhouse renders applications as a regular HTML <form id="application_form">
with stable input ids: first_name, last_name, email, phone. Resume and cover
letter are dual-input — either upload a file OR paste text. Required custom
questions appear below the contact section and vary per role.

Test recipe:
  url = "https://job-boards.greenhouse.io/<company>/jobs/<id>"
  Run with submit=False; verify the screenshot shows the contact fields
  populated. Then re-run with submit=True. Greenhouse's success page is
  typically a thank-you message containing "Application submitted" and the
  URL changes to /<id>/thanks or similar.
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
    if not url:
        return False
    u = url.lower()
    return (
        "greenhouse.io" in u
        or "boards.greenhouse.io" in u
        or "job-boards.greenhouse.io" in u
        or "grnh.se" in u
    )


# Greenhouse uses stable ids on most fields, but custom themes occasionally
# override. Try id first, then name attr, then label/placeholder.
_FIRST_NAME_SELECTORS = [
    "#first_name",
    'input[name="job_application[first_name]"]',
    'input[name="first_name"]',
    'input[autocomplete="given-name"]',
]
_LAST_NAME_SELECTORS = [
    "#last_name",
    'input[name="job_application[last_name]"]',
    'input[name="last_name"]',
    'input[autocomplete="family-name"]',
]
_EMAIL_SELECTORS = [
    "#email",
    'input[type="email"]',
    'input[name="job_application[email]"]',
]
_PHONE_SELECTORS = [
    "#phone",
    'input[type="tel"]',
    'input[name="job_application[phone]"]',
]
# Greenhouse exposes resume + cover as dual file/text inputs. The file
# input usually has id="resume" or name="job_application[resume]".
_RESUME_FILE_SELECTORS = [
    "#resume",
    'input[type="file"][name*="resume" i]',
    'input[type="file"]',
]
_COVER_FILE_SELECTORS = [
    "#cover_letter",
    'input[type="file"][name*="cover" i]',
]
_COVER_TEXT_SELECTORS = [
    'textarea[name="job_application[cover_letter_text]"]',
    'textarea[name*="cover" i]',
    "#cover_letter_text",
]
_LINKEDIN_SELECTORS = [
    'input[name*="linkedin" i]',
    'input[id*="linkedin" i]',
    'input[placeholder*="linkedin" i]',
]

_SUBMIT_SELECTORS = [
    "#submit_app",
    'button[type="submit"]:has-text("Submit")',
    'input[type="submit"]',
    'button:has-text("Submit Application")',
    'button:has-text("Apply")',
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
    linkedin = personal.get("linkedin_url", "").strip()

    log.info("greenhouse: navigating to %s", url[:120])
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
        ("linkedin",   _LINKEDIN_SELECTORS,   linkedin),
    ]:
        ok = fill_text(page, selectors, value)
        (filled if ok else skipped).append(label)

    # Resume: prefer file upload when we have a PDF on disk; Greenhouse
    # accepts most common formats. Skip silently when no PDF is supplied.
    if resume_pdf_path:
        if upload_file(page, _RESUME_FILE_SELECTORS, resume_pdf_path):
            filled.append("resume")
        else:
            skipped.append("resume(no_target)")
    else:
        skipped.append("resume(no_pdf)")

    # Cover letter: try the textarea first (no file required), fall back
    # to a synthesised PDF only if needed and we have one.
    if cover_text and fill_text(page, _COVER_TEXT_SELECTORS, cover_text):
        filled.append("cover_text")
    elif resume_pdf_path:
        # Heuristic: if there's no textarea the form may want a file
        # upload — try the cover-letter file input. We don't ship a cover
        # PDF separately; reuse the resume PDF only when explicitly
        # requested by the caller.
        skipped.append("cover(no_textarea)")
    else:
        skipped.append("cover(no_textarea_no_pdf)")

    log.info("greenhouse: filled=%s skipped=%s", filled, skipped)

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
        "your application has been received",
        "we received your application",
        "thanks for applying",
    ]
    body_lower = body.lower()
    success = any(s in body_lower for s in success_signals)
    if not success:
        # Greenhouse routes to /thanks or appends ?application_sent=true
        success = bool(re.search(r"/(thanks|thank-you|thank_you|confirmation)", confirmation_url, re.I))

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
