"""Execute a FillPlan on a live page; locate and click submit; verify outcome."""
from __future__ import annotations

import re
import time

from playwright.sync_api import Page

from jobagent.apply.field_mapper import FillPlan

SUBMIT_SELECTORS = [
    "button[type=submit]",
    "input[type=submit]",
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "button:has-text('Apply')",
]

CONFIRMATION_RE = re.compile(
    r"(thank you|thanks for applying|application (was )?(received|submitted)|"
    r"we('ve| have) received your application|successfully submitted)",
    re.IGNORECASE,
)
ERROR_RE = re.compile(
    r"(required field|please (fill|complete|select|enter)|fix the errors|"
    r"there (was|were) (a |some )?(problem|error)|invalid)",
    re.IGNORECASE,
)


def execute_plan(page: Page, plan: FillPlan, threshold: float) -> list[str]:
    """Apply confident actions. Returns labels of actions that errored."""
    failures = []
    for act in plan.actions:
        if act.action == "skip" or act.confidence < threshold:
            continue
        try:
            loc = page.locator(act.selector).first
            if act.action == "fill":
                loc.fill(act.value, timeout=8000)
                # Comboboxes (react-select etc.) need the suggestion committed.
                role = loc.get_attribute("role") or ""
                if loc.get_attribute("aria-autocomplete") or role == "combobox":
                    time.sleep(0.8)
                    loc.press("ArrowDown")
                    loc.press("Enter")
            elif act.action == "select":
                loc.select_option(label=act.value, timeout=8000)
            elif act.action == "check":
                loc.check(timeout=8000)
            time.sleep(0.25)
        except Exception as e:  # noqa: BLE001 — collect, decide upstream
            failures.append(f"{act.label or act.selector}: {type(e).__name__}")
    return failures


def upload_files(page: Page, resume_path: str, cover_path: str | None) -> bool:
    """Attach resume (and cover letter when a second slot exists)."""
    inputs = page.locator("input[type=file]")
    n = inputs.count()
    if n == 0:
        return False
    inputs.nth(0).set_input_files(resume_path)
    time.sleep(1.5)  # many ATS forms parse the resume async
    if cover_path and n > 1:
        try:
            inputs.nth(1).set_input_files(cover_path)
            time.sleep(1.0)
        except Exception:  # noqa: BLE001 — cover letter slot is best-effort
            pass
    return True


def find_submit(page: Page):
    for sel in SUBMIT_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def submit_and_verify(page: Page, submit_loc) -> tuple[str, str]:
    """Click submit. Returns (outcome, evidence_text).

    outcome: confirmed | error | uncertain — `uncertain` must NEVER be retried
    upstream (risk of double submit); it goes to the review queue.
    """
    url_before = page.url
    submit_loc.click()
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            body = page.inner_text("body", timeout=5000)
        except Exception:  # noqa: BLE001 — page may be navigating
            continue
        if CONFIRMATION_RE.search(body):
            return "confirmed", CONFIRMATION_RE.search(body).group(0)
        if ERROR_RE.search(body):
            return "error", ERROR_RE.search(body).group(0)
    if page.url != url_before:
        return "confirmed", f"url changed to {page.url}"
    return "uncertain", ""
