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

# Platform-level bot blocks (e.g. SmartRecruiters "Access is temporarily
# restricted"). Hitting these repeatedly extends the block — the runner must
# back off the whole ATS for the rest of the run.
BOT_BLOCK_HINTS = [
    "access is temporarily restricted",
    "detected unusual activity",
    "automated (bot) activity",
    "verify you are a human",
    "pardon our interruption",
    "verification required",
    "slide right to secure",
]


# CAPTCHA/anti-bot vendors that gate the apply step itself (SmartRecruiters'
# "Apply" click loads a DataDome captcha iframe). These need a human, so the
# job is routed straight to the manual-apply email, not retried.
CAPTCHA_FRAME_HINTS = [
    "captcha-delivery.com",   # DataDome
    "hcaptcha.com",
    "recaptcha/api2/bframe",
    "geo.captcha",
    "challenges.cloudflare.com",
]


def detect_bot_block(page: Page) -> bool:
    # Vendor captcha loaded in any frame -> hard block (SmartRecruiters/DataDome).
    try:
        for fr in page.frames:
            if any(h in (fr.url or "") for h in CAPTCHA_FRAME_HINTS):
                return True
    except Exception:  # noqa: BLE001
        pass
    try:
        body = (page.inner_text("body", timeout=5000) or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(h in body for h in BOT_BLOCK_HINTS)

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
    // nearest label-ish element, walking up a few field-container levels —
    // Ashby keeps the label 2-3 wrappers above the input (combobox shells).
    let cont = el.closest('div,fieldset,li');
    for (let k = 0; k < 4 && cont; k++) {
      const cand = cont.querySelector('label, legend, .label, [class*="label"]');
      if (cand && cand.innerText.trim()) return cand.innerText.trim();
      cont = cont.parentElement ? cont.parentElement.closest('div,fieldset,li') : null;
    }
    return el.getAttribute('placeholder') || el.name || el.id || '';
  };
  // Radios/checkboxes: their own label is just the option text ("Yes") —
  // prepend the question from the group container so the mapper has context.
  const groupLabel = (el, optLabel) => {
    let group = el.closest('fieldset');
    if (!group && el.name) {
      let p = el.parentElement;
      for (let k = 0; k < 6 && p; k++) {
        if (p.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length > 1) { group = p; break; }
        p = p.parentElement;
      }
    }
    if (!group) return optLabel;
    const legend = group.querySelector('legend');
    let q = legend ? legend.innerText.trim() : '';
    if (!q) {
      // Climb until the container text is more than just the option words
      // ("YesNo") — the question usually sits 1-2 wrappers above the group.
      let node = group;
      for (let k = 0; k < 4 && node; k++) {
        const t = (node.textContent || '').replace(/\s+/g, ' ').trim();
        if (t.length > 30) { q = t.slice(0, 160); break; }
        node = node.parentElement;
      }
    }
    if (q && !optLabel.includes(q.slice(0, 25))) return q.slice(0, 200) + ' :: ' + optLabel;
    return optLabel;
  };
  const selectorFor = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) {
      // Radio/checkbox options share a name — the selector must pin the
      // value or every option resolves to the first one in the group.
      if ((el.type === 'radio' || el.type === 'checkbox') && el.value)
        return `input[name="${CSS.escape(el.name)}"][value="${CSS.escape(el.value)}"]`;
      return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    }
    const all = Array.from(document.querySelectorAll(el.tagName.toLowerCase()));
    return `${el.tagName.toLowerCase()} >> nth=${all.indexOf(el)}`;
  };
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const type = el.tagName === 'SELECT' ? 'select'
      : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text');
    if (['hidden', 'submit', 'button', 'image'].includes(type)) return;
    // Keep file inputs and radio/checkbox even when visually hidden —
    // custom-styled widgets (Zilch/Ashby) hide the real input behind a
    // styled control; check() with force still works.
    if (el.offsetParent === null && !['file', 'radio', 'checkbox'].includes(type)) return;
    // Skip framework-internal companions (react-select's hidden required
    // input, intl-tel-input search box): filling the real combobox/tel
    // control satisfies them.
    if ((el.className || '').includes('requiredInput')) return;
    // ...but never exclude the combobox itself (Ashby's location input has
    // no id/name/aria-label and IS the role=combobox — it must stay in).
    if (!el.id && !el.name && !el.getAttribute('aria-label') &&
        el.getAttribute('role') !== 'combobox' &&
        el.closest('div')?.parentElement?.querySelector('[role="combobox"]')) return;
    const sel = selectorFor(el);
    const key = type === 'radio' || type === 'checkbox' ? `${el.name}::${el.value}` : sel;
    if (seen.has(key)) return;
    seen.add(key);
    const baseLabel = labelFor(el);
    const entry = {
      selector: sel,
      type,
      label: ((type === 'radio' || type === 'checkbox')
        ? groupLabel(el, baseLabel) : baseLabel).slice(0, 300),
      required: el.required || el.getAttribute('aria-required') === 'true',
      value: type === 'checkbox' || type === 'radio' ? el.checked : (el.value || ''),
    };
    // Modern Greenhouse renders screening/EEO questions as react-select
    // widgets: an input.select__input (role=combobox) behind a .select__control
    // shell with fixed options. Tag them so the mapper skips them and the
    // dedicated picker handles them (typing into them as text never commits).
    // Ashby's free-text location autocomplete has no select__ class — untouched.
    if ((el.className || '').includes('select__input')) {
      entry.widget = 'react_select';
    }
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


# Mask the automation tells Greenhouse/Ashby bot-detection keys on
# (navigator.webdriver etc.). Repeated security-code challenges on 2026-06-11
# were Greenhouse's anti-bot reacting to the stock Playwright fingerprint.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
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
            timezone_id="Asia/Kolkata",  # consistent with an India-based applicant
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(_STEALTH_JS)
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
