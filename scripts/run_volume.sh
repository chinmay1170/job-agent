#!/bin/bash
# Robust volume driver: each tailor batch and apply chunk is a FRESH short-lived
# python subprocess, so no process accumulates memory (the long-process exit-144
# / EPIPE crashes that capped earlier runs). Loops until the queue drains.
cd /Users/chinmaykrishna/Documents/job-agent || exit 1
export PYTHONPATH=/Users/chinmaykrishna/Documents/job-agent
PY=.venv/bin/python

# High-fit (>= tailor_min_chance) queued jobs get a per-JD TAILORED v2 resume
# before applying (honesty-checked). On tailor failure, fall back to the master
# so the job still applies. Batch-limited (LLM cost) — runs each iteration.
tailor_highfit() {
  $PY -c "import sys;sys.path.insert(0,'.');from jobagent.tailor.tailor_v2 import tailor_highfit_batch;tailor_highfit_batch(limit=4)"
}

# LOW-fit (< tailor_min_chance) queued jobs use the master resume (no LLM).
# High-fit untailored jobs are intentionally left for tailor_highfit().
point_at_original() {
  $PY -c "
import sys; sys.path.insert(0,'.')
from jobagent import db, config
caps=config.caps(); orig=caps.get('original_resume_path','config/Chinmay_Krishna_Resume.pdf'); minc=caps.get('tailor_min_chance',35)
conn=db.connect()
rows=conn.execute(\"SELECT j.id FROM jobs j LEFT JOIN applications a ON a.job_id=j.id WHERE j.status='apply_queued' AND a.resume_path IS NULL AND (j.selection_chance < ? OR j.selection_chance IS NULL)\",(minc,)).fetchall()
n=0
for r in rows:
    if conn.execute('SELECT id FROM applications WHERE job_id=?',(r['id'],)).fetchone():
        conn.execute(\"UPDATE applications SET resume_path=?, status='pending' WHERE job_id=? AND status NOT IN ('submitted','rejected','interview')\",(orig,r['id']))
    else:
        conn.execute(\"INSERT INTO applications(job_id,resume_path,status,created_at) VALUES(?,?,'pending',datetime('now'))\",(r['id'],orig))
    n+=1
conn.commit(); conn.close(); print('pointed',n,flush=True)
"
}

# Retry portals that failed (up to 3x), then escalate exhausted ones to manual
# email — so the queue fully clears (every job ends submitted OR emailed).
retry_sweep() {
  $PY -c "import sys;sys.path.insert(0,'.');from jobagent.inbox.manual_email import requeue_fixable;requeue_fixable(max_attempts=3)"
}

apply_chunk() {
  pkill -9 -f "Chrome for Testing" 2>/dev/null
  rm -f data/browser_profile/Singleton* 2>/dev/null
  sleep 2
  $PY -c "
import sys; sys.path.insert(0,'.')
from jobagent.apply.runner import run_apply
run_apply(limit=6, dry_run=False)
" 2>&1 | grep -E -- "-> submitted|-> queued|-> platform_cap|-> company_cap|-> ats_blocked"
}

# Goal: CLEAR THE QUEUE. Loop more times since retries re-feed the queue; the
# loop exits early when nothing is left to apply. 60 iters is a safe backstop.
for i in $(seq 1 60); do
  retry_sweep          # bring failed-but-<3-attempts jobs back; escalate exhausted
  tailor_highfit       # tailor high-fit (>=35) jobs into the v2 design (honesty-checked)
  point_at_original    # low-fit jobs -> master resume (no LLM)
  QUEUED=$($PY -c "import sys;sys.path.insert(0,'.');from jobagent import db;c=db.connect();print(c.execute(\"SELECT COUNT(*) FROM jobs WHERE status='apply_queued'\").fetchone()[0])")
  echo "=== iter $i: queued=$QUEUED ==="
  if [ "$QUEUED" -eq 0 ]; then echo "QUEUE EMPTY"; break; fi
  apply_chunk
  # send manual-apply email for any retry-exhausted/blocked jobs accumulated so far
  $PY -c "import sys;sys.path.insert(0,'.');from jobagent.inbox.manual_email import send_manual_apply_email;send_manual_apply_email()" 2>/dev/null
done
$PY -c "import sys;sys.path.insert(0,'.');from jobagent.inbox.manual_email import send_manual_apply_email;send_manual_apply_email()"
echo "RUN_VOLUME_DONE"
