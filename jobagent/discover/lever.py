"""Lever postings API source."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import httpx

from jobagent import db
from jobagent.discover import base

API = "https://api.lever.co/v0/postings/{slug}"
API_EU = "https://api.eu.lever.co/v0/postings/{slug}"


def _created_iso(ms: int | str | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Fetch one Lever board (US endpoint, EU fallback on 404)."""
    slug, name = company["slug"], company["name"]
    try:
        postings = base.get_json(API.format(slug=slug), params={"mode": "json"})
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        postings = base.get_json(API_EU.format(slug=slug), params={"mode": "json"})
    company_id = db.upsert_company(conn, name, ats_type="lever",
                                   ats_slug=slug, region=company.get("region"))
    inserted = 0
    for job in postings:
        location = (job.get("categories") or {}).get("location")
        hosted = job.get("hostedUrl")
        remote = 1 if (job.get("workplaceType") == "remote"
                       or "remote" in (location or "").lower()) else 0
        if base.insert_job(
            conn, company_id=company_id, company_name=name, source="lever",
            source_id=str(job.get("id")), title=job.get("text") or "",
            location=location, url=hosted,
            apply_url=(hosted + "/apply") if hosted else None,
            description=job.get("descriptionPlain") or base.strip_html(job.get("description")),
            posted_at=_created_iso(job.get("createdAt")), remote=remote,
        ):
            inserted += 1
    conn.commit()
    return len(postings), inserted
