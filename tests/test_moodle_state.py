from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Thread

import pytest

from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    MoodleStateError,
    NotificationAttachment,
    NotificationDraft,
)


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


def _draft(task: str = "a", revision: str = "b") -> NotificationDraft:
    return NotificationDraft(
        _key(task),
        _key(revision, "assignment"),
        "Course",
        "C",
        "Assignment",
        0,
        1,
        2,
        3,
        4,
        (),
    )


def test_outbox_classifies_claims_completes_and_suppresses(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    event = state.enqueue(_draft(), now=1)
    assert event is not None and event.status == "NEW"
    assert state.enqueue(_draft(), now=1) is None
    assert state.status(_key("a"), _key("b", "assignment")) == "NEW"
    claim = state.claim("test", 1, 6, now=1)[0]
    assert state.complete(claim, now=2)
    assert state.status(_key("a"), _key("b", "assignment")) is None
    update = state.enqueue(_draft("a", "c"), now=3)
    assert update is not None and update.status == "UPDATED"


def test_outbox_lease_recovery_wrong_token_and_backoff(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    state.enqueue(_draft(), now=1)
    claim = state.claim("one", 1, 6, now=1)[0]
    assert not state.complete(type(claim)(claim.event, "a" * 43), now=2)
    assert state.renew(claim, 6, now=2)
    assert state.fail(claim, 2, 10, now=3)
    assert not state.claim("two", 1, 6, now=4)
    recovered = state.claim("two", 1, 6, now=5)[0]
    assert recovered.lease_token != claim.lease_token


def test_v1_migration_preserves_acknowledgements_and_manual_ack_suppresses(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE acknowledgements (task_key TEXT NOT NULL, revision_digest TEXT NOT NULL, "
            "acknowledged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (task_key, revision_digest))"
        )
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '1')")
        connection.execute(
            "INSERT INTO acknowledgements(task_key, revision_digest) VALUES (?, ?)",
            (_key("a"), _key("b", "assignment")),
        )
    state = MoodleState(path)
    assert state.status(_key("a"), _key("b", "assignment")) is None
    state.enqueue(_draft("c", "d"), now=1)
    state.acknowledge(_key("c"), _key("d", "assignment"))
    assert not state.claim("test", 1, 6, now=2)


def test_concurrent_enqueue_claim_expiry_renew_and_capped_starvation(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "state.sqlite3"
    MoodleState(path)
    barrier = Barrier(3)
    results: list[object] = []

    def enqueue() -> None:
        barrier.wait()
        results.append(MoodleState(path).enqueue(_draft(), now=1))

    threads = [Thread(target=enqueue) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sum(item is not None for item in results) == 1
    state = MoodleState(path)
    first = state.claim("one", 1, 6, now=1)[0]
    assert not state.claim("two", 1, 6, now=2)
    assert state.renew(first, 6, now=6)
    assert not state.claim("two", 1, 6, now=7)
    recovered = state.claim("two", 1, 6, now=12)[0]
    assert recovered.lease_token != first.lease_token
    assert not state.renew(first, 6, now=12)
    assert not state.fail(first, 1, 2, now=12)
    assert not state.complete(first, now=12)
    state.enqueue(_draft("c", "d"), now=1)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE outbox SET attempts = 1000000 WHERE event_id = ?", (recovered.event.event_id,)
        )
    assert state.claim("three", 1, 6, now=13)[0].event.task_key == _key("c")


def test_corrupt_payload_and_v2_shape_are_rejected_before_claim(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    state = MoodleState(path)
    event = state.enqueue(_draft(), now=1)
    assert event is not None
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE outbox SET payload = '{bad' WHERE event_id = ?", (event.event_id,)
        )
    with pytest.raises(MoodleStateError):
        state.claim("owner", 1, 6, now=1)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX outbox_task_idx")
    with pytest.raises(MoodleStateError):
        MoodleState(path)


@pytest.mark.parametrize(
    "constraint",
    (
        "CHECK ((delivery_state = 'leased') = (lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))",
        "CHECK ((delivery_state = 'delivered') = (delivered_at IS NOT NULL))",
    ),
)
def test_v2_relational_outbox_checks_cannot_be_weakened(tmp_path: Path, constraint: str) -> None:
    path = tmp_path / "state.sqlite3"
    MoodleState(path)
    with sqlite3.connect(path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'outbox'"
        ).fetchone()[0]
        weakened = sql.replace(constraint, "CHECK (1)")
        assert weakened != sql
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'outbox'",
            (weakened,),
        )
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(MoodleStateError, match="schema is corrupt"):
        MoodleState(path)


def test_v2_rejects_unique_replacement_of_nonunique_outbox_index(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    MoodleState(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX outbox_task_idx")
        connection.execute("CREATE UNIQUE INDEX outbox_task_idx ON outbox(task_key)")
    with pytest.raises(MoodleStateError, match="schema is corrupt"):
        MoodleState(path)


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE INDEX unexpected_outbox_index ON outbox(created_at)",
        "CREATE VIEW unexpected_outbox_view AS SELECT event_id FROM outbox",
        "CREATE TRIGGER unexpected_outbox_trigger AFTER INSERT ON outbox BEGIN SELECT 1; END",
    ),
)
def test_v2_rejects_unexpected_schema_objects(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "state.sqlite3"
    MoodleState(path)
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    with pytest.raises(MoodleStateError, match="schema is corrupt"):
        MoodleState(path)


def test_v2_rejects_rewritten_table_semantics_outside_check_markers(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    MoodleState(path)
    with sqlite3.connect(path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'outbox'"
        ).fetchone()[0]
        rewritten = sql.replace("payload TEXT NOT NULL", "payload TEXT NOT NULL COLLATE NOCASE")
        assert rewritten != sql
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'outbox'",
            (rewritten,),
        )
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(MoodleStateError, match="schema is corrupt"):
        MoodleState(path)


@pytest.mark.parametrize(
    "replacement",
    ("'PENDING'", "'pend ing'"),
)
def test_v2_rejects_altered_delivery_state_literals(tmp_path: Path, replacement: str) -> None:
    path = tmp_path / "state.sqlite3"
    MoodleState(path)
    with sqlite3.connect(path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'outbox'"
        ).fetchone()[0]
        rewritten = sql.replace("'pending'", replacement, 1)
        assert rewritten != sql
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'outbox'",
            (rewritten,),
        )
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(MoodleStateError, match="schema is corrupt"):
        MoodleState(path)


@pytest.mark.parametrize(
    "case",
    (
        "invalid_json",
        "top_level_extra",
        "top_level_missing",
        "payload_event_id",
        "bool_date",
        "wrong_date_type",
        "attachment_extra",
        "attachment_missing",
        "attachment_wrong_type",
        "row_event_id",
        "row_task_key",
        "row_revision_digest",
    ),
)
def test_claim_fails_closed_for_payload_and_row_corruption(tmp_path: Path, case: str) -> None:
    path = tmp_path / f"{case}.sqlite3"
    state = MoodleState(path)
    event = state.enqueue(
        replace(
            _draft(),
            attachments=(NotificationAttachment("brief.txt", 1, "text/plain", False),),
        ),
        now=1,
    )
    assert event is not None
    payload = event.as_dict()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    column = "payload"
    value: object = encoded
    if case == "invalid_json":
        value = "{not-json"
    elif case == "top_level_extra":
        payload["unexpected"] = "value"
    elif case == "top_level_missing":
        del payload["course_name"]
    elif case == "payload_event_id":
        payload["event_id"] = "moodle-notification-event-v1:" + "0" * 64
    elif case == "bool_date":
        payload["due_date"] = True
    elif case == "wrong_date_type":
        payload["due_date"] = "1"
    elif case == "attachment_extra":
        payload["attachments"][0]["unexpected"] = "value"  # type: ignore[index]
    elif case == "attachment_missing":
        del payload["attachments"][0]["filename"]  # type: ignore[index]
    elif case == "attachment_wrong_type":
        payload["attachments"][0]["size_bytes"] = True  # type: ignore[index]
    elif case == "row_event_id":
        column, value = "event_id", "moodle-notification-event-v1:" + "0" * 64
    elif case == "row_task_key":
        column, value = "task_key", _key("c")
    elif case == "row_revision_digest":
        column, value = "revision_digest", _key("c", "assignment")
    if column == "payload" and case != "invalid_json":
        value = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE outbox SET {column} = ?", (value,))
    with pytest.raises(MoodleStateError, match="corrupt"):
        state.claim("owner", 1, 6, now=1)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT delivery_state, attempts FROM outbox").fetchone() == (
            "pending",
            0,
        )
