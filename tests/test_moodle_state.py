from __future__ import annotations

from pathlib import Path
from threading import Thread

import pytest

from moddle_autotask.adapters.moodle.state import MoodleState, MoodleStateError


def _key(letter: str, kind: str = "task") -> str:
    return f"moodle-{kind}-v1:{letter * 64}"


def test_state_has_at_least_once_exact_acknowledgements(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    task = "moodle-task-v1:" + "a" * 64
    first = "moodle-assignment-v1:" + "b" * 64
    second = "moodle-assignment-v1:" + "c" * 64
    assert state.status(task, first) == "NEW"
    state.acknowledge(task, first)
    assert state.status(task, first) is None
    assert state.status(task, second) == "UPDATED"


def test_duplicate_acknowledgement_is_idempotent(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    assert state.acknowledge(_key("a"), _key("b", "assignment")) == state.acknowledge(
        _key("a"), _key("b", "assignment")
    )


def test_invalid_task_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(MoodleStateError):
        MoodleState(tmp_path / "s.sqlite3").acknowledge("bad", _key("b", "assignment"))


def test_invalid_revision_rejected(tmp_path: Path) -> None:
    with pytest.raises(MoodleStateError):
        MoodleState(tmp_path / "s.sqlite3").status(_key("a"), "bad")


def test_swapped_task_and_revision_namespaces_are_rejected(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "s.sqlite3")
    with pytest.raises(MoodleStateError):
        state.status(_key("a", "assignment"), _key("b"))
    with pytest.raises(MoodleStateError):
        state.status(_key("a"), _key("b", "task"))


def test_posix_state_mode_is_private(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    if __import__("os").name != "nt":
        assert state.path.stat().st_mode & 0o077 == 0


def test_symlink_state_rejected_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "link.sqlite3"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(MoodleStateError):
        MoodleState(link)


def test_concurrent_identical_acknowledgements_converge(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    task, revision = _key("a"), _key("b", "assignment")
    MoodleState(path)
    errors: list[Exception] = []

    def acknowledge() -> None:
        try:
            MoodleState(path).acknowledge(task, revision)
        except Exception as error:
            errors.append(error)

    threads = [Thread(target=acknowledge) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert MoodleState(path).status(task, revision) is None


def test_wrong_existing_schema_is_rejected(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
    with pytest.raises(MoodleStateError, match="schema"):
        MoodleState(path)
