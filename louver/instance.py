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
from typing import IO, NamedTuple


def lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path.home() / ".cache" / "louver"
    return base / "louver.lock"


class Lock:
    """The held lock file; keep a reference for as long as you need it."""

    def __init__(self, f: IO):
        self._f = f

    def close(self) -> None:
        self._f.close()


def _try_lock(path: Path) -> IO | None:
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


class Busy(NamedTuple):
    """Another copy holds the lock; its PID, if known."""

    pid: int | None


def _written_pid(path: Path) -> int | None:
    # Read without locking: probing with flock would briefly hold the real
    # lock and could make a daemon starting at that moment give up.
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def try_acquire(path: Path | None = None) -> Lock | Busy:
    """Take the lock, or report which copy holds it."""
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = _try_lock(path)
    if f is None:
        return Busy(_written_pid(path))
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return Lock(f)


def acquire(path: Path | None = None) -> Lock | None:
    """The held lock, or None if another copy holds it."""
    got = try_acquire(path)
    return got if isinstance(got, Lock) else None
