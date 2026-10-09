"""One worker at a time.

Jeff, 2026-10-09: two `work --loop` processes ran side by side on 10/8 and
both sent the same outbox rows, so 11 numbers got the same text twice. A
second worker must refuse to start, not race the first.

The lock is an OS file lock, so it is released the moment the holding process
dies (crash, kill, reboot). There is no stale lock file to clean up by hand.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import config

LOCK_PATH = config.DB_PATH.parent / "worker.lock"

_handle = None  # kept open for the life of the process; closing it releases the lock


def acquire(path: Optional[Path] = None) -> bool:
    """True if this process now holds the worker lock, False if another does."""
    global _handle
    if _handle is not None:
        return True
    path = Path(path or LOCK_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _handle = fh
    # Who holds it, for a human reading the folder. Separate file because the
    # locked byte cannot be read by another process on Windows.
    try:
        path.with_suffix(".pid").write_text(str(os.getpid()))
    except OSError:
        pass
    return True


def holder_pid(path: Optional[Path] = None) -> str:
    try:
        return Path(path or LOCK_PATH).with_suffix(".pid").read_text().strip()
    except OSError:
        return "unknown"


def release() -> None:
    global _handle
    if _handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            _handle.seek(0)
            msvcrt.locking(_handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    _handle.close()
    _handle = None
