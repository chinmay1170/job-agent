"""Tailor stage: per-job resume + cover letter.

For each target job, an LLM (via jobagent.llm.ask_json) SELECTS and lightly
REPHRASES content from config/profile.yaml — it never invents. Identity,
skills items, dates, education and awards are taken verbatim from the
profile; the LLM only chooses ordering, bullet selection/wording, the summary
line, and the cover-letter paragraphs. Output PDFs land in
artifacts/{job_id}/ and an applications row is upserted.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from jobagent import config, db
from jobagent.llm import ask_json
from jobagent.tailor.render import render_cover, render_resume

ROOT = Path(__file__).resolve().parent.parent.parent
ARTIFACTS_DIR = ROOT / "artifacts"

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_SKILL_LABELS = {
    "languages_backend": "Languages & Backend",
    "cloud_devops_data": "Cloud, DevOps & Data",
    "ai_genai": "AI / GenAI",
}


# ── LLM plan schema ─────────────────────────────────────────────────────

class SelectedExperience(BaseModel):
    company: str = Field(description="Company name copied EXACTLY from the profile.")
    title: str = Field(description="Job title copied EXACTLY from the profile.")
    bullets: list[str] = Field(
        description="Selected bullets for this role, lightly rephrased from the "
                    "profile bullets only. Keep every number exactly as written."
    )


class SelectedProject(BaseModel):
    name: str = Field(description="Project name copied EXACTLY from the profile.")
    bullets: list[str] = Field(
        description="Selected bullets, lightly rephrased from the profile only."
    )


class TailorPlan(BaseModel):
    summary_line: str = Field(
        description="2-3 sentence professional summary rephrased ONLY from the "
                    "profile summary, angled at this job. No new claims or numbers."
    )
    skills_order: list[str] = Field(
        description="The profile skill CATEGORY KEYS (exactly as given), "
                    "reordered most-relevant-first. Do not invent keys."
    )
    selected_experience: list[SelectedExperience]
    selected_projects: list[SelectedProject]
    cover_letter_paragraphs: list[str] = Field(
        min_length=3, max_length=3,
        description="Exactly 3 short paragraphs, 180 words total maximum.",
    )


# ── helpers ─────────────────────────────────────────────────────────────

def _load_profile() -> dict:
    """Load config/profile.yaml, tolerating unquoted scalars containing ': '.

    The checked-in profile.yaml has values like
    ``degree: B.Tech, Electrical Engineering (Minor: CS)`` which are invalid
    YAML (yaml.safe_load raises "mapping values are not allowed here").
    We must not modify the file, so on parse failure we quote the offending
    scalar in-memory (guided by the parser's error mark) and retry.
    """
    try:
        return config.profile()
    except yaml.YAMLError:
        pass
    text = (ROOT / "config" / "profile.yaml").read_text()
    for _ in range(25):
        try:
            return yaml.safe_load(text)
        except yaml.MarkedYAMLError as e:
            mark = e.problem_mark or e.context_mark
            if mark is None:
                raise
            lines = text.split("\n")
            m = re.match(r'^(\s*(?:- )?[\w.-]+:\s+)([^"\'#&*>|].*?)(\s*)$',
                         lines[mark.line])
            if not m or '"' in m.group(2) or "\\" in m.group(2):
                raise
            lines[mark.line] = f'{m.group(1)}"{m.group(2)}"{m.group(3)}'
            text = "\n".join(lines)
    raise RuntimeError("config/profile.yaml could not be parsed")


def _fmt_ym(v) -> str:
    """'2025-02' -> 'Feb 2025'; 'present' -> 'Present'; 2017 -> '2017'."""
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in ("present", "now", "current"):
        return "Present"
    if "-" in s:
        parts = s.split("-")
        try:
            return f"{_MONTHS[int(parts[1]) - 1]} {parts[0]}"
        except (ValueError, IndexError):
            return s
    return s


def _dates(entry: dict) -> str:
    start, end = _fmt_ym(entry.get("start")), _fmt_ym(entry.get("end"))
    return f"{start} – {end}" if start and end else start or end


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split())


def _build_prompt(profile: dict, job: sqlite3.Row, company_name: str) -> str:
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    desc = (job["description"] or "")[:6000]
    skill_keys = ", ".join(profile.get("skills", {}).keys())
    return f"""You are tailoring a resume and cover letter for a specific job opening.

CANDIDATE PROFILE (the complete superset of true facts — YAML):
---
{profile_yaml}
---

TARGET JOB:
Company: {company_name}
Title: {job['title']}
Location: {job['location'] or 'unspecified'}
Description:
{desc}

TASK: Produce a tailoring plan.
- summary_line: rephrase the profile summary into 2-3 sentences angled at this job.
- skills_order: reorder these exact category keys by relevance: [{skill_keys}].
- selected_experience: pick the most relevant roles (keep every employer; you may
  trim bullets). Copy company and title EXACTLY as written in the profile. Pick
  3-5 bullets for the most relevant role, 2-4 for others, fewest for the oldest.
- selected_projects: pick at most 1 project with 2-3 bullets (or none if irrelevant).
- cover_letter_paragraphs: exactly 3 short paragraphs, 180 words TOTAL maximum,
  addressed to {company_name} for the {job['title']} role. Ground every claim in
  the profile.

HONESTY RULES (absolute): You may SELECT and lightly REPHRASE bullets from the
profile only. Never add skills, employers, metrics, or claims not present in
the profile. Keep every number exactly as written. Do not claim experience with
technologies the profile does not list, even if the job asks for them."""


# ── plan → template JSON ────────────────────────────────────────────────

def _resume_content(profile: dict, plan: TailorPlan) -> dict:
    ident = profile["identity"]
    contact = {
        "email": ident.get("email", ""),
        "phone": ident.get("phone", ""),
        "linkedin": ident.get("linkedin", ""),
        "location": ident.get("location", ""),
    }

    # Skills: categories reordered per plan, items verbatim from profile.
    prof_skills = profile.get("skills", {})
    ordered = [k for k in plan.skills_order if k in prof_skills]
    ordered += [k for k in prof_skills if k not in ordered]
    skills = [
        {"label": _SKILL_LABELS.get(k, k.replace("_", " ").title()),
         "items": list(prof_skills[k])}
        for k in ordered
    ]

    # Experience: keep profile order/dates; take LLM-selected bullets when the
    # entry matches, otherwise fall back to the profile bullets verbatim.
    sel_exp = {(_norm(e.company), _norm(e.title)): e.bullets
               for e in plan.selected_experience}
    experience = []
    for entry in profile.get("experience", []):
        key = (_norm(entry["company"]), _norm(entry["title"]))
        bullets = sel_exp.get(key) or [b["text"].strip() for b in entry["bullets"]]
        experience.append({
            "company": entry["company"],
            "title": entry["title"],
            "dates": _dates(entry),
            "bullets": [b.strip() for b in bullets],
        })

    # Projects: only those the LLM selected, matched back to profile entries.
    sel_proj = {_norm(p.name): p.bullets for p in plan.selected_projects}
    projects = []
    for entry in profile.get("projects", []):
        match = next((b for n, b in sel_proj.items()
                      if n == _norm(entry["name"])
                      or n in _norm(entry["name"]) or _norm(entry["name"]) in n),
                     None)
        if match:
            projects.append({
                "name": entry["name"],
                "stack": entry.get("stack", ""),
                "dates": _dates(entry),
                "bullets": [b.strip() for b in match],
            })

    education = [
        {"school": e["school"], "degree": e["degree"],
         "detail": f"GPA {e['gpa']}" if e.get("gpa") else "",
         "dates": _dates(e)}
        for e in profile.get("education", [])
    ]

    return {
        "name": ident["full_name"],
        "headline": profile.get("headline", ""),
        "contact": contact,
        "summary": plan.summary_line.strip(),
        "skills": skills,
        "experience": experience,
        "projects": projects,
        "education": education,
        "awards": list(profile.get("awards", [])),
    }


def _cover_content(profile: dict, plan: TailorPlan, job: sqlite3.Row,
                   company_name: str) -> dict:
    ident = profile["identity"]
    return {
        "name": ident["full_name"],
        "contact": {
            "email": ident.get("email", ""),
            "phone": ident.get("phone", ""),
            "linkedin": ident.get("linkedin", ""),
            "location": ident.get("location", ""),
        },
        "company": company_name,
        "role": job["title"],
        "body_paragraphs": [p.strip() for p in plan.cover_letter_paragraphs],
        "closing": "Sincerely,",
    }


# ── orchestration ───────────────────────────────────────────────────────

def _target_jobs(conn: sqlite3.Connection, job_id: int | None,
                 all_queued: bool) -> list[sqlite3.Row]:
    base = ("SELECT j.*, c.name AS company_name FROM jobs j "
            "LEFT JOIN companies c ON c.id = j.company_id ")
    if job_id is not None:
        row = conn.execute(base + "WHERE j.id = ?", (job_id,)).fetchone()
        if row is None:
            raise SystemExit(f"tailor: job {job_id} not found")
        return [row]
    if all_queued:
        return conn.execute(
            base +
            "LEFT JOIN applications a ON a.job_id = j.id "
            "WHERE j.status = 'apply_queued' "
            "AND (a.id IS NULL OR a.resume_path IS NULL) "
            "ORDER BY j.id"
        ).fetchall()
    raise SystemExit("tailor: pass --job-id <id> or --all-queued")


def _tailor_one(conn: sqlite3.Connection, job: sqlite3.Row, profile: dict) -> None:
    company_name = job["company_name"] or "the company"
    print(f"[tailor] job {job['id']}: {job['title']} @ {company_name}")

    plan = ask_json(_build_prompt(profile, job, company_name), TailorPlan,
                    model="sonnet")

    resume_content = _resume_content(profile, plan)
    cover_content = _cover_content(profile, plan, job, company_name)

    out_dir = ARTIFACTS_DIR / str(job["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    resume_pdf = out_dir / "resume.pdf"
    cover_pdf = out_dir / "cover.pdf"

    render_resume(resume_content, resume_pdf)
    render_cover(cover_content, cover_pdf)
    (out_dir / "content.json").write_text(
        json.dumps({"resume": resume_content, "cover": cover_content,
                    "plan": plan.model_dump()},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    conn.execute(
        "INSERT INTO applications (job_id, resume_path, cover_path) "
        "VALUES (?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "resume_path = excluded.resume_path, cover_path = excluded.cover_path",
        (job["id"], str(resume_pdf), str(cover_pdf)),
    )
    conn.commit()
    db.log_event(conn, "job", job["id"], "tailored",
                 {"resume_path": str(resume_pdf), "cover_path": str(cover_pdf)})
    print(f"[tailor] job {job['id']}: wrote {resume_pdf} and {cover_pdf}")


def run_tailor(job_id: int | None = None, all_queued: bool = False) -> None:
    conn = db.connect()
    try:
        jobs = _target_jobs(conn, job_id, all_queued)
        if not jobs:
            print("[tailor] nothing to do (no queued jobs without a resume)")
            return
        profile = _load_profile()
        failures = 0
        for job in jobs:
            try:
                _tailor_one(conn, job, profile)
            except Exception as e:  # keep batch going; surface at the end
                failures += 1
                print(f"[tailor] job {job['id']} FAILED: {e}")
                db.log_event(conn, "job", job["id"], "tailor_failed", str(e)[:500])
        if failures:
            raise SystemExit(f"tailor: {failures}/{len(jobs)} job(s) failed")
    finally:
        conn.close()
