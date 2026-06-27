"""One-shot: tailor top-N queued jobs, then run the live apply batch.

Used for manual catch-up runs; the daily launchd pipeline does the same via
run_daily.sh. Tailors only the top of the queue (by selection_chance) to keep
claude -p call volume proportional to what apply can actually submit today.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobagent import db
from jobagent.tailor.tailor import run_tailor

TAILOR_TOP_N = 60

conn = db.connect()
todo = conn.execute(
    """
    SELECT j.id FROM jobs j
    LEFT JOIN applications a ON a.job_id = j.id
    WHERE j.status = 'apply_queued' AND a.resume_path IS NULL
    ORDER BY j.selection_chance DESC, j.score DESC LIMIT ?
    """,
    (TAILOR_TOP_N,),
).fetchall()
conn.close()
print(f"tailoring {len(todo)} job(s)", flush=True)

ok = fail = 0
for row in todo:
    try:
        run_tailor(job_id=row["id"])
        ok += 1
    except Exception as e:  # keep going; one bad JD must not kill the batch
        fail += 1
        print(f"TAILOR_FAIL job={row['id']}: {e}", flush=True)
    time.sleep(2)
print(f"TAILOR_DONE ok={ok} fail={fail}", flush=True)

from jobagent.enrich import benchmark_salaries

benchmark_salaries(limit=60)
print("SALARY_DONE", flush=True)

from jobagent.apply.runner import run_apply

run_apply(limit=50, dry_run=False)
print("APPLY_DONE", flush=True)
