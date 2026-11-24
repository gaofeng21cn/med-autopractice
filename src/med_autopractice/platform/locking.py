"""Cross-platform exclusive advisory/mandatory file locking."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import time


def _lock_posix(fd: int) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)


def _lock_windows(fd: int) -> None:
    import msvcrt

    # Ensure at least 1 byte exists for LockFileEx/msvcrt byte-range locking.
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\x00")
    os.lseek(fd, 0, os.SEEK_SET)
    for attempt in range(200):
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if attempt == 199:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            time.sleep(0.025)


def _unlock_windows(fd: int) -> None:
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Acquire an exclusive per-task file lock across POSIX and Windows hosts."""

    if os.name == "nt":
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    locked_windows = False
    try:
        if os.name == "nt":
            _lock_windows(fd)
            locked_windows = True
        else:
            _lock_posix(fd)
        yield
    finally:
        if locked_windows:
            _unlock_windows(fd)
        os.close(fd)
