#!/bin/bash
# JobAgent daily pipeline. Invoked by launchd at 09:30 and 19:00 (same plist,
# same script). Evening behavior: pass "evening" as $1, OR let the script
# detect hour >= 17 — then it runs only inbox scan + apply second pass + digest.
#
# Each stage logs to logs/{stage}-YYYY-MM-DD.log; a stage failure never stops
# the run. Always exits 0 so launchd never marks the job broken.

set -u

# Guard: uv sync can rewrite editable-install .pth files with the macOS
# UF_HIDDEN flag set, which Python 3.12 site.py silently skips -> every
# jobagent command dies with ModuleNotFoundError. Unhide before each run.
export PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)"
chflags nohidden .venv/lib/python3.12/site-packages/*.pth 2>/dev/null || true

cd "$(dirname "$0")/.." || exit 0
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

mkdir -p logs
TODAY="$(date +%F)"
MODE="${1:-}"
HOUR="$(date +%H)"
EVENING=0
if [ "$MODE" = "evening" ] || [ "$HOUR" -ge 17 ]; then
    EVENING=1
fi

APPLY_LIMIT="$(awk -F': *' '/^applications_per_day:/ {print $2}' config/caps.yaml | awk '{print $1}')"
APPLY_LIMIT="${APPLY_LIMIT:-3}"

STAGES_RUN=()

run_stage() {
    local name="$1"; shift
    local log="logs/${name}-${TODAY}.log"
    STAGES_RUN+=("$name")
    {
        echo "=== ${name} @ $(date '+%F %T') ==="
        if "$@"; then
            echo "=== ${name} OK ==="
        else
            echo "=== ${name} FAILED (rc=$?) — continuing ==="
        fi
    } >> "$log" 2>&1
}

if [ "$EVENING" -eq 1 ]; then
    echo "JobAgent evening run ($(date '+%F %T'))"
    run_stage inbox    uv run --no-sync jobagent inbox scan
    run_stage apply    uv run --no-sync jobagent apply --limit "$APPLY_LIMIT"
    run_stage digest   uv run --no-sync jobagent digest
else
    echo "JobAgent morning run ($(date '+%F %T'))"
    if [ "$(date +%u)" -eq 1 ]; then
        run_stage sponsors uv run --no-sync jobagent sponsors ingest --all
    fi
    run_stage discover  uv run --no-sync jobagent discover --source all
    run_stage prefilter uv run --no-sync jobagent score prefilter
    run_stage judge     uv run --no-sync jobagent score judge
    run_stage tailor    uv run --no-sync jobagent tailor --all-queued
    run_stage apply     uv run --no-sync jobagent apply --limit "$APPLY_LIMIT"
    run_stage outreach  uv run --no-sync jobagent outreach run
    run_stage inbox     uv run --no-sync jobagent inbox scan
    run_stage digest    uv run --no-sync jobagent digest
fi

echo "--- stage log summary (${TODAY}) ---"
for name in "${STAGES_RUN[@]}"; do
    log="logs/${name}-${TODAY}.log"
    if [ -f "$log" ]; then
        lines="$(wc -l < "$log" | tr -d ' ')"
        tail_line="$(tail -n 1 "$log")"
        echo "${name}: ${lines} log lines | last: ${tail_line}"
    else
        echo "${name}: no log"
    fi
done

exit 0
