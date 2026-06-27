"""Config loading with simple caching."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    """Load config/<name>.yaml, falling back to the shipped config/<name>.example.yaml
    when the user hasn't created their own copy yet (so a fresh clone runs out of the box)."""
    p = CONFIG_DIR / f"{name}.yaml"
    if not p.exists():
        example = CONFIG_DIR / f"{name}.example.yaml"
        if example.exists():
            p = example
    return yaml.safe_load(p.read_text())


def profile() -> dict:
    return load("profile")


def identity() -> dict:
    """Personal facts that drive prompts, filters and outreach — the single
    source any deployment edits. Lives under profile.yaml `identity`."""
    return (profile() or {}).get("identity", {}) or {}


def answers() -> dict:
    return load("answers")


def search() -> dict:
    return load("search")


def caps() -> dict:
    return load("caps")


def blocklist() -> dict:
    return load("blocklist")


def resume_pdf(tailored_path: str | None) -> str:
    """The resume to upload/attach, honoring caps.resume_mode.

    'original' (default) returns the user's own hand-made PDF — tailored,
    AI-generated PDFs trip ATS/recruiter AI-resume filters.
    """
    c = caps()
    if c.get("resume_mode", "original") == "original":
        path = CONFIG_DIR.parent / c.get(
            "original_resume_path", "config/resume.pdf"
        )
        if path.exists():
            return str(path)
    return tailored_path or ""
