"""Discovery stage: pull job listings from ATS boards into the jobs table."""
from __future__ import annotations

from pathlib import Path

from jobagent import db
from jobagent.discover import (adzuna, ashby, base, greenhouse, hn_hiring,
                               jobspy_runner, lever, remoteok, remotive,
                               smartrecruiters, workable)

# source name -> fetch(conn, company_seed) -> (found, inserted)
SOURCES = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workable": workable.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "remotive": remotive.fetch,
    "remoteok": remoteok.fetch,
    "hn": hn_hiring.fetch,
    "adzuna": adzuna.fetch,
    "jobspy": jobspy_runner.fetch,
}


def run_discover(source: str = "all", seeds_path: str | Path | None = None) -> dict:
    """Run one or all discovery sources over the seed boards.

    Each board fetch is wrapped in try/except so one failure doesn't kill
    the rest. Returns the aggregate counts that are also logged/printed.
    """
    if source != "all" and source not in SOURCES:
        raise SystemExit(
            f"Unknown source '{source}'. Known: {', '.join(SOURCES)} or 'all'.")

    seeds = base.load_seeds(seeds_path)
    wanted = seeds if source == "all" else [c for c in seeds if c.get("ats") == source]

    conn = db.connect()
    per_source: dict[str, dict] = {}
    boards = found = inserted = errors = 0
    try:
        for i, company in enumerate(wanted):
            ats = company["ats"]
            fetch = SOURCES.get(ats)
            if fetch is None:
                print(f"WARNING: no source registered for ats '{ats}' "
                      f"(company {company['name']}); skipping.")
                continue
            if i:
                base.polite_sleep()
            boards += 1
            stats = per_source.setdefault(
                ats, {"boards": 0, "found": 0, "inserted": 0, "errors": 0})
            stats["boards"] += 1
            try:
                f, n = fetch(conn, company)
            except Exception as e:  # noqa: BLE001 — isolate board failures
                errors += 1
                stats["errors"] += 1
                print(f"ERROR: {ats}/{company['slug']}: {type(e).__name__}: {e}")
                db.log_event(conn, "source", None, "discover_error",
                             {"ats": ats, "slug": company["slug"], "error": str(e)[:300]})
                continue
            found += f
            inserted += n
            stats["found"] += f
            stats["inserted"] += n
            print(f"{ats}/{company['slug']}: {f} jobs found, {n} new")

        counts = {"source": source, "boards": boards, "found": found,
                  "inserted": inserted, "errors": errors, "per_source": per_source}
        db.log_event(conn, "source", None, "discover_done", counts)
        print(f"discover done: {boards} boards, {found} found, "
              f"{inserted} inserted, {errors} errors")
        return counts
    finally:
        conn.close()
