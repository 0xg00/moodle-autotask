"""Fail-closed heartbeat updates for the controller health publisher."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_ROOT = Path("/run/moodle-autotask-health")
_SERVICES = frozenset(("scheduler", "telegram", "worker", "agent"))


def pulse(service: str, *, root: Path = _ROOT) -> bool:
    """Update only a pre-created, empty regular heartbeat file.

    The service process cannot create the directory or replace the file: both are
    owned by root and the directory is not writable by service users.  A failed
    pulse is deliberately quiet; the root publisher will report the stale file.
    """
    if service not in _SERVICES:
        return False
    path = root / service
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        parent = root.lstat()
        before = path.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != 0
            or stat.S_IMODE(parent.st_mode) & 0o022
            or not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) != 0o620
            or before.st_size != 0
        ):
            return False
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != 0
                or stat.S_IMODE(opened.st_mode) != 0o620
                or opened.st_size != 0
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                return False
            os.utime(descriptor, None)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True
