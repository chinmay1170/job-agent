import sys, time, subprocess
sys.path.insert(0, "/Users/chinmaykrishna/Documents/job-agent")
from jobagent import db
from jobagent.tailor.tailor import run_tailor

# 1) tailor every untailored queued job
conn = db.connect()
todo = [r["id"] for r in conn.execute(
    "SELECT j.id FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
    "WHERE j.status='apply_queued' AND a.resume_path IS NULL "
    "ORDER BY j.selection_chance DESC").fetchall()]
conn.close()
print(f"TAILORING {len(todo)}", flush=True)
for jid in todo:
    try:
        run_tailor(job_id=jid)
    except Exception as e:
        print("tfail", jid, str(e)[:40], flush=True)
    time.sleep(2)
print("TAILOR_DONE", flush=True)

# 2) chunked apply, fresh browser per chunk (avoids the ~13-job driver crash)
from jobagent.apply.runner import run_apply
ROOT = "/Users/chinmaykrishna/Documents/job-agent"
for ch in range(16):
    conn = db.connect()
    q = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='apply_queued'").fetchone()[0]
    conn.close()
    if q == 0:
        print("QUEUE EMPTY", flush=True)
        break
    print(f"=== chunk {ch+1}: {q} queued ===", flush=True)
    subprocess.run(["pkill", "-9", "-f", "Chrome for Testing"], capture_output=True)
    subprocess.run("rm -f data/browser_profile/Singleton*", shell=True, cwd=ROOT)
    time.sleep(2)
    try:
        run_apply(limit=6, dry_run=False)
    except Exception as e:
        print("chunk err", str(e)[:60], flush=True)
    time.sleep(4)

from jobagent.inbox.manual_email import send_manual_apply_email
send_manual_apply_email()
print("BIG_VOLUME_DONE", flush=True)
