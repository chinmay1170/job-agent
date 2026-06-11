"""Contact harvesting.

Two sources:
  (a) emails embedded in HN job-post descriptions (jobs.source='hn');
  (b) careers@/jobs@/talent@{domain} guesses for companies we queued/applied
      to, kept only when the domain has MX records (dnspython).

Blocklisted companies never get contacts.
"""
from __future__ import annotations

import re
import sqlite3

from jobagent import config, db

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# things the regex catches that aren't mailboxes (image@2x.png etc.)
_BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ts", "md"}

GUESS_LOCALPARTS = ("careers", "jobs", "talent")


def _blocked(company_name: str | None, domain: str | None, blocklisted_flag: int | None) -> bool:
    if blocklisted_flag:
        return True
    bl = config.blocklist()
    if company_name:
        norm = db.normalize_company(company_name)
        for blocked in bl.get("companies") or []:
            if db.normalize_company(blocked) == norm:
                return True
    if domain:
        d = domain.lower().lstrip("www.")
        for blocked in bl.get("domains") or []:
            if d == blocked.lower():
                return True
    return False


def mx_valid(domain: str) -> bool:
    """True when the domain has at least one MX record."""
    import dns.resolver

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        return len(answers) > 0
    except Exception:
        return False


def _insert_contact(conn: sqlite3.Connection, company_id: int | None, email: str,
                    source: str, mx: int | None, role: str | None = None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO contacts (company_id, email, role, source, mx_valid) "
        "VALUES (?,?,?,?,?)",
        (company_id, email.lower(), role, source, mx),
    )
    return cur.rowcount > 0


def harvest_contacts(conn: sqlite3.Connection) -> int:
    """Harvest new contacts; returns the number of contacts inserted."""
    inserted = 0

    # (a) emails inside HN job-post descriptions (source may not exist yet).
    hn_rows = conn.execute(
        "SELECT j.id, j.company_id, j.description, c.name AS company_name, "
        "       c.domain, c.blocklisted "
        "FROM jobs j LEFT JOIN companies c ON j.company_id = c.id "
        "WHERE j.source = 'hn' AND j.description IS NOT NULL"
    ).fetchall()
    checked_domains: dict[str, bool] = {}
    for row in hn_rows:
        if _blocked(row["company_name"], row["domain"], row["blocklisted"]):
            continue
        for email in set(EMAIL_RE.findall(row["description"] or "")):
            email = email.lower().strip(".")
            domain = email.rsplit("@", 1)[-1]
            if domain.rsplit(".", 1)[-1] in _BAD_TLDS:
                continue
            if _blocked(None, domain, 0):
                continue
            if domain not in checked_domains:
                checked_domains[domain] = mx_valid(domain)
            if _insert_contact(conn, row["company_id"], email, "hn",
                               1 if checked_domains[domain] else 0):
                inserted += 1
                db.log_event(conn, "contact", None, "contact_harvested",
                             {"email": email, "source": "hn", "job_id": row["id"]})

    # (b) careers@/jobs@/talent@ guesses for companies with queued/applied jobs.
    companies = conn.execute(
        "SELECT DISTINCT c.id, c.name, c.domain, c.blocklisted "
        "FROM companies c JOIN jobs j ON j.company_id = c.id "
        "WHERE j.status IN ('apply_queued','applied') "
        "  AND c.domain IS NOT NULL AND c.domain != ''"
    ).fetchall()
    for comp in companies:
        if _blocked(comp["name"], comp["domain"], comp["blocklisted"]):
            continue
        domain = comp["domain"].lower().removeprefix("http://").removeprefix("https://")
        domain = domain.removeprefix("www.").split("/")[0]
        if not domain or "." not in domain:
            continue
        existing = conn.execute(
            "SELECT 1 FROM contacts WHERE company_id=? AND source='careers_guess' LIMIT 1",
            (comp["id"],),
        ).fetchone()
        if existing:
            continue
        if domain not in checked_domains:
            checked_domains[domain] = mx_valid(domain)
        if not checked_domains[domain]:
            continue
        for local in GUESS_LOCALPARTS:
            if _insert_contact(conn, comp["id"], f"{local}@{domain}",
                               "careers_guess", 1, role="recruiting"):
                inserted += 1
                db.log_event(conn, "contact", None, "contact_harvested",
                             {"email": f"{local}@{domain}", "source": "careers_guess",
                              "company_id": comp["id"]})

    conn.commit()
    return inserted
