"""Tailored-resume v2 pipeline: moderate JD-mirroring tailoring (honest) ->
honesty fact-check -> render into the approved v2 design.

Ground rules (set with the user 2026-06-23):
  0. HONESTY (non-negotiable): only reorder / re-emphasise / rephrase content
     already true in the master profile. Never invent skills/metrics/employers.
  1. Moderate depth: per-JD summary, skills reordered, bullets rephrased to the
     JD's language, true-but-buried skills surfaced.
  5. Honesty fact-check: a tailored bullet may not introduce a number/metric
     absent from the master bullet for that role — if it does, that role's
     bullets are reverted to the master verbatim.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from jobagent import config, db, llm
from jobagent.tailor.tailor import (
    TailorPlan, _build_prompt, _load_profile, _resume_content, _norm, ROOT,
)
from jobagent.tailor.render_html import render_resume_v2

_NUM = re.compile(r"\$?\d[\d,]*\.?\d*\s*(?:k|m|b|bn|%|\+|x)?", re.I)


def _nums(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", n).lower() for n in _NUM.findall(text or "")}


def _master_nums(profile: dict) -> set[str]:
    """All numeric tokens that legitimately appear anywhere in the master."""
    out: set[str] = set()
    out |= _nums(profile.get("summary", ""))
    for e in profile.get("experience", []) + profile.get("projects", []):
        for b in e.get("bullets", []):
            out |= _nums(b.get("text", ""))
    return out


def _role_headline(title: str) -> str:
    """Honest role-line that adapts the focus tag to the JD (still 'Senior SWE')."""
    t = (title or "").lower()
    if "platform" in t:    focus = "Platform & Distributed Systems"
    elif "data" in t or "pipeline" in t: focus = "Data & Distributed Systems"
    elif "infra" in t:     focus = "Infrastructure & Distributed Systems"
    elif "full" in t or "product" in t:  focus = "Backend & Full-Stack"
    else:                  focus = "Backend & Distributed Systems"
    return f"Senior Software Engineer · {focus}"


def _honesty_check(rc: dict, profile: dict) -> tuple[dict, list[str]]:
    """Revert any role whose tailored bullets add a number absent from the
    master. Returns (content, violations)."""
    master_all = _master_nums(profile)
    # per-role master bullets (verbatim) for reverting
    by_role = {(_norm(e["company"]), _norm(e["title"])):
               [b["text"].strip() for b in e["bullets"]]
               for e in profile.get("experience", [])}
    violations = []
    for entry in rc.get("experience", []):
        bad = {n for b in entry["bullets"] for n in _nums(b)} - master_all
        if bad:
            key = (_norm(entry["company"]), _norm(entry["title"]))
            if key in by_role:
                entry["bullets"] = by_role[key]
            violations.append(f"{entry['title']}: invented {sorted(bad)} -> reverted")
    # summary: if it adds a number not in the master, fall back to master summary
    if _nums(rc["summary"]) - master_all:
        rc["summary"] = profile.get("summary", "").strip()
        violations.append("summary: invented number -> reverted to master")
    return rc, violations


def _jd_keywords_matched(job: sqlite3.Row, content: dict) -> list[str]:
    """Keywords present in BOTH the JD and the tailored resume (for tracking)."""
    TERMS = ["java", "spring", "kafka", "aws", "gcp", "kubernetes", "docker",
             "microservic", "distributed", "event-driven", "event driven", "rest",
             "grpc", "postgres", "dynamodb", "cassandra", "redis", "terraform",
             "python", "go ", "golang", "scalab", "latency", "throughput",
             "observability", "resilien", "ci/cd", "platform", "infrastructure",
             "backend", "full stack", "full-stack", "api", "sql", "streaming",
             "data pipeline", "llm", "rag"]
    jd = (job["description"] or "").lower()
    blob = (content["summary"] + " " + " ".join(s["items"] for s in content["skills"])
            + " " + " ".join(b for e in content["experience"] for b in e["bullets"])).lower()
    return sorted({t.strip() for t in TERMS if t in jd and t in blob})


def _to_v2(rc: dict, job: sqlite3.Row) -> dict:
    edu = (rc.get("education") or [{}])[0]
    return {
        "name": rc["name"],
        "role": _role_headline(job["title"]),
        "email": rc["contact"]["email"],
        "phone": rc["contact"]["phone"],
        "linkedin": rc["contact"]["linkedin"],
        "summary": rc["summary"],
        "skills": [{"label": s["label"], "items": ", ".join(s["items"])} for s in rc["skills"]],
        "experience": [{"co": e["company"], "title": e["title"], "dates": e["dates"],
                        "bullets": e["bullets"]} for e in rc["experience"]],
        "projects": [{"name": p["name"], "stack": p.get("stack", ""), "dates": p["dates"],
                      "bullets": p["bullets"]} for p in rc["projects"]],
        "education": {"line": f"{edu.get('school','')} — {edu.get('degree','')}"
                              + (f" · {edu['detail']}" if edu.get("detail") else ""),
                      "when": edu.get("dates", "")},
        "awards": rc["awards"],
    }


def tailor_one_v2(conn: sqlite3.Connection, job_id: int) -> dict:
    """Tailor + render a v2 resume for one job. Writes artifacts/<id>/resume_v2.pdf
    and a cover; sets the application's resume_path/cover_path. Returns a summary."""
    job = conn.execute(
        "SELECT j.*, c.name AS company_name FROM jobs j "
        "LEFT JOIN companies c ON c.id=j.company_id WHERE j.id=?", (job_id,)).fetchone()
    if job is None:
        raise ValueError(f"job {job_id} not found")
    profile = _load_profile()
    plan = llm.ask_json(_build_prompt(profile, job, job["company_name"] or ""),
                        TailorPlan, model="sonnet", timeout=240)
    rc = _resume_content(profile, plan)
    rc, violations = _honesty_check(rc, profile)
    content = _to_v2(rc, job)

    art = ROOT / "artifacts" / str(job_id)
    resume_pdf = art / "resume_v2.pdf"
    meta = render_resume_v2(content, resume_pdf)
    cover_text = "\n\n".join(p.strip() for p in plan.cover_letter_paragraphs)
    (art).mkdir(parents=True, exist_ok=True)
    (art / "cover.txt").write_text(cover_text)
    keywords = _jd_keywords_matched(job, content)

    conn.execute(
        "UPDATE applications SET resume_path=?, cover_path=?, "
        "answers_json=COALESCE(answers_json,'{}') WHERE job_id=?",
        (str(resume_pdf), str(art / "cover.txt"), job_id))
    conn.commit()
    db.log_event(conn, "job", job_id, "tailored_v2",
                 {"keywords": keywords, "honesty_violations": violations,
                  "render": meta, "headline": content["role"]})
    return {"job_id": job_id, "company": job["company_name"], "title": job["title"],
            "resume_pdf": str(resume_pdf), "keywords": keywords,
            "honesty_violations": violations, "render": meta}


def tailor_highfit_batch(limit: int = 4) -> dict:
    """Driver entry point: tailor up to `limit` high-fit (>= tailor_min_chance)
    queued jobs not yet tailored into the v2 design. On any failure, fall back
    to the master resume so the job still applies. Returns counts."""
    caps = config.caps()
    minc = caps.get("tailor_min_chance", 35)
    master = str(ROOT / caps.get("original_resume_path", "config/Chinmay_Krishna_Resume.pdf"))
    conn = db.connect()
    # Only tailor jobs with NO resume yet (resume_path IS NULL). A job that
    # already has a path — tailored (resume_v2.pdf) OR a master fallback after a
    # rate-limit failure — is left alone, so a transient claude-p failure doesn't
    # get retry-hammered every iteration (which amplifies the rate-limit).
    ids = [r["id"] for r in conn.execute(
        "SELECT j.id FROM jobs j LEFT JOIN applications a ON a.job_id = j.id "
        "WHERE j.status='apply_queued' AND j.selection_chance >= ? "
        "AND a.resume_path IS NULL "
        "ORDER BY j.selection_chance DESC LIMIT ?", (minc, limit)).fetchall()]
    conn.close()
    done = fail = 0
    for jid in ids:
        try:
            c = db.connect()
            r = tailor_one_v2(c, jid)
            c.execute("UPDATE applications SET status='pending' WHERE job_id=? "
                      "AND status NOT IN ('submitted','rejected','interview')", (jid,))
            c.commit(); c.close()
            print(f"tailored {jid} kw={len(r['keywords'])} viol={len(r['honesty_violations'])}",
                  flush=True)
            done += 1
        except Exception as e:  # noqa: BLE001 — fall back to master, never strand
            c = db.connect()
            c.execute("INSERT OR IGNORE INTO applications(job_id,resume_path,status,created_at) "
                      "VALUES(?,?,'pending',datetime('now'))", (jid, master))
            c.execute("UPDATE applications SET resume_path=?, status='pending' WHERE job_id=? "
                      "AND status NOT IN ('submitted','rejected','interview')", (master, jid))
            c.commit(); c.close()
            print(f"tfail->master {jid}: {str(e)[:60]}", flush=True)
            fail += 1
    print(f"TAILOR_HF done: {done} tailored, {fail} fell back", flush=True)
    return {"tailored": done, "fallback": fail}
