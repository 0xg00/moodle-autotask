"""No-follow filesystem storage admission for controller and agent spools.

The filesystem, rather than a reservation ledger, is authoritative.  Callers
must keep their publication lock while admitting and creating a new entry.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

_GIB = 1024 * 1024 * 1024
_ROOT_HEADROOM_BYTES = 12 * _GIB
_ROOT_HEADROOM_NODES = 100_000


class StorageCapacityError(RuntimeError):
    """A transient capacity or root-headroom admission refusal."""


class StorageEnvelopeError(RuntimeError):
    """A permanent untrusted-input or filesystem-envelope violation."""


@dataclass(frozen=True, slots=True)
class StorageLimit:
    max_allocated_bytes: int
    max_nodes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_allocated_bytes, int)
            or isinstance(self.max_allocated_bytes, bool)
            or self.max_allocated_bytes < 0
            or not isinstance(self.max_nodes, int)
            or isinstance(self.max_nodes, bool)
            or self.max_nodes < 0
        ):
            raise ValueError("storage limit is invalid")


@dataclass(frozen=True, slots=True)
class StorageDemand:
    allocated_bytes: int
    nodes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allocated_bytes, int)
            or isinstance(self.allocated_bytes, bool)
            or self.allocated_bytes < 0
            or not isinstance(self.nodes, int)
            or isinstance(self.nodes, bool)
            or self.nodes < 0
        ):
            raise ValueError("storage demand is invalid")

    def __add__(self, other: StorageDemand) -> StorageDemand:
        if not isinstance(other, StorageDemand):
            return NotImplemented
        return StorageDemand(self.allocated_bytes + other.allocated_bytes, self.nodes + other.nodes)


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    """Fixed controller/agent storage envelopes.

    ``workspace_admission`` is deliberately below the hard workspace maximum,
    leaving room for Codex's write amplification after materialization.
    """

    jobs: StorageLimit = StorageLimit(16 * _GIB, 16_384)
    workspace_hard: StorageLimit = StorageLimit(16 * _GIB, 100_000)
    workspace_admission: StorageLimit = StorageLimit(14 * _GIB, 80_000)
    results: StorageLimit = StorageLimit(2 * _GIB, 16_384)
    bundles: StorageLimit = StorageLimit(512 * 1024 * 1024, 512)


def measure_tree_no_follow(
    root: Path,
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    expected_file_mode: int | None = None,
    expected_directory_mode: int | None = None,
    exclude: frozenset[str] = frozenset(),
) -> StorageDemand:
    """Measure allocated blocks and nodes without following any pathname.

    Every entry is opened and compared to its lstat identity.  Only regular
    files and directories are accepted; hard links and special nodes fail
    closed.  ``exclude`` applies only to direct root entries.
    """
    if not isinstance(root, Path) or not root.is_absolute() or any(
        not isinstance(item, str) or not item or "/" in item or "\\" in item
        for item in exclude
    ):
        raise StorageEnvelopeError("storage root is invalid")
    if os.name == "nt":
        return _measure_tree_windows(
            root,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=expected_file_mode,
            expected_directory_mode=expected_directory_mode,
            exclude=exclude,
        )
    try:
        before = root.lstat()
        _validate_node(
            before,
            directory=True,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=expected_file_mode,
            expected_directory_mode=expected_directory_mode,
        )
        descriptor = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise StorageEnvelopeError("storage root is unsafe") from error
    try:
        opened = os.fstat(descriptor)
        _same_identity(before, opened)
        demand = _measure_directory(
            descriptor,
            before,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=expected_file_mode,
            expected_directory_mode=expected_directory_mode,
            root_exclude=exclude,
        )
        after = root.lstat()
        _same_identity(before, after)
        return demand
    except OSError as error:
        raise StorageEnvelopeError("storage tree is unsafe") from error
    finally:
        os.close(descriptor)


def admit_owner_write(
    root: Path,
    demand: StorageDemand,
    limit: StorageLimit,
    *,
    exclude: frozenset[str] = frozenset(),
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    expected_file_mode: int | None = None,
    expected_directory_mode: int | None = None,
    root_headroom: bool = True,
) -> StorageDemand:
    """Refuse a write that would exceed its tree or filesystem headroom."""
    if not isinstance(demand, StorageDemand) or not isinstance(limit, StorageLimit):
        raise ValueError("storage admission arguments are invalid")
    current = measure_tree_no_follow(
        root,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        expected_file_mode=expected_file_mode,
        expected_directory_mode=expected_directory_mode,
        exclude=exclude,
    )
    projected = current + demand
    if (
        projected.allocated_bytes > limit.max_allocated_bytes
        or projected.nodes > limit.max_nodes
    ):
        raise StorageCapacityError("storage limit would be exceeded")
    if root_headroom and os.name != "nt":
        try:
            before = root.lstat()
            descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            _same_identity(before, opened)
            filesystem = _filesystem_stats(descriptor)
            _same_identity(before, root.lstat())
        except OSError as error:
            raise StorageCapacityError("storage root capacity is unavailable") from error
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        fragment = filesystem.f_frsize or filesystem.f_bsize
        available_bytes = filesystem.f_bavail * fragment
        available_nodes = getattr(filesystem, "f_favail", -1)
        if available_bytes < demand.allocated_bytes + _ROOT_HEADROOM_BYTES or (
            available_nodes >= 0 and available_nodes < demand.nodes + _ROOT_HEADROOM_NODES
        ):
            raise StorageCapacityError("storage root headroom is exhausted")
    return projected


def storage_demand_for_files(root: Path, sizes: tuple[int, ...], nodes: int) -> StorageDemand:
    """Conservatively project allocated bytes for separately-created files."""
    if (
        not isinstance(root, Path)
        or any(not isinstance(size, int) or isinstance(size, bool) or size < 0 for size in sizes)
        or not isinstance(nodes, int)
        or isinstance(nodes, bool)
        or nodes < 0
    ):
        raise ValueError("storage write demand is invalid")
    unit = 1
    if os.name != "nt":
        try:
            before = root.lstat()
            descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            _same_identity(before, os.fstat(descriptor))
            filesystem = _filesystem_stats(descriptor)
            _same_identity(before, root.lstat())
            unit = filesystem.f_frsize or filesystem.f_bsize
        except OSError as error:
            raise StorageCapacityError("storage root capacity is unavailable") from error
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
    allocated = sum(((size + unit - 1) // unit) * unit for size in sizes)
    return StorageDemand(allocated, nodes)


@contextlib.contextmanager
def storage_admission_lock(root: Path) -> Iterator[None]:
    """Use the root directory inode as a POSIX-only admission lock.

    This adds no mutable lock pathname to an untrusted spool tree.  Windows
    test runs retain deterministic single-process behaviour without flock.
    """
    if os.name == "nt":
        try:
            metadata = root.lstat()
            _validate_node(
                metadata,
                directory=True,
                expected_uid=None,
                expected_gid=None,
                expected_file_mode=None,
                expected_directory_mode=None,
            )
        except OSError as error:
            raise StorageEnvelopeError("storage lock root is unsafe") from error
        yield
        return
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise StorageEnvelopeError("storage lock root is unsafe") from error
    try:
        if os.name != "nt":
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name != "nt":
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _measure_directory(
    descriptor: int,
    metadata: os.stat_result,
    *,
    expected_uid: int | None,
    expected_gid: int | None,
    expected_file_mode: int | None,
    expected_directory_mode: int | None,
    root_exclude: frozenset[str] = frozenset(),
) -> StorageDemand:
    total = StorageDemand(_allocated_bytes(metadata), 1)
    for name in os.listdir(descriptor):
        if name in root_exclude:
            continue
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise StorageEnvelopeError("storage tree changed while measuring") from error
        mode = before.st_mode
        if stat.S_ISDIR(mode):
            _validate_node(
                before,
                directory=True,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                expected_file_mode=expected_file_mode,
                expected_directory_mode=expected_directory_mode,
            )
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(child)
                _same_identity(before, opened)
                total += _measure_directory(
                    child,
                    before,
                    expected_uid=expected_uid,
                    expected_gid=expected_gid,
                    expected_file_mode=expected_file_mode,
                    expected_directory_mode=expected_directory_mode,
                )
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                _same_identity(before, after)
            finally:
                os.close(child)
            continue
        _validate_node(
            before,
            directory=False,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=expected_file_mode,
            expected_directory_mode=expected_directory_mode,
        )
        child = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
        try:
            opened = os.fstat(child)
            _same_identity(before, opened)
            after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            _same_identity(before, after)
        finally:
            os.close(child)
        total += StorageDemand(_allocated_bytes(before), 1)
    return total


def _validate_node(
    metadata: os.stat_result,
    *,
    directory: bool,
    expected_uid: int | None,
    expected_gid: int | None,
    expected_file_mode: int | None,
    expected_directory_mode: int | None,
) -> None:
    valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (
        _is_indirection(metadata)
        or not valid_type
        or (not directory and metadata.st_nlink != 1)
        or (expected_uid is not None and metadata.st_uid != expected_uid)
        or (expected_gid is not None and metadata.st_gid != expected_gid)
        or (
            expected_file_mode is not None
            and not directory
            and stat.S_IMODE(metadata.st_mode) != expected_file_mode
        )
        or (
            expected_directory_mode is not None
            and directory
            and stat.S_IMODE(metadata.st_mode) != expected_directory_mode
        )
    ):
        raise StorageEnvelopeError("storage tree node is unsafe")


def _same_identity(before: os.stat_result, after: os.stat_result) -> None:
    if (before.st_dev, before.st_ino, before.st_mode, before.st_nlink) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
    ):
        raise StorageEnvelopeError("storage tree changed while measuring")


def _allocated_bytes(metadata: os.stat_result) -> int:
    blocks = getattr(metadata, "st_blocks", None)
    if isinstance(blocks, int) and blocks >= 0:
        return blocks * 512
    return metadata.st_size


class _FilesystemStats(Protocol):
    f_frsize: int
    f_bsize: int
    f_bavail: int
    f_favail: int


def _filesystem_stats(descriptor: int) -> _FilesystemStats:
    """Call the POSIX-only descriptor API without exposing it to Windows typing."""
    return cast(_FilesystemStats, getattr(os, "fstat" + "vfs")(descriptor))


def _is_indirection(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _measure_tree_windows(
    root: Path,
    *,
    expected_uid: int | None,
    expected_gid: int | None,
    expected_file_mode: int | None,
    expected_directory_mode: int | None,
    exclude: frozenset[str],
) -> StorageDemand:
    """Compatibility fallback; POSIX uses descriptor-relative traversal above."""
    try:
        before = root.lstat()
        _validate_node(
            before,
            directory=True,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=expected_file_mode,
            expected_directory_mode=expected_directory_mode,
        )
        total = StorageDemand(_allocated_bytes(before), 1)
        for entry in root.iterdir():
            if entry.name in exclude:
                continue
            total += _measure_tree_windows_entry(
                entry,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                expected_file_mode=expected_file_mode,
                expected_directory_mode=expected_directory_mode,
            )
        _same_identity(before, root.lstat())
        return total
    except OSError as error:
        raise StorageEnvelopeError("storage tree is unsafe") from error


def _measure_tree_windows_entry(
    entry: Path,
    *,
    expected_uid: int | None,
    expected_gid: int | None,
    expected_file_mode: int | None,
    expected_directory_mode: int | None,
) -> StorageDemand:
    before = entry.lstat()
    if stat.S_ISDIR(before.st_mode):
        _validate_node(
            before,
            directory=True,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=expected_file_mode,
            expected_directory_mode=expected_directory_mode,
        )
        total = StorageDemand(_allocated_bytes(before), 1)
        for child in entry.iterdir():
            total += _measure_tree_windows_entry(
                child,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                expected_file_mode=expected_file_mode,
                expected_directory_mode=expected_directory_mode,
            )
    else:
        _validate_node(
            before,
            directory=False,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=expected_file_mode,
            expected_directory_mode=expected_directory_mode,
        )
        total = StorageDemand(_allocated_bytes(before), 1)
    _same_identity(before, entry.lstat())
    return total
