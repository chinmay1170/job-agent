"""Discover ONLY the freshly-added (verified 2026-06-26) companies, then
prefilter -> enrich -> judge. Fast refill without re-scraping all 701 boards."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml

SEED = str(ROOT / "config/seeds/eu_uae_boards.yaml")
doc = yaml.safe_load(open(SEED))
new = [c for c in doc["companies"] if str(c.get("verified")) == "2026-06-26"]
yaml.safe_dump({"companies": new}, open("/tmp/new_seeds.yaml", "w"))
print(f"new-company seeds: {len(new)}", flush=True)

from jobagent.discover import run_discover
print("== DISCOVER (new companies only) ==", flush=True)
print(run_discover("all", seeds_path="/tmp/new_seeds.yaml"), flush=True)
print("DISCOVER_DONE", flush=True)

from jobagent.score.prefilter import run_prefilter
print("== PREFILTER ==", flush=True)
print(run_prefilter(), flush=True)

from jobagent.enrich import enrich_pipeline
print("== ENRICH ==", flush=True)
try:
    enrich_pipeline(limit=80)
except Exception as e:  # noqa: BLE001
    print("enrich warn:", str(e)[:100], flush=True)

from jobagent.score.judge import run_judge
print("== JUDGE ==", flush=True)
print(run_judge(limit=250), flush=True)
print("DISCOVER_NEW_DONE", flush=True)
