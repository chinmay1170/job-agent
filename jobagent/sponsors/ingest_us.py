"""US — USCIS H-1B Employer Data Hub annual export.

Source: https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub
The interactive hub itself is a Tableau embed with no machine-readable feed,
but USCIS keeps annual CSV exports at
``https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-<FY>.csv``
(linked from https://www.uscis.gov/archive/h-1b-employer-data-hub-files).
As of 2026-06 the most recent export is FY2023; we probe recent fiscal years
downwards and take the first that exists. Employers are aggregated across
rows (one row per employer/location) and ingested when
initial+continuing approvals >= 5 in that year.

Manual fallback (if USCIS/Akamai blocks programmatic downloads):
  1. Open https://www.uscis.gov/archive/h-1b-employer-data-hub-files in a
     browser and download the latest fiscal-year CSV, or use the hub's
     "Download" button after filtering to the latest fiscal year.
  2. Save it as data/sponsors/us_h1b.csv (keep the original header row:
     Fiscal Year, Employer, Initial Approval, ..., Continuing Approval, ...).
  3. Re-run: uv run jobagent sponsors ingest --us
You can also pass an explicit path: ingest_us(path="/path/to/export.csv").
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import date

import httpx

from jobagent.sponsors import _common, match

EXPORT_URL_TMPL = (
    "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{year}.csv"
)
DATASET = "uscis_h1b_employer_data_hub"
CSV_NAME = "us_h1b.csv"
MIN_APPROVALS = 5


def _int(v: str | None) -> int:
    try:
        return int((v or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0


def _refresh_csv(path: str | None) -> "_common.Path":
    if path:
        return _common.Path(path)
    dest = _common.sponsors_dir() / CSV_NAME
    if _common.is_fresh(dest):
        return dest
    last_err: Exception | None = None
    for year in range(date.today().year, 2018, -1):  # newest first
        try:
            _common.download(EXPORT_URL_TMPL.format(year=year), dest)
            return dest
        except httpx.HTTPError as e:
            last_err = e
    if dest.exists():  # stale cache beats nothing
        print(f"  us: download failed ({last_err}); using stale {dest}")
        return dest
    raise RuntimeError(
        f"USCIS export download failed ({last_err}). "
        f"Download manually per the module docstring to {dest}."
    )


def ingest_us(conn: sqlite3.Connection, path: str | None = None) -> int:
    """Ingest employers with >= 5 H-1B approvals in the latest export year.

    `path` overrides the download; otherwise data/sponsors/us_h1b.csv is used
    (downloaded if missing/stale). Returns #companies ingested.
    """
    csv_path = _refresh_csv(path)
    approvals: dict[str, int] = {}
    names: dict[str, str] = {}
    year_seen: set[str] = set()
    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        emp_col = cols.get("employer") or cols.get("employer (petitioner) name")
        if not emp_col:
            raise ValueError(f"us_h1b.csv: unexpected columns {reader.fieldnames}")
        init_col = next((c for k, c in cols.items() if "initial approval" in k), None)
        cont_col = next((c for k, c in cols.items() if "continuing approval" in k), None)
        fy_col = next((c for k, c in cols.items() if "fiscal year" in k), None)
        for row in reader:
            name = (row.get(emp_col) or "").strip()
            if not name:
                continue
            if fy_col and row.get(fy_col):
                year_seen.add(row[fy_col].strip())
            total = _int(row.get(init_col)) + _int(row.get(cont_col))
            key = name.lower()
            approvals[key] = approvals.get(key, 0) + total
            names.setdefault(key, name)
    year = max(year_seen) if year_seen else date.today().year
    count = 0
    for key, total in approvals.items():
        if total < MIN_APPROVALS:
            continue
        match.upsert_register_row(conn, names[key], "us", DATASET, year)
        count += 1
    return count
