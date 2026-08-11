from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
    _submission_manifest,
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
        43,
    )
    event = MoodleState(tmp_path / f"moodle-{revision}.sqlite3").enqueue(draft, now=1)
    assert event is not None
    return event


def test_submission_approval_is_bound_to_manifest_and_persists_draft_before_save(
    tmp_path: Path,
) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path, lab=False)
    state.prepare(event, now=1)
    manifest, buttons = state.prepare_submission(event, "ok", "# report", now=2)
    assert state.prepare_submission(event, "ok", "# report", now=3) == (manifest, buttons)
    with pytest.raises(ApprovalStateError, match="conflicts"):
        state.prepare_submission(event, "ok", "changed", now=3)
    assert state.resolve_submission(buttons.submit, 42, 42, now=4)[1] == "approved"
    claim = state.claim_submission("worker", 60, now=5)
    assert claim is not None and claim.phase == "uploading"
    saved = state.record_submission_draft(claim, 123, now=6)
    assert saved is not None and saved.phase == "saving" and saved.draft_item_id == 123
    restarted = ApprovalState(tmp_path / "approval.sqlite3")
    recovered = restarted.claim_submission("worker2", 60, now=67)
    assert recovered is not None and recovered.phase == "saving" and recovered.draft_item_id == 123
    assert restarted.complete_submission(recovered, "moodle-submission:9", now=68)
    pending = restarted.pending_submission_notification()
    assert pending is not None and pending.status == "submitted"
    with restarted._connect() as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_payload FROM submissions").fetchone()[0]
        )
    assert receipt == {
        "approvedAt": 4,
        "approvedBy": 42,
        "manifestDigest": manifest.manifest_digest,
        "reference": "moodle-submission:9",
        "submittedAt": 68,
    }


def test_submission_manifest_binds_submission_policies(tmp_path: Path) -> None:
    event = _event(tmp_path, lab=False)
    direct = _submission_manifest(event, "report")
    draft = _submission_manifest(replace(event, submission_drafts=True), "report")
    statement = _submission_manifest(replace(event, requires_submission_statement=True), "report")

    assert direct.manifest_digest != draft.manifest_digest
    assert direct.as_dict()["submissionDrafts"] is False
    assert draft.as_dict()["submissionDrafts"] is True
    assert statement.manifest_digest != direct.manifest_digest
    assert statement.as_dict()["requireSubmissionStatement"] is True


def test_statement_manifest_binds_formatted_unicode_bytes_and_plain_presentation(
    tmp_path: Path,
) -> None:
    event = replace(
        _event(tmp_path, lab=False),
        submission_drafts=True,
        requires_submission_statement=True,
        submission_statement="<p>Jo sóc �� — 你好 <strong>autora</strong>.</p>",
        submission_statement_format=1,
    )
    manifest = _submission_manifest(event, "report")
    assert manifest.submission_statement_plain == "Jo sóc �� — 你好 autora."
    assert manifest.submission_statement_digest is not None
    changed = _submission_manifest(replace(event, submission_statement="<p>canvi</p>"), "report")
    assert changed.manifest_digest != manifest.manifest_digest


def test_draft_submission_records_finalizing_before_moodle_submit(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = replace(_event(tmp_path, "c", lab=False), submission_drafts=True)
    state.prepare(event, now=1)
    _manifest, buttons = state.prepare_submission(event, "ok", "report", now=2)
    state.resolve_submission(buttons.submit, 42, 42, now=3)
    claim = state.claim_submission("worker", 60, now=4)
    assert claim is not None
    saving = state.record_submission_draft(claim, 19, now=5)
    assert saving is not None
    finalizing = state.record_submission_finalizing(saving, now=6)
    assert finalizing is not None and finalizing.phase == "finalizing"


def test_submission_receipt_binds_exact_approver_time_and_statement_manifest(
    tmp_path: Path,
) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = replace(
        _event(tmp_path, "d", lab=False),
        submission_drafts=True,
        requires_submission_statement=True,
        submission_statement="<p>Declaro que esta entrega es mía.</p>",
        submission_statement_format=1,
    )
    state.prepare(event, now=1)
    manifest, buttons = state.prepare_submission(event, "ok", "report", now=2)
    state.resolve_submission(buttons.submit, 77, 77, now=3)
    claim = state.claim_submission("worker", 60, now=4)
    assert claim is not None and state.complete_submission(claim, "moodle-submission:7", now=5)

    with state._connect() as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_payload FROM submissions").fetchone()[0]
        )
    assert receipt == {
        "approvedAt": 3,
        "approvedBy": 77,
        "manifestDigest": manifest.manifest_digest,
        "reference": "moodle-submission:7",
        "submittedAt": 5,
    }
    assert manifest.as_dict()["submissionStatementDigest"] is not None


@pytest.mark.parametrize("phase", ("saving", "finalizing"))
def test_expired_submission_leases_reclaim_only_durable_phase(tmp_path: Path, phase: str) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = replace(_event(tmp_path, "e", lab=False), submission_drafts=True)
    state.prepare(event, now=1)
    _manifest, buttons = state.prepare_submission(event, "ok", "report", now=2)
    state.resolve_submission(buttons.submit, 42, 42, now=3)
    claim = state.claim_submission("first", 6, now=4)
    assert claim is not None
    saving = state.record_submission_draft(claim, 19, now=4)
    assert saving is not None
    if phase == "finalizing":
        assert state.record_submission_finalizing(saving, now=4) is not None

    recovered = state.claim_submission("second", 60, now=10)

    assert recovered is not None and recovered.phase == phase and recovered.draft_item_id == 19


def test_statement_without_drafts_cannot_create_a_second_approval(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path, lab=False)
    state.prepare(event, now=1)
    blocked = replace(event, requires_submission_statement=True)
    with pytest.raises(ApprovalStateError, match="policy is not supported"):
        state.prepare_submission(blocked, "ok", "# report", now=2)


@pytest.mark.parametrize(
    ("legacy_version", "with_submission"),
    (("1", False), ("1", True), ("2", True), ("3", True)),
)
def test_legacy_marker_with_v4_submission_schema_is_corrupt(
    tmp_path: Path, legacy_version: str, with_submission: bool
) -> None:
    """A marker/schema disagreement is corrupt, including an empty future table."""

    path = tmp_path / "approval.sqlite3"
    state = ApprovalState(path)
    if with_submission:
        event = _event(tmp_path, lab=False)
        state.prepare(event, now=1)
        state.prepare_submission(event, "ok", "# report", now=2)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'", (legacy_version,)
        )

    with pytest.raises(ApprovalStateError, match="schema is corrupt"):
        ApprovalState(path)


def test_legacy_event_cannot_create_unbound_submission(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path)
    legacy_event = NotificationEvent(
        event.event_id,
        event.kind,
        event.status,
        event.task_key,
        event.revision_digest,
        event.course_name,
        event.course_shortname,
        event.assignment_title,
        event.allows_submissions_from,
        event.due_date,
        event.cutoff_date,
        event.grading_due_date,
        event.time_modified,
        event.attachments,
    )
    with pytest.raises(ApprovalStateError, match="exact assignment"):
        state.prepare_submission(legacy_event, "ok", "report", now=1)


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
    manifest = {
        "kind": "artifact-manifest-v1",
        "files": [{"path": "report.md", "size": 1, "sha256": "0" * 64}],
        "totals": {"bytes": 1, "files": 1},
    }
    manifest_digest = sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    planner, executor, reviewer = ("a" * 64, "b" * 64, "c" * 64)
    provenance = {
        "kind": "moodle-central-provenance-v2",
        "roles": ["central_planner", "central_executor", "central_reviewer"],
        "jobIds": [planner, executor, reviewer],
        "plannerJobId": planner,
        "executorJobId": executor,
        "reviewerJobId": reviewer,
        "selectedMode": "central",
        "specificationDigest": "d" * 64,
        "preparedInputManifestDigest": "e" * 64,
        "planDigest": "f" * 64,
        "plannerResultDigest": "1" * 64,
        "executorResultDigest": "2" * 64,
        "artifactManifestDigest": manifest_digest,
        "artifactBundleDigest": "3" * 64,
        "reviewerResultDigest": "4" * 64,
        "reviewerAccepted": True,
        "bundleLocator": f"bundles/{'3' * 64}.zip",
        "artifactManifest": manifest,
    }
    assert state.complete_execution(
        ready,
        succeeded=True,
        summary="done",
        report_markdown="# done",
        provenance=provenance,
        now=4,
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
