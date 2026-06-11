"""Australia — Home Affairs approved (standard business) sponsors list.

We query the data.gov.au CKAN API
(``https://data.gov.au/data/api/3/action/package_search?q=...``) for the
"standard business sponsor" / approved sponsors dataset and download the
first CSV resource found to data/sponsors/au.csv.

IMPORTANT (verified 2026-06-11): the Department of Home Affairs has WITHDRAWN
the public sponsor list from data.gov.au — the only `immi` datasets left are
aggregate visa-statistics pivot tables (no sponsor names), so the CKAN lookup
is expected to come up empty until/unless the dataset is republished.

Manual fallback:
  1. Obtain the current approved business sponsors list — Home Affairs
     publishes occasional snapshots at
     https://www.homeaffairs.gov.au (search "approved business sponsors"),
     and FOI/disclosure-log releases contain the full list.
  2. Save it as data/sponsors/au.csv. Any CSV works as long as one column
     header contains "sponsor", "organisation", "business" or "name"
     (that column is treated as the company name).
  3. Re-run: uv run jobagent sponsors ingest --au
You can also pass an explicit path: ingest_au(path="/path/to/sponsors.csv").
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date

import httpx

from jobagent import db
from jobagent.sponsors import _common, match

CKAN_SEARCH = "https://data.gov.au/data/api/3/action/package_search"
SEARCH_QUERIES = (
    '"standard business sponsor"',
    "approved business sponsors",
    "business sponsor list",
)
DATASET = "au_approved_business_sponsors"
CSV_NAME = "au.csv"

_NAME_HINTS = ("sponsor", "organisation", "organization", "business", "name", "employer")


def resolve_csv_url() -> str | None:
    """Find a CSV resource for the sponsor list via the CKAN API, or None."""
    with httpx.Client(headers=_common.HEADERS, timeout=30, follow_redirects=True) as client:
        for q in SEARCH_QUERIES:
            try:
                r = client.get(CKAN_SEARCH, params={"q": q, "rows": 20})
                r.raise_for_status()
                result = r.json().get("result", {})
            except (httpx.HTTPError, json.JSONDecodeError):
                continue
            for pkg in result.get("results", []):
                blob = f"{pkg.get('name','')} {pkg.get('title','')}".lower()
                if "sponsor" not in blob:
                    continue
                org = (pkg.get("organization") or {}).get("name", "")
                if org and org not in ("immi", "departmentofimmigrationandborderprotection"):
                    continue  # only Home Affairs / immigration datasets
                for res in pkg.get("resources", []):
                    if (res.get("format") or "").upper() == "CSV" and res.get("url"):
                        return res["url"]
    return None


def _refresh_csv(path: str | None) -> "_common.Path | None":
    if path:
        return _common.Path(path)
    dest = _common.sponsors_dir() / CSV_NAME
    if _common.is_fresh(dest):
        return dest
    url = resolve_csv_url()
    if url:
        try:
            _common.download(url, dest)
            return dest
        except httpx.HTTPError as e:
            print(f"  au: CSV download failed ({e})")
    if dest.exists():  # manual drop or stale cache
        return dest
    return None


def ingest_au(conn: sqlite3.Connection, path: str | None = None) -> int:
    """Ingest the AU sponsor list. Returns #companies (0 if no data available)."""
    csv_path = _refresh_csv(path)
    if csv_path is None or not csv_path.exists():
        print(
            "  au: sponsor list not available on data.gov.au and no manual file at "
            f"{_common.SPONSORS_DIR / CSV_NAME} — see jobagent/sponsors/ingest_au.py "
            "docstring for the manual download path."
        )
        return 0
    year = date.today().year
    seen: dict[str, str] = {}
    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        name_col = next(
            (
                c
                for hint in _NAME_HINTS
                for c in (reader.fieldnames or [])
                if c and hint in c.lower()
            ),
            None,
        )
        if not name_col:
            raise ValueError(f"au.csv: no company-name column in {reader.fieldnames}")
        for row in reader:
            name = (row.get(name_col) or "").strip()
            if not name:
                continue
            seen.setdefault(db.normalize_company(name), name)
    for name in seen.values():
        match.upsert_register_row(conn, name, "au", DATASET, year)
    return len(seen)
