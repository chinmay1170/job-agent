"""HN "Ask HN: Who is hiring?" source (aggregate; not seed-based).

Feeds the email-outreach channel: precision over recall. A top-level comment
is only inserted when it BOTH looks like a relevant role AND mentions
visa/sponsorship/relocation. The full plain-text comment is stored as the
description so the outreach contact harvester can regex emails out of it
(jobs.source='hn').
"""
from __future__ import annotations

import re
import sqlite3

from jobagent import db
from jobagent.discover import base

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"

_HIRING_TITLE = re.compile(r"who is hiring", re.I)
# Loose relevance gate over the whole comment.
_ROLEISH = re.compile(
    r"senior|backend|java|platform|distributed|software\s+engineer", re.I)
_VISA = re.compile(r"visa|sponsor|relocat", re.I)
# Header-segment heuristics for "Company | Role | Location | ..." first lines.
_TITLE_SEG = re.compile(
    r"engineer|developer|backend|platform|software|architect|sre|devops", re.I)
_LOC_SEG = re.compile(r"\b(remote|onsite|on-site|hybrid)\b", re.I)
_REMOTE = re.compile(r"\bremote\b", re.I)


def _latest_hiring_story() -> dict | None:
    data = base.get_json(SEARCH, params={
        "tags": "story,author_whoishiring",
        "query": '"who is hiring"',
        "hitsPerPage": 5,
    })
    for hit in data.get("hits", []):  # search_by_date: newest first
        if _HIRING_TITLE.search(hit.get("title") or ""):
            return hit
    return None


def _parse_header(first_line: str) -> tuple[str | None, str | None, str | None]:
    """'Company | Role | Location | ...' -> (company, title, location)."""
    segs = [s.strip() for s in first_line.split("|") if s.strip()]
    if not segs:
        return None, None, None
    company = segs[0][:80]
    title = next((s for s in segs[1:] if _TITLE_SEG.search(s)), None)
    location = next(
        (s for s in segs[1:]
         if s != title and (_LOC_SEG.search(s) or "," in s)),
        None)
    return company, title and title[:120], location and location[:100]


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Walk the latest Who-is-hiring thread. Returns (found, inserted)."""
    story = _latest_hiring_story()
    if story is None:
        print("WARNING: hn: no 'Who is hiring?' story found via Algolia.")
        return 0, 0
    base.polite_sleep()
    item = base.get_json(ITEM.format(id=story["objectID"]))
    children = item.get("children") or []
    inserted = 0
    for child in children:
        text = base.strip_html(child.get("text"))
        if not text:
            continue  # deleted/empty comment
        if not (_ROLEISH.search(text) and _VISA.search(text)):
            continue
        name, title, location = _parse_header(text.splitlines()[0])
        if not name:
            continue
        title = title or text.splitlines()[0][:120]
        company_id = db.upsert_company(conn, name)
        if base.insert_job(
            conn, company_id=company_id, company_name=name, source="hn",
            source_id=str(child.get("id")), title=title, location=location,
            url=HN_ITEM_URL.format(id=child.get("id")),
            apply_url=None,
            description=text,  # full comment: contact harvester regexes emails
            posted_at=child.get("created_at"),
            remote=1 if _REMOTE.search(text) else 0,
        ):
            inserted += 1
    conn.commit()
    return len(children), inserted
