"""Outreach sender. `jobagent outreach run [--shadow]` lands here.

Safety model (in order, before every send):
  - killswitch.check()
  - daily counter 'emails' < caps.emails_per_day (and emails_hard_max)
  - min gap caps.min_email_gap_minutes between sends (jitter ±2min)
  - send window caps.email_send_window (IST approximation = local time)
  - weekends skipped unless caps.email_weekends
  - SHADOW mode (--shadow or caps.dry_run): recipient rewritten to the user's
    own inbox, subject prefixed "[SHADOW to {real}] "
"""
from __future__ import annotations

import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from jobagent import config, db, killswitch
from jobagent.outreach import compose, contacts, gmail

SHADOW_RECIPIENT = config.identity().get("email", "")
_GUESS_PRIORITY = {"careers": 0, "jobs": 1, "talent": 2}


def _within_send_window(caps_cfg: dict) -> tuple[bool, str]:
    now = datetime.now()  # local time ~= IST; fine as approximation
    if not caps_cfg.get("email_weekends", False) and now.weekday() >= 5:
        return False, "weekend (caps.email_weekends=false)"
    win = caps_cfg.get("email_send_window") or {}
    start, end = win.get("start_hour", 9), win.get("end_hour", 18)
    if not (start <= now.hour < end):
        return False, f"outside send window {start}:00-{end}:00 (now {now:%H:%M})"
    return True, ""


def _contact_sort_key(row) -> tuple:
    """Prefer real harvested addresses, then careers@ > jobs@ > talent@."""
    src = 0 if row["source"] != "careers_guess" else 1
    local = row["email"].split("@", 1)[0]
    return (src, _GUESS_PRIORITY.get(local, 9), row["contact_id"])

def _first_touch_candidates(conn: sqlite3.Connection, caps_cfg: dict) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ct.id AS contact_id, ct.email, ct.company_id, ct.source,
               j.id AS job_id
        FROM contacts ct
        JOIN companies c ON ct.company_id = c.id
        JOIN jobs j ON j.company_id = c.id
        WHERE j.status IN ('apply_queued','applied')
          AND c.blocklisted = 0
          AND (ct.mx_valid IS NULL OR ct.mx_valid = 1)
          AND NOT EXISTS (SELECT 1 FROM outreach o WHERE o.contact_id = ct.id)
          -- never outreach to a company that has already rejected us
          AND NOT EXISTS (
            SELECT 1 FROM applications a2 JOIN jobs j2 ON j2.id = a2.job_id
            WHERE j2.company_id = c.id AND a2.status = 'rejected')
        ORDER BY j.id DESC
        """
    ).fetchall()

    per_company_cap = caps_cfg.get("per_company_lifetime", 2)
    by_contact: dict[int, sqlite3.Row] = {}
    for r in rows:  # first row per contact = most recent job at that company
        by_contact.setdefault(r["contact_id"], r)

    picked: list[dict] = []
    seen_companies: set[int] = set()
    for r in sorted(by_contact.values(), key=_contact_sort_key):
        cid = r["company_id"]
        if cid in seen_companies:
            continue  # one contact per company per run
        prior = conn.execute(
            "SELECT COUNT(*) FROM outreach o JOIN contacts x ON o.contact_id = x.id "
            "WHERE x.company_id = ?",
            (cid,),
        ).fetchone()[0]
        if prior >= per_company_cap:
            continue
        seen_companies.add(cid)
        picked.append({"kind": "first_touch", "contact_id": r["contact_id"],
                       "email": r["email"], "job_id": r["job_id"]})
    return picked


def _followup_candidates(conn: sqlite3.Connection, caps_cfg: dict) -> list[dict]:
    days = caps_cfg.get("followup_after_days", 7)
    rows = conn.execute(
        f"""
        SELECT o.id AS first_id, o.contact_id, o.job_id, o.subject, o.body,
               o.gmail_thread_id, ct.email
        FROM outreach o
        JOIN contacts ct ON o.contact_id = ct.id
        WHERE o.kind = 'first_touch' AND o.status = 'sent'
          AND o.sent_at <= datetime('now', '-{int(days)} days')
          AND NOT EXISTS (SELECT 1 FROM outreach f
                          WHERE f.contact_id = o.contact_id AND f.kind = 'followup')
        ORDER BY o.sent_at
        """
    ).fetchall()
    return [
        {"kind": "followup", "contact_id": r["contact_id"], "email": r["email"],
         "job_id": r["job_id"], "first_subject": r["subject"], "first_body": r["body"],
         "thread_id": r["gmail_thread_id"]}
        for r in rows
    ]


def run_outreach(shadow: bool = False) -> None:
    caps_cfg = config.caps()
    if caps_cfg.get("dry_run", True):
        shadow = True

    try:
        killswitch.check()
    except killswitch.KilledError as e:
        print(str(e))
        raise SystemExit(1)

    try:
        svc = gmail.service()
    except gmail.GmailNotConfigured as e:
        print(str(e))
        raise SystemExit(1)

    ok, why = _within_send_window(caps_cfg)
    if not ok:
        print(f"outreach: not sending — {why}")
        return

    conn = db.connect()
    harvested = contacts.harvest_contacts(conn)
    if harvested:
        print(f"outreach: harvested {harvested} new contact(s)")

    queue = _followup_candidates(conn, caps_cfg) + _first_touch_candidates(conn, caps_cfg)
    if not queue:
        print("outreach: nothing to send")
        return

    daily_cap = min(caps_cfg.get("emails_per_day", 10), caps_cfg.get("emails_hard_max", 20))
    gap_seconds = caps_cfg.get("min_email_gap_minutes", 8) * 60
    sent_this_run = 0

    for item in queue:
        remaining = daily_cap - db.counter_get(conn, "emails")
        if remaining <= 0:
            print(f"outreach: daily email cap reached ({daily_cap})")
            break

        try:
            killswitch.check()  # before EVERY send
        except killswitch.KilledError as e:
            print(str(e))
            raise SystemExit(1)

        job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (item["job_id"],)).fetchone()
        contact_row = conn.execute(
            "SELECT * FROM contacts WHERE id=?", (item["contact_id"],)
        ).fetchone()
        if not job_row or not contact_row:
            continue

        if sent_this_run > 0:
            pause = max(0, gap_seconds + random.uniform(-120, 120))
            print(f"outreach: sleeping {pause / 60:.1f}min (min gap)")
            time.sleep(pause)

        try:
            if item["kind"] == "followup":
                draft = compose.compose_followup(
                    conn, job_row, contact_row, item["first_subject"], item["first_body"]
                )
                thread_id = item.get("thread_id")
                attach: list[Path] = []
            else:
                draft = compose.compose_email(conn, job_row, contact_row)
                thread_id = None
                attach = []
                app = conn.execute(
                    "SELECT resume_path FROM applications WHERE job_id=?", (item["job_id"],)
                ).fetchone()
                resume = config.resume_pdf(app["resume_path"] if app else None)
                if resume and Path(resume).exists():
                    attach = [Path(resume)]

            real_to = item["email"]
            to, subject = real_to, draft["subject"]
            if shadow:
                to = SHADOW_RECIPIENT
                subject = f"[SHADOW to {real_to}] {subject}"

            result = gmail.send_message(
                to, subject, draft["body"], attachments=attach,
                thread_id=thread_id, svc=svc,
            )
        except Exception as e:  # one bad send must not kill the run
            print(f"outreach: send failed for {item['email']}: {e}")
            db.log_event(conn, "outreach", None, "outreach_send_failed",
                         {"contact_id": item["contact_id"], "job_id": item["job_id"],
                          "kind": item["kind"], "error": str(e)[:300]})
            continue

        conn.execute(
            "INSERT INTO outreach (contact_id, job_id, kind, subject, body, status, "
            "gmail_message_id, gmail_thread_id, sent_at) "
            "VALUES (?,?,?,?,?,'sent',?,?,datetime('now'))",
            (item["contact_id"], item["job_id"], item["kind"], subject,
             draft["body"], result["id"], result["threadId"]),
        )
        conn.commit()
        db.counter_bump(conn, "emails")
        db.log_event(conn, "outreach", conn.execute(
            "SELECT id FROM outreach WHERE gmail_message_id=?", (result["id"],)
        ).fetchone()["id"], "outreach_sent",
            {"to": to, "real_to": real_to, "kind": item["kind"],
             "shadow": shadow, "job_id": item["job_id"]})
        sent_this_run += 1
        mode = "SHADOW " if shadow else ""
        print(f"outreach: {mode}{item['kind']} sent to {to} ({real_to}) — {subject}")

    print(f"outreach: done, {sent_this_run} email(s) sent this run")
