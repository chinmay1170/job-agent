"""Adzuna search API source (aggregate; not seed-based).

Needs ADZUNA_APP_ID / ADZUNA_APP_KEY in the environment (.env honored when
python-dotenv is installed); without keys this source prints a one-line
warning and skips. Country list comes from the region->adzuna mapping in
config/search.yaml. Every job URL is passed through slugs.harvest so that
jobs hosted on a known ATS register the company's board for direct discovery
on future runs.
"""
from __future__ import annotations

import os
import sqlite3

from jobagent import config, db
from jobagent.discover import base, slugs

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
WHAT = "senior backend engineer"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _country_regions() -> list[tuple[str, str]]:
    """[(adzuna country code, region key), ...] from config/search.yaml."""
    out: list[tuple[str, str]] = []
    for region, cfg in (config.search().get("regions") or {}).items():
        adz = (cfg or {}).get("adzuna")
        if not adz:
            continue
        for c in adz if isinstance(adz, list) else [adz]:
            out.append((c, region))
    return out


def fetch(conn: sqlite3.Connection, company: dict) -> tuple[int, int]:
    """Search each configured Adzuna country. Returns (found, inserted)."""
    _load_env()
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("WARNING: adzuna: ADZUNA_APP_ID/ADZUNA_APP_KEY not set; skipping.")
        return 0, 0

    found = inserted = 0
    for i, (country, region) in enumerate(_country_regions()):
        if i:
            base.polite_sleep()
        data = base.get_json(API.format(country=country), params={
            "app_id": app_id, "app_key": app_key, "what": WHAT,
            "max_days_old": 7, "results_per_page": 50,
        })
        results = data.get("results", [])
        found += len(results)
        for job in results:
            title = base.strip_html(job.get("title"))  # Adzuna bolds matches
            name = (job.get("company") or {}).get("display_name") or ""
            if not name or not base.title_passes(title):
                continue
            url = job.get("redirect_url")
            board = slugs.harvest(url)
            if board:  # known ATS -> register the whole board for next runs
                company_id = slugs.register_company_board(
                    conn, board[0], board[1], name, region=region)
            else:
                company_id = db.upsert_company(conn, name, region=region)
            location = (job.get("location") or {}).get("display_name")
            if base.insert_job(
                conn, company_id=company_id, company_name=name, source="adzuna",
                source_id=str(job.get("id")), title=title, location=location,
                url=url, apply_url=url,
                description=base.strip_html(job.get("description")),
                posted_at=job.get("created"),
                remote=1 if "remote" in f"{title} {location or ''}".lower() else 0,
            ):
                inserted += 1
        conn.commit()  # short transactions; sibling process writes too
    return found, inserted
