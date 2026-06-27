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

GATES = ("blocklist", "cooldown", "title", "seniority", "ml_core", "min_yoe",
         "location", "sponsorship")

_REMOTE = re.compile(r"\bremote\b", re.I)

# Home-country (India) locations — candidate wants to relocate OUT, so an
# India-located role is never the goal. Used to reject India postings even when
# the company's seed region is a target (a US company's Bengaluru office is
# still an India job).
_HOME_EXCLUDE = re.compile(
    r"\bindia\b|bengaluru|bangalore|hyderabad|mumbai|\bpune\b|"
    r"\b(new )?delhi\b|\bncr\b|noida|gurugram|gurgaon|chennai|kolkata|"
    r"ahmedabad|\bgoa\b", re.I)

# Titles above Senior that the candidate (Senior SDE3, ~5y) can't realistically
# land. "Lead" is intentionally absent (kept as a senior IC title).
_TOO_SENIOR = re.compile(
    r"\b(staff|principal|distinguished|director|vice\s*president|vp|"
    r"head\s+of|chief|architect|fellow)\b", re.I)

# Minimum-years-of-experience asks, tied to experience context only (avoids
# matching unrelated numbers like "10 years of innovation").
_MIN_YOE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:to\s*\d{1,2}\s*)?(?:years?|yrs?)\s*"
    r"(?:of\s+)?(?:experience|industry|professional|relevant|hands-on)", re.I)


def _seniority_acceptable(title: str) -> bool:
    """False when the title is above Senior (Staff/Principal/Director/VP/...)."""
    return not _TOO_SENIOR.search(title or "")


# Core-ML / research / data-science roles the candidate (backend / distributed
# systems, NOT an ML modeller) shouldn't apply to. AI-PLATFORM / AI-infra /
# backend-for-AI / GenAI-tooling roles ARE a fit and must NOT be filtered — they
# match the real Adobe GenAI-tooling experience.
_ML_CORE = re.compile(
    r"\b(machine learning|ml)\s+(engineer|scientist|researcher)\b|"
    r"\b(applied|research|data)\s+scientist\b|\bdata science\b|"
    r"\bresearch engineer\b|\bdeep learning\b|\bcomputer vision\b|"
    r"\bnlp\s+engineer\b|\bml\s+research\b|\bai\s+research(er)?\b|"
    r"\b(ml|ai)\s*/\s*(ai|ml)\s+(engineer|scientist)\b", re.I)
# Override: keep the role if the title signals engineering/platform work around
# ML rather than modelling itself.
_AI_PLATFORM_KEEP = re.compile(
    r"\b(platform|infrastructure|infra|backend|back-end|tooling|serving|"
    r"systems|devx|developer|deploy|ops|pipeline)\b", re.I)


def _ml_core_acceptable(title: str) -> bool:
    """False for core-ML / research / data-science titles (model-building work),
    but TRUE when the title is AI-platform/infra/tooling/backend-for-AI."""
    t = title or ""
    if not _ML_CORE.search(t):
        return True
    return bool(_AI_PLATFORM_KEEP.search(t))


def _min_yoe_acceptable(description: str, candidate_years: int,
                        year_stretch: int) -> bool:
    """False only for clearly-too-high experience asks.

    Takes the SMALLEST matched 'N years of experience' as the floor and rejects
    when it exceeds candidate_years + year_stretch (e.g. 8+ for a 5y candidate).
    """
    if not description:
        return True
    found = [int(m) for m in _MIN_YOE.findall(description) if m.isdigit()]
    found = [n for n in found if 0 < n <= 25]  # sanity bound
    if not found:
        return True  # no explicit ask -> let it through
    return min(found) <= candidate_years + year_stretch


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
    fit_cfg = config.answers().get("fit", {})
    candidate_years = int(fit_cfg.get("candidate_years_experience", 5))
    year_stretch = int(fit_cfg.get("year_stretch", 2))

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

        # 3b. seniority gate — reject Staff/Principal/Director/VP/Head/etc.
        #     (belt-and-suspenders with the title exclude list, since aggregator
        #     sources bypass the curated seed).
        if not _seniority_acceptable(job["title"] or ""):
            fail(job["id"], "seniority")
            continue

        # 3b2. core-ML gate — reject ML-modelling / research / data-science
        #      roles (candidate is backend, not an ML modeller); KEEP
        #      AI-platform / AI-infra / GenAI-tooling roles.
        if not _ml_core_acceptable(job["title"] or ""):
            fail(job["id"], "ml_core")
            continue

        # 3c. minimum-experience gate — reject clearly-too-high YoE asks.
        if not _min_yoe_acceptable(job["description"] or "",
                                   candidate_years, year_stretch):
            fail(job["id"], "min_yoe")
            continue

        # 4. location gate — onsite/hybrid in a target region only.
        #    Remote-only roles are the wrong path for visa sponsorship.
        loc = (job["location"] or "").strip()
        is_remote = (not loc and job["remote"]) or bool(loc and _REMOTE.search(loc))
        region = _match_region(loc, region_pats) if loc else None
        # HOME-COUNTRY exclusion: the candidate wants to LEAVE India for a
        # sponsored role. If the posting explicitly names India (a non-target
        # country), reject it OUTRIGHT — never fall back to the company's seed
        # region (a US-tagged company's Bengaluru office is still an India job).
        if loc and _HOME_EXCLUDE.search(loc) and not region:
            fail(job["id"], "location")
            continue
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
