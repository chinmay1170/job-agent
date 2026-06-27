"""LLM judge: fit-score prefilter survivors in batches via claude -p."""
from __future__ import annotations

import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from jobagent import config, db, llm

BATCH_SIZE = 15
DESC_TRUNC = 1500


class JudgeScore(BaseModel):
    job_id: int
    fit_score: int = Field(ge=0, le=100)
    selection_chance: int = Field(
        ge=0, le=100,
        description="Estimated probability (0-100) of a POSITIVE recruiter "
                    "response (reply/screen/interview) for THIS candidate — "
                    "India-based, needs visa sponsorship + relocation.")
    sponsorship_confidence: Literal["high", "medium", "low"]
    reasons: list[str]
    red_flags: list[str]


class JudgeBatch(BaseModel):
    scores: list[JudgeScore]


def _profile_summary() -> str:
    p = config.profile()
    ident = p.get("identity", {})
    skills = p.get("skills", {})
    key_skills = ", ".join(
        (skills.get("languages_backend") or [])[:10]
        + (skills.get("cloud_devops_data") or [])[:8])
    sp = ("needs visa sponsorship: YES" if ident.get("needs_visa_sponsorship")
          else "needs visa sponsorship: no (work-authorized)")
    rl = ("willing to relocate: yes" if ident.get("willing_to_relocate", True)
          else "willing to relocate: no")
    return (
        f"CANDIDATE PROFILE\n"
        f"Headline: {p.get('headline', '')}\n"
        f"Seniority: SENIOR level (SDE3, ~5 years) — targets Senior / Lead IC "
        f"roles. ALSO open to mid-level (SDE2/SE2) roles when compensation is "
        f"strong — don't penalise those for being a step down. Staff / Principal "
        f"/ Director / VP / Head are a POOR fit (over-leveled).\n"
        f"Domain: BACKEND-leaning FULL-STACK / distributed-systems engineer "
        f"(Java/Spring, Kafka, AWS, microservices; also JavaScript/TypeScript) "
        f"— backend AND full-stack roles are a strong fit. NOT an ML / "
        f"data-science / research candidate — score core-ML, applied-science, "
        f"model-training and research roles LOW. He DOES have real GenAI-TOOLING "
        f"experience (LLM integration, RAG, MCP servers), so AI-PLATFORM / "
        f"AI-infra / GenAI-tooling / backend-for-AI roles ARE a strong fit too. "
        f"(Pure front-end-only roles are a weak fit — he is backend-strong.)\n"
        f"Location: {ident.get('location', '')} | {sp} | {rl} | target regions: "
        f"{', '.join(ident.get('target_regions', []))}\n"
        f"Summary: {p.get('summary', '').strip()}\n"
        f"Key skills: {key_skills}"
    )


def _build_prompt(profile: str, batch: list) -> str:
    lines = [
        "You are scoring job postings for the candidate below.",
        "",
        profile,
        "",
        "For EACH job, score it for a senior backend / distributed-systems "
        "engineer who needs visa sponsorship + relocation support.",
        "- fit_score (0-100): how well the role matches the candidate's "
        "seniority, stack (Java/Spring Boot, Kafka, AWS, distributed systems) "
        "and trajectory.",
        "- selection_chance (0-100): your honest estimate of the PROBABILITY "
        "this specific candidate gets a positive recruiter response "
        "(reply/screen/interview). Weigh ALL of: fit, seniority match, how "
        "likely the company sponsors a visa + relocates an India-based hire "
        "(known sponsors and roles that mention visa/relocation score higher; "
        "roles silent on sponsorship are uncertain, not zero; EU/UAE generally "
        "easier), role competition, and posting freshness. Be calibrated and "
        "realistic — most cold applications are long shots; reserve 60+ for "
        "genuinely strong matches at likely sponsors.",
        "- US ROLES ARE A PRIORITY for this candidate: do NOT down-rank US "
        "postings for H-1B difficulty — big US tech sponsors H-1B routinely. "
        "Score US roles on fit/sponsor-likelihood like any other region (a "
        "strong-fit US role at a known sponsor should clear the apply bar).",
        "- SPONSORSHIP REALISM (this is where past scores were too optimistic): "
        "an India-based hire needing visa + RELOCATION is a real cost. Reserve "
        "35%+ for (a) large, well-known H-1B sponsors (US big tech / scaled "
        "public cos) OR (b) any role whose posting EXPLICITLY mentions visa "
        "sponsorship / relocation. Small & mid-size NON-US fintechs/startups "
        "(typical EU/UK/AU) that are SILENT on sponsorship rarely sponsor an "
        "India relocation for a mid/senior IC — score those LOWER (≤25) and add "
        "a sponsorship red_flag. Do not assume 'EU/UAE easier' by default.",
        "- DOMAIN FIT: candidate is a BACKEND-leaning FULL-STACK engineer, not "
        "ML. Backend AND full-stack roles are a strong fit. Core-ML / "
        "applied-science / model-training / research / data-science roles are a "
        "POOR fit — score selection_chance LOW and add a domain red_flag. "
        "AI-platform / AI-infrastructure / model-SERVING-platform / "
        "GenAI-tooling / backend-for-AI roles ARE a strong fit too (real "
        "GenAI-tooling experience). Pure front-end-only roles are a weak fit.",
        "- SENIORITY: the candidate is Senior (SDE3, ~5y). If the title or "
        "description targets Staff/Principal/Director/VP/Head, or the posting "
        "requires materially more than ~6-7 years, treat it as a poor fit — "
        "lower selection_chance and note it in red_flags.",
        "- sponsorship_confidence (high/medium/low): how confident the "
        "posting/company suggests visa sponsorship is realistic.",
        "- reasons: short bullets; red_flags: concerns (sponsorship doubts, "
        "stack/seniority mismatch, over-senior level, too many years, "
        "contract/agency role).",
        "",
        "JOBS:",
    ]
    for job in batch:
        desc = (job["description"] or "")[:DESC_TRUNC]
        size_bits = [b for b in (
            (job["employee_count"] if "employee_count" in job.keys() else None),
            (job["market_cap"] if "market_cap" in job.keys() else None),
            (job["tier"] if "tier" in job.keys() else None),
        ) if b and b != "Unknown"]
        size_hint = f"  [{', '.join(size_bits)}]" if size_bits else ""
        lines += [
            f"--- job_id: {job['id']}",
            f"title: {job['title']}",
            f"company: {job['company_name']}{size_hint}",
            f"location: {job['location'] or 'unknown'}",
            f"description: {desc or '(no description available)'}",
        ]
    lines.append("\nReturn one scores[] entry per job_id above, no extras.")
    return "\n".join(lines)


def run_judge(limit: int = 60) -> dict:
    conn = db.connect()
    caps = config.caps()
    min_chance = caps.get("min_selection_chance", 50)
    borderline = max(20, min_chance - 20)  # show near-misses in the digest
    jobs = conn.execute(
        "SELECT j.id, j.title, j.location, j.description, c.name AS company_name, "
        "c.employee_count, c.market_cap, c.tier "
        "FROM jobs j JOIN companies c ON j.company_id = c.id "
        "WHERE j.status = 'prefiltered' ORDER BY j.id LIMIT ?",
        (limit,),
    ).fetchall()
    if not jobs:
        print("judge: no prefiltered jobs to score.")
        conn.close()
        return {"scored": 0}

    profile = _profile_summary()
    totals = {"apply_queued": 0, "scored": 0, "skipped": 0, "missing": 0, "batches": 0}

    for start in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[start:start + BATCH_SIZE]
        batch_ids = {j["id"] for j in batch}
        totals["batches"] += 1

        # claude -p rate-limits when fired back-to-back; pace + retry with
        # exponential backoff so a transient rc=1 doesn't drop a whole batch.
        result = None
        for attempt in range(4):
            try:
                result = llm.ask_json(_build_prompt(profile, batch), JudgeBatch,
                                      model="sonnet")
                break
            except llm.LLMError as e:
                wait = 10 * (2 ** attempt)  # 10, 20, 40, 80s
                print(f"  batch {totals['batches']} attempt {attempt + 1} failed "
                      f"({str(e)[:80]}); retrying in {wait}s")
                time.sleep(wait)
        if result is None:
            print(f"ERROR: judge batch {totals['batches']} gave up after 4 attempts")
            db.log_event(conn, "score", None, "judge_batch_error",
                         {"batch": totals["batches"]})
            continue

        seen: set[int] = set()
        for s in result.scores:
            if s.job_id not in batch_ids or s.job_id in seen:
                continue  # guard against hallucinated/duplicated ids
            seen.add(s.job_id)
            if s.selection_chance >= min_chance:
                status = "apply_queued"
            elif s.selection_chance >= borderline:
                status = "scored"  # near-miss — surfaces in digest
            else:
                status = "skipped"
            totals[status] += 1
            conn.execute(
                "UPDATE jobs SET status=?, score=?, selection_chance=?, "
                "score_reasons=? WHERE id=?",
                (status, s.fit_score, s.selection_chance,
                 json.dumps({"reasons": s.reasons, "red_flags": s.red_flags,
                             "sponsorship_confidence": s.sponsorship_confidence,
                             "selection_chance": s.selection_chance},
                            ensure_ascii=False),
                 s.job_id))
        conn.commit()
        missing = batch_ids - seen  # left as 'prefiltered'; retried next run
        totals["missing"] += len(missing)
        db.log_event(conn, "score", None, "judge_batch_done",
                     {"batch": totals["batches"], "jobs": len(batch),
                      "scored": len(seen), "missing": sorted(missing)})
        print(f"judge batch {totals['batches']}: {len(seen)}/{len(batch)} scored")
        time.sleep(3)  # pace successful batches to avoid rate-limiting

    print(f"judge: {len(jobs)} prefiltered -> {totals['apply_queued']} apply_queued, "
          f"{totals['scored']} scored (borderline), {totals['skipped']} skipped, "
          f"{totals['missing']} missing (left for retry)")
    conn.close()
    return totals
