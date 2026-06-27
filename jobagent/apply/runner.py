"""Apply runner: orchestrates routing, filling, safety checks, and proof.

Safety order before any submit click:
  kill switch -> daily cap -> applications.job_id uniqueness -> company cooldown.
`uncertain` post-submit outcomes are NEVER retried (double-submit risk) — they
go to the review queue with full proof.
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import date, timedelta

from jobagent import config, db, killswitch
from jobagent.apply import browser, field_mapper, filler, proof, router, security_code

# Order tiers cycle through so big-cos aren't starved by a wall of startups.
_TIER_ORDER = ["megacap", "large", "mid", "startup"]


_US_RE = re.compile(
    r"united states|\busa\b|\bu\.s\.|, ca\b|, ny\b|, wa\b|, tx\b|, ma\b|"
    r"new york|san francisco|seattle|austin|boston|mountain view|palo alto|"
    r"sunnyvale|remote ?- ?us|remote, us", re.IGNORECASE)


def _is_us(r) -> bool:
    if _US_RE.search(r["location"] or ""):
        return True
    keys = r.keys()
    return "company_region" in keys and (r["company_region"] or "") == "us"


def _tier_balanced(ranked: list, limit: int) -> list:
    """US-FIRST, then round-robin the rest across company tiers.

    User priority: US roles go to the front (chance-sorted) — they're harder
    (H-1B) so we want them attempted first while daily caps have room. The
    remaining non-US jobs are tier-balanced so the batch still spans
    large/public AND startups.
    """
    from collections import defaultdict, deque
    us = [r for r in ranked if _is_us(r)]
    rest = [r for r in ranked if not _is_us(r)]
    out: list = list(us[:limit])
    if len(out) >= limit:
        return out
    buckets: dict[str, deque] = defaultdict(deque)
    for r in rest:
        buckets[(r["company_tier"] or "mid")].append(r)
    order = [t for t in _TIER_ORDER if buckets.get(t)] + \
            [t for t in buckets if t not in _TIER_ORDER]
    while len(out) < limit and any(buckets[t] for t in order):
        for t in order:
            if buckets[t]:
                out.append(buckets[t].popleft())
                if len(out) >= limit:
                    break
    return out


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


def _apply_one(conn, page, job, app_row, caps: dict, dry_run: bool,
               blocked_ats: set | None = None,
               last_submit: dict | None = None) -> str:
    job_id = job["id"]
    strategy = router.route(job["apply_url"] or job["url"])
    if strategy.startswith("queue_"):
        _enqueue_review(conn, job_id, strategy.removeprefix("queue_"),
                        {"url": job["apply_url"] or job["url"]})
        return "queued"
    if blocked_ats and strategy in blocked_ats:
        # Platform bot-blocked us earlier this run — don't even visit, every
        # hit extends the block. Job stays apply_queued for a later run.
        return "ats_blocked_skip"
    # Per-platform daily cap — keep each ATS under its per-IP anti-bot ceiling.
    if not dry_run:
        plat_cap = (caps.get("per_platform_daily_caps") or {}).get(strategy)
        if plat_cap is not None and db.counter_get(conn, f"apply_{strategy}") >= plat_cap:
            return "platform_cap_reached"
    # Per-platform pacing — space out submits to the SAME platform.
    if last_submit is not None and strategy in last_submit:
        gap = (caps.get("per_platform_min_gap_seconds") or {}).get(strategy, 0)
        wait = gap - (time.time() - last_submit[strategy])
        if wait > 0:
            time.sleep(wait)

    form_url = router.to_form_url(strategy, job["apply_url"] or job["url"])
    page.goto(form_url, wait_until="domcontentloaded", timeout=45000)
    page_opened = time.time()  # for the minimum human dwell before submit
    page.wait_for_timeout(2500)

    if browser.detect_bot_block(page):
        if blocked_ats is not None:
            blocked_ats.add(strategy)
        db.counter_bump(conn, f"blocked_{strategy}")  # persist same-day backoff
        db.log_event(conn, "job", job_id, "ats_bot_block",
                     {"strategy": strategy, "url": form_url})
        print(f"  !! {strategy} has bot-blocked this IP — backing off that "
              f"platform for the rest of the day")
        return "ats_blocked_skip"

    if browser.detect_captcha(page):
        _enqueue_review(conn, job_id, "captcha",
                        {"url": form_url, "screenshot": proof.snap(page, job_id, "captcha")})
        return "queued"
    if browser.detect_login_wall(page):
        _enqueue_review(conn, job_id, "login_wall", {"url": form_url})
        return "queued"

    # SPA application forms (e.g. N26) mount late — retry with backoff and
    # scroll, and follow an in-page "Apply" button if the form isn't present.
    schema = browser.extract_form_schema(page)
    if not schema:
        # SmartRecruiters uses "I'm interested"; Ashby/Greenhouse use "Apply".
        for sel in ("button:has-text(\"I'm interested\")",
                    "a:has-text(\"I'm interested\")",
                    "button:has-text('Apply Now')",
                    "a:has-text('Apply')", "button:has-text('Apply')",
                    "a[href*='apply']"):
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    loc.click(timeout=5000)
                    # SmartRecruiters redirects to a new page after click —
                    # wait for navigation to settle before re-extracting.
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:  # noqa: BLE001
                        page.wait_for_timeout(3500)
                    break
            except Exception:  # noqa: BLE001
                continue
    for _ in range(4):
        schema = browser.extract_form_schema(page)
        if schema:
            break
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(2500)
    # Careers sites (MongoDB, Elastic, Databricks, Wayfair...) wrap the real
    # Greenhouse/Ashby form in an iframe; the wrapper page often has stray
    # inputs so `schema` isn't empty, but it has NO file input — meaning we're
    # not on the real application. Jump into the embed iframe whenever there's
    # one and the current page lacks a resume upload.
    has_file = any(c.get("type") == "file" for c in schema)
    if not has_file:
        try:
            frame_el = page.locator(
                "iframe[src*='greenhouse'], iframe[id*='grnhse'], "
                "iframe[src*='ashbyhq'], iframe[src*='lever']").first
            src = frame_el.get_attribute("src") if frame_el.count() else None
            if not src:
                # Careers pages with no embed iframe but a gh_jid in the URL
                # (e.g. jobs.elastic.co/jobs?gh_jid=123): Greenhouse's universal
                # token embed renders the real form from just the job id.
                m = re.search(r"gh_jid=(\d+)", job["apply_url"] or job["url"] or "")
                if m:
                    src = f"https://boards.greenhouse.io/embed/job_app?token={m.group(1)}"
            if src:
                page.goto(src, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2800)
                schema = browser.extract_form_schema(page)
        except Exception:  # noqa: BLE001
            pass
    if not schema:
        # No form AND a captcha frame after the apply-click (SmartRecruiters
        # DataDome) — a human must solve it. Back off the ATS and route to the
        # manual-apply email rather than retrying.
        if browser.detect_bot_block(page):
            if blocked_ats is not None:
                blocked_ats.add(strategy)
            db.counter_bump(conn, f"blocked_{strategy}")  # persist same-day backoff
            _enqueue_review(conn, job_id, "captcha",
                            {"url": form_url, "screenshot": proof.snap(page, job_id, "captcha")})
            print(f"  !! {strategy} served a captcha — routing to manual, "
                  f"backing off that platform for the day")
            return "queued"
        _enqueue_review(conn, job_id, "no_form_found",
                        {"url": form_url, "screenshot": proof.snap(page, job_id, "no_form")})
        return "queued"

    plan = field_mapper.plan_fill(schema, _cover_text(job_id), job_facts={
        "title": job["title"],
        "location": job["location"] or "",
        "salary_benchmark": job["salary_benchmark"] or "not researched",
    })
    threshold = caps["field_confidence_threshold"]
    # NOTE: the required-field gate runs AFTER every filler (below), against the
    # live DOM — react-select / yes-no / consent dropdowns are answered by the
    # resolvers, not the mapper's plan, so checking the plan here would wrongly
    # abandon forms the resolvers can complete.
    fill_failures = filler.execute_plan(page, plan, threshold)
    consents = filler.resolve_consent_combos(page)
    if consents:
        db.log_event(conn, "job", job_id, "consents_acknowledged", {"labels": consents})
    consent_boxes = filler.resolve_consent_checkboxes(page)
    if consent_boxes:
        db.log_event(conn, "job", job_id, "consent_checkboxes_ticked", {"labels": consent_boxes})
    react_sels = filler.resolve_react_selects(page)
    if react_sels:
        db.log_event(conn, "job", job_id, "react_selects_answered", {"labels": react_sels})
    yesnos = filler.resolve_yesno_buttons(page)
    if yesnos:
        db.log_event(conn, "job", job_id, "yesno_answered", {"answers": yesnos})
    picks = filler.resolve_option_lists(page)
    if picks:
        db.log_event(conn, "job", job_id, "option_lists_answered", {"picks": picks})
    shells = filler.resolve_select_shells(page)
    if shells:
        db.log_event(conn, "job", job_id, "select_shells_answered", {"picks": shells})
    resume_pdf = config.resume_pdf(app_row["resume_path"])
    uploaded = filler.upload_files(page, resume_pdf, app_row["cover_path"])

    # Authoritative gate: what required controls are STILL empty on the live
    # page, after the mapper + every resolver have run.
    problems = field_mapper.unmet_required_live(page, schema, threshold)
    if problems:
        _enqueue_review(conn, job_id, "unmapped_required_field", {
            "url": form_url, "fields": problems, "fill_failures": fill_failures,
            "plan": [a.model_dump() for a in plan.actions],
            "screenshot": proof.snap(page, job_id, "blocked"),
        })
        return "queued"
    if not uploaded:
        _enqueue_review(conn, job_id, "low_confidence", {
            "url": form_url, "resume_uploaded": uploaded,
            "screenshot": proof.snap(page, job_id, "fill_failed"),
        })
        return "queued"

    # Second-opinion pass: a skeptical-recruiter model reviews every planned
    # answer for bot tells before anything is submitted. Free-text answers it
    # flags get rewritten in place; factual-field problems block the submit.
    try:
        check = field_mapper.verify_human(plan, job["title"])
    except Exception as e:  # noqa: BLE001 — verification must not crash a run
        check = None
        db.log_event(conn, "job", job_id, "human_check_skipped", str(e)[:200])
    if check is not None:
        protected = re.compile(r"visa|sponsor|salary|notice|email|phone|name",
                               re.IGNORECASE)
        free_text = {c["selector"] for c in schema
                     if c.get("type") in ("textarea", "text")
                     and not protected.search(c.get("label") or "")}
        applied = []
        for rw in check.rewrites:
            if rw.selector in free_text and rw.value:
                try:
                    page.locator(rw.selector).first.fill(rw.value, timeout=5000)
                    applied.append(rw.selector)
                except Exception:  # noqa: BLE001
                    continue
        if applied or check.issues:
            db.log_event(conn, "job", job_id, "human_check", {
                "human_like": check.human_like, "issues": check.issues,
                "rewrites_applied": applied})
        if not check.human_like and not applied:
            _enqueue_review(conn, job_id, "human_check_failed", {
                "url": form_url, "issues": check.issues,
                "screenshot": proof.snap(page, job_id, "human_check"),
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
    lifetime = conn.execute(
        "SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id=a.job_id "
        "WHERE j.company_id=? AND a.status NOT IN ('pending','failed')",
        (job["company_id"],),
    ).fetchone()[0]
    if lifetime >= caps["per_company_lifetime"]:
        conn.execute("UPDATE jobs SET status='skipped' WHERE id=?", (job_id,))
        conn.commit()
        return "company_cap"

    submit = filler.find_submit(page)
    if submit is None:
        _enqueue_review(conn, job_id, "submit_error",
                        {"url": form_url, "note": "no submit button found", "screenshot": shot})
        return "queued"

    # Humans take a minute+ to fill an application; submitting 20s after page
    # load is a classic bot signal (time-on-page check).
    dwell = random.uniform(15, 28) - (time.time() - page_opened)
    if dwell > 0:
        time.sleep(dwell)

    submit_ts = time.time()
    outcome, evidence = filler.submit_and_verify(page, submit)
    if outcome != "confirmed" and security_code.find_security_input(page):
        # Greenhouse emailed a security code — fetch it from Gmail and finish.
        db.log_event(conn, "job", job_id, "security_challenge", {"url": form_url})
        url_now = page.url
        code = security_code.complete_challenge(page, submit, submit_ts)
        if code:
            outcome, evidence = filler.watch_outcome(page, url_now)
            db.log_event(conn, "job", job_id, "security_code_entered",
                         {"outcome": outcome})
    conf_shot = proof.snap(page, job_id, "confirmation")
    dom = proof.save_dom(page, job_id)

    if outcome == "confirmed":
        conn.execute(
            "UPDATE applications SET method='browser', answers_json=?, status='submitted', "
            "submitted_at=datetime('now'), proof_screenshot=?, proof_dom=?, confirmation_text=?, "
            "predicted_chance=? WHERE job_id=?",
            (answers_audit, conf_shot, dom, evidence, job["selection_chance"], job_id),
        )
        conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
        cooldown = (date.today() + timedelta(days=caps["company_cooldown_days"])).isoformat()
        conn.execute("UPDATE companies SET cooldown_until=? WHERE id=?", (cooldown, job["company_id"]))
        conn.commit()
        db.counter_bump(conn, "applications")
        db.counter_bump(conn, f"apply_{strategy}")   # per-platform daily count
        if last_submit is not None:
            last_submit[strategy] = time.time()      # per-platform pacing clock
        db.log_event(conn, "job", job_id, "application_submitted", {"evidence": evidence})
        return "submitted"

    # error / uncertain / spam: never blind-retry — human eyes.
    reason = {"error": "submit_error", "spam": "spam_flagged"}.get(
        outcome, "submit_uncertain")
    _enqueue_review(conn, job_id, reason, {
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
        "SELECT j.*, a.resume_path, a.cover_path, c.tier AS company_tier, "
        "c.region AS company_region "
        "FROM jobs j JOIN applications a ON a.job_id = j.id "
        "LEFT JOIN companies c ON c.id = j.company_id "
        "WHERE a.resume_path IS NOT NULL AND a.status='pending' "
    )
    # Seed blocked platforms from earlier today (persisted) so the 19:00 rerun
    # and retries don't re-hit an ATS that captcha'd/blocked us this morning.
    blocked_ats: set = {
        s for s in ("smartrecruiters", "ashby", "greenhouse", "lever",
                    "workable", "generic")
        if db.counter_get(conn, f"blocked_{s}") > 0
    }
    if blocked_ats:
        print(f"apply: skipping platforms blocked earlier today: {sorted(blocked_ats)}")

    params: list = []
    if job_id:
        q += "AND j.id=? "
        params.append(job_id)
        jobs = conn.execute(q, params).fetchall()
    else:
        # chance and fit score are different scales — never mix them. Jobs judged
        # before selection_chance existed go last.
        q += ("AND j.status='apply_queued' "
              "ORDER BY j.selection_chance IS NULL, j.selection_chance DESC, "
              "j.score DESC")
        ranked = conn.execute(q, params).fetchall()
        # Exclude jobs on blocked platforms from selection — otherwise the top
        # chance-sorted jobs on a blocked ATS (e.g. lever) get re-picked every
        # batch, skipped, and starve the appliable (greenhouse/...) jobs behind
        # them. This was the "48 iters, 0 progress" stall.
        if blocked_ats:
            ranked = [r for r in ranked
                      if router.route(r["apply_url"] or r["url"] or "") not in blocked_ats]
        jobs = _tier_balanced(ranked, limit)
    if not jobs:
        print("Nothing to apply to (all queued jobs are on blocked platforms?).")
        return

    started = time.time()
    results: dict[str, int] = {}
    last_submit: dict[str, float] = {}
    with browser.open_page(headless=bool(caps.get("apply_headless", False))) as page:
        for job in jobs:
            if time.time() - started > caps["max_apply_minutes_per_run"] * 60:
                print("Run time budget exhausted.")
                break
            if not dry_run and db.counter_get(conn, "applications") >= caps["applications_per_day"]:
                print("Daily application cap reached.")
                break
            try:
                killswitch.check()
                outcome = _apply_one(conn, page, job, job, caps, dry_run,
                                     blocked_ats=blocked_ats,
                                     last_submit=last_submit)
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
            # ATS platforms see cross-company traffic from one session —
            # rapid-fire submits minutes apart triggered Greenhouse's
            # security-code challenges. Pace like a person browsing.
            time.sleep(random.uniform(8, 18))
    print(f"apply done ({'DRY RUN' if dry_run else 'LIVE'}): {results}")
