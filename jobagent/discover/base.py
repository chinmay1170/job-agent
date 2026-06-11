"""Shared helpers for discovery sources: HTTP, seeds, HTML stripping, insert."""
from __future__ import annotations

import hashlib
import html as html_mod
import re
import sqlite3
import time
from pathlib import Path

import httpx
import yaml

from jobagent import config, db

USER_AGENT = "jobagent/0.1 (personal job search; chinmaykrishna3@gmail.com)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
TIMEOUT = 20
BOARD_SLEEP = 0.5  # politeness gap between board / detail fetches

SEEDS_PATH = config.CONFIG_DIR / "seeds" / "eu_uae_boards.yaml"

# Seed-less aggregate sources. run_discover dispatches via the seed list, so
# each aggregate gets one synthetic "board" entry appended by load_seeds.
AGGREGATE_SEEDS = [
    {"name": "Remotive", "ats": "remotive", "slug": "remote-jobs"},
    {"name": "RemoteOK", "ats": "remoteok", "slug": "api"},
    {"name": "HN Who is hiring", "ats": "hn", "slug": "whoishiring"},
    {"name": "Adzuna", "ats": "adzuna", "slug": "search"},
    {"name": "JobSpy", "ats": "jobspy", "slug": "scrape"},
]

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(?:p|div|li|ul|ol|h[1-6]|tr|table)[^>]*>|<br\s*/?>", re.I)
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")


def get_json(url: str, params: dict | None = None) -> dict | list:
    resp = httpx.get(url, params=params, headers=HEADERS, timeout=TIMEOUT,
                     follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def polite_sleep() -> None:
    time.sleep(BOARD_SLEEP)


def strip_html(raw: str | None) -> str:
    """HTML (possibly entity-escaped, e.g. Greenhouse) -> readable plain text."""
    if not raw:
        return ""
    text = html_mod.unescape(raw)
    if "<" in text:  # escaped HTML needs a second pass after unescape
        text = _SCRIPT.sub(" ", text)
        text = _BLOCK.sub("\n", text)
        text = _TAG.sub(" ", text)
        text = html_mod.unescape(text)
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL.sub("\n\n", text).strip()


def load_seeds(path: str | Path | None = None) -> list[dict]:
    """Load seed company boards. Missing/empty file -> [] with a warning."""
    p = Path(path) if path else SEEDS_PATH
    aggregates = [dict(s) for s in AGGREGATE_SEEDS]
    if not p.exists():
        print(f"WARNING: seeds file not found at {p}; no boards to discover.")
        return aggregates
    data = yaml.safe_load(p.read_text()) or {}
    companies = data.get("companies") or []
    out = []
    for c in companies:
        if not isinstance(c, dict) or not all(c.get(k) for k in ("name", "ats", "slug")):
            print(f"WARNING: skipping malformed seed entry: {c!r}")
            continue
        out.append(c)
    if not out:
        print(f"WARNING: seeds file {p} contains no usable companies.")
    return out + aggregates


def location_bucket(location: str | None) -> str:
    """First comma-segment, lowercased: 'Amsterdam, NL' -> 'amsterdam'."""
    return (location or "").split(",")[0].strip().lower()


def make_dedupe_key(company_name: str, title: str, location: str | None) -> str:
    raw = f"{db.normalize_company(company_name)}|{(title or '').lower().strip()}|{location_bucket(location)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def insert_job(conn: sqlite3.Connection, *, company_id: int, company_name: str,
               source: str, source_id: str, title: str, location: str | None,
               url: str | None, apply_url: str | None, description: str | None,
               posted_at: str | None, remote: int = 0) -> bool:
    """INSERT OR IGNORE; returns True if a new row was inserted."""
    key = make_dedupe_key(company_name, title, location)
    cur = conn.execute(
        "INSERT OR IGNORE INTO jobs (company_id, source, source_id, dedupe_key, "
        "title, location, remote, url, apply_url, description, posted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (company_id, source, source_id, key, title, location, remote,
         url, apply_url, description, posted_at),
    )
    return cur.rowcount == 1


# --- cheap title gate (shared with score.prefilter) -------------------------

_SENIORITY = re.compile(r"\b(senior|staff|principal|lead|iii|3)\b", re.I)


def _exclude_patterns() -> list[re.Pattern]:
    words = config.search().get("titles", {}).get("exclude", [])
    return [re.compile(rf"\b{re.escape(w)}\b", re.I) for w in words]


def title_passes(title: str) -> bool:
    """include-entry match OR (seniority word AND domain word); never an exclude."""
    titles_cfg = config.search().get("titles", {})
    t = (title or "").lower()
    for pat in _exclude_patterns():
        if pat.search(t):
            return False
    if any(inc.lower() in t for inc in titles_cfg.get("include", [])):
        return True
    domains = titles_cfg.get("domains", [])
    return bool(_SENIORITY.search(t)) and any(d.lower() in t for d in domains)
