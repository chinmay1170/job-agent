"""Greenhouse job-board API source."""
from __future__ import annotations

import sqlite3

from jobagent import db
from jobagent.discover import base

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Fetch one Greenhouse board. Returns (found, inserted)."""
    slug, name = company["slug"], company["name"]
    data = base.get_json(API.format(slug=slug), params={"content": "true"})
    jobs = data.get("jobs", [])
    company_id = db.upsert_company(conn, name, ats_type="greenhouse",
                                   ats_slug=slug, region=company.get("region"))
    inserted = 0
    for job in jobs:
        title = job.get("title") or ""
        location = (job.get("location") or {}).get("name")
        url = job.get("absolute_url")
        if base.insert_job(
            conn, company_id=company_id, company_name=name, source="greenhouse",
            source_id=str(job.get("id")), title=title, location=location,
            url=url, apply_url=url,
            description=base.strip_html(job.get("content")),
            posted_at=job.get("updated_at"),
            remote=1 if "remote" in (location or "").lower() else 0,
        ):
            inserted += 1
    conn.commit()
    return len(jobs), inserted
