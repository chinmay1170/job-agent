"""Daily digest. `jobagent digest` lands here.

Writes artifacts/digests/YYYY-MM-DD.md, then emails it to the user via
outreach.gmail. If Gmail isn't configured yet, it just writes the file and
prints the path — never crashes.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from jobagent import config, db, killswitch
from jobagent.db import ROOT
from jobagent.inbox import digest_html
from jobagent.outreach import gmail

DIGEST_DIR = ROOT / "artifacts" / "digests"
DIGEST_TO = "chinmaykrishna3@gmail.com"


def _section(title: str, lines: list[str], empty: str = "_none_") -> str:
    body = "\n".join(lines) if lines else empty
    return f"## {title}\n\n{body}\n"


def _funnel(conn: sqlite3.Connection) -> dict:
    j = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    a = dict(conn.execute("SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall())
    replies = conn.execute(
        "SELECT COUNT(*) FROM inbox_threads WHERE classification IS NOT NULL "
        "AND classification != 'auto_ack'"
    ).fetchone()[0]
    return {
        "discovered": sum(j.values()),
        "queued": j.get("apply_queued", 0),
        "applied": j.get("applied", 0),
        "replies": replies,
        "interviews": a.get("interview", 0),
        "rejections": a.get("rejected", 0),
    }


def _build_digest(conn: sqlite3.Connection, today: str) -> str:
    caps_cfg = config.caps()

    apps = conn.execute(
        "SELECT c.name AS company, j.title, j.url, a.method, a.status "
        "FROM applications a JOIN jobs j ON a.job_id = j.id "
        "LEFT JOIN companies c ON j.company_id = c.id "
        "WHERE date(a.submitted_at) = ? ORDER BY a.submitted_at",
        (today,),
    ).fetchall()
    app_lines = [
        f"- **{r['company'] or '?'}** — {r['title'] or '?'} "
        f"({r['status']}, via {r['method'] or '?'})" + (f" — [job]({r['url']})" if r["url"] else "")
        for r in apps
    ]

    sent = conn.execute(
        "SELECT o.kind, o.subject, ct.email FROM outreach o "
        "LEFT JOIN contacts ct ON o.contact_id = ct.id "
        "WHERE o.status IN ('sent','replied') AND date(o.sent_at) = ? ORDER BY o.sent_at",
        (today,),
    ).fetchall()
    sent_lines = [f"- [{r['kind']}] to {r['email'] or '?'} — {r['subject']}" for r in sent]

    replies = conn.execute(
        "SELECT classification, COUNT(*) AS n FROM inbox_threads "
        "WHERE date(classified_at) = ? GROUP BY classification ORDER BY n DESC",
        (today,),
    ).fetchall()
    reply_lines = [f"- {r['classification'] or 'unclassified'}: {r['n']}" for r in replies]

    rq_open = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL"
    ).fetchone()[0]

    borderline = conn.execute(
        "SELECT c.name AS company, j.title, j.score FROM jobs j "
        "LEFT JOIN companies c ON j.company_id = c.id "
        "WHERE j.status = 'scored' ORDER BY j.score DESC LIMIT 15"
    ).fetchall()
    borderline_lines = [
        f"- {r['company'] or '?'} — {r['title'] or '?'} (score {r['score']})"
        for r in borderline
    ]

    errors = conn.execute(
        "SELECT ts, entity_type, entity_id, event, detail FROM events "
        "WHERE date(ts) = ? AND (event LIKE '%error%' OR event LIKE '%failed%') "
        "ORDER BY id",
        (today,),
    ).fetchall()
    error_lines = [
        f"- {r['ts']} {r['entity_type']}#{r['entity_id']} {r['event']}: "
        f"{(r['detail'] or '')[:140]}"
        for r in errors
    ]

    counters = conn.execute(
        "SELECT kind, count FROM daily_counters WHERE date = ? ORDER BY kind", (today,)
    ).fetchall()
    cap_for = {"emails": caps_cfg.get("emails_per_day"),
               "applications": caps_cfg.get("applications_per_day")}
    counter_lines = []
    for r in counters:
        cap = cap_for.get(r["kind"])
        counter_lines.append(f"- {r['kind']}: {r['count']}" + (f" / {cap}" if cap else ""))
    for kind, cap in cap_for.items():
        if kind not in {r["kind"] for r in counters}:
            counter_lines.append(f"- {kind}: 0 / {cap}")

    parts = [
        f"# JobAgent digest — {today}\n",
        _section(f"Applications submitted today ({len(apps)})", app_lines),
        _section(f"Outreach emails sent today ({len(sent)})", sent_lines),
        _section("Replies by classification (classified today)", reply_lines),
        _section("Review queue", [f"- open items: {rq_open}"]),
        _section(f"Borderline jobs awaiting decision (status=scored, top {len(borderline)})",
                 borderline_lines),
        _section(f"Errors today ({len(errors)})", error_lines, empty="_none — clean day_"),
        _section("Counters vs caps", counter_lines),
    ]
    markdown = "\n".join(parts)

    reply_rows = conn.execute(
        "SELECT subject, from_email, classification FROM inbox_threads "
        "WHERE date(classified_at) = ? ORDER BY "
        "CASE classification WHEN 'interview_request' THEN 0 WHEN 'rejection' THEN 1 "
        "ELSE 2 END, last_message_at DESC LIMIT 20",
        (today,),
    ).fetchall()
    counter_data = {}
    counts_by_kind = {r["kind"]: r["count"] for r in counters}
    for kind, cap in cap_for.items():
        if cap:
            counter_data[kind] = (counts_by_kind.get(kind, 0), cap)

    html = digest_html.render(today, {
        "funnel": _funnel(conn),
        "apps": [dict(r) for r in apps],
        "sent": [dict(r) for r in sent],
        "replies": [dict(r) for r in reply_rows],
        "rq_open": rq_open,
        "borderline": [dict(r) for r in borderline[:8]],
        "errors": [dict(r) for r in errors[:10]],
        "counters": counter_data,
    })
    return markdown, html


def run_digest() -> None:
    conn = db.connect()
    today = date.today().isoformat()
    content, html = _build_digest(conn, today)

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"{today}.md"
    path.write_text(content)
    (DIGEST_DIR / f"{today}.html").write_text(html)
    print(f"digest: wrote {path}")

    if killswitch.is_killed():
        print("digest: kill switch engaged — skipping email send")
        return

    subject = f"JobAgent digest {today}"
    try:
        result = gmail.send_message(DIGEST_TO, subject, content, html_body=html)
        db.log_event(conn, "digest", None, "digest_emailed",
                     {"to": DIGEST_TO, "gmail_message_id": result["id"], "date": today})
        print(f"digest: emailed to {DIGEST_TO} (message {result['id']})")
    except gmail.GmailNotConfigured:
        print("digest: Gmail not set up yet — digest written to file only.")
        print(f"        Read it at: {path}")
        print("        (run `uv run python scripts/setup_gmail_oauth.py` to enable email)")
    except Exception as e:
        print(f"digest: email send failed ({e}); digest is at {path}")
        db.log_event(conn, "digest", None, "digest_email_failed", str(e)[:300])
