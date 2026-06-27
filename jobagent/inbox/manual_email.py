"""Email the user the jobs the agent could NOT apply to, so he applies by hand.

Zero-LLM by design: pure DB read -> HTML -> Gmail send to self. Each review
item is emailed once (review_queue.notified_at). Includes everything needed
to apply in two minutes: link, predicted chance, salary ask, canonical answers.
"""
from __future__ import annotations

import html

from jobagent import config, db
from jobagent.outreach import gmail

# ONLY reasons that genuinely require a human (CAPTCHA, login walls, spam
# flags, Workday). Everything else (unmapped_required_field, low_confidence,
# no_form_found, submit_error, ...) is a FIXABLE agent gap — those stay in the
# review queue for the auto-retry sweep, never emailed. This keeps the
# manual-apply email to the rare un-automatable cases.
MANUAL_REASONS = {
    "login_board", "workday", "captcha", "ats_blocked", "spam_flagged",
    "email_listing", "retry_exhausted",
}
# Fixable reasons the auto-retry sweep re-attempts each run (not emailed).
RETRYABLE_REASONS = {
    "no_form_found", "unmapped_required_field", "submit_error",
    "submit_uncertain", "low_confidence", "human_check_failed",
}
SELF_EMAIL = config.identity().get("email", "")


def requeue_fixable(max_attempts: int = 3) -> dict:
    """Self-heal: retry fixable review items, then escalate to manual email.

    Called before each apply stage. A job that failed a fixable reason fewer
    than `max_attempts` times is reset to apply_queued for another attempt
    (don't abandon a portal on the first failure). Once it has been attempted
    `max_attempts` times and still fails, it is flagged `retry_exhausted` so
    send_manual_apply_email() emails it for the user to finish by hand — this
    is how the queue fully clears (every job ends submitted OR emailed).
    Returns {"retried": n, "exhausted": n}.
    """
    from jobagent import db
    conn = db.connect()
    rph = ",".join("?" * len(RETRYABLE_REASONS))
    # Count UNRESOLVED retryable rows per still-failing job — each failed attempt
    # leaves one row, so the count IS the attempt number. CRITICAL: do NOT
    # resolve these rows on requeue, or the count resets to 1 every iteration and
    # the job retries forever, never escalating to manual email.
    rows = conn.execute(
        f"SELECT r.job_id, COUNT(*) AS n FROM review_queue r "
        f"JOIN jobs j ON j.id = r.job_id "
        f"WHERE r.reason IN ({rph}) AND r.resolved_at IS NULL "
        f"AND j.status = 'needs_review' GROUP BY r.job_id",
        tuple(RETRYABLE_REASONS),
    ).fetchall()
    retry_ids = [r["job_id"] for r in rows if r["job_id"] and r["n"] < max_attempts]
    exhausted = [r["job_id"] for r in rows if r["job_id"] and r["n"] >= max_attempts]

    if retry_ids:
        ph = ",".join("?" * len(retry_ids))
        # bump back to apply_queued WITHOUT resolving the rows (they accumulate
        # as the attempt counter); only re-apply jobs that have a resume.
        conn.execute(
            f"UPDATE jobs SET status='apply_queued' WHERE status='needs_review' "
            f"AND id IN ({ph}) AND id IN (SELECT job_id FROM applications "
            f"WHERE resume_path IS NOT NULL)", retry_ids)

    flagged = 0
    for jid in exhausted:
        if conn.execute("SELECT 1 FROM review_queue WHERE job_id=? "
                        "AND reason='retry_exhausted'", (jid,)).fetchone():
            continue  # already escalated
        # carry the last unmet-fields snapshot into the manual email
        last = conn.execute(
            f"SELECT state_json FROM review_queue WHERE job_id=? AND reason IN ({rph}) "
            f"ORDER BY id DESC LIMIT 1", (jid, *RETRYABLE_REASONS)).fetchone()
        conn.execute(
            f"UPDATE review_queue SET resolved_at=datetime('now'), "
            f"resolution='retry_exhausted' WHERE job_id=? AND reason IN ({rph}) "
            f"AND resolved_at IS NULL", (jid, *RETRYABLE_REASONS))
        conn.execute(
            "INSERT INTO review_queue(job_id, reason, state_json, created_at) "
            "VALUES(?, 'retry_exhausted', ?, datetime('now'))",
            (jid, last["state_json"] if last else "{}"))
        conn.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (jid,))
        flagged += 1
    conn.commit()
    conn.close()
    print(f"requeue_fixable: {len(retry_ids)} retried, {flagged} exhausted->manual email")
    return {"retried": len(retry_ids), "exhausted": flagged}


def _items(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.id AS rq_id, r.reason, j.id AS job_id, j.title, j.url,
               j.apply_url, j.selection_chance, j.salary_benchmark,
               c.name AS company, COALESCE(j.location, '') AS location
        FROM review_queue r
        JOIN jobs j ON j.id = r.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE r.resolved_at IS NULL AND r.notified_at IS NULL
        ORDER BY j.selection_chance DESC
        """
    ).fetchall()
    return [dict(r) for r in rows if r["reason"] in MANUAL_REASONS]


def _row_html(it: dict) -> str:
    link = it["apply_url"] or it["url"] or "#"
    chance = f"{it['selection_chance']}%" if it["selection_chance"] else "—"
    salary = it["salary_benchmark"] or "see regional defaults"
    return (
        '<tr>'
        f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;">'
        f'<a href="{html.escape(link)}" style="font-weight:600;color:#2563eb;'
        f'text-decoration:none;">{html.escape(it["company"])} — '
        f'{html.escape(it["title"][:70])}</a><br>'
        f'<span style="color:#6b7280;font-size:12px;">{html.escape(it["location"][:40])} '
        f'&middot; reason: {html.escape(it["reason"])}</span></td>'
        f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;'
        f'text-align:center;font-weight:700;">{chance}</td>'
        f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;'
        f'font-size:12px;">{html.escape(salary)}</td>'
        '</tr>'
    )


def send_manual_apply_email() -> int:
    """Send the un-automatable jobs to the user. Returns count sent."""
    conn = db.connect()
    items = _items(conn)
    if not items:
        conn.close()
        return 0

    answers = config.answers()
    notice = answers.get("logistics", {}).get("notice_period", "30 days")
    rows = "".join(_row_html(it) for it in items)
    html_body = f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;margin:0 auto;">
  <h2 style="color:#111827;">JobAgent — {len(items)} job(s) need you to apply manually</h2>
  <p style="color:#374151;">The agent couldn't finish these (bot blocks, login walls,
  or fields it refused to guess). Each link opens the application.</p>
  <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;">
    <tr style="background:#f9fafb;">
      <th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7280;">ROLE</th>
      <th style="padding:8px 12px;font-size:12px;color:#6b7280;">CHANCE</th>
      <th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7280;">SALARY TO QUOTE</th>
    </tr>
    {rows}
  </table>
  <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;margin-top:14px;font-size:13px;color:#374151;">
    <b>Standard answers:</b> visa sponsorship required: <b>Yes</b> &middot;
    notice period: <b>{html.escape(notice)}</b> &middot;
    relocation: <b>Yes, willing</b> &middot; resume: your original PDF &middot;
    EEO/demographics: decline to self-identify.
  </div>
  <p style="color:#9ca3af;font-size:12px;">After you apply, mark it on the
  <a href="http://localhost:8787">dashboard</a> review queue (Mark applied) so
  tracking and reply-watching pick it up.</p>
</div>"""
    text_body = f"JobAgent: {len(items)} job(s) need manual application:\n" + "\n".join(
        f"- {it['company']} — {it['title']} ({it['selection_chance'] or '-'}%): "
        f"{it['apply_url'] or it['url']}"
        for it in items
    )
    gmail.send_message(SELF_EMAIL, f"Apply manually: {len(items)} job(s) the agent couldn't finish",
                       text_body, html_body=html_body)
    conn.executemany(
        "UPDATE review_queue SET notified_at=datetime('now') WHERE id=?",
        [(it["rq_id"],) for it in items],
    )
    conn.commit()
    db.log_event(conn, "inbox", None, "manual_apply_email_sent",
                 {"count": len(items), "job_ids": [it["job_id"] for it in items]})
    conn.close()
    print(f"manual-apply email sent: {len(items)} job(s)")
    return len(items)
