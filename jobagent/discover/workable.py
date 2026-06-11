"""Workable widget API source.

Descriptions need a per-job call, so they are fetched lazily and only for
jobs that pass the cheap title filter, to limit request volume.
"""
from __future__ import annotations

import sqlite3

import httpx

from jobagent import db
from jobagent.discover import base

LIST_API = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
DETAIL_API_V1 = "https://apply.workable.com/api/v1/widget/jobs/{shortcode}"
DETAIL_API_V2 = "https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}"


def _fetch_description(slug: str, shortcode: str) -> str:
    """v1 widget detail endpoint, falling back to v2 (v1 currently 404s)."""
    for url, params in (
        (DETAIL_API_V1.format(shortcode=shortcode), {"account": slug}),
        (DETAIL_API_V2.format(slug=slug, shortcode=shortcode), None),
    ):
        try:
            detail = base.get_json(url, params=params)
        except (httpx.HTTPError, ValueError):
            continue
        parts = [detail.get("description"), detail.get("requirements"),
                 detail.get("benefits")]
        text = "\n\n".join(base.strip_html(p) for p in parts if p)
        if text:
            return text
    return ""


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Fetch one Workable account. Returns (found, inserted)."""
    slug, name = company["slug"], company["name"]
    data = base.get_json(LIST_API.format(slug=slug))
    jobs = data.get("jobs", [])
    company_id = db.upsert_company(conn, name, ats_type="workable",
                                   ats_slug=slug, region=company.get("region"))
    inserted = 0
    for job in jobs:
        title = job.get("title") or ""
        shortcode = job.get("shortcode") or ""
        location = ", ".join(p for p in (job.get("city"), job.get("country")) if p)
        description = ""
        if base.title_passes(title):  # lazy: only spend a call on plausible titles
            base.polite_sleep()
            description = _fetch_description(slug, shortcode)
        if base.insert_job(
            conn, company_id=company_id, company_name=name, source="workable",
            source_id=shortcode, title=title, location=location,
            url=job.get("url"), apply_url=job.get("url"),
            description=description, posted_at=job.get("published_on") or job.get("created_at"),
            remote=1 if job.get("telecommuting") else 0,
        ):
            inserted += 1
    conn.commit()
    return len(jobs), inserted
