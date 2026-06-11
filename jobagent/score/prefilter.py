"""Deterministic prefilter gates over status='discovered' jobs.

Gates run in order; the first failure sets status='prefiltered_out' with
score_reasons = the gate name. Survivors get status='prefiltered' for the
LLM judge to pick up.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date

from jobagent import config, db
from jobagent.discover.base import title_passes

GATES = ("blocklist", "cooldown", "title", "location", "sponsorship")

_REMOTE = re.compile(r"\bremote\b", re.I)


def _region_patterns() -> list[tuple[str, list[re.Pattern]]]:
    regions = config.search().get("regions", {})
    out = []
    for region, spec in regions.items():
        pats = [re.compile(rf"\b{re.escape(c)}\b", re.I)
                for c in (spec or {}).get("countries", [])]
        out.append((region, pats))
    return out


def _match_region(location: str, patterns) -> str | None:
    for region, pats in patterns:
        if any(p.search(location) for p in pats):
            return region
    return None


def _blocklist_norms() -> list[str]:
    entries = config.blocklist().get("companies") or []
    return [db.normalize_company(e) for e in entries if e]


_SPONSOR_COL = {"us": "sponsor_us", "uk": "sponsor_uk", "au": "sponsor_au",
                "eu": "sponsor_nl", "nl": "sponsor_nl"}


def _company_sponsors(company: sqlite3.Row, region: str) -> bool:
    if region == "remote":  # no specific country: any sponsor evidence counts
        return any(company[c] for c in ("sponsor_us", "sponsor_nl",
                                        "sponsor_uk", "sponsor_au"))
    col = _SPONSOR_COL.get(region)
    return bool(col and company[col])


def run_prefilter() -> dict:
    conn = db.connect()
    caps = config.caps()
    posting_cfg = config.search().get("posting_text", {})
    positives = [p.lower() for p in posting_cfg.get("positive", [])]
    negatives = [n.lower() for n in posting_cfg.get("negative", [])]
    region_pats = _region_patterns()
    block_norms = _blocklist_norms()
    lifetime_cap = caps.get("per_company_lifetime", 2)
    exclude_remote = caps.get("exclude_remote", True)

    funnel = {g: 0 for g in GATES}
    passed = 0
    signals: dict[str, int] = {}

    jobs = conn.execute(
        "SELECT j.*, c.name AS company_name, c.name_norm, c.region AS company_region, "
        "c.cooldown_until, c.blocklisted, c.sponsor_us, c.sponsor_nl, c.sponsor_uk, "
        "c.sponsor_au, c.id AS cid "
        "FROM jobs j JOIN companies c ON j.company_id = c.id "
        "WHERE j.status = 'discovered'"
    ).fetchall()

    app_counts = dict(conn.execute(
        "SELECT j.company_id, COUNT(*) FROM applications a "
        "JOIN jobs j ON a.job_id = j.id GROUP BY j.company_id"
    ).fetchall())

    def fail(job_id: int, gate: str, signal: str | None = None) -> None:
        funnel[gate] += 1
        conn.execute(
            "UPDATE jobs SET status='prefiltered_out', score_reasons=?, "
            "sponsorship_signal=COALESCE(?, sponsorship_signal) WHERE id=?",
            (gate, signal, job_id))

    for job in jobs:
        # 1. blocklist
        name_norm = job["name_norm"]
        if job["blocklisted"] or any(b in name_norm for b in block_norms):
            fail(job["id"], "blocklist")
            continue

        # 2. cooldown / per-company lifetime cap
        cooldown = job["cooldown_until"]
        if (cooldown and cooldown > date.today().isoformat()) \
                or app_counts.get(job["cid"], 0) >= lifetime_cap:
            fail(job["id"], "cooldown")
            continue

        # 3. title gate
        if not title_passes(job["title"] or ""):
            fail(job["id"], "title")
            continue

        # 4. location gate — onsite/hybrid in a target region only.
        #    Remote-only roles are the wrong path for visa sponsorship.
        loc = (job["location"] or "").strip()
        is_remote = (not loc and job["remote"]) or bool(loc and _REMOTE.search(loc))
        region = _match_region(loc, region_pats) if loc else None
        if region:
            if not job["company_region"]:
                conn.execute("UPDATE companies SET region=? WHERE id=?",
                             (region, job["cid"]))
        elif job["company_region"] and job["company_region"] != "remote":
            region = job["company_region"]  # seed region fallback (e.g. 'Amsterdam')
        if not region or (exclude_remote and is_remote and not _match_region(loc, region_pats)):
            fail(job["id"], "location")
            continue

        # 5. sponsorship signal — ONLY an explicit negation hard-rejects.
        #    Absence of sponsorship language no longer kills the job; the judge
        #    folds sponsorship likelihood into selection_chance instead.
        text = f"{job['title'] or ''}\n{job['description'] or ''}".lower()
        if any(n in text for n in negatives):
            fail(job["id"], "sponsorship", signal="none")
            continue
        if _company_sponsors(job, region):
            signal = "company_dataset"
        elif region == "uae" or job["company_region"] == "uae":
            signal = "uae_default"
        elif any(p in text for p in positives):
            signal = "posting_text"
        else:
            signal = "unknown"  # passes; judge estimates the odds

        passed += 1
        signals[signal] = signals.get(signal, 0) + 1
        conn.execute(
            "UPDATE jobs SET status='prefiltered', sponsorship_signal=? WHERE id=?",
            (signal, job["id"]))

    conn.commit()
    summary = {"discovered": len(jobs), "passed": passed,
               "out_per_gate": funnel, "signals": signals}
    db.log_event(conn, "score", None, "prefilter_done", summary)

    print(f"prefilter: {len(jobs)} discovered -> {passed} prefiltered")
    for gate in GATES:
        print(f"  out at {gate:12s}: {funnel[gate]}")
    if signals:
        print(f"  sponsorship signals: {signals}")
    conn.close()
    return summary
