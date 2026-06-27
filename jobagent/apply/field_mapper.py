"""Map a form schema to a fill plan via claude -p, confidence-gated.

The integrity rules live in the prompt here. Legal/visa/salary/numeric
questions may only be answered verbatim from answers.yaml; anything required
that isn't grounded in config gets confidence 0 and the application is queued
for human review instead of guessed.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from jobagent import config
from jobagent.llm import ask_json


class FillAction(BaseModel):
    selector: str
    action: Literal["fill", "select", "check", "skip", "react_select"]
    value: str = ""
    confidence: float = Field(ge=0, le=1)
    source: str = ""        # which config key / profile fact grounds this
    label: str = ""
    required: bool = False


class FillPlan(BaseModel):
    actions: list[FillAction]
    notes: list[str] = []


PROMPT = """You are filling a job application form for Chinmay Krishna. Map each
form control to an action. You must be conservative and honest.

## Candidate facts (the ONLY permitted sources of answers)
Identity/profile:
{identity}

Canonical screening answers (answers.yaml):
{answers}

Cover letter text (for "why do you want to work here" style questions):
{cover}

## Form controls (from the live page)
{schema}

## Rules — follow exactly
1. Standard identity fields (name, email, phone, location, LinkedIn): fill from
   identity. confidence 0.95+.
2. Work authorization / visa / sponsorship questions: answer ONLY from
   answers.yaml, verbatim meaning. He REQUIRES sponsorship and is NOT currently
   authorized to work in US/EU/UK/AU/UAE. Never claim authorization he lacks.
   If a yes/no or select option exists matching the honest answer, choose it
   with confidence 0.9+. If no option matches honestly, action=skip confidence 0.
3. Salary expectations: prefer answers.this_job.salary_benchmark (the
   web-searched market rate for THIS role) — use its "ask" number. Fall back
   to the regional expected_salary_* in compensation. Free-text fields: write
   it the way a LOCAL CANDIDATE would type it — "£103,000" (UK), "€115,000"
   (EU), "S$185,000" (SG), "A$175,000" (AU), "AED 480,000" (UAE),
   "$185,000" (US). NEVER ISO codes like "GBP 103,000" — that reads as
   bot-fill. Numeric-only fields: bare number in the job's local currency.
   Notice period, start date, years of experience: ONLY from answers.yaml.
   If a value is null or contains "REVIEW ME", action=skip with confidence 0.
4. EEO / demographics / gender / race / veteran / disability: choose the
   "decline to self-identify" style option when present; if the question is
   optional and no decline option exists, action=skip with confidence 0.9.
4a2. Tech-stack screeners ("Do you have experience with X / exposure to Y?"):
    answer Yes IFF every technology asked about is in
    skills_confirmed_exposure (treat close aliases as matches: AWS covers
    Lambda/EKS/S3, GCP covers Google Cloud). If ANY asked technology is
    absent from that list, answer No honestly. confidence 0.9.
4b. "Do you have relatives/friends working at this company?" → No (from
    misc.relatives_at_company). confidence 0.95.
4c. "AI policy / AI-assisted interviews" agreement checkboxes or yes/no
    questions → agree/yes (misc.ai_policy_agreement). confidence 0.95.
4d. "How did you hear about this role / where did you find this job?" →
    misc.how_did_you_hear ("Company careers page"). confidence 0.95.
4e. Location / city autocomplete fields → misc.current_city + ", India"
    ("Bengaluru, India"). confidence 0.9.
4e2. CURRENT-location questions (answer with where he lives NOW — honest):
    - "what country are you based in / country of residence / where do you
      live" -> misc.current_country ("India"). confidence 0.95.
    - "which state or province do you currently live in" ->
      misc.current_state ("Karnataka"). confidence 0.9.
    - "are you currently located/based in the US / in <country>?" -> No
      (misc.currently_located_in_us is false; he is in India). confidence 0.9.
    - "if located in the US, what city/state" -> action=skip (he is not). conf 0.9.
    - nationality / citizenship / "are you a citizen of X" -> answer from
      misc.citizenship_country ("India") / "are you an EU citizen" -> No
      (misc.eu_citizen). NEVER claim a citizenship he lacks. confidence 0.9.
4e3. PREFERRED / future work-location ("preferred office location", "in what
    cities are you available to work", "where will you be based for this role")
    -> the ROLE's city/region from answers.this_job.location (he relocates for
    it). If unknown, the role's country. confidence 0.85.
4g. Military / veteran status -> misc.veteran_status ("I am not a protected
    veteran") or the "I am not a veteran" / decline option. confidence 0.9.
4h. CERTIFICATION / attestation required to apply ("I certify all information
    provided is true and accurate", "I confirm the above is correct") ->
    agree/check (it is true). For ARBITRATION / dispute-resolution / "by
    clicking accept you agree disputes will be resolved by..." consent that is
    a CONDITION OF APPLYING -> agree/check (same as terms & conditions).
    confidence 0.9.
4i. MARKETING opt-ins (WhatsApp/SMS/email marketing, "may we contact you about
    other roles / news") -> these are OPTIONAL: leave unchecked / answer No
    (misc.marketing_opt_in is false). NEVER let one block submission. conf 0.9.
4j. EDUCATION-HISTORY text/select fields (these appear as separate controls,
    often required, on Greenhouse/Workday education blocks) -> fill from
    profile.education[0]:
    - "school" / "university" / "institution" / "most recent school" ->
      misc.education_school ("IIT Kharagpur")
    - "degree" / "degree type" / "highest level of education" / "most recent
      degree" -> misc.education_degree ("Bachelor's Degree"); if it's a
      FREE-TEXT degree field use misc.education_degree_full
    - "field of study" / "discipline" / "major" -> misc.education_field
    - "start date year" / "end date year" / "graduation year" ->
      misc.education_start_year / misc.education_end_year
    - "GPA" -> misc.education_gpa. confidence 0.9.
4k. "When can you start / earliest start date / availability to start" ->
    logistics.earliest_start_date (or logistics.notice_period for a duration
    field). confidence 0.9.
4f. Office attendance / commutable distance / hybrid days-in-office
    questions → Yes (he is relocating for the role and is open to
    onsite/hybrid per relocation.*). confidence 0.9.
5. Free-text "why us" / motivation questions: derive 2-4 sentences from the
   cover letter text only. confidence 0.85.
6. For select controls, `value` must be EXACTLY one of the listed options.
   For radio/checkbox groups, pick the control whose option_value/label matches
   and use action=check.
7. Anything you cannot ground in the facts above: action=skip. If that control
   is required, confidence MUST be 0.
7b. Common recurring custom questions (answer confidently, 0.9):
   - "languages you speak / fluent in" -> "English" (he is fluent in English).
   - "cities you are available to work in / where will you be based" -> the
     ROLE's city/region from answers.this_job.location (he relocates for it).
   - "do you plan to work remotely / will you work remotely" -> "No" (he wants
     onsite/hybrid + relocation, not remote).
   - interview-recording / BrightHire / "record and transcribe" consent,
     and any "I agree/acknowledge" policy -> the affirmative/agree option.
8. Never invent employers, dates, references, IDs, or documents.
9. File-upload controls: action=skip (handled separately by the runner).
10. Controls with "widget": "react_select" are fixed-option dropdowns handled
    by a dedicated picker. For LEGITIMATELY groundable custom ones use
    action="react_select" with `value` = the single best option text:
      - highest level of education -> from identity/profile (e.g. "Bachelor's Degree")
      - pay-range / salary band -> answers.this_job.salary_benchmark or compensation
      - "where will you be located for this role / by your start date" -> the
        ROLE's location city from answers.this_job.location (the candidate
        relocates for the role), e.g. "France", "Paris", "London".
    ALWAYS action="skip" (a deterministic resolver owns these — do NOT emit
    react_select/fill for them, or you corrupt the widget):
      - work-authorization, visa-sponsorship ("require sponsorship now or in
        the future"), right-to-work, nationality, tax-residency
      - consent / privacy / acknowledgement / interview-recording
      - ANY EEO/demographic (gender, race, age, disability, sexual orientation,
        veteran, neurodiversity, pronouns)
      - "Location (City)" / current city, "How did you hear/learn about this
        job", notice period, hybrid/relocation/remote questions.
    EXCEPTION — DO emit action="react_select" (a resolver does NOT own these):
      - "preferred office location" / "which office" -> the option matching the
        ROLE's city/region from answers.this_job.location (he relocates for the
        role); if no city is known, pick the first listed office. confidence 0.85.
10b. "If located in the US, what city/state do you reside" and similar
    conditional fields PREMISED on being in the US -> he is NOT in the US.
    If it is a free-text field, action="fill" value="N/A — based in India".
    If it is required and a "N/A"/"not applicable" option exists, pick it.
    confidence 0.9.

Return every control from the schema in `actions` (same selectors), including
the skipped ones."""


def plan_fill(schema: list[dict], cover_text: str,
              job_facts: dict | None = None) -> FillPlan:
    prof = config.profile()
    identity = dict(prof["identity"])
    identity["headline"] = prof["headline"]
    answers = dict(config.answers())
    if job_facts:
        # Per-job market data (e.g. web-searched salary benchmark) outranks
        # the static regional defaults in answers.yaml.
        answers["this_job"] = job_facts
    return ask_json(
        PROMPT.format(
            identity=json.dumps(identity, ensure_ascii=False),
            answers=json.dumps(answers, ensure_ascii=False),
            cover=cover_text[:2000],
            schema=json.dumps(schema, ensure_ascii=False),
        ),
        FillPlan,
        model="sonnet",
        timeout=240,
    )


class HumanCheck(BaseModel):
    human_like: bool          # would a recruiter believe a person filled this?
    issues: list[str] = []    # bot tells found
    rewrites: list[FillAction] = []  # improved values for flagged free-text fields


VERIFY_PROMPT = """You are a skeptical recruiter reviewing a submitted job
application for the role "{title}". Decide whether these answers look like a
REAL HUMAN candidate wrote them, or like an auto-apply bot.

Question -> planned answer:
{qa}

Bot tells to check for:
- Templated/robotic phrasing, answers that don't actually address the question
- Identical boilerplate reused across different questions
- Wrong currency or implausible numbers for the location
- Over-formal filler ("I am writing to express my keen interest...")
- Answers a human would never type into that field

Rules:
- human_like=false ONLY for problems that would make a recruiter suspect
  automation; normal brief answers are fine (humans are terse on forms).
- For flagged FREE-TEXT answers, provide `rewrites` with the same selector and
  a natural, specific, honest replacement (same facts — NEVER new claims).
- NEVER rewrite factual/legal fields (visa, salary, notice period, name,
  email): if one of those is wrong, list it in issues and set human_like=false.
"""


def verify_human(plan: FillPlan, title: str) -> HumanCheck:
    qa = "\n".join(
        f"- [{a.selector}] {a.label or '(unlabelled)'} -> {a.value!r}"
        for a in plan.actions if a.action in ("fill", "select") and a.value
    )
    if not qa:
        return HumanCheck(human_like=True)
    return ask_json(VERIFY_PROMPT.format(title=title, qa=qa), HumanCheck,
                    model="sonnet", timeout=180)


def unmet_required_live(page, schema: list[dict], threshold: float) -> list[str]:
    """Required controls still empty on the LIVE page, after all fillers ran.

    Checks real DOM state rather than the plan, so a field answered by ANY
    filler (mapper, react-select resolver, yes/no resolver, …) counts as
    satisfied. This is the gate that decides submit-vs-review.
    """
    upload_re = re.compile(r"upload file|resume|\bcv\b|cover letter", re.IGNORECASE)
    problems: list[str] = []
    seen_groups: set = set()

    for ctl in schema:
        if not ctl.get("required"):
            continue
        ctype = ctl.get("type")
        label = ctl.get("label") or ""
        sel = ctl["selector"]
        if ctype == "file" or upload_re.search(label):
            continue  # uploads handled by upload_files()

        # radio/checkbox group: satisfied if any option is checked live
        if ctype in ("radio", "checkbox"):
            key = label.split(" :: ")[0]
            group = ctl.get("group") or ""
            if key in seen_groups:
                continue
            seen_groups.add(key)
            try:
                checked = page.evaluate(
                    "(name) => !!document.querySelector(`input[name=\"${name}\"]:checked`)",
                    group,
                ) if group else page.locator(sel).first.is_checked(timeout=1500)
            except Exception:  # noqa: BLE001
                checked = False
            if not checked:
                problems.append(key)
            continue

        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue  # control vanished (conditional field) — not our gate
            if ctl.get("widget") == "react_select":
                # satisfied when the shell no longer reads "Select…"/empty
                shell_txt = (loc.evaluate(
                    "el => { const c = el.closest('div[class*=select__control]') "
                    "|| el.parentElement; return (c?.textContent || '').trim(); }"
                ) or "").lower()
                if not shell_txt or "select" in shell_txt and len(shell_txt) < 18:
                    problems.append(label or sel)
                continue
            val = (loc.input_value(timeout=1500) or "").strip()
            if not val:
                problems.append(label or sel)
        except Exception:  # noqa: BLE001 — unreadable control: flag for human
            problems.append(label or sel)
    return problems
