"""Remotive public remote-jobs API source (aggregate; not seed-based)."""
from __future__ import annotations

import sqlite3

from jobagent import db
from jobagent.discover import base

API = "https://remotive.com/api/remote-jobs"
SEARCHES = ["backend", "java"]


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Fetch Remotive listings for each search term. Returns (found, inserted)."""
    found = inserted = 0
    for i, term in enumerate(SEARCHES):
        if i:
            base.polite_sleep()
        data = base.get_json(API, params={"search": term})
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        found += len(jobs)
        for job in jobs:
            title = job.get("title") or ""
            name = job.get("company_name") or ""
            if not name or not base.title_passes(title):
                continue
            url = job.get("url")
            company_id = db.upsert_company(conn, name)
            if base.insert_job(
                conn, company_id=company_id, company_name=name, source="remotive",
                source_id=str(job.get("id")), title=title,
                location=job.get("candidate_required_location"),
                url=url, apply_url=url,
                description=base.strip_html(job.get("description")),
                posted_at=job.get("publication_date"), remote=1,
            ):
                inserted += 1
        conn.commit()  # keep transactions short; sibling process writes too
    return found, inserted
