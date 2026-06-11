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
