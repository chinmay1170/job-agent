"""Wait for the running apply to finish, then requeue fixable + re-apply with
the consent-checkbox / token-embed / mapper-deferral fixes. Headless."""
import sys, time, subprocess
sys.path.insert(0,"/Users/chinmaykrishna/Documents/job-agent")
while subprocess.run(["pgrep","-f","run_apply"],capture_output=True,text=True).stdout.strip():
    time.sleep(15)
subprocess.run(["pkill","-9","-f","Chrome for Testing"],capture_output=True)
subprocess.run("rm -f data/browser_profile/Singleton*",shell=True,cwd="/Users/chinmaykrishna/Documents/job-agent")
time.sleep(3)
from jobagent.inbox.manual_email import requeue_fixable, send_manual_apply_email
n=requeue_fixable(); print(f"requeued {n}",flush=True)
# tailor any requeued that lack a resume
from jobagent import db
from jobagent.tailor.tailor import run_tailor
conn=db.connect()
todo=[r["id"] for r in conn.execute("SELECT j.id FROM jobs j LEFT JOIN applications a ON a.job_id=j.id WHERE j.status='apply_queued' AND a.resume_path IS NULL").fetchall()]
conn.close()
for jid in todo:
    try: run_tailor(job_id=jid)
    except Exception as e: print("tfail",jid,str(e)[:40],flush=True)
    time.sleep(2)
print("TAILOR_DONE",flush=True)
from jobagent.apply.runner import run_apply
run_apply(limit=40, dry_run=False)
send_manual_apply_email()
print("FINAL_SWEEP_DONE",flush=True)
