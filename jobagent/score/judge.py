"""LLM judge: fit-score prefilter survivors in batches via claude -p."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from jobagent import config, db, llm

BATCH_SIZE = 15
DESC_TRUNC = 1500
BORDERLINE_FLOOR = 60


class JudgeScore(BaseModel):
    job_id: int
    fit_score: int = Field(ge=0, le=100)
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
    return (
        f"CANDIDATE PROFILE\n"
        f"Headline: {p.get('headline', '')}\n"
        f"Location: {ident.get('location', 'India')} | needs visa sponsorship: yes | "
        f"willing to relocate: yes | target regions: "
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
        "For EACH job, score fit for a senior backend / distributed-systems "
        "engineer who needs visa sponsorship + relocation support. "
        "fit_score (0-100) reflects how well the role matches the candidate's "
        "seniority, stack (Java/Spring Boot, Kafka, AWS, distributed systems) "
        "and trajectory. sponsorship_confidence reflects how confident the "
        "posting/company suggests visa sponsorship is realistic for an "
        "India-based candidate (high/medium/low). reasons: short bullets for "
        "the score; red_flags: anything concerning (sponsorship doubts, "
        "stack mismatch, seniority mismatch, contract/agency role).",
        "",
        "JOBS:",
    ]
    for job in batch:
        desc = (job["description"] or "")[:DESC_TRUNC]
        lines += [
            f"--- job_id: {job['id']}",
            f"title: {job['title']}",
            f"company: {job['company_name']}",
            f"location: {job['location'] or 'unknown'}",
            f"description: {desc or '(no description available)'}",
        ]
    lines.append("\nReturn one scores[] entry per job_id above, no extras.")
    return "\n".join(lines)


def run_judge(limit: int = 60) -> dict:
    conn = db.connect()
    threshold = config.caps().get("judge_fit_threshold", 70)
    jobs = conn.execute(
        "SELECT j.id, j.title, j.location, j.description, c.name AS company_name "
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
        try:
            result = llm.ask_json(_build_prompt(profile, batch), JudgeBatch,
                                  model="sonnet")
        except llm.LLMError as e:
            print(f"ERROR: judge batch {totals['batches']} failed: {e}")
            db.log_event(conn, "score", None, "judge_batch_error",
                         {"batch": totals["batches"], "error": str(e)[:300]})
            continue

        seen: set[int] = set()
        for s in result.scores:
            if s.job_id not in batch_ids or s.job_id in seen:
                continue  # guard against hallucinated/duplicated ids
            seen.add(s.job_id)
            if s.fit_score >= threshold and s.sponsorship_confidence != "low":
                status = "apply_queued"
            elif s.fit_score >= BORDERLINE_FLOOR:
                status = "scored"  # borderline — surfaces in digest
            else:
                status = "skipped"
            totals[status] += 1
            conn.execute(
                "UPDATE jobs SET status=?, score=?, score_reasons=? WHERE id=?",
                (status, s.fit_score,
                 json.dumps({"reasons": s.reasons, "red_flags": s.red_flags,
                             "sponsorship_confidence": s.sponsorship_confidence},
                            ensure_ascii=False),
                 s.job_id))
        conn.commit()
        missing = batch_ids - seen  # left as 'prefiltered'; retried next run
        totals["missing"] += len(missing)
        db.log_event(conn, "score", None, "judge_batch_done",
                     {"batch": totals["batches"], "jobs": len(batch),
                      "scored": len(seen), "missing": sorted(missing)})
        print(f"judge batch {totals['batches']}: {len(seen)}/{len(batch)} scored")

    print(f"judge: {len(jobs)} prefiltered -> {totals['apply_queued']} apply_queued, "
          f"{totals['scored']} scored (borderline), {totals['skipped']} skipped, "
          f"{totals['missing']} missing (left for retry)")
    conn.close()
    return totals
