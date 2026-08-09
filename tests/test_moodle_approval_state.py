from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from moddle_autotask.adapters.moodle.approval_state import (
    _CALLBACKS_SQL,
    _CURSOR_SQL,
    _METADATA_SQL,
    _REQUESTS_SQL,
    _WORK_CLAIMABLE_INDEX_SQL,
    _WORK_SQL_V2,
    ApprovalState,
    ApprovalStateError,
)
from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationAttachment,
    NotificationDraft,
    NotificationEvent,
)
from moddle_autotask.domain.models import Digest, LabHandle


def _event(tmp_path: Path, revision: str = "b", *, lab: bool = True) -> NotificationEvent:
    draft = NotificationDraft(
        "moodle-task-v1:" + "a" * 64,
        "moodle-assignment-v1:" + revision * 64,
        "Administración de sistemas",
        "ASIX-M06",
        "Despliegue de un servicio",
        0,
        100,
        0,
        0,
        1,
        (NotificationAttachment("lab.ova", 123, "application/octet-stream", True),) if lab else (),
    )
    event = MoodleState(tmp_path / f"moodle-{revision}.sqlite3").enqueue(draft, now=1)
    assert event is not None
    return event


def test_prepare_is_stable_and_decision_is_bound_to_exact_revision(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    first = _event(tmp_path, "b")
    second = _event(tmp_path, "c")
    buttons = state.prepare(first, now=1)
    assert state.prepare(first, now=2) == buttons
    outcome = state.resolve(buttons.approve, 42, 42, now=3)
    assert outcome.action == "approve" and outcome.result == "approved"
    assert state.decision(first.task_key, first.revision_digest) == "approved"
    assert state.decision(second.task_key, second.revision_digest) is None
    second_buttons = state.prepare(second, now=4)
    assert second_buttons != buttons
    assert state.decision(second.task_key, second.revision_digest) == "pending"


def test_decisions_are_idempotent_and_opposite_action_conflicts(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path, lab=False)
    buttons = state.prepare(event, now=1)
    assert state.resolve(buttons.ignore, 42, 42, now=2).result == "ignored"
    assert state.resolve(buttons.ignore, 42, 42, now=3).result == "already_ignored"
    assert state.resolve(buttons.approve, 42, 42, now=4).result == "conflict"
    assert state.resolve(buttons.details, 42, 42, now=5).result == "details"
    assert state.decision(event.task_key, event.revision_digest) == "ignored"


def test_competing_decisions_have_one_transactional_winner(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path, lab=False)
    buttons = state.prepare(event, now=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(state.resolve, buttons.approve, 42, 42, 2),
            pool.submit(state.resolve, buttons.ignore, 42, 42, 2),
        )
        results = {future.result().result for future in futures}
    assert results in ({"approved", "conflict"}, {"ignored", "conflict"})
    assert state.decision(event.task_key, event.revision_digest) in {"approved", "ignored"}


def test_unauthorized_unknown_and_malformed_callbacks_do_not_decide(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path)
    buttons = state.prepare(event, now=1)
    with pytest.raises(ApprovalStateError, match="unauthorized"):
        state.resolve(buttons.approve, 41, 42, now=2)
    with pytest.raises(ApprovalStateError, match="invalid"):
        state.resolve("../bad", 42, 42, now=2)
    with pytest.raises(ApprovalStateError, match="unknown"):
        state.resolve("x" * 32, 42, 42, now=2)
    assert state.decision(event.task_key, event.revision_digest) == "pending"


def test_notification_receipt_and_cursor_persist_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "approval.sqlite3"
    state = ApprovalState(path)
    event = _event(tmp_path)
    state.prepare(event, now=1)
    state.mark_notified(event, 42, 7)
    assert state.next_update_id() == 0
    assert state.advance_update_id(10) == 11
    assert state.advance_update_id(4) == 11
    assert ApprovalState(path).next_update_id() == 11


def test_mark_notified_requires_prepared_exact_event(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path)
    with pytest.raises(ApprovalStateError, match="does not match"):
        state.mark_notified(event, 42, 7)
    state.prepare(event, now=1)
    forged = NotificationEvent(
        event.event_id,
        event.kind,
        event.status,
        event.task_key,
        event.revision_digest,
        event.course_name,
        event.course_shortname,
        "Other",
        event.allows_submissions_from,
        event.due_date,
        event.cutoff_date,
        event.grading_due_date,
        event.time_modified,
        event.attachments,
    )
    with pytest.raises(ApprovalStateError, match="does not match"):
        state.mark_notified(forged, 42, 7)


def test_completion_outbox_survives_restart_without_reclaiming_central_work(tmp_path: Path) -> None:
    path = tmp_path / "approval.sqlite3"
    state = ApprovalState(path)
    event = _event(tmp_path, lab=False)
    buttons = state.prepare(event, now=1)
    state.resolve(buttons.approve, 42, 42, now=2)
    pending = state.claim_work("worker", 300, now=3)
    assert pending is not None
    assert state.mark_ready(pending, now=3, for_execution=True)
    ready = state.claim_work("worker", 300, now=4)
    assert ready is not None
    assert state.complete_execution(
        ready, succeeded=True, summary="done", report_markdown="# done", now=4
    )

    restarted = ApprovalState(path)
    notification = restarted.pending_execution_notification()
    assert notification is not None and notification.event == event
    assert restarted.claim_work("worker", 300, now=5) is None
    assert restarted.mark_execution_notification_delivered(notification, now=6)
    assert restarted.pending_execution_notification() is None


def test_failed_lab_cleanup_blocks_second_lab_until_cleaned(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    first = _event(tmp_path, "b")
    second = _event(tmp_path, "c")
    for event in (first, second):
        buttons = state.prepare(event, now=1)
        state.resolve(buttons.approve, 42, 42, now=2)
    pending = state.claim_work("worker", 300, now=3)
    assert pending is not None
    first, second = pending.item.event, second if pending.item.event == first else first
    assert state.record_lab(pending, LabHandle("lab:first"), now=3)
    lab_pending = state.claim_work("worker", 300, now=4)
    assert lab_pending is not None and state.mark_ready(lab_pending, now=4, for_execution=True)
    ready = state.claim_work("worker", 300, now=5)
    assert ready is not None
    assert state.complete_execution(
        ready, succeeded=False, summary="failed", report_markdown="", now=5
    )
    cleanup = state.claim_work("worker", 300, now=6)
    assert cleanup is not None
    assert state.retry_work(cleanup, "cleanup_failed", 60, now=6, exhaustible=False)

    assert state.claim_work("worker", 300, now=7) is None
    retried_cleanup = state.claim_work("worker", 300, now=66)
    assert retried_cleanup is not None
    assert state.mark_cleaned(retried_cleanup, now=66)
    next_item = state.claim_work("worker", 300, now=67)
    assert next_item is not None and next_item.item.event == second


def test_v2_ready_central_work_migrates_and_is_claimable(tmp_path: Path) -> None:
    path = tmp_path / "approval.sqlite3"
    event = _event(tmp_path, lab=False)
    with sqlite3.connect(path) as connection:
        for statement in (
            _METADATA_SQL,
            _REQUESTS_SQL,
            _CALLBACKS_SQL,
            _CURSOR_SQL,
            _WORK_SQL_V2,
            _WORK_CLAIMABLE_INDEX_SQL,
        ):
            connection.execute(statement)
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
        connection.execute("INSERT INTO telegram_cursor VALUES (1, 0)")
        payload = json.dumps(
            event.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        connection.execute(
            "INSERT INTO requests(event_id, task_key, revision_digest, payload, delivery_state, "
            "decision, decided_by, decided_at, chat_id, message_id, created_at) VALUES "
            "(?, ?, ?, ?, 'prepared', 'approved', 1, 1, NULL, NULL, 1)",
            (event.event_id, event.task_key, event.revision_digest, payload),
        )
        digest = Digest.of_json(event.as_dict()).value
        key = sha256(f"moodle-work-provision-v1\0{event.event_id}\0{digest}".encode()).hexdigest()
        connection.execute(
            "INSERT INTO work_items(event_id, selected_mode, specification_digest, provision_key, "
            "status, attempts, available_at, created_at, updated_at) "
            "VALUES (?, 'central', ?, ?, 'ready', 0, 1, 1, 1)",
            (event.event_id, digest, key),
        )
    state = ApprovalState(path)
    assert state.claim_work("worker", 300, now=2) is not None


def test_schema_corruption_is_rejected_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "approval.sqlite3"
    ApprovalState(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE injected(value TEXT)")
    with pytest.raises(ApprovalStateError, match="schema is corrupt"):
        ApprovalState(path)


def test_schema_trigger_injection_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "approval.sqlite3"
    ApprovalState(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER injected AFTER UPDATE ON telegram_cursor BEGIN SELECT 1; END"
        )
    with pytest.raises(ApprovalStateError, match="schema is corrupt"):
        ApprovalState(path)


@pytest.mark.parametrize("value", (-1, True, "1"))
def test_cursor_rejects_invalid_values(tmp_path: Path, value: object) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    with pytest.raises(ApprovalStateError, match="identity is invalid"):
        state.advance_update_id(value)  # type: ignore[arg-type]
