"""LLM email composition (first touch + followup) via jobagent.llm.ask_json."""
from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from jobagent import config
from jobagent.llm import ask_json

MAX_SUBJECT_CHARS = 60
_HYPE = ("passionate", "rockstar", "ninja", "guru", "synergy", "thrilled")


class EmailDraft(BaseModel):
    subject: str
    body: str


class FollowupDraft(BaseModel):
    body: str


def _company_name(conn: sqlite3.Connection, job_row) -> str:
    if job_row["company_id"]:
        row = conn.execute(
            "SELECT name FROM companies WHERE id=?", (job_row["company_id"],)
        ).fetchone()
        if row:
            return row["name"]
    return "the company"


def _sanitize(draft: EmailDraft) -> dict:
    subject = " ".join(draft.subject.split())
    if len(subject) > MAX_SUBJECT_CHARS:
        subject = subject[: MAX_SUBJECT_CHARS - 1].rstrip() + "…"
    body = draft.body.strip()
    return {"subject": subject, "body": body}


def compose_email(conn: sqlite3.Connection, job_row, contact_row) -> dict:
    """Compose a <=120-word first-touch email for (job, contact).

    Returns {"subject": ..., "body": ...}.
    """
    p = config.profile()
    ident = p["identity"]
    company = _company_name(conn, job_row)
    description = (job_row["description"] or "")[:2000]

    prompt = f"""You are writing a short cold outreach email on behalf of a job seeker.

SENDER (everything below is true; never invent anything):
- Name: {ident["full_name"]}
- India-based senior backend engineer at Adobe; builds Kafka/AWS distributed
  systems handling ~700M events/day. 4+ years experience. IIT Kharagpur grad.
- He needs visa sponsorship and is willing to relocate. Say this plainly.
- LinkedIn: {ident["linkedin"]}

RECIPIENT: {contact_row["email"]} at {company} (likely recruiting/hiring).

ROLE HE IS INTERESTED IN: "{job_row["title"]}" at {company}.

JOB DESCRIPTION (excerpt):
{description}

Write the email:
- 120 words MAX in the body. Shorter is better.
- Reference the SPECIFIC role title and ONE specific, concrete thing about the
  company or role drawn from the job description above.
- State plainly that he is an India-based senior backend engineer (Adobe,
  Kafka/AWS at 700M events/day) seeking sponsorship + relocation.
- End by asking for a short conversation.
- No links in the body except his LinkedIn URL.
- Honest and warm. Zero hype words ({", ".join(_HYPE)} are all banned).
- Sign off with his first name.
- Subject line: at most {MAX_SUBJECT_CHARS} characters and it must mention the role."""

    draft = ask_json(prompt, EmailDraft, model="sonnet")
    return _sanitize(draft)


def compose_followup(conn: sqlite3.Connection, job_row, contact_row,
                     original_subject: str, original_body: str) -> dict:
    """Compose a 2-sentence followup to an unanswered first touch.

    Returns {"subject": "Re: ...", "body": ...}.
    """
    company = _company_name(conn, job_row)
    p = config.profile()
    prompt = f"""A job seeker ({p["identity"]["full_name"]}) sent the email below to
{contact_row["email"]} at {company} about the "{job_row["title"]}" role and got
no reply after a week.

ORIGINAL EMAIL:
Subject: {original_subject}
{original_body[:1200]}

Write a followup that is EXACTLY 2 sentences: a polite nudge restating interest
in the specific role and the ask for a short conversation. Honest, warm, no
hype words, no links, no guilt-tripping. Sign off with "{p["identity"]["first_name"]}"."""

    draft = ask_json(prompt, FollowupDraft, model="sonnet")
    subject = original_subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return {"subject": subject, "body": draft.body.strip()}
