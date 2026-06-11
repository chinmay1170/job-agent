"""RemoteOK public API source (aggregate; not seed-based).

The API returns one big JSON array whose FIRST element is a legal/metadata
blob, not a job — skip it. No server-side search, so the title gate does
all the filtering.
"""
from __future__ import annotations

import sqlite3

from jobagent import db
from jobagent.discover import base

API = "https://remoteok.com/api"


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Fetch the RemoteOK feed. Returns (found, inserted)."""
    data = base.get_json(API)
    jobs = data[1:] if isinstance(data, list) else []  # [0] is metadata
    inserted = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = job.get("position") or ""
        name = job.get("company") or ""
        if not name or not base.title_passes(title):
            continue
        url = job.get("url")
        company_id = db.upsert_company(conn, name)
        if base.insert_job(
            conn, company_id=company_id, company_name=name, source="remoteok",
            source_id=str(job.get("id")), title=title,
            location=job.get("location"), url=url,
            apply_url=job.get("apply_url") or url,
            description=base.strip_html(job.get("description")),
            posted_at=job.get("date"), remote=1,
        ):
            inserted += 1
    conn.commit()
    return len(jobs), inserted
