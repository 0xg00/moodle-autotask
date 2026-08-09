"""Portable checks for filesystem indirection before security-sensitive writes."""

from __future__ import annotations

import os
from pathlib import Path


def assert_no_indirection(path: Path) -> None:
    """Reject symlinks and Windows junction/reparse points in existing ancestors."""
    current = path
    while current != current.parent:
        try:
            stat = current.lstat()
        except FileNotFoundError:
            current = current.parent
            continue
        if current.is_symlink() or _is_reparse_point(stat):
            raise ValueError("path contains a symlink or reparse point")
        current = current.parent


def _is_reparse_point(stat: os.stat_result) -> bool:
    attributes = getattr(stat, "st_file_attributes", 0)
    return bool(attributes & 0x400)
