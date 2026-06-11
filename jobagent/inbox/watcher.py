"""Inbox watcher. `jobagent inbox scan` lands here.

Reads recent Gmail threads, correlates them to outreach rows (by thread id)
or applications (by sender domain / company name), batch-classifies the
new/changed ones with one haiku call, upserts inbox_threads, and applies
side effects (application status, macOS notification, Gmail labels).

Deliberately conservative: threads that can't be correlated AND don't look
job-search-related (subject regex + non-personal sender) are skipped entirely.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
from typing import Literal

from pydantic import BaseModel

from jobagent import db, killswitch
from jobagent.llm import LLMError, ask_json
from jobagent.outreach import gmail

SCAN_QUERY = "newer_than:2d -category:promotions -category:social in:inbox"

JOBSEARCH_SUBJECT_RE = re.compile(
    r"application|interview|recruit|position|role|opportunity", re.IGNORECASE
)
EMAIL_IN_HEADER_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.\w+")

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in",
    "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com",
    "me.com", "proton.me", "protonmail.com", "aol.com", "rediffmail.com",
    "zoho.com", "gmx.com",
}

LABEL_INTERVIEW = "JobAgent/Interview"
LABEL_REJECTED = "JobAgent/Rejected"
LABEL_REPLY = "JobAgent/Reply"

Classification = Literal["interview_request", "rejection", "info_request", "auto_ack", "other"]


class ThreadVerdict(BaseModel):
    thread_id: str
    classification: Classification


class ThreadBatch(BaseModel):
    threads: list[ThreadVerdict]


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _email_of(header_value: str) -> str:
    m = EMAIL_IN_HEADER_RE.search(header_value or "")
    return m.group(0).lower() if m else ""


def _ensure_label(svc, name: str, cache: dict[str, str]) -> str:
    if not cache:
        resp = svc.users().labels().list(userId="me").execute()
        for lab in resp.get("labels", []):
            cache[lab["name"]] = lab["id"]
    if name not in cache:
        lab = (
            svc.users()
            .labels()
            .create(
                userId="me",
                body={"name": name, "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"},
            )
            .execute()
        )
        cache[name] = lab["id"]
    return cache[name]


def _label_thread(svc, thread_id: str, label_name: str, cache: dict[str, str]) -> None:
    label_id = _ensure_label(svc, label_name, cache)
    svc.users().threads().modify(
        userId="me", id=thread_id, body={"addLabelIds": [label_id]}
    ).execute()


def _notify_mac(title: str, text: str) -> None:
    text = text.replace('"', "'")[:180]
    title = title.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{text}" with title "{title}"'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass  # notification is best-effort


def _app_id_for_job(conn: sqlite3.Connection, job_id: int | None) -> int | None:
    if not job_id:
        return None
    row = conn.execute("SELECT id FROM applications WHERE job_id=?", (job_id,)).fetchone()
    return row["id"] if row else None


def _correlate(conn: sqlite3.Connection, thread_id: str, sender_email: str,
               subject: str, snippet: str,
               outreach_by_thread: dict, app_companies: list) -> tuple:
    """Returns (outreach_id, application_id, matched: bool)."""
    o = outreach_by_thread.get(thread_id)
    if o:
        return o["id"], _app_id_for_job(conn, o["job_id"]), True

    sender_domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else ""
    haystack = f"{subject} {snippet}".lower()
    for comp in app_companies:
        dom = (comp["domain"] or "").lower()
        if dom and sender_domain and (sender_domain == dom or sender_domain.endswith("." + dom)):
            return None, comp["app_id"], True
        name = (comp["name"] or "").lower().strip()
        if len(name) >= 4 and name in haystack:
            return None, comp["app_id"], True
    return None, None, False


def mark_stale_no_response(conn) -> int:
    """Submitted applications with no reply after N days -> 'no_response'.

    Keeps the calibration denominator honest: a predicted chance only counts
    against a real outcome once enough time has passed for a reply.
    """
    from jobagent import config
    days = config.caps().get("no_response_after_days", 14)
    cur = conn.execute(
        "UPDATE applications SET status='no_response' "
        "WHERE status IN ('submitted', 'confirmed') "
        "AND submitted_at IS NOT NULL "
        "AND submitted_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    conn.commit()
    return cur.rowcount


def run_scan() -> None:
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

    conn = db.connect()
    stale = mark_stale_no_response(conn)
    if stale:
        print(f"inbox: marked {stale} application(s) no_response (stale)")
    my_email = svc.users().getProfile(userId="me").execute()["emailAddress"].lower()

    outreach_by_thread = {
        r["gmail_thread_id"]: dict(r)
        for r in conn.execute(
            "SELECT id, job_id, status, gmail_thread_id FROM outreach "
            "WHERE gmail_thread_id IS NOT NULL"
        )
    }
    app_companies = [
        dict(r)
        for r in conn.execute(
            "SELECT a.id AS app_id, c.domain, c.name "
            "FROM applications a JOIN jobs j ON a.job_id = j.id "
            "JOIN companies c ON j.company_id = c.id"
        )
    ]

    stubs = gmail.list_threads(SCAN_QUERY, svc=svc)
    print(f"inbox: {len(stubs)} thread(s) match query")

    to_classify: list[dict] = []  # rows pending the batch LLM call
    for stub in stubs:
        tid = stub["id"]
        full = gmail.get_thread(tid, svc=svc)
        msgs = full.get("messages", [])
        if not msgs:
            continue
        last = msgs[-1]
        subject = _header(msgs[0], "Subject") or _header(last, "Subject")
        snippet = last.get("snippet", "") or stub.get("snippet", "")
        last_ts = max(int(m.get("internalDate", 0)) for m in msgs)

        external = [m for m in msgs if _email_of(_header(m, "From")) != my_email]
        sender_email = _email_of(_header(external[-1], "From")) if external else my_email

        outreach_id, application_id, matched = _correlate(
            conn, tid, sender_email, subject, snippet, outreach_by_thread, app_companies
        )

        if matched and outreach_id and external:
            o = outreach_by_thread[tid]
            if o["status"] == "sent":
                conn.execute("UPDATE outreach SET status='replied' WHERE id=?", (outreach_id,))
                conn.commit()
                db.log_event(conn, "outreach", outreach_id, "outreach_replied",
                             {"thread_id": tid, "from": sender_email})

        if not matched:
            # conservative gate: subject must look job-search-related AND the
            # sender must not be a personal/freemail address. Prefer skipping.
            sender_domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else ""
            if not JOBSEARCH_SUBJECT_RE.search(subject or "") or \
                    not sender_domain or sender_domain in FREEMAIL_DOMAINS:
                continue

        existing = conn.execute(
            "SELECT classification, last_message_at FROM inbox_threads WHERE gmail_thread_id=?",
            (tid,),
        ).fetchone()
        unchanged = (
            existing
            and existing["classification"]
            and existing["last_message_at"] == str(last_ts)
        )

        conn.execute(
            "INSERT INTO inbox_threads (gmail_thread_id, application_id, outreach_id, "
            " from_email, subject, snippet, last_message_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(gmail_thread_id) DO UPDATE SET "
            " application_id=COALESCE(excluded.application_id, application_id), "
            " outreach_id=COALESCE(excluded.outreach_id, outreach_id), "
            " from_email=excluded.from_email, subject=excluded.subject, "
            " snippet=excluded.snippet, last_message_at=excluded.last_message_at",
            (tid, application_id, outreach_id, sender_email, subject, snippet, str(last_ts)),
        )
        conn.commit()

        if not unchanged:
            to_classify.append({
                "thread_id": tid, "from": sender_email, "subject": subject,
                "snippet": snippet[:300], "application_id": application_id,
                "outreach_id": outreach_id,
            })

    if not to_classify:
        print("inbox: nothing new to classify")
        return

    lines = "\n".join(
        f'- thread_id "{t["thread_id"]}" | from: {t["from"]} | subject: {t["subject"]} '
        f'| snippet: {t["snippet"]}'
        for t in to_classify
    )
    prompt = f"""Classify each email thread below from a job seeker's inbox.
Categories:
- interview_request: recruiter/company asks to schedule a call or interview
- rejection: explicit "we will not move forward" style rejection
- info_request: company asks for more info/documents/availability (not a call yet)
- auto_ack: automated "we received your application" confirmation
- other: anything else

Threads:
{lines}

Return every thread_id exactly as given."""

    try:
        batch = ask_json(prompt, ThreadBatch, model="haiku")
    except LLMError as e:
        print(f"inbox: classification failed ({e}); will retry next scan")
        db.log_event(conn, "inbox", None, "inbox_classify_failed", str(e)[:300])
        return

    verdicts = {v.thread_id: v.classification for v in batch.threads}
    by_tid = {t["thread_id"]: t for t in to_classify}
    label_cache: dict[str, str] = {}
    counts: dict[str, int] = {}

    for tid, cls in verdicts.items():
        t = by_tid.get(tid)
        if not t:
            continue
        conn.execute(
            "UPDATE inbox_threads SET classification=?, classified_at=datetime('now') "
            "WHERE gmail_thread_id=?",
            (cls, tid),
        )
        conn.commit()
        counts[cls] = counts.get(cls, 0) + 1
        db.log_event(conn, "inbox", None, "thread_classified",
                     {"thread_id": tid, "classification": cls, "from": t["from"]})

        try:
            killswitch.check()
            if cls == "interview_request":
                if t["application_id"]:
                    conn.execute("UPDATE applications SET status='interview' WHERE id=?",
                                 (t["application_id"],))
                    conn.commit()
                _notify_mac("JobAgent: INTERVIEW",
                            f"{t['from']} — {t['subject']}")
                _label_thread(svc, tid, LABEL_INTERVIEW, label_cache)
            elif cls == "rejection":
                if t["application_id"]:
                    conn.execute("UPDATE applications SET status='rejected' WHERE id=?",
                                 (t["application_id"],))
                    conn.commit()
                _label_thread(svc, tid, LABEL_REJECTED, label_cache)
            else:
                _label_thread(svc, tid, LABEL_REPLY, label_cache)
        except killswitch.KilledError as e:
            print(str(e))
            raise SystemExit(1)
        except Exception as e:
            print(f"inbox: side effect failed for thread {tid}: {e}")

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    print(f"inbox: classified {len(verdicts)} thread(s): {summary}")
