"""One process at a time talks to the hardware.

The daemon holds an exclusive ``flock`` for as long as it runs; one-off
hardware commands (``probe``, ``mb-test``) take the same lock, so they can't
interleave queries or fight the daemon over a fan header. The kernel drops
the lock when the process exits, crash included, so there's no stale lock.

The fans belong to the machine, not to one account, so the hardware lock is
also taken machine-wide, in /run/louver: a second account's copy (or the
login screen's session, had the service been enabled globally) gives up
instead of fighting the first one over the hub. The package makes that
directory (tmpfiles.d, root:plugdev 3770: setgid and sticky), so only
plugdev members, who can drive the fans and the hub anyway, can take or
hold the lock, and none of them can unlink the lock another's copy holds
to start a second one. Without it (the per-user install, or an account
outside plugdev) the per-user lock is all there is.
"""

from __future__ import annotations

import fcntl
import logging
import os
import stat
from pathlib import Path
from typing import IO, NamedTuple

log = logging.getLogger("louver")

SYSTEM_LOCK_DIR = Path("/run/louver")  # packaging/louver.tmpfiles; tests point this elsewhere


def lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path.home() / ".cache" / "louver"
    return base / "louver.lock"


class Lock:
    """The held lock file(s); keep a reference for as long as you need it."""

    def __init__(self, *files: IO):
        self._files = files

    def close(self) -> None:
        for f in self._files:
            f.close()


def _try_lock(path: Path) -> IO | None:
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


class Busy(NamedTuple):
    """Another copy holds the lock; its PID, if known. elsewhere: only the
    machine-wide lock is held, so it's likely another account's copy."""

    pid: int | None
    elsewhere: bool = False


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


def _open_shared(path: Path) -> IO | None:
    """The machine-wide lock file, opened for flock; None if it can't be.

    The file may belong to another account: open an existing one without
    O_CREAT (in a sticky group-writable directory, fs.protected_regular=2
    refuses O_CREAT on someone else's file even when it's writable), and
    create it only when it's missing, readable and writable by the group
    (plugdev, from the setgid directory) so the other members' copies can
    lock it too. Read-only is enough for flock if writing isn't allowed.
    O_NONBLOCK: a FIFO put there must not hang the open (it's then refused).
    """
    flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    for _ in range(2):  # a second try if another copy created it meanwhile
        for mode in (os.O_RDWR, os.O_RDONLY):
            try:
                fd = os.open(path, mode | flags)
            except FileNotFoundError:
                break
            except PermissionError:
                continue
            except OSError as e:  # a symlink (ELOOP), ...
                log.debug("machine-wide lock %s unusable: %s", path, e)
                return None
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                log.debug("machine-wide lock %s is not a regular file", path)
                return None
            return os.fdopen(fd, "r+" if mode == os.O_RDWR else "r")
        else:
            log.debug("machine-wide lock %s can't be opened (not in plugdev?)", path)
            return None
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | flags, 0o660)
        except FileExistsError:
            continue
        except OSError as e:  # no /run/louver (not the package), or not ours to write in
            log.debug("no machine-wide lock (%s): %s", path, e)
            return None
        os.fchmod(fd, 0o660)  # past the umask
        return os.fdopen(fd, "r+")
    return None


def _shared_pid(path: Path) -> int | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd) as f:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            return int(f.read(32).strip())
        except (OSError, ValueError):
            return None


def try_acquire_hardware(path: Path | None = None, system_dir: Path | None = None) -> Lock | Busy:
    """The per-user lock plus the machine-wide one, or who holds either. If
    there's no usable /run/louver, the per-user lock alone."""
    got = try_acquire(path)
    if isinstance(got, Busy):
        return got
    shared = (system_dir or SYSTEM_LOCK_DIR) / "hardware.lock"
    f = _open_shared(shared)
    if f is None:
        return got
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        got.close()
        return Busy(_shared_pid(shared), elsewhere=True)
    except OSError as e:
        f.close()
        log.debug("machine-wide lock %s: %s", shared, e)
        return got
    if f.writable():
        try:
            f.seek(0)
            f.truncate()
            f.write(f"{os.getpid()}\n")
            f.flush()
        except OSError as e:  # the pid is only for the "already running" message
            log.debug("machine-wide lock %s: can't write the pid: %s", shared, e)
    return Lock(*got._files, f)
