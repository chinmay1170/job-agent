"""Sponsor flag attachment + lookup.

Register rows land in `companies` via :func:`upsert_register_row` (used by the
ingesters), which sets ``sponsor_<region>=1`` and appends to the
``sponsor_evidence`` JSON list: ``[{dataset, year, matched_name, score}]``.

For companies discovered later from job boards (no exact name match against a
register), :func:`attach_sponsor_flags` and :func:`is_sponsor` fall back to a
rapidfuzz ``token_set_ratio >= 92`` lookup against ``companies.name_norm``.
"""
from __future__ import annotations

import json
import sqlite3

from rapidfuzz import fuzz, process

from jobagent import db

FUZZ_THRESHOLD = 92

_REGION_COL = {
    "nl": "sponsor_nl",
    "uk": "sponsor_uk",
    "us": "sponsor_us",
    "au": "sponsor_au",
    # job-board seeds use 'eu'; the only EU register we ingest is the Dutch one
    "eu": "sponsor_nl",
}


def _column(region: str) -> str:
    try:
        return _REGION_COL[region.lower()]
    except KeyError:
        raise ValueError(f"unknown sponsor region {region!r} (expected nl/uk/us/au/eu)")


def _merge_evidence(existing: str | None, entry: dict) -> str:
    """Append `entry` to the evidence list, replacing any prior entry for the
    same dataset+year so re-ingests stay idempotent."""
    try:
        evidence = json.loads(existing) if existing else []
        if not isinstance(evidence, list):
            evidence = []
    except (json.JSONDecodeError, TypeError):
        evidence = []
    evidence = [
        e for e in evidence
        if not (e.get("dataset") == entry["dataset"] and e.get("year") == entry["year"])
    ]
    evidence.append(entry)
    return json.dumps(evidence, ensure_ascii=False)


def upsert_register_row(
    conn: sqlite3.Connection,
    name: str,
    region: str,
    dataset: str,
    year: int | str,
    score: int = 100,
    matched_name: str | None = None,
) -> int:
    """One register row -> companies upsert with sponsor flag + evidence."""
    col = _column(region)
    norm = db.normalize_company(name)
    row = conn.execute(
        "SELECT sponsor_evidence FROM companies WHERE name_norm=?", (norm,)
    ).fetchone()
    evidence = _merge_evidence(
        row["sponsor_evidence"] if row else None,
        {
            "dataset": dataset,
            "year": year,
            "matched_name": matched_name or name,
            "score": score,
        },
    )
    return db.upsert_company(conn, name, **{col: 1, "sponsor_evidence": evidence})


def _sponsor_norms(conn: sqlite3.Connection, col: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        f"SELECT id, name, name_norm, sponsor_evidence FROM companies WHERE {col}=1"
    ).fetchall()
    return {r["name_norm"]: r for r in rows}


def fuzzy_lookup(
    conn: sqlite3.Connection, company_name: str, region: str
) -> tuple[sqlite3.Row, int] | None:
    """Fuzzy match `company_name` against sponsor-flagged companies.name_norm.

    Returns (row, score) for the best match with token_set_ratio >= 92, else None.
    """
    col = _column(region)
    norm = db.normalize_company(company_name)
    candidates = _sponsor_norms(conn, col)
    if not candidates:
        return None
    if norm in candidates:  # exact hit, skip the fuzzy scan
        return candidates[norm], 100
    best = process.extractOne(
        norm, list(candidates.keys()), scorer=fuzz.token_set_ratio,
        score_cutoff=FUZZ_THRESHOLD,
    )
    if not best:
        return None
    matched_norm, score, _ = best
    return candidates[matched_norm], int(score)


def is_sponsor(conn: sqlite3.Connection, company_name: str, region: str) -> bool:
    """True if `company_name` (exactly or fuzzily) matches a sponsor in `region`."""
    return fuzzy_lookup(conn, company_name, region) is not None


def attach_sponsor_flags(conn: sqlite3.Connection) -> int:
    """Propagate sponsor flags to non-flagged companies by fuzzy name match.

    For every company with no sponsor flag set, fuzzy-match its name_norm
    against the sponsor-flagged companies per region; on a hit (>= 92), copy
    the flag and append evidence with the fuzzy score. Returns #companies updated.
    """
    unflagged = conn.execute(
        "SELECT id, name, name_norm, sponsor_evidence FROM companies "
        "WHERE COALESCE(sponsor_nl,0)=0 AND COALESCE(sponsor_uk,0)=0 "
        "AND COALESCE(sponsor_us,0)=0 AND COALESCE(sponsor_au,0)=0"
    ).fetchall()
    updated = 0
    for region in ("nl", "uk", "us", "au"):
        col = _REGION_COL[region]
        candidates = _sponsor_norms(conn, col)
        if not candidates:
            continue
        norms = list(candidates.keys())
        for company in unflagged:
            best = process.extractOne(
                company["name_norm"], norms, scorer=fuzz.token_set_ratio,
                score_cutoff=FUZZ_THRESHOLD,
            )
            if not best:
                continue
            matched_norm, score, _ = best
            matched = candidates[matched_norm]
            current = conn.execute(
                "SELECT sponsor_evidence FROM companies WHERE id=?", (company["id"],)
            ).fetchone()
            evidence = _merge_evidence(
                current["sponsor_evidence"] if current else None,
                {
                    "dataset": f"fuzzy:{region}",
                    "year": None,
                    "matched_name": matched["name"],
                    "score": int(score),
                },
            )
            conn.execute(
                f"UPDATE companies SET {col}=1, sponsor_evidence=? WHERE id=?",
                (evidence, company["id"]),
            )
            updated += 1
    conn.commit()
    return updated
