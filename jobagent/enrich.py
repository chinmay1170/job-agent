"""Enrich companies with market cap / employee count / HQ via claude -p.

Runs only for companies in the active pipeline (applied/queued), so it's a
handful of well-known firms per day — cheap and accurate from model knowledge.
"""
from __future__ import annotations

from pydantic import BaseModel

from jobagent import db
from jobagent.llm import ask_json


class CompanyFacts(BaseModel):
    market_cap: str   # "$48B", "Private", or "Unknown"
    employee_count: str  # "~4,000", "10,000+", or "Unknown"
    hq: str           # "Amsterdam, Netherlands" or "Unknown"


import re as _re


def classify_tier(market_cap: str | None, employee_count: str | None) -> str:
    """megacap | large | mid | startup, from enriched facts.

    megacap: >= $100B or >= 50k employees. large: >= $10B or >= 5k.
    mid: >= $1B or >= 500. else startup. Unknowns fall to 'mid' (neutral).
    """
    def _emps(s: str | None) -> int:
        if not s:
            return 0
        m = _re.search(r"([\d,]+)\s*(k|,000)?", s.replace("~", ""))
        if not m:
            return 0
        n = int(m.group(1).replace(",", ""))
        return n * 1000 if (m.group(2) or "").lower() == "k" else n

    def _cap_usd_b(s: str | None) -> float:
        if not s:
            return 0.0
        m = _re.search(r"\$?\s*([\d.]+)\s*([tTbBmM])", s)
        if not m:
            return 0.0
        v = float(m.group(1))
        unit = m.group(2).lower()
        return v * 1000 if unit == "t" else (v if unit == "b" else v / 1000)

    cap, emps = _cap_usd_b(market_cap), _emps(employee_count)
    if cap >= 100 or emps >= 50000:
        return "megacap"
    if cap >= 10 or emps >= 5000:
        return "large"
    if cap >= 1 or emps >= 500:
        return "mid"
    if cap == 0 and emps == 0:
        return "mid"  # unknown -> neutral, don't penalise
    return "startup"


PROMPT = (
    "Give concise public facts about the company \"{name}\"{hint}. "
    "market_cap: approximate (e.g. '$48B'); if privately held say 'Private'; "
    "if you don't know say 'Unknown'. employee_count: approximate headcount "
    "(e.g. '~4,000' or '10,000+'); 'Unknown' if unsure. hq: 'City, Country'. "
    "Do not guess wildly — prefer 'Unknown' to a fabricated number."
)


class SalaryBenchmark(BaseModel):
    job_id: int
    benchmark: str  # "GBP 95,000-120,000 | ask: GBP 110,000" or "Unknown"


class SalaryBatch(BaseModel):
    benchmarks: list[SalaryBenchmark]


SALARY_PROMPT = """For each job below, find the current market BASE salary for
that role at that company and location. Use web search (levels.fyi, Glassdoor,
official salary-transparency data) when you are not confident.

The candidate is a senior backend engineer (4-5 yrs, distributed systems,
ex-Adobe). For each job return `benchmark` as:
"<CCY> <p25>-<p75> | ask: <CCY> <number>"
- CCY = the LOCAL currency of the job location (GBP London, EUR Berlin/Amsterdam,
  SGD Singapore, AED Dubai, AUD Sydney, USD US).
- ask = a confident but defensible number around the 60-75th percentile of the
  range for THIS company tier (big tech / fintech pays above market).
- If you genuinely cannot establish a range, return "Unknown" — never invent.

JOBS:
{jobs}

Return one benchmarks[] entry per job_id."""


def benchmark_salaries(limit: int = 60, batch_size: int = 8) -> None:
    """Web-searched salary benchmark per queued job (skips ones that have it)."""
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT j.id, j.title, COALESCE(j.location,'') AS location, c.name AS company
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE j.status = 'apply_queued' AND j.salary_benchmark IS NULL
        ORDER BY j.selection_chance DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("salary: nothing to benchmark")
        return
    done = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        ids = {r["id"] for r in batch}
        jobs_txt = "\n".join(
            f"- job_id {r['id']}: {r['title']} @ {r['company']} ({r['location']})"
            for r in batch)
        try:
            res = ask_json(SALARY_PROMPT.format(jobs=jobs_txt), SalaryBatch,
                           model="sonnet", timeout=420, allowed_tools="WebSearch")
        except Exception as e:  # noqa: BLE001
            print(f"salary: batch failed ({str(e)[:120]}); continuing")
            continue
        for b in res.benchmarks:
            if b.job_id not in ids or not b.benchmark or b.benchmark == "Unknown":
                continue
            conn.execute("UPDATE jobs SET salary_benchmark=? WHERE id=?",
                         (b.benchmark[:120], b.job_id))
            done += 1
        conn.commit()
        print(f"salary: batch {start // batch_size + 1} done ({done} total)")
    print(f"salary: benchmarked {done}/{len(rows)} jobs")


def enrich_pipeline(limit: int = 25) -> None:
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT DISTINCT c.id, c.name, c.domain, c.region
        FROM companies c JOIN jobs j ON j.company_id = c.id
        WHERE j.status IN ('prefiltered', 'apply_queued', 'applied',
                           'needs_review', 'scored')
          AND c.enriched_at IS NULL
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("enrich: nothing to do")
        return
    for r in rows:
        hint = f" (domain {r['domain']})" if r["domain"] else ""
        try:
            facts = ask_json(PROMPT.format(name=r["name"], hint=hint),
                             CompanyFacts, model="haiku", timeout=120)
            tier = classify_tier(facts.market_cap, facts.employee_count)
            conn.execute(
                "UPDATE companies SET market_cap=?, employee_count=?, hq=?, "
                "tier=?, enriched_at=datetime('now') WHERE id=?",
                (facts.market_cap, facts.employee_count, facts.hq, tier, r["id"]),
            )
            conn.commit()
            print(f"enrich: {r['name']} -> {facts.market_cap}, {facts.employee_count}, "
                  f"{facts.hq} [{tier}]")
        except Exception as e:  # noqa: BLE001 — one failure shouldn't stop the rest
            db.log_event(conn, "company", r["id"], "enrich_failed", str(e)[:200])
            print(f"enrich: {r['name']} FAILED: {e}")
