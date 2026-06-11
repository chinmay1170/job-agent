"""Shared helpers for sponsor-register ingesters: cached downloads under data/sponsors/."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from jobagent.db import DATA_DIR

SPONSORS_DIR = DATA_DIR / "sponsors"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en"}

MAX_AGE_DAYS = 7


def sponsors_dir() -> Path:
    SPONSORS_DIR.mkdir(parents=True, exist_ok=True)
    return SPONSORS_DIR


def is_fresh(path: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """True if `path` exists, is non-empty and younger than `max_age_days`."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    return st.st_size > 0 and (time.time() - st.st_mtime) < max_age_days * 86400


def fetch_text(url: str, timeout: float = 60.0) -> str:
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def download(url: str, dest: Path, timeout: float = 120.0) -> Path:
    """Stream `url` to `dest` (atomic via .part temp file)."""
    sponsors_dir()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=1 << 16):
                    f.write(chunk)
    tmp.replace(dest)
    return dest
