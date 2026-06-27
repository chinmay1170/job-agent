"""Skip discovery — run prefilter -> enrich -> judge on already-discovered jobs,
so today's fresh finds get scored into the apply queue fast. run_volume (chained
after) does the tailor/point/apply loop."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobagent.score.prefilter import run_prefilter
print("== PREFILTER ==", flush=True)
print(run_prefilter(), flush=True)
print("PREFILTER_DONE", flush=True)

from jobagent.enrich import enrich_pipeline
print("== ENRICH ==", flush=True)
try:
    enrich_pipeline(limit=60)
except Exception as e:  # noqa: BLE001
    print("enrich warn:", str(e)[:120], flush=True)
print("ENRICH_DONE", flush=True)

from jobagent.score.judge import run_judge
print("== JUDGE ==", flush=True)
print(run_judge(limit=200), flush=True)
print("JUDGE_DONE", flush=True)
print("APPLY_NOW_DONE", flush=True)
