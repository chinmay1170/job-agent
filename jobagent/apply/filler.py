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
    time.sleep(6)  # ATS uploads/parses the file async — submit too early and
    #                validation still counts the field as empty
    if cover_path and n > 1:
        try:
            inputs.nth(1).set_input_files(cover_path)
            time.sleep(1.0)
        except Exception:  # noqa: BLE001 — cover letter slot is best-effort
            pass
    return True


CONSENT_RE = re.compile(
    r"(i confirm|i acknowledge|i have read|privacy (notice|policy)|"
    r"true and correct|i consent|i agree|terms (and|&) conditions)", re.IGNORECASE)
AFFIRM_RE = re.compile(
    r"^(yes|i confirm|i agree|i acknowledge|i consent|i accept|confirmed|accept)",
    re.IGNORECASE)


def resolve_consent_combos(page: Page) -> list[str]:
    """Open required consent react-selects and pick the affirmative option.

    Consent acknowledgements are a condition of applying, not a judgment
    call, so they're resolved deterministically — never anything else.
    """
    resolved = []
    combos = page.locator("[role=combobox]")
    for i in range(combos.count()):
        combo = combos.nth(i)
        try:
            labelled = combo.get_attribute("aria-labelledby") or ""
            label = " ".join(
                page.locator(f"[id='{lid}']").inner_text(timeout=2000)
                for lid in labelled.split() if lid
            ) if labelled else (combo.get_attribute("aria-label") or "")
            if not CONSENT_RE.search(label):
                continue
            shell_text = combo.evaluate(
                "e => (e.closest('.select-shell') || e.closest('div[class*=select]'))"
                "?.textContent || ''"
            )
            if shell_text and "select..." not in shell_text.lower():
                continue  # already has a value
            combo.click()
            page.wait_for_timeout(500)
            options = page.locator("[role='option']")
            pick = None
            for j in range(options.count()):
                if AFFIRM_RE.search(options.nth(j).inner_text(timeout=2000).strip()):
                    pick = options.nth(j)
                    break
            if pick is None and options.count() == 1:
                pick = options.first
            if pick:
                pick.click()
                page.wait_for_timeout(300)
                resolved.append(label[:80])
            else:
                page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001 — leave unresolved; submit gate catches it
            continue
    return resolved


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
    try:
        submit_loc.click(timeout=15000)
    except Exception:  # noqa: BLE001 — disabled/detached button
        return "uncertain", "submit click failed (button disabled?)"
    outcome, evidence = watch_outcome(page, url_before)
    if outcome == "error":
        # Validation errors mean nothing was submitted — async state (file
        # uploads, react-select commits) often settles just after the first
        # click. One re-submit is safe; a second failure goes to review.
        time.sleep(4)
        try:
            submit_loc.click(timeout=10000)
        except Exception:  # noqa: BLE001
            return "uncertain", "resubmit click failed (button disabled?)"
        outcome, evidence = watch_outcome(page, url_before)
    return outcome, evidence


def watch_outcome(page: Page, url_before: str, seconds: int = 20) -> tuple[str, str]:
    deadline = time.time() + seconds
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
    if url_before and page.url != url_before:
        return "confirmed", f"url changed to {page.url}"
    return "uncertain", ""
