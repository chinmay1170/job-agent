"""Apply runner: orchestrates routing, filling, safety checks, and proof.

Safety order before any submit click:
  kill switch -> daily cap -> applications.job_id uniqueness -> company cooldown.
`uncertain` post-submit outcomes are NEVER retried (double-submit risk) — they
go to the review queue with full proof.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

from jobagent import config, db, killswitch
from jobagent.apply import browser, field_mapper, filler, proof, router


def _enqueue_review(conn, job_id: int, reason: str, state: dict) -> None:
    conn.execute(
        "INSERT INTO review_queue (job_id, reason, state_json) VALUES (?,?,?)",
        (job_id, reason, json.dumps(state, ensure_ascii=False)),
    )
    conn.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (job_id,))
    conn.commit()
    db.log_event(conn, "job", job_id, "queued_for_review", {"reason": reason})


def _cover_text(job_id: int) -> str:
    content = proof.ARTIFACTS / str(job_id) / "content.json"
    if content.exists():
        data = json.loads(content.read_text())
        paras = (
            data.get("cover", {}).get("body_paragraphs")
            or data.get("plan", {}).get("cover_letter_paragraphs")
            or data.get("cover_letter_paragraphs")
            or []
        )
        return "\n\n".join(paras)
    return ""


def _apply_one(conn, page, job, app_row, caps: dict, dry_run: bool) -> str:
    job_id = job["id"]
    strategy = router.route(job["apply_url"] or job["url"])
    if strategy.startswith("queue_"):
        _enqueue_review(conn, job_id, strategy.removeprefix("queue_"),
                        {"url": job["apply_url"] or job["url"]})
        return "queued"

    form_url = router.to_form_url(strategy, job["apply_url"] or job["url"])
    page.goto(form_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)

    if browser.detect_captcha(page):
        _enqueue_review(conn, job_id, "captcha",
                        {"url": form_url, "screenshot": proof.snap(page, job_id, "captcha")})
        return "queued"
    if browser.detect_login_wall(page):
        _enqueue_review(conn, job_id, "login_wall", {"url": form_url})
        return "queued"

    schema = browser.extract_form_schema(page)
    if not schema:
        _enqueue_review(conn, job_id, "no_form_found",
                        {"url": form_url, "screenshot": proof.snap(page, job_id, "no_form")})
        return "queued"

    plan = field_mapper.plan_fill(schema, _cover_text(job_id))
    threshold = caps["field_confidence_threshold"]
    problems = field_mapper.unmet_required(schema, plan, threshold)
    if problems:
        _enqueue_review(conn, job_id, "unmapped_required_field", {
            "url": form_url, "fields": problems,
            "plan": [a.model_dump() for a in plan.actions],
            "screenshot": proof.snap(page, job_id, "blocked"),
        })
        return "queued"

    fill_failures = filler.execute_plan(page, plan, threshold)
    uploaded = filler.upload_files(page, app_row["resume_path"], app_row["cover_path"])
    if fill_failures or not uploaded:
        _enqueue_review(conn, job_id, "low_confidence", {
            "url": form_url, "fill_failures": fill_failures, "resume_uploaded": uploaded,
            "screenshot": proof.snap(page, job_id, "fill_failed"),
        })
        return "queued"

    shot = proof.snap(page, job_id, "filled_form")
    answers_audit = json.dumps(
        [a.model_dump() for a in plan.actions if a.action != "skip"], ensure_ascii=False
    )

    if dry_run:
        db.log_event(conn, "job", job_id, "dry_run_filled", {"screenshot": shot})
        return "dry_run"

    # Final pre-click gates.
    killswitch.check()
    if db.counter_get(conn, "applications") >= caps["applications_per_day"]:
        return "cap_reached"
    if conn.execute("SELECT 1 FROM applications WHERE job_id=? AND status NOT IN ('pending','failed')",
                    (job_id,)).fetchone():
        return "already_applied"

    submit = filler.find_submit(page)
    if submit is None:
        _enqueue_review(conn, job_id, "submit_error",
                        {"url": form_url, "note": "no submit button found", "screenshot": shot})
        return "queued"

    outcome, evidence = filler.submit_and_verify(page, submit)
    conf_shot = proof.snap(page, job_id, "confirmation")
    dom = proof.save_dom(page, job_id)

    if outcome == "confirmed":
        conn.execute(
            "UPDATE applications SET method='browser', answers_json=?, status='submitted', "
            "submitted_at=datetime('now'), proof_screenshot=?, proof_dom=?, confirmation_text=? "
            "WHERE job_id=?",
            (answers_audit, conf_shot, dom, evidence, job_id),
        )
        conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
        cooldown = (date.today() + timedelta(days=caps["company_cooldown_days"])).isoformat()
        conn.execute("UPDATE companies SET cooldown_until=? WHERE id=?", (cooldown, job["company_id"]))
        conn.commit()
        db.counter_bump(conn, "applications")
        db.log_event(conn, "job", job_id, "application_submitted", {"evidence": evidence})
        return "submitted"

    # error or uncertain: never retry — human eyes.
    _enqueue_review(conn, job_id, "submit_error" if outcome == "error" else "submit_uncertain", {
        "url": form_url, "evidence": evidence,
        "screenshot": conf_shot, "answers": answers_audit,
    })
    return "queued"


def run_apply(limit: int = 3, dry_run: bool | None = None, job_id: int | None = None) -> None:
    caps = config.caps()
    if dry_run is None:
        dry_run = bool(caps.get("dry_run", True))
    killswitch.check()

    conn = db.connect()
    q = (
        "SELECT j.*, a.resume_path, a.cover_path FROM jobs j "
        "JOIN applications a ON a.job_id = j.id "
        "WHERE a.resume_path IS NOT NULL AND a.status='pending' "
    )
    params: list = []
    if job_id:
        q += "AND j.id=? "
        params.append(job_id)
    else:
        q += "AND j.status='apply_queued' ORDER BY j.score DESC LIMIT ?"
        params.append(limit)
    jobs = conn.execute(q, params).fetchall()
    if not jobs:
        print("Nothing to apply to (tailor first?).")
        return

    started = time.time()
    results: dict[str, int] = {}
    with browser.open_page() as page:
        for job in jobs:
            if time.time() - started > caps["max_apply_minutes_per_run"] * 60:
                print("Run time budget exhausted.")
                break
            if not dry_run and db.counter_get(conn, "applications") >= caps["applications_per_day"]:
                print("Daily application cap reached.")
                break
            try:
                killswitch.check()
                outcome = _apply_one(conn, page, job, job, caps, dry_run)
            except killswitch.KilledError:
                print("Kill switch engaged — halting.")
                break
            except Exception as e:  # noqa: BLE001 — isolate per-job failures
                outcome = "failed"
                conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job["id"],))
                conn.commit()
                db.log_event(conn, "job", job["id"], "apply_failed",
                             {"error": f"{type(e).__name__}: {e}"[:500]})
            results[outcome] = results.get(outcome, 0) + 1
            print(f"  job {job['id']} [{job['title']} @ company {job['company_id']}] -> {outcome}")
            time.sleep(3)
    print(f"apply done ({'DRY RUN' if dry_run else 'LIVE'}): {results}")
