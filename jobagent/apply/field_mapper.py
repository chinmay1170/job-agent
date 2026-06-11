"""Map a form schema to a fill plan via claude -p, confidence-gated.

The integrity rules live in the prompt here. Legal/visa/salary/numeric
questions may only be answered verbatim from answers.yaml; anything required
that isn't grounded in config gets confidence 0 and the application is queued
for human review instead of guessed.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from jobagent import config
from jobagent.llm import ask_json


class FillAction(BaseModel):
    selector: str
    action: Literal["fill", "select", "check", "skip"]
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
3. Salary, notice period, start date, years of experience: ONLY from
   answers.yaml. If the value there is null or contains "REVIEW ME",
   action=skip with confidence 0.
4. EEO / demographics / gender / race / veteran / disability: choose the
   "decline to self-identify" style option when present; if the question is
   optional and no decline option exists, action=skip with confidence 0.9.
5. Free-text "why us" / motivation questions: derive 2-4 sentences from the
   cover letter text only. confidence 0.85.
6. For select controls, `value` must be EXACTLY one of the listed options.
   For radio/checkbox groups, pick the control whose option_value/label matches
   and use action=check.
7. Anything you cannot ground in the facts above: action=skip. If that control
   is required, confidence MUST be 0.
8. Never invent employers, dates, references, IDs, or documents.
9. File-upload controls: action=skip (handled separately by the runner).

Return every control from the schema in `actions` (same selectors), including
the skipped ones."""


def plan_fill(schema: list[dict], cover_text: str) -> FillPlan:
    prof = config.profile()
    identity = dict(prof["identity"])
    identity["headline"] = prof["headline"]
    return ask_json(
        PROMPT.format(
            identity=json.dumps(identity, ensure_ascii=False),
            answers=json.dumps(config.answers(), ensure_ascii=False),
            cover=cover_text[:2000],
            schema=json.dumps(schema, ensure_ascii=False),
        ),
        FillPlan,
        model="sonnet",
        timeout=240,
    )


def unmet_required(schema: list[dict], plan: FillPlan, threshold: float) -> list[str]:
    """Labels of required controls the plan can't confidently satisfy."""
    by_sel = {a.selector: a for a in plan.actions}
    problems = []
    for ctl in schema:
        if ctl.get("type") == "file":
            continue  # resume/cover uploads handled by the runner
        if not ctl.get("required"):
            continue
        act = by_sel.get(ctl["selector"])
        if act is None or act.action == "skip" or act.confidence < threshold:
            problems.append(ctl.get("label") or ctl["selector"])
    return problems
