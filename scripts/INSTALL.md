# Installing the JobAgent launchd schedule

The LaunchAgent runs `scripts/run_daily.sh` twice a day:

- **09:30** — full pipeline (sponsors on Mondays, discover, score, tailor,
  apply, outreach, inbox scan, digest)
- **19:00** — light evening pass (the script detects hour >= 17 and runs only
  inbox scan, a second apply pass, and the digest)

## Install

```bash
mkdir -p ~/Library/LaunchAgents
cp scripts/com.chinmay.jobagent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.chinmay.jobagent.plist
```

## Verify / operate

```bash
launchctl list | grep com.chinmay.jobagent     # is it loaded?
launchctl start com.chinmay.jobagent           # trigger a run right now
tail -f logs/launchd.log                       # watch output
```

Per-stage logs land in `logs/{stage}-YYYY-MM-DD.log`.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.chinmay.jobagent.plist
rm ~/Library/LaunchAgents/com.chinmay.jobagent.plist
```

## Notes

- The plist hardcodes the project path `/Users/chinmaykrishna/Documents/job-agent`
  in `WorkingDirectory` and the log paths; update those if the repo moves.
- `run_daily.sh` always exits 0 so launchd never disables the job; failures
  are visible in the per-stage logs and the daily digest's Errors section.
- Emergency stop at any time: `uv run jobagent stop` (or `touch data/KILL`).
