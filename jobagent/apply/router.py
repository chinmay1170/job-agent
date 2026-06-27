"""Route an apply URL to a strategy: known-ATS form, generic form, or queue."""
from __future__ import annotations

import re
from urllib.parse import urlparse

_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")


def route(apply_url: str) -> str:
    """Returns: greenhouse | lever | ashby | workable | smartrecruiters
    | generic | queue_workday | queue_unknown"""
    if not apply_url:
        return "queue_unknown"
    host = urlparse(apply_url).netloc.lower()
    if "myworkdayjobs.com" in host or "workday" in host:
        return "queue_workday"
    # Company career sites (brex.com, block.xyz, stripe.com/jobs ...) that carry
    # a gh_jid ARE Greenhouse jobs — the heavy marketing page just wraps the
    # Greenhouse form (and often times out on load). Treat them as greenhouse so
    # to_form_url() sends us straight to the lightweight token embed.
    if _GH_JID.search(apply_url):
        return "greenhouse"
    # Login-walled boards: no open form to drive — these are outreach
    # targets, not browser-appliable.
    if "indeed." in host or "glassdoor." in host or "linkedin." in host:
        return "queue_login_board"
    # HN Who's Hiring comments: apply-by-email listings, no form to drive.
    if "ycombinator.com" in host:
        return "queue_email_listing"
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "workable.com" in host:
        return "workable"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    return "generic"


# Per-ATS tweaks for getting from the posting URL to a live application form.
def to_form_url(strategy: str, apply_url: str) -> str:
    # gh_jid present (on ANY host) -> go straight to Greenhouse's universal token
    # embed. This skips heavy career-site marketing pages that time out, and
    # lands directly on the real, lightweight application form (~2s vs 45s).
    m = _GH_JID.search(apply_url or "")
    if m:
        return f"https://boards.greenhouse.io/embed/job_app?token={m.group(1)}"
    if strategy == "lever" and not apply_url.rstrip("/").endswith("/apply"):
        return apply_url.rstrip("/") + "/apply"
    if strategy == "ashby" and "application" not in apply_url:
        return apply_url.rstrip("/") + "/application"
    # SmartRecruiters: do NOT deep-link /new-application — hit cold (no
    # referrer/session from the listing) it serves a "temporarily
    # unavailable" error page. Land on the listing; the runner's
    # "I'm interested" click-through opens the real form.
    return apply_url
