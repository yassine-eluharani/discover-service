"""Per-platform application form handlers.

Each handler module exports:
  - `matches(url: str) -> bool`
  - `fill(page, job, profile, resume_text, cover_text, submit) -> dict`

`dispatch(url)` returns the first registered handler whose `matches()`
returns True, or None when no platform handler exists for that URL.
The discovery worker uses None to mark `apply_status='manual_only'`.

Add new handlers by importing them here and appending to `_HANDLERS`.
"""

from __future__ import annotations

from types import ModuleType

from apply_handlers import ashby

_HANDLERS: list[ModuleType] = [
    ashby,
    # greenhouse, lever, workable, workday — added in later phases
]


def dispatch(url: str | None) -> ModuleType | None:
    """Return the first handler whose matches(url) is True, or None."""
    if not url:
        return None
    for h in _HANDLERS:
        try:
            if h.matches(url):
                return h
        except Exception:
            continue
    return None


def all_handler_names() -> list[str]:
    return [h.__name__.rsplit(".", 1)[-1] for h in _HANDLERS]
