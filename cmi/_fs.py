"""File-system helpers that behave the same on POSIX and Windows.

POSIX: owner and mode checks (0600 files, 0700 directories) are enforced as before.
Windows: there is no uid and no POSIX mode; files under the user profile are private by the default NTFS
ACLs, so those checks are skipped. Binary file descriptors need O_BINARY there (otherwise os.write turns
"\\n" into "\\r\\n" and stored originals stop being byte-exact).
"""
from __future__ import annotations

import os
import time

IS_WINDOWS = os.name == "nt"
O_BINARY = getattr(os, "O_BINARY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:  # POSIX
    _msvcrt = None


def owned_by_me(info: os.stat_result) -> bool:
    return True if IS_WINDOWS else info.st_uid == os.getuid()


def chmod_private(path, mode: int) -> None:
    """chmod on POSIX; a no-op on Windows (permissions come from the NTFS ACLs of the profile)."""
    if not IS_WINDOWS:
        os.chmod(path, mode)


def mode_is(info: os.stat_result, expected: int) -> bool:
    return True if IS_WINDOWS else (info.st_mode & 0o777) == expected


def has_locking() -> bool:
    return _fcntl is not None or _msvcrt is not None


def try_lock(fd: int) -> None:
    """Exclusive non-blocking lock on an open file descriptor. Raises BlockingIOError on contention."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
    except OSError as exc:  # sharing violation / deadlock = somebody else holds it
        raise BlockingIOError(str(exc)) from exc


def unlock(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def lock_with_deadline(fd: int, seconds: float = 1.0) -> bool:
    if IS_WINDOWS:  # file syncs are slower and msvcrt locks are not queued: be more patient
        seconds = max(seconds, 10.0)
    deadline = time.monotonic() + seconds
    while True:
        try:
            try_lock(fd)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
