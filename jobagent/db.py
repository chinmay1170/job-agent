"""SQLite access layer. One DB at data/jobagent.db, WAL mode, auto-migrated."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobagent.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_LEGAL_SUFFIXES = re.compile(
    r"\b(incorporated|inc|llc|ltd|limited|corp|corporation|co|company|plc|"
    r"bv|b\.v|nv|n\.v|gmbh|ag|sa|sarl|pty|fz[- ]?llc|fze|dmcc|holdings?|group|"
    r"technologies|technology|labs|software)\b\.?",
    re.IGNORECASE,
)


def normalize_company(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation for matching."""
    n = name.lower().strip()
    n = _LEGAL_SUFFIXES.sub(" ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n or name.lower().strip()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def log_event(conn: sqlite3.Connection, entity_type: str, entity_id: int | None,
              event: str, detail: dict | str | None = None) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)
    conn.execute(
        "INSERT INTO events (entity_type, entity_id, event, detail) VALUES (?,?,?,?)",
        (entity_type, entity_id, event, detail),
    )
    conn.commit()


def upsert_company(conn: sqlite3.Connection, name: str, **fields) -> int:
    """Insert or update a company by normalized name; returns company id."""
    norm = normalize_company(name)
    row = conn.execute("SELECT id FROM companies WHERE name_norm=?", (norm,)).fetchone()
    if row:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE companies SET {sets} WHERE id=?", (*fields.values(), row["id"]))
            conn.commit()
        return row["id"]
    cols = ["name", "name_norm", *fields.keys()]
    vals = [name, norm, *fields.values()]
    cur = conn.execute(
        f"INSERT INTO companies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
    )
    conn.commit()
    return cur.lastrowid


def counter_get(conn: sqlite3.Connection, kind: str) -> int:
    row = conn.execute(
        "SELECT count FROM daily_counters WHERE date=? AND kind=?", (date.today().isoformat(), kind)
    ).fetchone()
    return row["count"] if row else 0


def counter_bump(conn: sqlite3.Connection, kind: str) -> None:
    conn.execute(
        "INSERT INTO daily_counters (date, kind, count) VALUES (?,?,1) "
        "ON CONFLICT(date, kind) DO UPDATE SET count=count+1",
        (date.today().isoformat(), kind),
    )
    conn.commit()
