# job-agent

An automated job-application agent. Discovers roles across public ATS boards,
scores them for fit, tailors a resume + cover letter per job, fills and submits
ATS applications, sends personalized recruiter emails, and watches the inbox —
human involvement only when a recruiter replies.

**Configurable for any candidate** — your name, location, visa/sponsorship
situation, target regions, resume and screening answers all live in config
(`config/profile.yaml` + `config/answers.yaml`); no code changes needed.

## Quick start (new user)

```bash
# 1. install deps
uv sync                     # or: pip install -e .

# 2. create your config from the shipped templates
cp config/profile.example.yaml config/profile.yaml
cp config/answers.example.yaml config/answers.yaml
#    edit BOTH with your real details — especially profile.yaml `identity`
#    (name, email, location, needs_visa_sponsorship, target_regions,
#     home_exclude_terms) and every answer in answers.yaml (HONEST).

# 3. drop your resume PDF at config/resume.pdf
cp /path/to/your_resume.pdf config/resume.pdf

# 4. (optional) integrations — Adzuna keys + Gmail OAuth (see setup below)

# 5. run
uv run jobagent discover && uv run jobagent score prefilter
uv run jobagent dashboard      # http://localhost:8787
```

`config/profile.yaml`, `config/answers.yaml` and `config/*.pdf` are
**gitignored** — your personal data stays local; the repo ships only the
`.example` templates. The agent falls back to the `.example` files if you
haven't created your own yet, so a fresh clone runs out of the box.

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
