"""Government sponsor-register ingestion.

``run_ingest(nl=..., uk=..., us=..., au=...)`` is the CLI entry point
(`jobagent sponsors ingest --nl --uk --us --au`). Each register is wrapped
so one failure never kills the others; raw downloads are cached under
data/sponsors/ and skipped when less than 7 days old.
"""
from __future__ import annotations

import traceback

from jobagent import db

__all__ = ["run_ingest"]


def run_ingest(nl: bool = False, uk: bool = False, us: bool = False, au: bool = False) -> dict[str, int | None]:
    """Ingest the selected sponsor registers into the companies table.

    Returns {region: row_count_or_None} (None = that ingester failed).
    """
    if not any((nl, uk, us, au)):
        print("nothing to do — pass --nl/--uk/--us/--au or --all")
        return {}

    # Local imports keep `from jobagent.sponsors import run_ingest` cheap.
    from jobagent.sponsors.ingest_au import ingest_au
    from jobagent.sponsors.ingest_nl import ingest_nl
    from jobagent.sponsors.ingest_uk import ingest_uk
    from jobagent.sponsors.ingest_us import ingest_us

    conn = db.connect()
    # db.upsert_company commits per row; registers are ~100k rows, so trade
    # fsync durability for speed during bulk ingest (WAL stays consistent).
    conn.execute("PRAGMA synchronous=OFF")
    selected = [
        ("nl", nl, ingest_nl),
        ("uk", uk, ingest_uk),
        ("us", us, ingest_us),
        ("au", au, ingest_au),
    ]
    results: dict[str, int | None] = {}
    try:
        for region, wanted, fn in selected:
            if not wanted:
                continue
            print(f"[{region}] ingesting...")
            try:
                count = fn(conn)
            except Exception as e:  # one register failing must not kill the rest
                results[region] = None
                print(f"[{region}] FAILED: {e}")
                traceback.print_exc()
                db.log_event(conn, "sponsor_register", None, "ingest_failed",
                             {"region": region, "error": str(e)})
                continue
            results[region] = count
            print(f"[{region}] {count} sponsor companies upserted")
            db.log_event(conn, "sponsor_register", None, "ingest_ok",
                         {"region": region, "count": count})
    finally:
        conn.execute("PRAGMA synchronous=FULL")
        conn.close()
    return results
