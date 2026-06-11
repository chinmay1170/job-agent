"""Review-queue helpers. Other modules import these to push/pop human-review items."""
from __future__ import annotations

import json
import sqlite3

from jobagent.db import log_event

VALID_RESOLUTIONS = {"applied_manually", "skipped"}


def enqueue(conn: sqlite3.Connection, job_id: int, reason: str, state: dict) -> int:
    """Insert an item into review_queue; returns the new item id."""
    cur = conn.execute(
        "INSERT INTO review_queue (job_id, reason, state_json) VALUES (?,?,?)",
        (job_id, reason, json.dumps(state, ensure_ascii=False)),
    )
    conn.commit()
    item_id = cur.lastrowid
    log_event(conn, "review_queue", item_id, "review_enqueued",
              {"job_id": job_id, "reason": reason})
    return item_id


def resolve(conn: sqlite3.Connection, item_id: int, resolution: str) -> bool:
    """Mark a review item resolved.

    When resolution == 'applied_manually', upsert an applications row for the
    item's job with status='submitted', method='manual'.
    Returns False if the item does not exist or is already resolved.
    """
    row = conn.execute(
        "SELECT id, job_id, resolved_at FROM review_queue WHERE id=?", (item_id,)
    ).fetchone()
    if row is None or row["resolved_at"] is not None:
        return False

    conn.execute(
        "UPDATE review_queue SET resolved_at=datetime('now'), resolution=? WHERE id=?",
        (resolution, item_id),
    )

    if resolution == "applied_manually" and row["job_id"] is not None:
        conn.execute(
            """
            INSERT INTO applications (job_id, method, status, submitted_at)
            VALUES (?, 'manual', 'submitted', datetime('now'))
            ON CONFLICT(job_id) DO UPDATE SET
              method='manual',
              status='submitted',
              submitted_at=COALESCE(submitted_at, datetime('now'))
            """,
            (row["job_id"],),
        )

    conn.commit()
    log_event(conn, "review_queue", item_id, "review_resolved",
              {"job_id": row["job_id"], "resolution": resolution})
    return True
