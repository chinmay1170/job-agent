# Installing the JobAgent launchd schedule

The LaunchAgent runs `scripts/run_daily.sh` twice a day:

- **09:30** — full pipeline (sponsors on Mondays, discover, score, tailor,
  apply, outreach, inbox scan, digest)
- **19:00** — light evening pass (the script detects hour >= 17 and runs only
  inbox scan, a second apply pass, and the digest)

## Install

First edit `scripts/com.jobagent.plist` and replace `/ABSOLUTE/PATH/TO/job-agent`
with the absolute path to your clone (in `WorkingDirectory` and the log paths).

```bash
mkdir -p ~/Library/LaunchAgents
cp scripts/com.jobagent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jobagent.plist
```

## Verify / operate

```bash
launchctl list | grep com.jobagent     # is it loaded?
launchctl start com.jobagent           # trigger a run right now
tail -f logs/launchd.log               # watch output
```

Per-stage logs land in `logs/{stage}-YYYY-MM-DD.log`.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.jobagent.plist
rm ~/Library/LaunchAgents/com.jobagent.plist
```

## Notes

- The plist hardcodes the project path in `WorkingDirectory` and the log paths —
  set `/ABSOLUTE/PATH/TO/job-agent` to your clone before loading.
- `run_daily.sh` always exits 0 so launchd never disables the job; failures
  are visible in the per-stage logs and the daily digest's Errors section.
- Emergency stop at any time: `uv run jobagent stop` (or `touch data/KILL`).
- A separate `scripts/com.jobagent.dashboard.plist` keeps the dashboard running.
