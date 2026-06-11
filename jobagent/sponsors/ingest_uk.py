"""UK — register of licensed sponsors (Workers).

Source page: https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers
The page links to a CSV on assets.publishing.service.gov.uk whose filename
changes daily (e.g. ``2026-06-11_-_Worker_and_Temporary_Worker.csv``). We
scrape the current link from the page, stream-download it to
data/sponsors/uk.csv (~100k rows), and ingest only rows whose ``Route``
column includes "Skilled Worker".

Organisations appear once per route/rating combination, so names are
deduplicated before upserting.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import date

from jobagent import db
from jobagent.sponsors import _common, match

PAGE_URL = (
    "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
)
DATASET = "uk_register_licensed_sponsors_workers"
CSV_NAME = "uk.csv"

_CSV_LINK_RE = re.compile(
    r'https://assets\.publishing\.service\.gov\.uk/[^"\']+\.csv'
)


def resolve_csv_url() -> str:
    page = _common.fetch_text(PAGE_URL)
    links = _CSV_LINK_RE.findall(page)
    if not links:
        raise ValueError("gov.uk sponsor register page: no .csv link found")
    # Prefer the Worker register if several CSVs are linked.
    for link in links:
        if "worker" in link.lower():
            return link
    return links[0]


def _refresh_csv() -> "_common.Path":
    dest = _common.sponsors_dir() / CSV_NAME
    if _common.is_fresh(dest):
        return dest
    url = resolve_csv_url()
    _common.download(url, dest)
    return dest


def ingest_uk(conn: sqlite3.Connection) -> int:
    """Download (if stale) and ingest Skilled Worker sponsors. Returns #companies."""
    path = _refresh_csv()
    year = date.today().year
    seen: dict[str, str] = {}  # name_norm -> original name
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        route_col = next(
            (c for c in reader.fieldnames or [] if c and "route" in c.lower()), None
        )
        name_col = next(
            (c for c in reader.fieldnames or [] if c and "organisation" in c.lower()),
            None,
        )
        if not route_col or not name_col:
            raise ValueError(f"uk.csv: unexpected columns {reader.fieldnames}")
        for row in reader:
            route = (row.get(route_col) or "")
            if "skilled worker" not in route.lower():
                continue
            name = (row.get(name_col) or "").strip()
            if not name:
                continue
            seen.setdefault(db.normalize_company(name), name)
    for name in seen.values():
        match.upsert_register_row(conn, name, "uk", DATASET, year)
    return len(seen)
