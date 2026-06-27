"""Execute a FillPlan on a live page; locate and click submit; verify outcome."""
from __future__ import annotations

import random
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
SPAM_RE = re.compile(
    r"flagged as a possible spam|flagged as spam|submission was flagged",
    re.IGNORECASE,
)


def pick_react_select(page: Page, opener, want_res: list | None = None,
                      value: str | None = None) -> bool:
    """Open a react-select widget and click the matching option.

    `opener` is a locator that opens the menu when clicked (the .select__control
    shell or its .select__input). Pass EITHER want_res (ordered list of regexes,
    deterministic resolver) OR value (target option text, mapper-driven custom
    question). Returns True if an option was clicked. Typing into select__input
    as plain text never commits — this is the only correct way to set them.
    """
    try:
        opener.click(timeout=4000)
    except Exception:  # noqa: BLE001
        return False
    page.wait_for_timeout(500)
    if value:
        # Searchable react-selects filter as you type — narrows long lists.
        # Location autocompletes (Greenhouse "Location (City)") fetch geo
        # suggestions async, so type then wait for the menu to populate rather
        # than a fixed beat.
        try:
            page.keyboard.type(value[:40], delay=30)
            for _ in range(6):  # up to ~3s for async suggestions
                page.wait_for_timeout(500)
                if page.locator(".select__option, [role='option']").count():
                    break
        except Exception:  # noqa: BLE001
            pass
    # CRITICAL: scope options to THIS dropdown's own menu. A page-wide
    # `.select__option` search collides with other react-selects on the page —
    # notably the phone widget's always-rendered 249-country code list — so a
    # yes/no question would search country names for "No" and fail. react-select
    # links the open menu to its input via aria-controls/aria-owns (the listbox
    # id); scope to that. Fall back to the control's sibling .select__menu, then
    # page-wide only as a last resort.
    options = None
    try:
        listbox_id = opener.evaluate(
            """el => {
                const root = el.closest('div[class*=select__control]') || el;
                const inp = root.querySelector('input');
                const id = inp && (inp.getAttribute('aria-controls')
                                   || inp.getAttribute('aria-owns'));
                if (id) return id;
                // sibling menu within the same react-select container
                let p = root.parentElement;
                for (let k = 0; k < 4 && p; k++) {
                    const menu = p.querySelector('div[class*=select__menu]');
                    if (menu) { if (!menu.id) menu.id = 'rs_menu_scope_tmp'; return menu.id; }
                    p = p.parentElement;
                }
                return '';
            }"""
        ) or ""
    except Exception:  # noqa: BLE001
        listbox_id = ""
    if listbox_id:
        esc = listbox_id.replace('"', '\\"')
        options = page.locator(
            f'#{esc} .select__option, #{esc} [role="option"], '
            f'[id="{esc}"] .select__option, [id="{esc}"] [role="option"]')
    if options is None or not options.count():
        options = page.locator(".select__option, [role='option']")
    # Options can load async after the menu opens (and for plain yes/no selects
    # they may render a beat late) — wait briefly and re-check before giving up.
    for _ in range(3):
        if options.count():
            break
        page.wait_for_timeout(400)
    pick = None
    try:
        if want_res:
            for want_re in want_res:
                for j in range(options.count()):
                    if want_re.search((options.nth(j).inner_text(timeout=1500) or "").strip()):
                        pick = options.nth(j)
                        break
                if pick is not None:
                    break
        elif value:
            v = value.strip().lower()
            # exact match first, then contains
            for want in (lambda t: t == v, lambda t: v in t or t in v):
                for j in range(options.count()):
                    t = (options.nth(j).inner_text(timeout=1500) or "").strip().lower()
                    if t and want(t):
                        pick = options.nth(j)
                        break
                if pick is not None:
                    break
    except Exception:  # noqa: BLE001
        pick = None
    if pick is not None:
        try:
            pick.click(timeout=3000)
            page.wait_for_timeout(300)
            return True
        except Exception:  # noqa: BLE001
            pass
    # Keyboard fallback for value/autocomplete mode: if we typed a query and at
    # least one suggestion rendered, commit the highlighted (first) one with
    # ArrowDown+Enter. This rescues flaky geo-autocompletes (Greenhouse
    # "Location (City)") where the option click misses or loads a beat late.
    if value:
        try:
            if page.locator(".select__option, [role='option']").count():
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(150)
                page.keyboard.press("Enter")
                page.wait_for_timeout(300)
                # committed if the input now holds a value / shell shows text
                shell_txt = (opener.evaluate(
                    "el => { const c = el.closest('div[class*=select__control]') "
                    "|| el; return (c.textContent || '').trim(); }") or "").lower()
                if shell_txt and "select" not in shell_txt[:18]:
                    return True
        except Exception:  # noqa: BLE001
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    return False


def execute_plan(page: Page, plan: FillPlan, threshold: float) -> list[str]:
    """Apply confident actions. Returns labels of actions that errored."""
    failures = []
    for act in plan.actions:
        if act.action == "skip" or act.confidence < threshold:
            continue
        try:
            loc = page.locator(act.selector).first
            if act.action == "react_select":
                # Mapper-answered custom dropdown (education, pay-range, ...).
                if not pick_react_select(page, loc, value=act.value):
                    failures.append(f"{act.label or act.selector}: react_select no match")
                time.sleep(random.uniform(0.4, 1.2))
                continue
            if act.action == "fill":
                cls = loc.get_attribute("class") or ""
                if "select__input" in cls:
                    # react-select: never type as text (won't commit). The
                    # mapper should emit action=react_select; if it sent fill,
                    # route through the picker using the value as target.
                    pick_react_select(page, loc, value=act.value)
                    time.sleep(random.uniform(0.4, 1.2))
                    continue
                role = loc.get_attribute("role") or ""
                is_combo = bool(loc.get_attribute("aria-autocomplete")) or role == "combobox"
                if is_combo:
                    # React autocompletes (Ashby location etc.) ignore
                    # programmatic fill — they need real keystrokes to open
                    # the dropdown, then a suggestion must be committed.
                    loc.click(timeout=8000)
                    loc.fill("")
                    loc.press_sequentially(act.value, delay=60)
                    # Suggestions load async. ONLY look inside this
                    # combobox's own listbox (aria-controls/aria-owns) —
                    # a page-wide option search hits other widgets'
                    # always-visible option divs and clicks the wrong thing.
                    listbox = (loc.get_attribute("aria-controls")
                               or loc.get_attribute("aria-owns"))
                    picked = False
                    if listbox:
                        for _ in range(10):
                            time.sleep(0.5)
                            try:
                                option = page.locator(
                                    f"[id='{listbox}'] [role='option'], "
                                    f"[id='{listbox}'] li, "
                                    f"[id='{listbox}'] div[class*='option']"
                                ).first
                                if option.count() and option.is_visible():
                                    option.click(timeout=3000)
                                    picked = True
                                    break
                            except Exception:  # noqa: BLE001
                                continue
                    if not picked:
                        # Keyboard commit — safe regardless of dropdown markup.
                        time.sleep(2.0)
                        loc.press("ArrowDown")
                        time.sleep(0.3)
                        loc.press("Enter")
                elif len(act.value) <= 60:
                    # Short fields: real keystrokes at human speed — instant
                    # programmatic fills are a bot-detection signal.
                    loc.click(timeout=8000)
                    loc.fill("")
                    loc.press_sequentially(act.value, delay=random.randint(25, 70))
                else:
                    loc.fill(act.value, timeout=8000)  # long text: paste-like
            elif act.action == "select":
                loc.select_option(label=act.value, timeout=8000)
            elif act.action == "check":
                try:
                    loc.check(timeout=5000)
                except Exception:  # noqa: BLE001
                    # Custom-styled radio/checkbox: the real input is hidden —
                    # force-check it, or click its label as a last resort.
                    try:
                        loc.check(timeout=5000, force=True)
                    except Exception:  # noqa: BLE001
                        loc.evaluate(
                            "el => (el.closest('label') || "
                            "document.querySelector(`label[for='${el.id}']`))"
                            "?.click()")
            time.sleep(random.uniform(0.4, 1.4))  # human pause between fields
        except Exception as e:  # noqa: BLE001 — collect, decide upstream
            failures.append(f"{act.label or act.selector}: {type(e).__name__}")
    return failures


def upload_files(page: Page, resume_path: str, cover_path: str | None) -> bool:
    """Attach resume and cover letter to the right slots, by label.

    Positional nth(0)/nth(1) is wrong on Ashby: the first file input is an
    "Autofill from resume" helper widget, the REAL resume slot comes second
    (#_systemfield_resume). Classify every input by its container text and
    never put the cover letter anywhere but an explicit cover slot.
    """
    inputs = page.locator("input[type=file]")
    n = inputs.count()
    if n == 0:
        return False

    resume_idx = cover_idx = None
    fallback_idx = None
    for i in range(n):
        try:
            ctx = inputs.nth(i).evaluate(
                "el => ((el.closest('div[class*=field], div[class*=upload], "
                "section, fieldset') || el.parentElement)?.textContent || '')"
                ".slice(0, 150)"
            ) or ""
        except Exception:  # noqa: BLE001
            ctx = ""
        low = ctx.lower()
        if "autofill" in low:
            continue  # helper widget, not the real slot
        if resume_idx is None and ("resume" in low or re.search(r"\bcv\b", low)):
            resume_idx = i
        elif cover_idx is None and "cover" in low:
            cover_idx = i
        elif fallback_idx is None:
            fallback_idx = i
    if resume_idx is None:
        resume_idx = fallback_idx if fallback_idx is not None else 0

    inputs.nth(resume_idx).set_input_files(resume_path)
    time.sleep(6)  # ATS uploads/parses the file async — submit too early and
    #                validation still counts the field as empty
    if cover_path and cover_idx is not None:
        try:
            inputs.nth(cover_idx).set_input_files(cover_path)
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
# Required demographic dropdowns -> always the decline/prefer-not option.
EEO_COMBO_RE = re.compile(
    r"age range|how old are you|what.{0,6}s your age|your age\b|gender|ethnicit|"
    r"race|veteran|disab|sexual orientation|pronoun|neurodiver|gender identity|"
    r"hispanic|latino|transgender|military status|lgbt|racial/ethnic|"
    r"member of the .{0,15}community", re.IGNORECASE)
DECLINE_OPT_RE = re.compile(
    r"prefer not|don'?t wish|do not wish|decline|rather not|"
    r"(not|don'?t|do not) (want|wish|like) to (say|answer|disclose|specify)|"
    r"not to (say|answer|disclose|specify)|i do not", re.IGNORECASE)
# Location/city react-selects are type-to-search (no fixed option list) — fill
# with the candidate's current city. Pulled from answers.yaml at call time.
LOCATION_CITY_RE = re.compile(
    r"location \(city\)|^city\b|current city|city of residence|where are you "
    r"(based|located)|your location", re.IGNORECASE)
# Education react-selects are type-to-search (school/degree lists) — fill via the
# value path from answers.misc.* (mirrors profile education). Order matters:
# more specific patterns first so "field of study" doesn't match the school rule.
EDU_FIELD_RES: list = [
    (re.compile(r"field of study|discipline|major|area of study|specializ", re.I),
     "education_field"),
    (re.compile(r"degree|level of education|qualification", re.I), "education_degree"),
    (re.compile(r"school|university|college|institution|alma mater|"
                r"educational? (establishment|institution)", re.I), "education_school"),
    (re.compile(r"(start|from).{0,10}year", re.I), "education_start_year"),
    (re.compile(r"(end|to|grad\w*|completion).{0,10}year", re.I), "education_end_year"),
]
# Country/state react-selects: searchable lists need the answer TYPED to reveal
# the option (a regex-only `want_res` match never opens a typed list), so route
# them through the value path with the honest current answer. More specific
# (state/province) first so it isn't swallowed by the country rule.
VALUE_FIELD_RES: list = [
    (re.compile(r"state or province|which (state|province)|"
                r"state/province .{0,15}(live|residen)|province .{0,10}live|"
                r"^state\b|state of residence", re.I), "current_state"),
    (re.compile(r"^country\*?$|^country\b|country .{0,20}(based|residen|live|located)|"
                r"based in .{0,10}country|country of (residence|origin)|"
                r"which country", re.I), "current_country"),
    # "In what cities are you available to work / preferred work location" —
    # the mapper fills the ROLE's city first; if it misses, fall back to the
    # candidate's current city (he can work from there and relocates). The
    # Other-fallback in the value branch covers lists without his city.
    (re.compile(r"cities are you available|city .{0,15}available to work|"
                r"preferred (work|office) location|which office|"
                r"available to work|work location", re.I), "current_city"),
]

# Deterministic question bank for dropdown widgets. First matching question
# rule wins; its option patterns are tried in order. HONEST answers only —
# sourced from answers.yaml facts (sponsorship yes, no EU permit, relocating,
# open to onsite/hybrid, 30-day notice, He/Him, decline demographics).
_YES = re.compile(r"^yes\b", re.IGNORECASE)
_NO = re.compile(r"^no\b", re.IGNORECASE)
# "Other" / "none of the above" fallback option, e.g. for short country lists
# that don't include India.
OTHER_OPT_RE = re.compile(r"^other\b|none of the (above|listed)|"
                          r"not (listed|in the list)|elsewhere|rest of", re.IGNORECASE)
# Bot-detection / human-verification dropdowns: "Which best describes you?" ->
# "I am a human being" (he is). HONEST.
HUMAN_VERIFY_RE = re.compile(
    r"best describes you|are you a (human|robot|bot)|human or (a )?(bot|robot|ai)|"
    r"human being|verify .{0,15}human|prove .{0,10}human", re.IGNORECASE)
COMBO_RULES: list = [
    (re.compile(r"i confirm|i acknowledge|acknowledge this|"
                r"aware of and acknowledge|aware of|i have read|privacy|onboarding|"
                r"protect your data|data privacy|gdpr|true and correct|"
                r"i consent|i agree|terms (and|&) conditions|"
                r"pay range transparency|approach to (base )?pay|base pay", re.I),
     [AFFIRM_RE, re.compile(r"yes|agree|consent|acknowledge|accept|confirm|"
                            r"i('?| ha)ve read|understand|aware", re.I)]),
    (re.compile(r"(require|need)\b.{0,25}sponsor|sponsor\w*\b.{0,15}(require|need)|"
                r"need us to sponsor|require sponsorship|sponsorship (required|needed)",
                re.I), [_YES]),
    # "Have you ever worked here / previously employed" -> No.
    (re.compile(r"ever worked (at|for|here|with)|previously worked|"
                r"worked\b.{0,25}before|previously (employed|been employed)|"
                r"former (employee|intern|contractor)|worked .{0,15}(here|us) before",
                re.I),
     # Options are often not "No" — many forms offer "I have never worked
     # here" / "I have not previously been employed" instead.
     [_NO, re.compile(r"never worked|have never|not (previously )?worked|"
                      r"no,? i ('?| ha)ve n|^no\b", re.I)]),
    # Work-authorisation / eligibility / nationality / residency questions.
    # Honest answer is NO for yes/no phrasings; for STATUS dropdowns (citizen /
    # PR / require-work-pass) pick the "require sponsorship / work pass" option.
    (re.compile(r"eu passport|work permit|work pass|work authoris|work authoriz|"
                r"authoris(ed|ation) to work|authoriz(ed|ation) to work|"
                r"right to work|legally entitled|eligible to work|"
                r"national of|are you a (citizen|national)|"
                r"citizen.{0,30}permanent resident|residency status|"
                r"tax resident", re.I),
     # Status dropdowns list several look-alikes — pick the option meaning
     # "I need NEW sponsorship", excluding the decoys: "don't require",
     # "currently sponsored by another company", "change employer".
     [re.compile(r"^(?!.*(don'?t require|do not require|no visa|already|"
                 r"currently sponsored|another (company|employer)|change emplo|"
                 r"national of|citizen)).*(require|need)\b.{0,25}"
                 r"(sponsor|work pass|visa|permit)", re.I),
      _NO]),
    (re.compile(r"hybrid|days? (in|per) (the )?(office|week)|office model|"
                r"work(ing)? (from|in) (the )?office|hybrid work polic", re.I),
     [_YES, re.compile(r"yes|agree|understand|acknowledge|happy to|accept", re.I)]),
    (re.compile(r"willing to relocate|able to relocate|relocat|"
                r"located in one of the areas", re.I),
     [re.compile(r"would need to relocate|need to relocate|willing to relocate|"
                 r"happy to relocate|open to relocat", re.I), _YES]),
    (re.compile(r"pronoun", re.I), [re.compile(r"he/him", re.I), DECLINE_OPT_RE]),
    # EEO/demographic incl. military/veteran: decline; for veteran-only option
    # sets (no decline choice) fall back to the honest "not a veteran" option.
    (EEO_COMBO_RE,
     [DECLINE_OPT_RE,
      re.compile(r"(am )?not a (protected )?veteran|i am not a veteran|"
                 r"no military|not (a )?member|no, i am not", re.I)]),
    # Bot-detection: "Which best describes you?" -> "I am a human being".
    (HUMAN_VERIFY_RE,
     [re.compile(r"human being|i am (a )?human|^human\b|real person|not a (bot|robot)",
                 re.I)]),
    # Certification / attestation / arbitration consent required to apply.
    (re.compile(r"certif|i (confirm|attest)|true and (accurate|correct)|"
                r"information .{0,20}(true|accurate|correct)|arbitrat|"
                r"resolv\w* by .{0,15}arbitrat|disputes? .{0,20}(arbitrat|resolv)|"
                r"binding arbitration", re.I),
     [AFFIRM_RE, re.compile(r"yes|agree|accept|consent|certify|confirm|"
                            r"i (have )?read|understand|acknowledge", re.I)]),
    # Conflict-of-interest disclosures ("do you have: a) personal/familial
    # relationships / outside business / investments / b)...") -> No.
    (re.compile(r"do you have:?\s*(a\)|any).{0,40}"
                r"(relationship|business activit|investment|conflict)|"
                r"personal/familial|outside business activit|"
                r"conflict of interest", re.I), [_NO]),
    # "Do you consent to ... recording / AI transcription / collecting" -> agree.
    (re.compile(r"do you consent|consent to (us|the|having|being|ai|record)|"
                r"transcrib|transcription|ai (transcription|note)|"
                r"summarize your interview", re.I),
     [re.compile(r"^yes\b|i consent|consent|agree|accept|ok with|understand", re.I)]),
    # "What country are you based in / country of residence / citizenship" -> India.
    # Standalone "Country*" (Greenhouse react-select) also resolves to India.
    (re.compile(r"^country\*?$|^country\b|country .{0,20}(based|residen|live|located)|"
                r"based in .{0,10}country|country of (residence|origin)|"
                r"which country|nationality|citizenship", re.I),
     [re.compile(r"^india$|^india\b", re.I)]),
    # "Which state or province do you currently live in?" -> Karnataka (current).
    (re.compile(r"state or province|which (state|province)|"
                r"state/province .{0,15}(live|residen)|province .{0,10}live", re.I),
     [re.compile(r"^karnataka\b", re.I)]),
    # "Are you currently located/based in the US/UK/...?" -> No (he is in India).
    (re.compile(r"currently (located|based|residing|living) in|"
                r"are you (currently )?in the (us|usa|uk|united)", re.I), [_NO]),
    # "Are you an EU citizen / citizen of an EU member state?" -> No.
    (re.compile(r"eu citizen|citizen of (a|an|the) (eu|european)|"
                r"european union member", re.I), [_NO]),
    # Sanctions / embargo screening (located in / citizen of / resident of
    # Cuba, Iran, North Korea, Syria, Russia, Belarus, Crimea, DNR/LNR ...)
    # -> No (he is in India, Indian citizen). Honest.
    (re.compile(r"belarus|cuba|iran|north korea|dprk|syria|crimea|donetsk|"
                r"luhansk|\bdnr\b|\blnr\b|sanction|embargo|"
                r"following countries? or regions?", re.I), [_NO]),
    (re.compile(r"how did you .{0,20}(hear|find|learn|come across)|"
                r"where did you .{0,20}(hear|find|learn)|"
                r"how.{0,15}(hear|learn) about", re.I),
     [re.compile(r"career page|careers? (site|website)|company (site|website)|"
                 r"company.?s? website|website|job board|linkedin", re.I)]),
    (re.compile(r"notice period", re.I),
     [re.compile(r"1 month|30 day|4 week|one month", re.I)]),
    # Common big-co custom dropdowns.
    (re.compile(r"languages? you (speak|are fluent)|fluent in|"
                r"languages? .{0,20}fluently", re.I),
     [re.compile(r"english", re.I)]),
    (re.compile(r"plan to work remotely|will you .{0,15}work remotely|"
                r"intend to work remotely", re.I), [_NO]),
    (re.compile(r"record and transcribe|brighthire|interview.{0,20}record|"
                r"recorded interview", re.I),
     [re.compile(r"yes|agree|consent|acknowledge|accept|understand|ok", re.I)]),
]


def resolve_react_selects(page: Page) -> list[str]:
    """Answer modern Greenhouse react-select dropdowns (.select__control).

    These (job-boards.greenhouse.io: GoCardless, Bitpanda) render screening
    questions as react-select widgets with NO aria-label — the question is in
    a sibling .select__label. The COMBO_RULES bank only scanned [role=combobox]
    by aria, so these were invisible to it. Here we read the label element,
    match the bank, open the control and click the option. Deterministic /
    honest answers only; unmatched questions are left for review.
    """
    resolved = []
    shells = page.locator(".select__control")
    n = shells.count()
    for i in range(n):
        shell = shells.nth(i)
        try:
            low = (shell.inner_text(timeout=1500) or "").strip().lower()
            # Treat empty / placeholder shells as unanswered. Location
            # type-aheads show "Start typing…", not "Select…", so a plain
            # "select" check skipped them before the location branch ran.
            placeholders = ("select...", "select…", "select an option",
                            "start typing", "search", "choose", "type to",
                            "please select", "-")
            answered = bool(low) and len(low) <= 60 and \
                not any(p in low for p in placeholders)
            if answered:
                continue  # already shows a chosen value
            label = shell.evaluate(
                """el => {
                    let p = el.parentElement;
                    for (let k = 0; k < 8 && p; k++) {
                        const lab = p.querySelector('label, .select__label, legend');
                        if (lab && lab.innerText.trim()) return lab.innerText.trim();
                        p = p.parentElement;
                    }
                    return '';
                }"""
            ) or ""
            # Location/city: type-to-search, so use the value path (not options).
            if LOCATION_CITY_RE.search(label):
                from jobagent import config
                city = (config.answers().get("misc", {}).get("current_city")
                        or "Bengaluru")
                if pick_react_select(page, shell, value=city):
                    resolved.append(f"{label[:60]} -> {city}")
                continue
            # Education react-selects (school/degree/field/year): value path from
            # answers.misc. Degree is option-list (pick "Bachelor's"); school is
            # type-to-search — both go through the value path.
            edu_key = next((k for rgx, k in EDU_FIELD_RES if rgx.search(label)), None)
            if edu_key:
                from jobagent import config
                val = (config.answers().get("misc", {}) or {}).get(edu_key)
                if val and pick_react_select(page, shell, value=str(val)):
                    resolved.append(f"{label[:50]} -> {val}")
                continue
            # Country / state searchable dropdowns: type the honest current
            # answer (India / Karnataka) and commit — regex want_res can't open
            # a type-to-search list. If the value isn't a listed option (e.g.
            # Brex lists only Brazil/Canada/USA/NL/UK/Other), fall back to
            # "Other" / the value-as-option / a decline choice.
            val_key = next((k for rgx, k in VALUE_FIELD_RES if rgx.search(label)), None)
            if val_key:
                from jobagent import config
                val = (config.answers().get("misc", {}) or {}).get(val_key)
                if val and pick_react_select(page, shell, value=str(val)):
                    resolved.append(f"{label[:50]} -> {val}")
                    continue
                # fallback: short fixed lists shown on open — match value or Other
                if val and pick_react_select(
                        page, shell,
                        want_res=[re.compile(re.escape(str(val)), re.I), OTHER_OPT_RE]):
                    resolved.append(f"{label[:50]} -> (value/Other)")
                continue
            opt_res = None
            for q_re, opts in COMBO_RULES:
                if q_re.search(label):
                    opt_res = opts
                    break
            if opt_res is not None:
                if pick_react_select(page, shell, want_res=opt_res):
                    resolved.append(label[:80])
                continue
            # Single-option dropdowns are forced acknowledgments (e.g. "I
            # understand the pay approach") — the only choice is the answer.
            shell.click(timeout=3000)
            page.wait_for_timeout(400)
            opts = page.locator(".select__option, [role='option']")
            if opts.count() == 1:
                opts.first.click(timeout=3000)
                page.wait_for_timeout(300)
                resolved.append(f"{label[:60]} (single-option)")
            else:
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 — submit gate / review catches leftovers
            continue
    return resolved


def resolve_consent_combos(page: Page) -> list[str]:
    """Answer dropdown questions from the deterministic COMBO_RULES bank.

    Covers consent acks, work-authorization (honest No), sponsorship (honest
    Yes), hybrid/relocation willingness, pronouns, EEO declines, source, and
    notice period. Anything not in the bank is left for the mapper/review.
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
            if not label:
                # No aria labelling (Greenhouse embeds) — use the question
                # paragraph from the surrounding field container.
                label = combo.evaluate(
                    """el => {
                        let p = el.parentElement;
                        for (let k = 0; k < 6 && p; k++) {
                            const t = (p.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (t.length > 25) return t.slice(0, 300);
                            p = p.parentElement;
                        }
                        return '';
                    }"""
                ) or ""
            opt_res = None
            for q_re, opts in COMBO_RULES:
                if q_re.search(label):
                    opt_res = opts
                    break
            if opt_res is None:
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
            for want_re in opt_res:
                for j in range(options.count()):
                    if want_re.search(options.nth(j).inner_text(timeout=2000).strip()):
                        pick = options.nth(j)
                        break
                if pick is not None:
                    break
            if pick is None and opt_res[0] is AFFIRM_RE and options.count() == 1:
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


# Yes/No button-pair questions (Ashby renders these as styled buttons, not
# radios, so they never appear in the extracted form schema). Only questions
# with a deterministic honest answer are resolved; anything else is left for
# the submit gate / review queue.
YESNO_ANSWER_NO_RE = re.compile(
    r"(relatives?|family member|friends?) .{0,40}(work|employ)|"
    r"previously (worked|been employed)|worked .{0,20}before|"
    r"criminal|convicted|non.?compete|restrictive covenant|"
    # "do you have a visa which allows you to work WITHOUT sponsorship" -> No
    r"work without (being )?sponsor|allows .{0,30}without sponsor|"
    r"(citizen|indefinite leave|permanent resident)",
    re.IGNORECASE)
YESNO_ANSWER_YES_RE = re.compile(
    r"ai (policy|tools?|approach)|artificial intelligence|"
    r"acknowledg|i (agree|understand|consent|confirm)|"
    r"understanding and agreement|privacy (notice|policy)|"
    r"18 years|legal working age|"
    # honest sponsorship answer is ALWAYS yes (answers.yaml)
    r"require .{0,40}sponsor|need .{0,30}sponsor|"
    # willing to attend office / onsite / hybrid (relocation.open_to_onsite)
    r"attend .{0,60}(office|days? per week)|"
    r"(work|commute|come) .{0,40}(onsite|on-site|office|hybrid)|"
    r"willing to relocate|able to relocate",
    re.IGNORECASE)

# Ashby select-style option lists (clickable divs, not form controls):
# question-pattern -> option-pattern to click. Deterministic & honest only.
OPTION_LIST_PICKS = [
    # right-to-work status -> the "require visa sponsorship" option, always
    (re.compile(r"right to work|work status|work authoris|work authoriz", re.I),
     re.compile(r"require .{0,40}sponsor", re.I)),
    # how-did-you-hear -> career page
    (re.compile(r"how did you (hear|find)|where did you (hear|find)|"
                r"influenced your decision", re.I),
     re.compile(r"career page|careers? (site|website)|company website", re.I)),
]


# Closed dropdown shells ("Please select…") whose options render on click.
# question-pattern -> option-pattern. Honest deterministic picks only.
SHELL_PICKS = [
    # start date: 30-day notice + visa processing -> the 1-3 month-ish option
    (re.compile(r"when would you like to start|earliest .{0,20}start|"
                r"available to start|start a new role", re.I),
     re.compile(r"(1|2|one|two)\s*(-|–|to)\s*3?\s*month|within .{0,8}month|"
                r"1-3|2-3|month", re.I)),
    (re.compile(r"how did you (hear|find)|where did you (hear|find)", re.I),
     re.compile(r"career page|careers? (site|website)|company website", re.I)),
    (re.compile(r"pronouns", re.I), re.compile(r"he/him", re.I)),
]


def resolve_select_shells(page: Page) -> list[str]:
    """Open unanswered 'Please select…' dropdowns and pick the honest option."""
    resolved = []
    shells = page.locator(
        "div[class*='select']:visible, button[class*='select']:visible")
    for i in range(shells.count()):
        sh = shells.nth(i)
        try:
            txt = (sh.inner_text(timeout=1200) or "").strip().lower()
            if "select" not in txt or len(txt) > 60:
                continue  # already answered or not a placeholder shell
            question = sh.evaluate(
                """el => {
                    let p = el.parentElement;
                    for (let k = 0; k < 6 && p; k++) {
                        const t = (p.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t.length > 25) return t.slice(0, 250);
                        p = p.parentElement;
                    }
                    return '';
                }"""
            ) or ""
            for q_re, opt_re in SHELL_PICKS:
                if not q_re.search(question):
                    continue
                sh.click(timeout=3000)
                time.sleep(0.6)
                opts = page.locator("div[class*='_option_']:visible, "
                                    "[role='option']:visible")
                clicked = False
                for j in range(opts.count()):
                    t = (opts.nth(j).inner_text(timeout=1200) or "").strip()
                    if opt_re.search(t):
                        opts.nth(j).click(timeout=3000)
                        resolved.append(f"{question[:50]} -> {t[:40]}")
                        clicked = True
                        break
                if not clicked:
                    page.keyboard.press("Escape")
                time.sleep(0.3)
                break
        except Exception:  # noqa: BLE001
            continue
    return resolved


def resolve_option_lists(page: Page) -> list[str]:
    """Click the honest option in Ashby's custom div-option widgets."""
    resolved = []
    for q_re, opt_re in OPTION_LIST_PICKS:
        opts = page.locator("div[class*='_option_']:visible")
        for i in range(opts.count()):
            opt = opts.nth(i)
            try:
                text = (opt.inner_text(timeout=1500) or "").strip()
                if not opt_re.search(text):
                    continue
                question = opt.evaluate(
                    """el => {
                        let p = el.parentElement;
                        for (let k = 0; k < 6 && p; k++) {
                            const t = (p.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (t.length > 60) return t.slice(0, 300);
                            p = p.parentElement;
                        }
                        return '';
                    }"""
                ) or ""
                if not q_re.search(question):
                    continue
                # Skip if this widget already has a selection.
                if opt.evaluate(
                    "el => Array.from(el.parentElement.children).some(c => "
                    "(c.className || '').includes('selected'))"
                ):
                    continue
                opt.click(timeout=3000)
                time.sleep(0.4)
                resolved.append(f"{text[:60]}")
                break
            except Exception:  # noqa: BLE001
                continue
    return resolved


def resolve_consent_checkboxes(page: Page) -> list[str]:
    """Tick required consent checkboxes ("By checking this box, I consent...").

    Plain <input type=checkbox> consents (MongoDB/Asana GDPR data-consent) — the
    combobox resolver doesn't cover them and the mapper defers consents. Only
    ticks affirmative consent/agreement boxes; never marketing opt-ins.
    """
    resolved = []
    boxes = page.locator("input[type=checkbox]")
    for i in range(boxes.count()):
        box = boxes.nth(i)
        try:
            if box.is_checked():
                continue
            label = box.evaluate(
                """el => {
                    if (el.id) { const l=document.querySelector(`label[for="${el.id}"]`);
                        if (l) return l.innerText.trim(); }
                    const w = el.closest('label'); if (w) return w.innerText.trim();
                    let p = el.parentElement;
                    for (let k=0;k<4&&p;k++){ const t=(p.textContent||'').replace(/\\s+/g,' ').trim();
                        if (t.length>15) return t.slice(0,200); p=p.parentElement; }
                    return '';
                }"""
            ) or ""
            # consent/acknowledgement yes — but NOT marketing opt-ins.
            if re.search(r"stay up to date|marketing|newsletter|similar jobs|"
                         r"receive (alerts|emails|updates)|opt[- ]?in to", label, re.I):
                continue
            if CONSENT_RE.search(label) or re.search(
                    r"by checking|i consent|i agree|i acknowledge|i confirm|"
                    r"consent to .{0,30}(collect|process|stor)|i have read|"
                    r"privacy (policy|notice)|terms", label, re.I):
                try:
                    box.check(timeout=4000)
                except Exception:  # noqa: BLE001
                    box.check(timeout=4000, force=True)
                resolved.append(label[:70])
        except Exception:  # noqa: BLE001
            continue
    return resolved


def resolve_yesno_buttons(page: Page) -> list[str]:
    """Answer Ashby-style Yes/No button groups with deterministic answers."""
    resolved = []
    groups = page.locator("div[class*=_yesno_]")
    for i in range(groups.count()):
        g = groups.nth(i)
        try:
            # Skip if an option is already selected.
            sel = g.evaluate(
                "el => Array.from(el.querySelectorAll('button')).some(b => "
                "(b.className || '').includes('selected') || "
                "b.getAttribute('aria-pressed') === 'true' || "
                "b.getAttribute('aria-checked') === 'true')"
            )
            if sel:
                continue
            # The question text lives on an ancestor of the buttons container.
            label = g.evaluate(
                """el => {
                    let p = el.parentElement;
                    for (let k = 0; k < 5 && p; k++) {
                        const t = (p.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t.length > 20) return t.slice(0, 300);
                        p = p.parentElement;
                    }
                    return '';
                }"""
            ) or ""
            if YESNO_ANSWER_NO_RE.search(label):
                answer = "No"
            elif YESNO_ANSWER_YES_RE.search(label):
                answer = "Yes"
            else:
                continue  # not a question we can answer deterministically
            g.get_by_role("button", name=answer, exact=True).first.click(timeout=4000)
            time.sleep(0.3)
            resolved.append(f"{label[:70]} -> {answer}")
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
    if outcome == "spam":
        # Ashby's spam screen explicitly invites resubmission. One slow,
        # human-paced retry; a second flag means the session is burned.
        time.sleep(random.uniform(8, 15))
        try:
            submit_loc.click(timeout=10000)
        except Exception:  # noqa: BLE001
            return "spam", evidence
        outcome, evidence = watch_outcome(page, url_before)
        if outcome == "spam":
            return "spam", evidence
    if outcome == "error":
        # Validation errors mean nothing was submitted — async state (file
        # uploads, react-select commits) often settles just after the first
        # click. One re-submit is safe; a second failure goes to review.
        time.sleep(4)
        try:
            submit_loc.click(timeout=10000)
        except Exception:  # noqa: BLE001
            # The button detaching often means the FIRST click navigated to
            # the confirmation page — look before declaring uncertainty.
            outcome, evidence = watch_outcome(page, url_before, seconds=8)
            if outcome == "confirmed":
                return outcome, evidence
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
        if SPAM_RE.search(body):
            return "spam", SPAM_RE.search(body).group(0)
        if CONFIRMATION_RE.search(body):
            return "confirmed", CONFIRMATION_RE.search(body).group(0)
        if ERROR_RE.search(body):
            return "error", ERROR_RE.search(body).group(0)
    if url_before and page.url != url_before:
        return "confirmed", f"url changed to {page.url}"
    return "uncertain", ""
