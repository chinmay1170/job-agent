"""Backfill SDE2/SE2 roles title-rejected before the include-list widened.
Waits for any running fresh_cycle/apply to finish, then reprocesses them."""
import sys, time, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Wait for the in-flight cycle to release the DB / browser / claude budget.
def _busy():
    out = subprocess.run(["pgrep","-f","fresh_cycle"], capture_output=True, text=True).stdout
    return bool(out.strip())
while _busy():
    time.sleep(20)
print("prior cycle finished — starting SDE2 backfill", flush=True)

from jobagent import db
from jobagent.discover.base import title_passes
from jobagent.score.prefilter import _seniority_acceptable, run_prefilter
conn = db.connect()
rows = conn.execute("SELECT id,title FROM jobs WHERE status='prefiltered_out' AND score_reasons='title'").fetchall()
ids = [r["id"] for r in rows if title_passes(r["title"] or "") and _seniority_acceptable(r["title"] or "")]
conn.executemany("UPDATE jobs SET status='discovered', score_reasons=NULL WHERE id=?", [(i,) for i in ids])
conn.commit(); conn.close()
print(f"reset {len(ids)} SDE2 roles to discovered", flush=True)

print("== PREFILTER ==", flush=True); print(run_prefilter(), flush=True)
from jobagent.enrich import enrich_pipeline
print("== ENRICH ==", flush=True); enrich_pipeline(limit=40)
from jobagent.score.judge import run_judge
print("== JUDGE ==", flush=True); print(run_judge(limit=80), flush=True)

from jobagent.tailor.tailor import run_tailor
conn = db.connect()
todo = [r["id"] for r in conn.execute(
    "SELECT j.id FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
    "WHERE j.status='apply_queued' AND a.resume_path IS NULL ORDER BY j.selection_chance DESC LIMIT 25").fetchall()]
conn.close()
print(f"== TAILOR {len(todo)} ==", flush=True)
for jid in todo:
    try: run_tailor(job_id=jid)
    except Exception as e: print("tfail", jid, str(e)[:50], flush=True)
    time.sleep(2)

from jobagent.apply.runner import run_apply
print("== APPLY ==", flush=True); run_apply(limit=20, dry_run=False)
from jobagent.inbox.manual_email import send_manual_apply_email
send_manual_apply_email()
print("BACKFILL_DONE", flush=True)
