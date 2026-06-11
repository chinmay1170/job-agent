"""SmartRecruiters public postings API source.

Posting text requires a per-posting detail call, fetched lazily and only for
jobs passing the cheap title filter.
"""
from __future__ import annotations

import sqlite3

import httpx

from jobagent import db
from jobagent.discover import base

LIST_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{post_id}"
APPLY_URL = "https://jobs.smartrecruiters.com/{slug}/{post_id}"
PAGE_LIMIT = 100


def _fetch_description(slug: str, post_id: str) -> str:
    try:
        detail = base.get_json(DETAIL_API.format(slug=slug, post_id=post_id))
    except (httpx.HTTPError, ValueError):
        return ""
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    parts = []
    for sec in sections.values():
        if isinstance(sec, dict) and sec.get("text"):
            parts.append(base.strip_html(sec["text"]))
    return "\n\n".join(parts)


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Fetch one SmartRecruiters company (paginated). Returns (found, inserted)."""
    slug, name = company["slug"], company["name"]
    company_id = db.upsert_company(conn, name, ats_type="smartrecruiters",
                                   ats_slug=slug, region=company.get("region"))
    found = inserted = offset = 0
    while True:
        data = base.get_json(LIST_API.format(slug=slug),
                             params={"limit": PAGE_LIMIT, "offset": offset})
        postings = data.get("content", [])
        found += len(postings)
        for job in postings:
            post_id = str(job.get("id"))
            title = job.get("name") or ""
            loc = job.get("location") or {}
            location = loc.get("fullLocation") or ", ".join(
                p for p in (loc.get("city"), loc.get("country")) if p)
            description = ""
            if base.title_passes(title):  # lazy detail fetch
                base.polite_sleep()
                description = _fetch_description(slug, post_id)
            if base.insert_job(
                conn, company_id=company_id, company_name=name,
                source="smartrecruiters", source_id=post_id, title=title,
                location=location,
                url=APPLY_URL.format(slug=slug, post_id=post_id),
                apply_url=APPLY_URL.format(slug=slug, post_id=post_id),
                description=description, posted_at=job.get("releasedDate"),
                remote=1 if loc.get("remote") else 0,
            ):
                inserted += 1
        total = data.get("totalFound", 0)
        offset += len(postings)
        if not postings or offset >= total:
            break
        base.polite_sleep()
    conn.commit()
    return found, inserted
