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


PROMPT = (
    "Give concise public facts about the company \"{name}\"{hint}. "
    "market_cap: approximate (e.g. '$48B'); if privately held say 'Private'; "
    "if you don't know say 'Unknown'. employee_count: approximate headcount "
    "(e.g. '~4,000' or '10,000+'); 'Unknown' if unsure. hq: 'City, Country'. "
    "Do not guess wildly — prefer 'Unknown' to a fabricated number."
)


def enrich_pipeline(limit: int = 25) -> None:
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT DISTINCT c.id, c.name, c.domain, c.region
        FROM companies c JOIN jobs j ON j.company_id = c.id
        WHERE j.status IN ('apply_queued', 'applied', 'needs_review', 'scored')
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
            conn.execute(
                "UPDATE companies SET market_cap=?, employee_count=?, hq=?, "
                "enriched_at=datetime('now') WHERE id=?",
                (facts.market_cap, facts.employee_count, facts.hq, r["id"]),
            )
            conn.commit()
            print(f"enrich: {r['name']} -> {facts.market_cap}, {facts.employee_count}, {facts.hq}")
        except Exception as e:  # noqa: BLE001 — one failure shouldn't stop the rest
            db.log_event(conn, "company", r["id"], "enrich_failed", str(e)[:200])
            print(f"enrich: {r['name']} FAILED: {e}")
