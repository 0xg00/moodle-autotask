from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from moddle_autotask.health import pulse

pytestmark = pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="requires a root POSIX ownership test invocation",
)


def _chown(path: Path, user: int, group: int) -> None:
    command = getattr(os, "chown", None)
    if command is None:
        pytest.skip("requires POSIX ownership semantics")
    command(path, user, group)


def test_pulse_rejects_unknown_service(tmp_path: Path) -> None:
    assert not pulse("unknown", root=tmp_path)


def test_pulse_fails_closed_when_the_root_is_missing(tmp_path: Path) -> None:
    assert not pulse("scheduler", root=tmp_path / "missing")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership semantics")
def test_pulse_updates_only_the_mtime_of_a_precreated_safe_file(tmp_path: Path) -> None:
    root = tmp_path / "health"
    root.mkdir(mode=0o711)
    root.chmod(0o711)
    pulse_file = root / "scheduler"
    pulse_file.touch()
    pulse_file.chmod(0o620)
    old_mtime = time.time_ns() - 5_000_000_000
    os.utime(pulse_file, ns=(old_mtime, old_mtime))

    before = pulse_file.stat()
    assert pulse("scheduler", root=root)
    after = pulse_file.stat()

    assert stat.S_IMODE(after.st_mode) == 0o620
    assert after.st_ino == before.st_ino
    assert after.st_size == before.st_size == 0
    assert after.st_mtime_ns > before.st_mtime_ns


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership semantics")
@pytest.mark.parametrize("root_mode", (0o722, 0o777))
def test_pulse_rejects_unsafe_root_permissions(tmp_path: Path, root_mode: int) -> None:
    root = tmp_path / "health"
    root.mkdir(mode=root_mode)
    root.chmod(root_mode)
    assert not pulse("scheduler", root=root)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership semantics")
def test_pulse_rejects_a_root_owned_by_a_service_account(tmp_path: Path) -> None:
    root = tmp_path / "health"
    root.mkdir(mode=0o711)
    root.chmod(0o711)
    _chown(root, 1, -1)
    assert not pulse("scheduler", root=root)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership semantics")
def test_pulse_rejects_a_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "health"
    root.symlink_to(target, target_is_directory=True)
    assert not pulse("scheduler", root=root)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership semantics")
@pytest.mark.parametrize(
    "mutation",
    ("directory", "symlink", "wrong-owner", "wrong-mode", "nonempty"),
)
def test_pulse_rejects_unsafe_precreated_file(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / "health"
    root.mkdir(mode=0o711)
    root.chmod(0o711)
    pulse_file = root / "scheduler"
    if mutation == "directory":
        pulse_file.mkdir()
    elif mutation == "symlink":
        target = tmp_path / "target"
        target.touch()
        pulse_file.symlink_to(target)
    else:
        pulse_file.touch()
        pulse_file.chmod(0o620)
        if mutation == "wrong-owner":
            _chown(pulse_file, 1, -1)
        elif mutation == "wrong-mode":
            pulse_file.chmod(0o600)
        else:
            pulse_file.write_text("unexpected", encoding="utf-8")

    assert not pulse("scheduler", root=root)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX ownership semantics")
def test_pulse_rejects_an_inode_swap_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "health"
    root.mkdir(mode=0o711)
    root.chmod(0o711)
    pulse_file = root / "scheduler"
    pulse_file.touch()
    pulse_file.chmod(0o620)
    replacement = root / "replacement"
    replacement.touch()
    replacement.chmod(0o620)
    original_open = os.open

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777
    ) -> int:
        raw_path = os.fspath(path)
        if isinstance(raw_path, bytes):
            return original_open(path, flags, mode)
        candidate = Path(raw_path)
        if candidate == pulse_file:
            os.replace(replacement, candidate)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", swap_before_open)
    assert not pulse("scheduler", root=root)
