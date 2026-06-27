"""Tracking dashboard — single server-rendered page, auto-refreshing, localhost only."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from jobagent import db, killswitch
from jobagent.dashboard import queue

ARTIFACTS_DIR = (db.ROOT / "artifacts").resolve()
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

app = FastAPI(title="jobagent dashboard")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
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
        # Current backlog the agent STILL needs to apply to (not cumulative) —
        # 'applied' jobs have left the queue, so only count status='apply_queued'.
        ("Apply queued",
         "SELECT discovered_at AS ts FROM jobs WHERE status = 'apply_queued'"),
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


def calibration(conn: sqlite3.Connection, n_buckets: int = 4) -> list[dict]:
    """Predicted selection-chance bands vs actual positive-response rate.

    Bands are ADAPTIVE (quantile-based) over the actual predicted_chance values,
    so each band holds a roughly equal share of applications. Fixed 0-20/20-40/...
    bands were useless here because selection_chance tops out ~45% — the 60-100%
    bands were always empty and 20-40% held everything.

    A "positive response" = interview/recruiter reply; 'rejected'/'no_response'
    are resolved-negative; submitted/pending are excluded from the rate.
    """
    rows = conn.execute(
        "SELECT predicted_chance AS pc, status FROM applications "
        "WHERE predicted_chance IS NOT NULL"
    ).fetchall()
    vals = sorted(r["pc"] for r in rows if r["pc"] is not None)
    if not vals:
        return []

    # Quantile edges over the observed range; dedup so identical values don't
    # create zero-width bands. Always span the true min..max.
    lo_v, hi_v = vals[0], vals[-1]
    if lo_v == hi_v:
        edges = [lo_v, hi_v + 1]
    else:
        qs = [vals[min(len(vals) - 1, round(i * len(vals) / n_buckets))]
              for i in range(n_buckets)]
        edges = sorted(set([lo_v] + qs + [hi_v + 1]))

    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        # last band is inclusive of the max
        in_b = [r for r in rows if lo <= (r["pc"] or 0) < hi]
        if not in_b:
            continue
        resolved = [r for r in in_b if r["status"] in
                    ("interview", "rejected", "no_response")]
        positive = [r for r in resolved if r["status"] == "interview"]
        out.append({
            "label": f"{int(lo)}-{int(hi - 1)}%",
            "total": len(in_b),
            "resolved": len(resolved),
            "positive": len(positive),
            "rate": round(len(positive) / len(resolved) * 100) if resolved else None,
        })
    return out


def active_applications(conn: sqlite3.Connection) -> dict:
    """Submitted applications still awaiting a recruiter response — the live,
    in-flight pipeline (excludes rejected / interview / no_response). Returns
    a count plus the most recent rows for a dashboard card."""
    rows = conn.execute(
        """
        SELECT a.method, a.submitted_at, a.predicted_chance,
               j.title, COALESCE(j.apply_url, j.url) AS url,
               j.selection_chance, c.name AS company,
               COALESCE(j.location, '') AS location
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN companies c ON c.id = j.company_id
        WHERE a.status = 'submitted'
        ORDER BY COALESCE(j.selection_chance, a.predicted_chance, 0) DESC,
                 a.submitted_at DESC
        """
    ).fetchall()
    items = [dict(r) for r in rows]
    return {"count": len(items), "rows": items[:25]}


def top_chances(conn: sqlite3.Connection) -> list[dict]:
    """Highest selection-chance jobs still in play (queued/applied/borderline)."""
    rows = conn.execute(
        """
        SELECT j.id, j.selection_chance AS chance, j.score, j.status, j.title,
               COALESCE(j.apply_url, j.url) AS url,
               COALESCE(j.location, '') AS location, c.name AS company
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE j.selection_chance IS NOT NULL
          AND j.status IN ('apply_queued', 'applied', 'scored', 'needs_review')
        ORDER BY j.selection_chance DESC, j.score DESC
        LIMIT 10
        """
    ).fetchall()
    return [dict(r) for r in rows]


# Skills commonly demanded in target JDs; gap = demanded but absent from the
# resume's skills section. Word-boundary patterns; one-letter languages and
# ambiguous words (Go, Swift) get list-context guards to avoid prose noise.
SKILL_SCAN: list[tuple[str, str]] = [
    ("Go", r"\bgolang\b|\bgo\b(?=\s*[,/);.]|\s+(?:developer|engineer|experience|programming|services?))"),
    ("Kotlin", r"\bkotlin\b"),
    ("Rust", r"\brust\b"),
    ("Scala", r"\bscala\b"),
    ("C#/.NET", r"\bc#|\.net\b"),
    ("Ruby", r"\bruby\b"),
    ("PHP", r"\bphp\b"),
    ("Node.js", r"\bnode\.?js\b"),
    ("GraphQL", r"\bgraphql\b"),
    ("gRPC", r"\bgrpc\b"),
    ("Terraform", r"\bterraform\b"),
    ("Ansible", r"\bansible\b"),
    ("GCP", r"\bgcp\b|google cloud"),
    ("Azure", r"\bazure\b"),
    ("MongoDB", r"\bmongo\s?db\b|\bmongodb\b"),
    ("MySQL", r"\bmysql\b"),
    ("Elasticsearch", r"\belastic\s*search\b"),
    ("RabbitMQ", r"\brabbitmq\b"),
    ("Spark", r"\bspark\b"),
    ("Flink", r"\bflink\b"),
    ("Airflow", r"\bairflow\b"),
    ("Snowflake", r"\bsnowflake\b"),
    ("React", r"\breact(?:\.?js)?\b"),
    ("Angular", r"\bangular\b"),
    ("Vue", r"\bvue(?:\.?js)?\b"),
    ("Django", r"\bdjango\b"),
    ("Flask", r"\bflask\b"),
    ("FastAPI", r"\bfastapi\b"),
    ("Helm", r"\bhelm\b"),
    ("ArgoCD", r"\bargo\s?cd\b"),
    ("Datadog", r"\bdatadog\b"),
    ("OpenTelemetry", r"\bopentelemetry\b"),
    ("Pulsar", r"\bpulsar\b"),
    ("ClickHouse", r"\bclickhouse\b"),
]


def _resume_skill_phrases() -> set[str]:
    from jobagent import config
    skills = (config.profile().get("skills") or {})
    out: set[str] = set()
    for vals in skills.values():
        for s in vals or []:
            out.add(str(s).lower())
    return out


def skills_gap(conn: sqlite3.Connection) -> tuple[list[dict], int]:
    """Most-demanded skills across judged jobs that are NOT on the resume.

    Counts = number of judged job descriptions mentioning the skill. Use it to
    decide what to learn / add honestly to the resume next.
    """
    resume = _resume_skill_phrases()

    def on_resume(skill: str) -> bool:
        s = skill.lower()
        return any(s == p or re.search(rf"\b{re.escape(s)}\b", p) for p in resume)

    rows = conn.execute(
        "SELECT description FROM jobs "
        "WHERE selection_chance IS NOT NULL AND description IS NOT NULL"
    ).fetchall()
    descs = [r["description"].lower() for r in rows]
    total = len(descs)
    out = []
    for skill, pat in SKILL_SCAN:
        if on_resume(skill):
            continue
        cre = re.compile(pat, re.IGNORECASE)
        n = sum(1 for d in descs if cre.search(d))
        if n:
            out.append({"label": skill, "count": n,
                        "pct": round(n / total * 100) if total else 0})
    out.sort(key=lambda x: -x["count"])
    return out[:12], total


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


# ---------- "My Journey" motivational data (all live SQL, honest numbers) ----------

def _app_dates(conn: sqlite3.Connection) -> set:
    """Set of YYYY-MM-DD on which at least one application was submitted."""
    return {r["date"] for r in conn.execute(
        "SELECT date FROM daily_counters WHERE kind='applications' AND count > 0"
    ).fetchall()}


def _streaks(dates: set) -> tuple[int, int]:
    """(current streak ending today/yesterday, best streak ever) from a date set."""
    from datetime import date, timedelta
    if not dates:
        return 0, 0
    ds = sorted(date.fromisoformat(d) for d in dates)
    dset = set(ds)
    best = cur = 1
    for i in range(1, len(ds)):
        cur = cur + 1 if (ds[i] - ds[i - 1]).days == 1 else 1
        best = max(best, cur)
    today = date.today()
    probe = today if today in dset else (today - timedelta(days=1))
    current = 0
    while probe in dset:
        current += 1
        probe -= timedelta(days=1)
    return current, best


def momentum(conn: sqlite3.Connection) -> dict:
    """Hero stats — everything pulled live, nothing fabricated."""
    one = lambda sql: conn.execute(sql).fetchone()["c"]
    sent = one("SELECT COUNT(*) c FROM applications WHERE submitted_at IS NOT NULL")
    companies = one("SELECT COUNT(DISTINCT j.company_id) c FROM applications a "
                    "JOIN jobs j ON j.id=a.job_id WHERE a.submitted_at IS NOT NULL")
    responses = one("SELECT COUNT(*) c FROM applications "
                    "WHERE status IN ('rejected','interview','no_response')")
    interviews = one("SELECT COUNT(*) c FROM applications WHERE status='interview'")
    awaiting = one("SELECT COUNT(*) c FROM applications WHERE status='submitted'")
    today = one("SELECT COUNT(*) c FROM applications "
                "WHERE date(submitted_at,'localtime')=date('now','localtime')")
    this_week = conn.execute(
        "SELECT COALESCE(SUM(count),0) c FROM daily_counters "
        "WHERE kind='applications' AND date >= date('now','-6 days')").fetchone()["c"]
    cur_streak, best_streak = _streaks(_app_dates(conn))
    bd = conn.execute("SELECT date, count FROM daily_counters WHERE kind='applications' "
                      "ORDER BY count DESC, date DESC LIMIT 1").fetchone()
    return {"sent": sent, "companies": companies, "responses": responses,
            "interviews": interviews, "awaiting": awaiting, "today": today,
            "this_week": this_week, "streak": cur_streak, "best_streak": best_streak,
            "best_day": (dict(bd) if bd else None)}


def pipeline_stages(conn: sqlite3.Connection) -> list[dict]:
    """Where the applications are right now — answers 'where are my applications'."""
    one = lambda sql: conn.execute(sql).fetchone()["c"]
    return [
        {"key": "queue", "label": "In queue", "icon": "inventory_2",
         "count": one("SELECT COUNT(*) c FROM jobs WHERE status='apply_queued'"),
         "hint": "Ready for the agent to submit next"},
        {"key": "awaiting", "label": "Awaiting reply", "icon": "hourglass_top",
         "count": one("SELECT COUNT(*) c FROM applications WHERE status='submitted'"),
         "hint": "Submitted — waiting to hear back"},
        {"key": "heard", "label": "Heard back", "icon": "mark_email_read",
         "count": one("SELECT COUNT(*) c FROM applications "
                      "WHERE status IN ('rejected','no_response')"),
         "hint": "A company responded"},
        {"key": "interview", "label": "Interviews", "icon": "handshake",
         "count": one("SELECT COUNT(*) c FROM applications WHERE status='interview'"),
         "hint": "The goal — keep pushing"},
    ]


def waiting_apps(conn: sqlite3.Connection) -> dict:
    """Submitted apps awaiting a reply, longest-waiting first, each tagged against
    the real follow-up (7d) / no-response (14d) windows."""
    from jobagent import config
    caps = config.caps()
    fu = int(caps.get("followup_after_days", 7))
    nr = int(caps.get("no_response_after_days", 14))
    rows = conn.execute(
        """
        SELECT j.title, COALESCE(j.apply_url, j.url) AS url,
               c.name AS company, COALESCE(NULLIF(c.region,''),'') AS region,
               COALESCE(j.selection_chance, a.predicted_chance) AS chance,
               CAST(julianday('now') - julianday(a.submitted_at) AS INTEGER) AS days
        FROM applications a JOIN jobs j ON j.id = a.job_id
        LEFT JOIN companies c ON c.id = j.company_id
        WHERE a.status = 'submitted' AND a.submitted_at IS NOT NULL
        ORDER BY a.submitted_at ASC
        """
    ).fetchall()
    items = []
    for r in rows:
        d = max(0, r["days"] or 0)
        stage = "going_quiet" if d >= nr else ("follow_up" if d >= fu else "fresh")
        items.append({**dict(r), "days": d, "stage": stage})
    return {"followup_days": fu, "noresponse_days": nr, "items": items}


def velocity_calendar(conn: sqlite3.Connection, weeks: int = 13) -> dict:
    """date -> applications-submitted count, for the streak heatmap (last N weeks)."""
    rows = conn.execute(
        "SELECT date, count FROM daily_counters WHERE kind='applications' "
        "AND date >= date('now', ?) ORDER BY date", (f"-{weeks * 7} days",)
    ).fetchall()
    return {r["date"]: r["count"] for r in rows}


def milestones(conn: sqlite3.Connection, mom: dict) -> list[dict]:
    """Derived achievements — honest, DB-driven; 'First interview' stays the locked goal."""
    defs = [
        ("first_app", "First application", "rocket_launch", mom["sent"] >= 1,
         "You started — the hardest part."),
        ("apps_50", "50 applications", "local_fire_department", mom["sent"] >= 50,
         "Real volume building."),
        ("apps_100", "100 applications", "military_tech", mom["sent"] >= 100,
         "Triple digits. Grind respected."),
        ("companies_50", "50 companies reached", "corporate_fare", mom["companies"] >= 50,
         "Wide net cast."),
        ("first_resp", "First company response", "mark_email_read", mom["responses"] >= 1,
         "A real person/process engaged with you."),
        ("streak_5", "5-day streak", "bolt", mom["best_streak"] >= 5,
         "Consistency compounds."),
        ("first_interview", "First interview", "handshake", mom["interviews"] >= 1,
         "The big one — still ahead. Keep going."),
    ]
    return [{"key": k, "label": l, "icon": i, "achieved": bool(a), "hint": h}
            for k, l, i, a, h in defs]


def discovery_funnel(conn: sqlite3.Connection) -> list[dict]:
    """All-time pipeline funnel + TODAY's uptick (local-time, matches the wall clock)."""
    one = lambda sql: conn.execute(sql).fetchone()["c"]
    DT = "date(discovered_at,'localtime') = date('now','localtime')"
    discovered = one("SELECT COUNT(*) c FROM jobs")
    passed = one("SELECT COUNT(*) c FROM jobs "
                 "WHERE status NOT IN ('discovered','prefiltered_out')")
    queued = one("SELECT COUNT(*) c FROM jobs WHERE status='apply_queued'")
    applied = one("SELECT COUNT(*) c FROM applications WHERE submitted_at IS NOT NULL")
    disc_today = one(f"SELECT COUNT(*) c FROM jobs WHERE {DT}")
    passed_today = one("SELECT COUNT(*) c FROM jobs "
                       f"WHERE status NOT IN ('discovered','prefiltered_out') AND {DT}")
    applied_today = one("SELECT COUNT(*) c FROM applications "
                        "WHERE date(submitted_at,'localtime') = date('now','localtime')")
    steps = [
        {"label": "Discovered", "icon": "travel_explore", "count": discovered,
         "today": disc_today, "hint": "jobs the agent found"},
        {"label": "Passed prefilter", "icon": "filter_alt", "count": passed,
         "today": passed_today, "hint": "cleared seniority / location / fit gates"},
        {"label": "Apply queued", "icon": "inventory_2", "count": queued,
         "today": None, "hint": "scored & waiting to be submitted"},
        {"label": "Applied", "icon": "send", "count": applied,
         "today": applied_today, "hint": "actually submitted"},
    ]
    for s in steps:
        s["pct"] = round(s["count"] / discovered * 100, 2) if discovered else 0
    return steps


def outcomes(conn: sqlite3.Connection) -> dict:
    """Rejection-rate + predicted-chance-vs-outcome metrics over submitted apps."""
    rows = conn.execute(
        "SELECT a.predicted_chance AS pc, j.selection_chance AS sc, a.status AS status "
        "FROM applications a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.submitted_at IS NOT NULL"
    ).fetchall()
    rejected = sum(1 for r in rows if r["status"] == "rejected")
    no_resp = sum(1 for r in rows if r["status"] == "no_response")
    interview = sum(1 for r in rows if r["status"] == "interview")
    awaiting = sum(1 for r in rows if r["status"] == "submitted")
    resolved = rejected + no_resp + interview
    pcs = [(r["pc"] if r["pc"] is not None else r["sc"]) for r in rows
           if (r["pc"] is not None or r["sc"] is not None)]
    avg_chance = round(sum(pcs) / len(pcs)) if pcs else 0

    def band(pc):
        if pc is None:
            return None
        return "<20%" if pc < 20 else "20–29%" if pc < 30 else "30–39%" if pc < 40 else "40%+"

    order = ["<20%", "20–29%", "30–39%", "40%+"]
    acc: dict = {}
    for r in rows:
        b = band(r["pc"] if r["pc"] is not None else r["sc"])
        if not b:
            continue
        d = acc.setdefault(b, {"total": 0, "rejected": 0, "awaiting": 0})
        d["total"] += 1
        if r["status"] == "rejected":
            d["rejected"] += 1
        elif r["status"] == "submitted":
            d["awaiting"] += 1
    # rej_rate = rejected as a share of ALL apps sent in that band (incidence so far),
    # NOT of resolved — with 0 interviews, "of resolved" is a useless 100% everywhere.
    bands = [{"label": b, **acc[b],
              "rej_rate": round(acc[b]["rejected"] / acc[b]["total"] * 100) if acc[b]["total"] else 0}
             for b in order if b in acc]
    total = len(rows)
    return {
        "total": total, "rejected": rejected, "no_response": no_resp,
        "interview": interview, "awaiting": awaiting, "resolved": resolved,
        # rates over ALL sent — honest + motivating (60% still in play, not 100% rejected)
        "rejection_rate": round(rejected / total * 100) if total else 0,
        "in_play_rate": round(awaiting / total * 100) if total else 0,
        "positive_rate": round(interview / total * 100) if total else 0,
        "avg_chance": avg_chance, "bands": bands,
    }


def region_active(conn: sqlite3.Connection) -> list[dict]:
    """Per-region breakdown of submitted applications: awaiting / rejected / interview."""
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(UPPER(c.region), ''), 'OTHER') AS region,
               COUNT(*) AS sent,
               SUM(CASE WHEN a.status = 'submitted' THEN 1 ELSE 0 END) AS awaiting,
               SUM(CASE WHEN a.status = 'rejected'  THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN a.status = 'interview' THEN 1 ELSE 0 END) AS interview
        FROM applications a JOIN jobs j ON j.id = a.job_id
        LEFT JOIN companies c ON c.id = j.company_id
        WHERE a.submitted_at IS NOT NULL
        GROUP BY region ORDER BY sent DESC
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
                   j.title, j.url, j.score, j.selection_chance,
                   a.predicted_chance,
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

        # --- motivational "My Journey" data (client renders charts/rings from JSON) ---
        mom = momentum(conn)
        stages = pipeline_stages(conn)
        waiting = waiting_apps(conn)
        calendar = velocity_calendar(conn)
        miles = milestones(conn, mom)
        calib = calibration(conn)
        chances = top_chances(conn)
        funnel_steps = discovery_funnel(conn)
        outc = outcomes(conn)
        sg_rows, sg_total = skills_gap(conn)
        journey = {
            "momentum": mom,
            "stages": stages,
            "funnel": funnel_steps,
            "outcomes": outc,
            "regions_active": region_active(conn),
            "waiting": waiting,
            "calendar": calendar,
            "milestones": miles,
            "calibration": calib,
            "top_chances": chances,
            "timeline": chart_timeline(conn),
            "regions": chart_regions(conn),
            "companies": chart_companies(conn),
            "skills_gap": {"rows": sg_rows, "total": sg_total},
        }

        context = {
            "request": request,
            "killed": killswitch.is_killed(),
            "funnel": funnel(conn),
            "applications": applications,
            "replies": replies,
            "review_items": review_items,
            "events": events,
            "calibration": calib,
            "top_chances": chances,
            "active_apps": active_applications(conn),
            "applied_today": mom["today"],
            "momentum": mom,
            "stages": stages,
            "funnel_steps": funnel_steps,
            "outcomes": outc,
            "waiting": waiting,
            "milestones": miles,
            "skills_gap": sg_rows,
            "skills_gap_total": sg_total,
            "journey_json": (json.dumps(journey, default=str)
                             .replace("<", "\\u003c").replace(">", "\\u003e")
                             .replace("&", "\\u0026")),
            # cache-bust static assets on every code change (mtime-based)
            "asset_v": max((int((STATIC_DIR / f).stat().st_mtime)
                            for f in ("journey.css", "journey.js")
                            if (STATIC_DIR / f).exists()), default=1),
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


@app.post("/jobs/{job_id}/mark-applied")
def mark_applied(job_id: int) -> RedirectResponse:
    """User applied to this job by hand — record it like any submission."""
    conn = db.connect()
    try:
        job = conn.execute(
            "SELECT id, selection_chance FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        conn.execute(
            """
            INSERT INTO applications (job_id, method, status, submitted_at, predicted_chance)
            VALUES (?, 'manual', 'submitted', datetime('now'), ?)
            ON CONFLICT(job_id) DO UPDATE SET
              method='manual', status='submitted',
              submitted_at=COALESCE(submitted_at, datetime('now')),
              predicted_chance=COALESCE(predicted_chance, excluded.predicted_chance)
            """,
            (job_id, job["selection_chance"]),
        )
        conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
        conn.execute(
            "UPDATE review_queue SET resolved_at=datetime('now'), "
            "resolution='applied_manually' WHERE job_id=? AND resolved_at IS NULL",
            (job_id,),
        )
        conn.commit()
        db.log_event(conn, "job", job_id, "application_submitted",
                     {"method": "manual", "via": "dashboard"})
    finally:
        conn.close()
    return RedirectResponse(url="/", status_code=303)


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
