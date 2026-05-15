"""Shared utilities for application form handlers.

All handlers operate on a Playwright `Page` and a small set of inputs
(profile dict, resume text, cover letter text). These helpers smooth over
the fact that ATSes pick wildly different field names / labels / DOM
shapes for the same semantic concept (e.g. "phone").

Conventions:
  - Functions return None / False on miss; never throw.
  - Selectors are tried in order; first hit wins.
  - String fields are filled, file inputs are set via `set_input_files`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.sync_api import Page, Locator, TimeoutError as PWTimeout

log = logging.getLogger(__name__)

DEFAULT_FIELD_TIMEOUT_MS = 4000


def first_visible(page: Page, selectors: list[str], timeout_ms: int = DEFAULT_FIELD_TIMEOUT_MS) -> Locator | None:
    """Return the first selector that yields a visible element, else None.

    Tries each selector in order. Each gets `timeout_ms / N` budget so the
    total wait is bounded even when most miss.
    """
    if not selectors:
        return None
    per = max(150, timeout_ms // len(selectors))
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=per)
            return loc
        except PWTimeout:
            continue
        except Exception:
            continue
    return None


def fill_text(page: Page, selectors: list[str], value: str | None,
              timeout_ms: int = DEFAULT_FIELD_TIMEOUT_MS) -> bool:
    """Find a visible input/textarea matching selectors and type `value`.

    No-op (returns False) if value is empty or no matching field is found.
    """
    if not value:
        return False
    loc = first_visible(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        loc.fill(value)
        return True
    except Exception as e:
        log.debug("fill_text failed for %s: %s", selectors[0], e)
        return False


def select_option(page: Page, selectors: list[str], value: str | None,
                  timeout_ms: int = DEFAULT_FIELD_TIMEOUT_MS) -> bool:
    """Select an option by visible label or value on the first matching <select>."""
    if not value:
        return False
    loc = first_visible(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        loc.select_option(label=value)
        return True
    except Exception:
        try:
            loc.select_option(value=value)
            return True
        except Exception as e:
            log.debug("select_option failed for %s: %s", selectors[0], e)
            return False


def upload_file(page: Page, selectors: list[str], file_path: str,
                timeout_ms: int = DEFAULT_FIELD_TIMEOUT_MS) -> bool:
    """Set a file on the first matching `<input type=file>` element."""
    p = Path(file_path)
    if not p.exists():
        log.debug("upload_file: %s does not exist", file_path)
        return False
    if not selectors:
        return False
    per = max(150, timeout_ms // len(selectors))
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            # Don't require visible — file inputs are often hidden behind a
            # styled label; set_input_files works on the underlying element.
            loc.wait_for(state="attached", timeout=per)
            loc.set_input_files(file_path)
            return True
        except PWTimeout:
            continue
        except Exception as e:
            log.debug("upload_file failed for %s: %s", sel, e)
            continue
    return False


def safe_click_submit(page: Page, selectors: list[str], dry_run: bool,
                      timeout_ms: int = DEFAULT_FIELD_TIMEOUT_MS) -> bool:
    """Locate the submit button. Click it iff `dry_run` is False.

    Returning True on dry_run means "we found the button, the form is
    fillable and submittable". The caller still treats the run as
    `ready_to_submit`.
    """
    loc = first_visible(page, selectors, timeout_ms)
    if loc is None:
        return False
    if dry_run:
        return True
    try:
        loc.click()
        return True
    except Exception as e:
        log.debug("safe_click_submit failed: %s", e)
        return False


def take_full_page_screenshot(page: Page, dest: Path) -> Path:
    """Capture the full scroll height of the current page to `dest`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(dest), full_page=True)
    return dest


def wait_for_form_ready(page: Page, anchor_selectors: list[str], timeout_ms: int = 8000) -> bool:
    """Wait until at least one anchor selector is visible — used to pause
    until a SPA-rendered form has finished hydrating. Returns False on
    timeout (caller can decide whether to abort)."""
    return first_visible(page, anchor_selectors, timeout_ms) is not None
