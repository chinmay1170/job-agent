"""Route an apply URL to a strategy: known-ATS form, generic form, or queue."""
from __future__ import annotations

from urllib.parse import urlparse


def route(apply_url: str) -> str:
    """Returns: greenhouse | lever | ashby | workable | smartrecruiters
    | generic | queue_workday | queue_unknown"""
    if not apply_url:
        return "queue_unknown"
    host = urlparse(apply_url).netloc.lower()
    if "myworkdayjobs.com" in host or "workday" in host:
        return "queue_workday"
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
    if strategy == "lever" and not apply_url.rstrip("/").endswith("/apply"):
        return apply_url.rstrip("/") + "/apply"
    if strategy == "ashby" and "application" not in apply_url:
        return apply_url.rstrip("/") + "/application"
    return apply_url
