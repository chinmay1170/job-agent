"""Proof capture: screenshots + DOM snapshots into artifacts/{job_id}/."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page

from jobagent.db import ROOT

ARTIFACTS = ROOT / "artifacts"


def artifact_dir(job_id: int) -> Path:
    d = ARTIFACTS / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def snap(page: Page, job_id: int, name: str) -> str:
    path = artifact_dir(job_id) / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def save_dom(page: Page, job_id: int, name: str = "dom") -> str:
    path = artifact_dir(job_id) / f"{name}.html"
    path.write_text(page.content())
    return str(path)
