"""Kill switch: `touch data/KILL` (or `jobagent stop`) halts all side effects."""
from __future__ import annotations

from jobagent.db import DATA_DIR

KILL_FILE = DATA_DIR / "KILL"


class KilledError(RuntimeError):
    pass


def is_killed() -> bool:
    return KILL_FILE.exists()


def check() -> None:
    """Raise if the kill switch is engaged. Call before every side effect."""
    if is_killed():
        raise KilledError(f"Kill switch engaged ({KILL_FILE}). Run `jobagent resume` to clear.")


def engage() -> None:
    KILL_FILE.parent.mkdir(exist_ok=True)
    KILL_FILE.touch()


def release() -> None:
    KILL_FILE.unlink(missing_ok=True)
