"""jobagent CLI — every pipeline stage is a discrete, idempotent command.

    uv run jobagent init                  # create/migrate DB
    uv run jobagent sponsors ingest --nl --uk --us --au
    uv run jobagent discover --source all
    uv run jobagent score prefilter
    uv run jobagent score judge
    uv run jobagent tailor --job-id 12    # or --all-queued
    uv run jobagent apply --limit 3 [--dry-run/--live]
    uv run jobagent outreach run [--shadow]
    uv run jobagent inbox scan
    uv run jobagent digest
    uv run jobagent dashboard             # localhost:8787
    uv run jobagent stop / resume         # kill switch
    uv run jobagent log --today
"""
from __future__ import annotations

import json

import typer

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
sponsors_app = typer.Typer(no_args_is_help=True)
score_app = typer.Typer(no_args_is_help=True)
outreach_app = typer.Typer(no_args_is_help=True)
inbox_app = typer.Typer(no_args_is_help=True)
app.add_typer(sponsors_app, name="sponsors")
app.add_typer(score_app, name="score")
app.add_typer(outreach_app, name="outreach")
app.add_typer(inbox_app, name="inbox")


@app.command()
def init() -> None:
    """Create/migrate the database."""
    from jobagent import db
    conn = db.connect()
    conn.close()
    typer.echo(f"DB ready at {db.DB_PATH}")


@sponsors_app.command("ingest")
def sponsors_ingest(
    nl: bool = typer.Option(False, "--nl"),
    uk: bool = typer.Option(False, "--uk"),
    us: bool = typer.Option(False, "--us"),
    au: bool = typer.Option(False, "--au"),
    all_: bool = typer.Option(False, "--all"),
) -> None:
    """Ingest government sponsor registers into the companies table."""
    from jobagent.sponsors import run_ingest
    run_ingest(nl=nl or all_, uk=uk or all_, us=us or all_, au=au or all_)


@app.command()
def discover(source: str = typer.Option("all", "--source")) -> None:
    """Fetch job listings from one or all sources."""
    from jobagent.discover import run_discover
    run_discover(source)


@score_app.command("prefilter")
def score_prefilter() -> None:
    """Deterministic gates: sponsorship, title, location, blocklist, cooldown."""
    from jobagent.score.prefilter import run_prefilter
    run_prefilter()


@score_app.command("judge")
def score_judge(limit: int = typer.Option(60, "--limit")) -> None:
    """LLM fit-scoring of prefilter survivors (batched claude -p)."""
    from jobagent.score.judge import run_judge
    run_judge(limit=limit)


@app.command()
def tailor(
    job_id: int = typer.Option(None, "--job-id"),
    all_queued: bool = typer.Option(False, "--all-queued"),
) -> None:
    """Generate tailored resume + cover letter PDFs for a job."""
    from jobagent.tailor.tailor import run_tailor
    run_tailor(job_id=job_id, all_queued=all_queued)


@app.command()
def apply(
    limit: int = typer.Option(3, "--limit"),
    dry_run: bool = typer.Option(None, "--dry-run/--live"),
    job_id: int = typer.Option(None, "--job-id"),
) -> None:
    """Fill and submit queued applications via Playwright."""
    from jobagent.apply.runner import run_apply
    run_apply(limit=limit, dry_run=dry_run, job_id=job_id)


@outreach_app.command("run")
def outreach_run(shadow: bool = typer.Option(False, "--shadow")) -> None:
    """Find contacts, compose and send personalized outreach emails."""
    from jobagent.outreach.send import run_outreach
    run_outreach(shadow=shadow)


@inbox_app.command("scan")
def inbox_scan() -> None:
    """Read Gmail, classify replies, update statuses, notify on interviews."""
    from jobagent.inbox.watcher import run_scan
    run_scan()


@app.command()
def digest() -> None:
    """Write + email the daily digest."""
    from jobagent.inbox.digest import run_digest
    run_digest()


@app.command()
def enrich(limit: int = typer.Option(25, "--limit")) -> None:
    """Fill market cap / employee count / HQ for pipeline companies."""
    from jobagent.enrich import enrich_pipeline
    enrich_pipeline(limit=limit)


@app.command()
def dashboard(port: int = typer.Option(8787, "--port")) -> None:
    """Serve the tracking dashboard on localhost."""
    import uvicorn
    uvicorn.run("jobagent.dashboard.app:app", host="127.0.0.1", port=port)


@app.command()
def stop() -> None:
    """Engage the kill switch — halts all submits/sends immediately."""
    from jobagent import killswitch
    killswitch.engage()
    typer.echo("KILL switch engaged.")


@app.command()
def resume() -> None:
    """Release the kill switch."""
    from jobagent import killswitch
    killswitch.release()
    typer.echo("Kill switch released.")


@app.command("log")
def log_(today: bool = typer.Option(True, "--today/--all")) -> None:
    """Print the events audit trail."""
    from jobagent import db
    conn = db.connect()
    q = "SELECT ts, entity_type, entity_id, event, detail FROM events"
    if today:
        q += " WHERE date(ts)=date('now')"
    q += " ORDER BY id"
    for r in conn.execute(q):
        detail = r["detail"] or ""
        if len(detail) > 120:
            detail = detail[:117] + "..."
        typer.echo(f"{r['ts']}  {r['entity_type']}#{r['entity_id']}  {r['event']}  {detail}")


@app.command()
def status() -> None:
    """One-line funnel summary."""
    from jobagent import db
    conn = db.connect()
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    apps = dict(conn.execute("SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall())
    rq = conn.execute("SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL").fetchone()[0]
    typer.echo(f"jobs: {json.dumps(counts)}")
    typer.echo(f"applications: {json.dumps(apps)}")
    typer.echo(f"review queue (open): {rq}")


if __name__ == "__main__":
    app()
