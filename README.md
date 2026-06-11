# job-agent

Personal automated job-application agent for Chinmay Krishna. Discovers senior
backend/distributed-systems roles with **visa sponsorship + relocation**
(US / EU / UK / UAE / Australia), tailors a resume + cover letter per job,
fills and submits ATS applications, sends personalized recruiter emails, and
watches the inbox — human involvement only when a recruiter replies.

## Design principles

- **Sponsorship pre-filter is the core**: only apply where there's evidence the
  company sponsors (government registers: US H-1B Data Hub, NL IND, UK licensed
  sponsors, AU sponsor register; UAE sponsors all expat hires by default).
- **Never touch LinkedIn with a logged-in account** — discovery is via free
  public ATS APIs (Greenhouse/Lever/Ashby/Workable/SmartRecruiters), Adzuna,
  Remotive, RemoteOK, HN Who's Hiring, and logged-out JobSpy scrapes.
- **Honest answers, always**: visa/salary/legal questions are answered verbatim
  from `config/answers.yaml` or not at all (→ review queue). Never fabricated.
- **Proof for everything**: every submission saves the exact PDFs, answers,
  filled-form + confirmation screenshots under `artifacts/{job_id}/`.

## Daily pipeline

```
sponsors ingest → discover → score prefilter → score judge (claude -p)
→ tailor (claude -p + typst) → apply (Playwright) → outreach run
→ inbox scan → digest
```

Run any stage: `uv run jobagent <stage>` — see `uv run jobagent --help`.
Dashboard: `uv run jobagent dashboard` → http://localhost:8787.
Kill switch: `uv run jobagent stop` (or `touch data/KILL`).

## Safety

- `config/caps.yaml` — daily caps (applications start at 3/day), per-company
  lifetime limit 2 + 30-day cooldown, email ≤10/day with jittered spacing.
- `dry_run: true` in caps.yaml is the global default — fills forms and
  screenshots but never clicks submit until flipped.
- Blocklist (`config/blocklist.yaml`) includes Adobe + subsidiaries.
- Required form fields the LLM can't ground in config at ≥0.85 confidence are
  never guessed — the application goes to the review queue on the dashboard.

## One-time setup still needed

1. Fill `REVIEW ME` items in `config/answers.yaml` (notice period, start date)
   and salary expectations (null = salary questions queue for review).
2. Adzuna free API keys → `.env` (`ADZUNA_APP_ID`, `ADZUNA_APP_KEY`).
3. Gmail OAuth for sending: `uv run python scripts/setup_gmail_oauth.py`
   (instructions print on first run).
4. Install the schedule: see `scripts/INSTALL.md` (launchd, 09:30 + 19:00).
