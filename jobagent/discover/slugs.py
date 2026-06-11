"""Harvest ATS board slugs from arbitrary apply URLs.

When other sources discover jobs whose apply URLs point at a known ATS,
`harvest` extracts (ats, slug) so the whole board can be registered for
direct discovery via `register_company_board`.
"""
from __future__ import annotations

import re
import sqlite3

from jobagent import db

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?(?:[^#]*&)?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.\-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
]

# Path segments that are never company slugs.
_NOT_SLUGS = {"api", "j", "embed", "jobs", "v1", "v2"}


def harvest(url: str | None) -> tuple[str, str] | None:
    """Extract (ats, slug) from an apply/board URL, or None if unrecognized."""
    if not url:
        return None
    for ats, pat in _PATTERNS:
        m = pat.search(url)
        if m:
            slug = m.group(1)
            if slug.lower() in _NOT_SLUGS:
                continue
            return ats, slug
    return None


def register_company_board(conn: sqlite3.Connection, ats: str, slug: str,
                           name: str, region: str | None = None) -> int:
    """Upsert the company with its board coordinates; returns company id."""
    fields: dict = {"ats_type": ats, "ats_slug": slug}
    if region:
        fields["region"] = region
    company_id = db.upsert_company(conn, name, **fields)
    db.log_event(conn, "company", company_id, "board_registered",
                 {"ats": ats, "slug": slug})
    return company_id
