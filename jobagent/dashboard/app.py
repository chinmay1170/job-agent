"""Tracking dashboard — single server-rendered page, auto-refreshing, localhost only."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from jobagent import db, killswitch
from jobagent.dashboard import queue

ARTIFACTS_DIR = (db.ROOT / "artifacts").resolve()
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="jobagent dashboard")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------- helpers ----------

def artifact_url(path: str | None) -> str | None:
    """Map a stored filesystem path to a /artifacts/ URL, or None if not servable."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = db.ROOT / p
    try:
        rel = p.resolve().relative_to(ARTIFACTS_DIR)
    except ValueError:
        return None
    return f"/artifacts/{rel.as_posix()}"


templates.env.filters["artifact_url"] = artifact_url


def status_class(status: str | None) -> str:
    return {
        "interview": "green",
        "rejected": "red",
        "submitted": "blue",
        "confirmed": "blue",
        "needs_review": "orange",
    }.get(status or "", "gray")


templates.env.filters["status_class"] = status_class


def tri_count(conn: sqlite3.Connection, base_sql: str, params: tuple = ()) -> dict:
    """Counts for today / last 7 days / all-time over a subquery exposing a `ts` column."""
    row = conn.execute(
        f"SELECT COUNT(*) AS total, "
        f"COALESCE(SUM(date(ts) = date('now')), 0) AS today, "
        f"COALESCE(SUM(date(ts) >= date('now', '-6 days')), 0) AS week "
        f"FROM ({base_sql})",
        params,
    ).fetchone()
    return {"today": row["today"], "week": row["week"], "all": row["total"]}


def funnel(conn: sqlite3.Connection) -> list[dict]:
    metrics = [
        ("Jobs discovered",
         "SELECT discovered_at AS ts FROM jobs"),
        ("Passed prefilter",
         "SELECT discovered_at AS ts FROM jobs "
         "WHERE status NOT IN ('discovered', 'prefiltered_out')"),
        ("Apply queued",
         "SELECT discovered_at AS ts FROM jobs WHERE status IN ('apply_queued', 'applied')"),
        ("Applications submitted",
         "SELECT submitted_at AS ts FROM applications WHERE submitted_at IS NOT NULL"),
        ("Replies",
         "SELECT last_message_at AS ts FROM inbox_threads "
         "WHERE classification IS NOT NULL AND classification != 'auto_ack'"),
        ("Interviews",
         "SELECT COALESCE(submitted_at, created_at) AS ts FROM applications "
         "WHERE status = 'interview'"),
        ("Rejections",
         "SELECT COALESCE(submitted_at, created_at) AS ts FROM applications "
         "WHERE status = 'rejected'"),
    ]
    return [{"label": label, **tri_count(conn, sql)} for label, sql in metrics]


def pretty_state(state_json: str | None) -> str:
    if not state_json:
        return ""
    try:
        return json.dumps(json.loads(state_json), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return state_json


def state_screenshot(state_json: str | None) -> str | None:
    """Pull a screenshot path out of state_json if one exists."""
    if not state_json:
        return None
    try:
        state = json.loads(state_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    for key in ("screenshot", "screenshot_path", "proof_screenshot"):
        if isinstance(state.get(key), str):
            return state[key]
    return None


def state_url(state_json: str | None) -> str | None:
    """Pull the page URL out of state_json if one exists (for the Open URL action)."""
    if not state_json:
        return None
    try:
        state = json.loads(state_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    for key in ("url", "apply_url", "page_url"):
        if isinstance(state.get(key), str):
            return state[key]
    return None


def chart_regions(conn: sqlite3.Connection) -> list[dict]:
    """Pipeline volume by region: queued + applications, for the region chart."""
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(c.region, ''), 'other') AS label,
               SUM(j.status IN ('apply_queued', 'applied', 'needs_review')) AS queued,
               COUNT(a.id) AS applied
        FROM jobs j
        LEFT JOIN companies c ON c.id = j.company_id
        LEFT JOIN applications a ON a.job_id = j.id AND a.submitted_at IS NOT NULL
        WHERE j.status IN ('apply_queued', 'applied', 'needs_review', 'prefiltered', 'scored')
        GROUP BY label ORDER BY queued DESC LIMIT 8
        """
    ).fetchall()
    return [dict(r) for r in rows if (r["queued"] or 0) + (r["applied"] or 0) > 0]


def chart_companies(conn: sqlite3.Connection) -> list[dict]:
    """Top companies in the active pipeline."""
    rows = conn.execute(
        """
        SELECT c.name AS label,
               SUM(j.status IN ('apply_queued', 'needs_review')) AS queued,
               COUNT(a.id) AS applied
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        LEFT JOIN applications a ON a.job_id = j.id AND a.submitted_at IS NOT NULL
        WHERE j.status IN ('apply_queued', 'applied', 'needs_review')
        GROUP BY c.name
        HAVING queued + applied > 0
        ORDER BY applied DESC, queued DESC LIMIT 10
        """
    ).fetchall()
    return [dict(r) for r in rows]


def chart_timeline(conn: sqlite3.Connection) -> list[dict]:
    """Last 14 days: discovered jobs and submitted applications per day."""
    rows = conn.execute(
        """
        WITH RECURSIVE days(d) AS (
            SELECT date('now', '-13 days')
            UNION ALL SELECT date(d, '+1 day') FROM days WHERE d < date('now')
        )
        SELECT d AS day,
            (SELECT COUNT(*) FROM jobs WHERE date(discovered_at) = d) AS discovered,
            (SELECT COUNT(*) FROM applications WHERE date(submitted_at) = d) AS applied
        FROM days
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    conn = db.connect()
    try:
        applications = conn.execute(
            """
            SELECT a.id, a.status, a.method, a.submitted_at, a.created_at,
                   a.resume_path, a.cover_path, a.proof_screenshot,
                   j.title, j.url, j.score,
                   c.name AS company, c.region,
                   c.market_cap, c.employee_count, c.hq
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            LEFT JOIN companies c ON c.id = j.company_id
            ORDER BY COALESCE(a.submitted_at, a.created_at) DESC
            LIMIT 200
            """
        ).fetchall()

        replies = conn.execute(
            """
            SELECT classification, from_email, subject, snippet, last_message_at
            FROM inbox_threads
            ORDER BY (classification = 'interview_request') DESC,
                     last_message_at DESC
            LIMIT 200
            """
        ).fetchall()

        review_rows = conn.execute(
            """
            SELECT r.id, r.reason, r.state_json, r.created_at,
                   j.title, j.url, j.apply_url,
                   c.name AS company
            FROM review_queue r
            LEFT JOIN jobs j ON j.id = r.job_id
            LEFT JOIN companies c ON c.id = j.company_id
            WHERE r.resolved_at IS NULL
            ORDER BY r.created_at ASC
            """
        ).fetchall()
        review_items = [
            {
                **dict(r),
                "state_pretty": pretty_state(r["state_json"]),
                "screenshot": state_screenshot(r["state_json"]),
                "open_url": r["apply_url"] or r["url"] or state_url(r["state_json"]),
            }
            for r in review_rows
        ]

        events = conn.execute(
            "SELECT ts, entity_type, entity_id, event, detail "
            "FROM events ORDER BY id DESC LIMIT 100"
        ).fetchall()

        context = {
            "request": request,
            "killed": killswitch.is_killed(),
            "funnel": funnel(conn),
            "applications": applications,
            "replies": replies,
            "review_items": review_items,
            "events": events,
            "chart_regions": chart_regions(conn),
            "chart_companies": chart_companies(conn),
            "chart_timeline": chart_timeline(conn),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/artifacts/{path:path}")
def artifacts(path: str) -> FileResponse:
    target = (ARTIFACTS_DIR / path).resolve()
    # Path-traversal protection: resolved path must stay under artifacts/.
    if ARTIFACTS_DIR != target and ARTIFACTS_DIR not in target.parents:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)


@app.post("/review/{item_id}/resolve")
def review_resolve(item_id: int, resolution: str = Form(...)) -> RedirectResponse:
    if resolution not in queue.VALID_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid resolution: {resolution}")
    conn = db.connect()
    try:
        queue.resolve(conn, item_id, resolution)
    finally:
        conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/kill")
def kill_toggle() -> RedirectResponse:
    if killswitch.is_killed():
        killswitch.release()
    else:
        killswitch.engage()
    return RedirectResponse(url="/", status_code=303)
