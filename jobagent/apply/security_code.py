"""Greenhouse email-verification handling.

Some Greenhouse boards anti-bot the submit with a security code emailed to
the applicant ("Copy and paste this code into the security code field on
your application"). We have Gmail access — poll for the code and finish the
submission unattended.
"""
from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timezone

from playwright.sync_api import Page

_TAG_RE = re.compile(r"<[^>]+>")
# Greenhouse phrasing: "...security code field on your application: <code> After
# you enter the code, resubmit..." — the code is the token right after the colon.
_ANCHORED_RE = re.compile(
    r"(?:security|verification) code(?: field)?(?: on your application)?\s*:?\s*"
    r"([A-Za-z0-9]{6,12})\b", re.IGNORECASE)
_QUERY = '("security code" OR "verification code") newer_than:1d'

SECURITY_INPUT_SELECTORS = [
    "input[id*='security' i]",
    "input[name*='security' i]",
    "input[aria-label*='security code' i]",
    "input[placeholder*='security code' i]",
]


def find_security_input(page: Page):
    for sel in SECURITY_INPUT_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    # label-text fallback
    loc = page.get_by_label(re.compile("security code", re.IGNORECASE)).first
    try:
        if loc.count() and loc.is_visible():
            return loc
    except Exception:  # noqa: BLE001
        pass
    return None


def _message_text(svc, msg_id: str) -> str:
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    parts = [msg.get("payload", {})]
    out = []
    while parts:
        p = parts.pop()
        parts.extend(p.get("parts") or [])
        data = (p.get("body") or {}).get("data")
        if data and p.get("mimeType", "").startswith("text/"):
            out.append(base64.urlsafe_b64decode(data).decode("utf-8", "replace"))
    return "\n".join(out)


def fetch_code(sent_after: float, timeout_s: int = 120, svc=None) -> str | None:
    """Poll Gmail for a security code emailed after `sent_after` (epoch)."""
    from jobagent.outreach import gmail

    svc = svc or gmail.service()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = svc.users().messages().list(
            userId="me", q=_QUERY, maxResults=5).execute()
        for stub in resp.get("messages", []):
            msg = svc.users().messages().get(
                userId="me", id=stub["id"], format="metadata").execute()
            if int(msg.get("internalDate", 0)) / 1000 < sent_after - 60:
                continue  # older than our submit — stale code
            text = _TAG_RE.sub(" ", _message_text(svc, stub["id"]))
            text = re.sub(r"\s+", " ", text)
            low = text.lower()
            if "security code" not in low and "verification code" not in low:
                continue
            m = _ANCHORED_RE.search(text)
            if m and m.group(1).lower() not in {"field", "after", "enter"}:
                return m.group(1)
        time.sleep(8)
    return None


def complete_challenge(page: Page, submit_loc, sent_after: float) -> str | None:
    """Fill the emailed code and resubmit. Returns the code used, or None."""
    from jobagent.apply.filler import find_submit

    box = find_security_input(page)
    if box is None:
        return None
    code = fetch_code(sent_after)
    if code is None:
        return None
    try:
        segmented = page.locator("input[maxlength='1']")
        if segmented.count() >= 6:
            # one box per character — type so focus auto-advances
            segmented.first.click()
            page.keyboard.type(code, delay=90)
        else:
            box.click()
            box.fill(code)
            box.press("Tab")  # blur so the form validates and re-enables submit
        time.sleep(1.0)
    except Exception:  # noqa: BLE001
        return None
    deadline = time.time() + 30
    while time.time() < deadline:
        btn = find_submit(page)
        try:
            if btn and btn.is_enabled():
                btn.click(timeout=10000)
                return code
        except Exception:  # noqa: BLE001 — keep waiting for an enabled button
            pass
        time.sleep(1.5)
    return None


def now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()
