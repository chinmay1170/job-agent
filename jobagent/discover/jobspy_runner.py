"""python-jobspy scraper source (aggregate; not seed-based).

Scrapes Indeed+Bayt for Dubai and Indeed for Sydney, plus a small
best-effort LinkedIn guest search (ToS-gray: tiny volume, failures are
tolerated). jobspy is imported lazily so registering this source doesn't
drag pandas into every discover run.
"""
from __future__ import annotations

import random
import sqlite3
import time

from jobagent import db
from jobagent.discover import base

SEARCH_TERM = "senior backend engineer"
SCRAPES = [
    {"site_name": ["indeed", "bayt"], "location": "Dubai",
     "country_indeed": "united arab emirates", "region": "uae"},
    {"site_name": ["indeed"], "location": "Sydney NSW",
     "country_indeed": "australia", "region": "au"},
]


def _txt(value) -> str | None:
    """DataFrame cell -> clean str or None (handles NaN/None/dates)."""
    if value is None:
        return None
    try:
        import pandas as pd
        if pd.isna(value):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    s = str(value).strip()
    return s or None


def _scrape_sleep() -> None:
    time.sleep(random.uniform(5, 15))


def _insert_df(conn: sqlite3.Connection, df, region: str | None) -> int:
    inserted = 0
    for _, row in df.iterrows():
        title = _txt(row.get("title")) or ""
        name = _txt(row.get("company"))
        if not name or not base.title_passes(title):
            continue
        url = _txt(row.get("job_url"))
        site = _txt(row.get("site")) or "unknown"
        location = _txt(row.get("location"))
        fields = {"region": region} if region else {}
        company_id = db.upsert_company(conn, name, **fields)
        if base.insert_job(
            conn, company_id=company_id, company_name=name,
            source=f"jobspy_{site}", source_id=_txt(row.get("id")) or url or "",
            title=title, location=location, url=url, apply_url=url,
            description=_txt(row.get("description")),
            posted_at=_txt(row.get("date_posted")),
            remote=1 if "remote" in (location or "").lower() else 0,
        ):
            inserted += 1
    conn.commit()
    return inserted


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Run the configured scrapes. Returns (found, inserted)."""
    from jobspy import scrape_jobs  # lazy: heavy import

    found = inserted = 0
    for i, spec in enumerate(SCRAPES):
        if i:
            _scrape_sleep()
        try:
            df = scrape_jobs(
                site_name=spec["site_name"], search_term=SEARCH_TERM,
                location=spec["location"], results_wanted=25,
                country_indeed=spec["country_indeed"],
            )
        except Exception as e:  # noqa: BLE001 — one site failing is fine
            print(f"WARNING: jobspy {'+'.join(spec['site_name'])}/"
                  f"{spec['location']}: {type(e).__name__}: {e}")
            continue
        found += len(df)
        inserted += _insert_df(conn, df, spec["region"])

    # LinkedIn guest scrape: ToS-gray, keep volume small, accept failure.
    _scrape_sleep()
    try:
        df = scrape_jobs(site_name=["linkedin"], search_term=SEARCH_TERM,
                         results_wanted=20, linkedin_fetch_description=False)
        found += len(df)
        inserted += _insert_df(conn, df, None)
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: jobspy linkedin guest scrape failed "
              f"(tolerated): {type(e).__name__}: {e}")
    return found, inserted
