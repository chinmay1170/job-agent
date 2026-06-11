"""Ashby posting API source."""
from __future__ import annotations

import sqlite3

from jobagent import db
from jobagent.discover import base

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Fetch one Ashby job board. Returns (found, inserted)."""
    slug, name = company["slug"], company["name"]
    data = base.get_json(API.format(slug=slug), params={"includeCompensation": "true"})
    jobs = data.get("jobs", [])
    company_id = db.upsert_company(conn, name, ats_type="ashby",
                                   ats_slug=slug, region=company.get("region"))
    inserted = 0
    for job in jobs:
        location = job.get("location")
        description = job.get("descriptionPlain") or base.strip_html(job.get("descriptionHtml"))
        if base.insert_job(
            conn, company_id=company_id, company_name=name, source="ashby",
            source_id=str(job.get("id")), title=(job.get("title") or "").strip(),
            location=location, url=job.get("jobUrl"),
            apply_url=job.get("applyUrl") or job.get("jobUrl"),
            description=description, posted_at=job.get("publishedAt"),
            remote=1 if job.get("isRemote") else 0,
        ):
            inserted += 1
    conn.commit()
    return len(jobs), inserted
