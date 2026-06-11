"""Playwright harness: page setup, form-schema extraction, hazard detection."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import Page, sync_playwright

from jobagent.db import DATA_DIR

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Only VISIBLE challenge widgets block — many ATS forms (e.g. Greenhouse)
# embed invisible token-based reCAPTCHA that never challenges a normal browser.
CAPTCHA_SELECTORS = [
    "iframe[src*='hcaptcha.com/captcha']",
    "iframe[src*='recaptcha'][src*='bframe']",
    "iframe[title*='challenge']",
    "iframe[src*='turnstile']",
]

LOGIN_HINTS = ["sign in to apply", "log in to apply", "create an account to apply"]

# JS walker: compact schema of every fillable control in the page's forms.
_FORM_SCHEMA_JS = r"""
() => {
  const controls = [];
  const seen = new Set();
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.innerText.trim();
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const t = labelled.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
      if (t) return t;
    }
    const wrap = el.closest('label');
    if (wrap) return wrap.innerText.trim();
    // nearest preceding label-ish element within the same field container
    const cont = el.closest('div,fieldset,li');
    if (cont) {
      const cand = cont.querySelector('label, legend, .label, [class*="label"]');
      if (cand) return cand.innerText.trim();
    }
    return el.getAttribute('placeholder') || el.name || el.id || '';
  };
  const selectorFor = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    const all = Array.from(document.querySelectorAll(el.tagName.toLowerCase()));
    return `${el.tagName.toLowerCase()} >> nth=${all.indexOf(el)}`;
  };
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const type = el.tagName === 'SELECT' ? 'select'
      : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text');
    if (['hidden', 'submit', 'button', 'image'].includes(type)) return;
    if (el.offsetParent === null && type !== 'file') return;  // invisible
    const sel = selectorFor(el);
    const key = type === 'radio' || type === 'checkbox' ? `${el.name}::${el.value}` : sel;
    if (seen.has(key)) return;
    seen.add(key);
    const entry = {
      selector: sel,
      type,
      label: labelFor(el).slice(0, 300),
      required: el.required || el.getAttribute('aria-required') === 'true',
      value: type === 'checkbox' || type === 'radio' ? el.checked : (el.value || ''),
    };
    if (type === 'select') {
      entry.options = Array.from(el.options).map(o => o.text.trim()).filter(Boolean).slice(0, 60);
    }
    if (type === 'radio' || type === 'checkbox') {
      entry.option_value = el.value;
      entry.group = el.name || '';
    }
    controls.push(entry);
  });
  return controls;
}
"""


@contextmanager
def open_page(headless: bool = True) -> Iterator[Page]:
    profile_dir = DATA_DIR / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            user_agent=USER_AGENT,
            viewport={"width": 1380, "height": 940},
            locale="en-US",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            yield page
        finally:
            ctx.close()


def extract_form_schema(page: Page) -> list[dict]:
    return page.evaluate(_FORM_SCHEMA_JS)


def detect_captcha(page: Page) -> bool:
    for s in CAPTCHA_SELECTORS:
        loc = page.locator(s)
        for i in range(loc.count()):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:  # noqa: BLE001 — detached frames
                continue
    return False


def detect_login_wall(page: Page) -> bool:
    body = (page.inner_text("body", timeout=5000) or "").lower()
    return any(h in body for h in LOGIN_HINTS)
