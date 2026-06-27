"""Apply to every queued Greenhouse job: tailor + benchmark what's missing,
park non-Greenhouse queue entries for the run, apply, restore."""
import sys
import time

sys.path.insert(0, "/Users/chinmaykrishna/Documents/job-agent")

from jobagent import db
from jobagent.enrich import benchmark_salaries
from jobagent.tailor.tailor import run_tailor

conn = db.connect()
gh = [r["id"] for r in conn.execute(
    "SELECT id FROM jobs WHERE status='apply_queued' AND apply_url LIKE '%greenhouse%' "
    "ORDER BY selection_chance DESC").fetchall()]
print(f"greenhouse queued: {len(gh)}", flush=True)

untailored = [r["id"] for r in conn.execute(
    f"SELECT j.id FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
    f"WHERE j.id IN ({','.join('?' * len(gh))}) AND a.resume_path IS NULL",
    gh).fetchall()]
for jid in untailored:
    try:
        run_tailor(job_id=jid)
    except Exception as e:  # noqa: BLE001
        print(f"TAILOR_FAIL {jid}: {e}", flush=True)
    time.sleep(2)
print("TAILOR_DONE", flush=True)

# Park everything that's not Greenhouse so the runner only sees these jobs.
conn.execute("UPDATE jobs SET status='gh_parked' WHERE status='apply_queued' "
             "AND apply_url NOT LIKE '%greenhouse%'")
conn.commit()
conn.close()

try:
    benchmark_salaries(limit=len(gh))
    print("SALARY_DONE", flush=True)
    from jobagent.apply.runner import run_apply
    run_apply(limit=len(gh), dry_run=False)
finally:
    conn = db.connect()
    conn.execute("UPDATE jobs SET status='apply_queued' WHERE status='gh_parked'")
    conn.commit()
    conn.close()
    print("RESTORED_PARKED", flush=True)
print("GH_SWEEP_DONE", flush=True)
