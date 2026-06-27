"""One-shot fresh cycle with the new system: discover big-cos -> prefilter
(seniority/YoE-gated) -> enrich (tier) -> judge (tier-aware) -> tailor -> apply
(tier-balanced + per-platform caps)."""
import sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobagent.discover import run_discover
print("== DISCOVER ==", flush=True)
try: run_discover("all")
except Exception as e: print("discover warn:", str(e)[:120], flush=True)
print("DISCOVER_DONE", flush=True)

from jobagent.score.prefilter import run_prefilter
print("== PREFILTER ==", flush=True)
print(run_prefilter(), flush=True)
print("PREFILTER_DONE", flush=True)

from jobagent.enrich import enrich_pipeline
print("== ENRICH (tier) ==", flush=True)
enrich_pipeline(limit=60)
print("ENRICH_DONE", flush=True)

from jobagent.score.judge import run_judge
print("== JUDGE ==", flush=True)
print(run_judge(limit=200), flush=True)
print("JUDGE_DONE", flush=True)

from jobagent.tailor.tailor import run_tailor
from jobagent import db
conn = db.connect()
todo = [r["id"] for r in conn.execute(
    "SELECT j.id FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
    "WHERE j.status='apply_queued' AND a.resume_path IS NULL "
    "ORDER BY j.selection_chance DESC LIMIT 40").fetchall()]
conn.close()
print(f"== TAILOR {len(todo)} ==", flush=True)
for jid in todo:
    try: run_tailor(job_id=jid)
    except Exception as e: print("tfail", jid, str(e)[:60], flush=True)
    time.sleep(2)
print("TAILOR_DONE", flush=True)

from jobagent.apply.runner import run_apply
print("== APPLY ==", flush=True)
run_apply(limit=30, dry_run=False)
from jobagent.inbox.manual_email import send_manual_apply_email
send_manual_apply_email()
print("FRESH_CYCLE_DONE", flush=True)
