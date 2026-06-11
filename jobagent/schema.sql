PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL UNIQUE,
  domain TEXT,
  ats_type TEXT,
  ats_slug TEXT,
  region TEXT,
  sponsor_us INTEGER DEFAULT 0,
  sponsor_nl INTEGER DEFAULT 0,
  sponsor_uk INTEGER DEFAULT 0,
  sponsor_au INTEGER DEFAULT 0,
  sponsor_evidence TEXT,
  blocklisted INTEGER DEFAULT 0,
  cooldown_until TEXT,
  market_cap TEXT,          -- e.g. "$48B" or "Private"
  employee_count TEXT,      -- e.g. "~4,000" or "10,000+"
  hq TEXT,                  -- headquarters city, country
  enriched_at TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  source TEXT NOT NULL,
  source_id TEXT,
  dedupe_key TEXT NOT NULL UNIQUE,
  title TEXT,
  location TEXT,
  remote INTEGER DEFAULT 0,
  url TEXT,
  apply_url TEXT,
  description TEXT,
  posted_at TEXT,
  sponsorship_signal TEXT,
  status TEXT DEFAULT 'discovered',
  -- discovered | prefiltered_out | scored | apply_queued | applied
  -- | needs_review | skipped | failed
  score INTEGER,
  score_reasons TEXT,
  discovered_at TEXT DEFAULT (datetime('now')),
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY,
  job_id INTEGER UNIQUE REFERENCES jobs(id),
  method TEXT,
  resume_path TEXT,
  cover_path TEXT,
  answers_json TEXT,
  status TEXT DEFAULT 'pending',
  -- pending | submitted | confirmed | needs_review | failed
  -- | rejected | interview | no_response
  submitted_at TEXT,
  proof_screenshot TEXT,
  proof_dom TEXT,
  confirmation_text TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  email TEXT NOT NULL UNIQUE,
  name TEXT,
  role TEXT,
  source TEXT,
  mx_valid INTEGER
);

CREATE TABLE IF NOT EXISTS outreach (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER REFERENCES contacts(id),
  job_id INTEGER,
  kind TEXT DEFAULT 'first_touch',
  subject TEXT,
  body TEXT,
  status TEXT DEFAULT 'drafted',
  -- drafted | sent | bounced | replied | stopped
  gmail_message_id TEXT,
  gmail_thread_id TEXT,
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  reason TEXT,
  -- captcha | workday | unmapped_required_field | low_confidence
  -- | login_wall | multi_step | submit_error
  state_json TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  resolved_at TEXT,
  resolution TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT DEFAULT (datetime('now')),
  entity_type TEXT,
  entity_id INTEGER,
  event TEXT,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS daily_counters (
  date TEXT,
  kind TEXT,
  count INTEGER DEFAULT 0,
  PRIMARY KEY (date, kind)
);

CREATE TABLE IF NOT EXISTS inbox_threads (
  id INTEGER PRIMARY KEY,
  gmail_thread_id TEXT UNIQUE,
  application_id INTEGER REFERENCES applications(id),
  outreach_id INTEGER REFERENCES outreach(id),
  classification TEXT,
  -- interview_request | rejection | info_request | auto_ack | other
  from_email TEXT,
  subject TEXT,
  snippet TEXT,
  last_message_at TEXT,
  classified_at TEXT
);
