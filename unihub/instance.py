"""One process at a time talks to the hardware.

The daemon holds an exclusive ``flock`` for as long as it runs; one-off
hardware commands (``probe``, ``mb-test``) take the same lock, so they can't
interleave queries or fight the daemon over a fan header. The kernel drops
the lock when the process exits, crash included, so there's no stale lock.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


def lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path.home() / ".cache" / "unihub"
    return base / "unihub.lock"


def acquire(path: Path | None = None) -> IO | None:
    """Return the held lock (keep a reference to it), or None if another
    UniHub process already holds it."""
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


def holder(path: Path | None = None) -> int | None:
    """PID written by the current lock holder, for messages only."""
    try:
        return int((path or lock_path()).read_text().strip())
    except (OSError, ValueError):
        return None
