"""Netherlands — IND public register of recognised sponsors.

Source: https://ind.nl/en/public-register-recognised-sponsors
The "Regular labour and highly skilled migrants" register lives on the
sub-page /public-register-recognised-sponsors/public-register-work, which
embeds the FULL table inline in the HTML (one <tr> per organisation:
<th>Organisation</th><td>KvK number</td>; ~6,400 rows, no pagination,
no downloadable CSV as of 2026-06). We parse that table with a regex
(stdlib only) and cache the parsed rows to data/sponsors/nl.csv.
"""
from __future__ import annotations

import csv
import html as html_mod
import re
import sqlite3
from datetime import date

from jobagent.sponsors import _common, match

REGISTER_URL = (
    "https://ind.nl/en/public-register-recognised-sponsors/public-register-work"
)
DATASET = "ind_recognised_sponsors_work"
CSV_NAME = "nl.csv"

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(cell: str) -> str:
    return html_mod.unescape(_TAG_RE.sub("", cell)).strip()


def _parse_register(page: str) -> list[tuple[str, str]]:
    """Return [(organisation, kvk_number), ...] from the embedded table."""
    start = page.find("<table")
    end = page.find("</table>", start)
    if start == -1 or end == -1:
        raise ValueError("IND page: no <table> found — page layout changed?")
    table = page[start:end]
    rows: list[tuple[str, str]] = []
    for tr in _ROW_RE.findall(table):
        cells = [_clean(c) for c in _CELL_RE.findall(tr)]
        if len(cells) < 2:
            continue
        org, kvk = cells[0], cells[1]
        if not org or org.lower() == "organisation":  # header row
            continue
        rows.append((org, kvk))
    return rows


def _refresh_csv() -> "_common.Path":
    dest = _common.sponsors_dir() / CSV_NAME
    if _common.is_fresh(dest):
        return dest
    page = _common.fetch_text(REGISTER_URL)
    rows = _parse_register(page)
    if len(rows) < 1000:
        raise ValueError(f"IND register parse looks wrong: only {len(rows)} rows")
    tmp = dest.with_suffix(".part")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["organisation", "kvk_number"])
        w.writerows(rows)
    tmp.replace(dest)
    return dest


def ingest_nl(conn: sqlite3.Connection) -> int:
    """Download (if stale), parse and upsert the NL register. Returns row count."""
    path = _refresh_csv()
    year = date.today().year
    count = 0
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("organisation") or "").strip()
            if not name:
                continue
            match.upsert_register_row(conn, name, "nl", DATASET, year)
            count += 1
    return count
