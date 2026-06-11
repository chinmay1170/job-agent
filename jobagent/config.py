"""Config loading with simple caching."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / f"{name}.yaml").read_text())


def profile() -> dict:
    return load("profile")


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
            "original_resume_path", "config/Chinmay_Krishna_Resume.pdf"
        )
        if path.exists():
            return str(path)
    return tailored_path or ""
