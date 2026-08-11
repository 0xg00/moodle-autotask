from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from moddle_autotask.adapters.aws.retention import (
    AgentRetentionAck,
    CommittedTombstone,
    RetentionError,
    decode_ack,
    decode_committed,
    decode_prepared,
    plan_retention,
)
from moddle_autotask.adapters.moodle.approval_state import (
    ApprovalState,
    ApprovalStateError,
    RetentionRecord,
)
from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationDraft,
    _event_id,
)
from moddle_autotask.domain.models import ExecutionMode


def _record(*, delivered: int | None = None, scratch: int = 10) -> RetentionRecord:
    task = "moodle-task-v1:" + "b" * 64
    revision = "moodle-assignment-v1:" + "a" * 64
    return RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.CENTRAL,
        "cleaned",
        True,
        1,
        delivered,
        scratch,
        None if delivered is None else delivered + 7,
        ("b" * 64, "c" * 64, "d" * 64),
        "e" * 64,
    )


def test_planner_never_uses_delivery_age_without_proof() -> None:
    assert plan_retention((_record(),), now=9, limit=1) == ()
    pending = plan_retention((_record(),), now=10, limit=1)
    assert pending[0].bundle_digest is None
    delivered = plan_retention((_record(delivered=20),), now=27, limit=1)
    assert delivered[0].bundle_digest == "e" * 64


def test_planner_rejects_contradictory_delivery_and_identity_records() -> None:
    contradictory = replace(_record(), evidence_eligible_at=8)
    with pytest.raises(RetentionError, match="inconsistent"):
        plan_retention((contradictory,), now=10, limit=1)
    forged = replace(_record(), event_id="moodle-notification-event-v1:" + "0" * 64)
    with pytest.raises(RetentionError, match="inconsistent"):
        plan_retention((forged,), now=10, limit=1)


@pytest.mark.parametrize(
    "change",
    [
        {"central_job_ids": ["b" * 64, "c" * 64, "d" * 64]},
        {"central_job_ids": (1, "c" * 64, "d" * 64)},
        {"central_job_ids": ([], "c" * 64, "d" * 64)},
        {"bundle_digest": 1},
    ],
)
def test_planner_rejects_hostile_central_record_types(change: dict[str, object]) -> None:
    with pytest.raises(RetentionError):
        hostile = replace(_record(delivered=1), **change)  # type: ignore[arg-type]
        plan_retention((hostile,), now=10, limit=1)


def test_every_accepted_plan_round_trips_to_the_same_tuple_job_identity() -> None:
    plan = plan_retention((_record(delivered=1),), now=10, limit=1)[0]
    assert isinstance(plan.job_ids, tuple)
    assert decode_prepared(plan.as_json()) == plan


def test_planner_does_not_starve_central_record_after_legitimate_lab_prefix() -> None:
    lab = RetentionRecord(
        _event_id("moodle-task-v1:" + "e" * 64, "moodle-assignment-v1:" + "f" * 64),
        "moodle-task-v1:" + "e" * 64,
        "moodle-assignment-v1:" + "f" * 64,
        ExecutionMode.IN_GUEST,
        "ready",
        True,
        1,
        None,
        1,
        None,
        (),
        None,
    )
    assert len(plan_retention((lab, _record()), now=10, limit=1)) == 1


def test_tombstone_and_ack_codecs_are_canonical_and_tamper_closed() -> None:
    prepared = plan_retention((_record(delivered=1),), now=10, limit=1)[0]
    assert decode_prepared(prepared.as_json()) == prepared
    committed = CommittedTombstone(prepared, 10)
    assert decode_committed(committed.as_json()) == committed
    ack = AgentRetentionAck(prepared.tombstone_id, 10, 11)
    assert decode_ack(ack.as_json()) == ack
    with pytest.raises(RetentionError):
        decode_prepared(prepared.as_json().replace(b'"state":"prepared"', b'"state":"x"'))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"kind":"moodle-retention-v1","kind":"moodle-retention-v1"}',
        b'{"kind":"moodle-retention-v1","state":"ack","tombstoneId":"'
        + b"a" * 64
        + b'","committedAt":true,"acknowledgedAt":1}',
    ],
)
def test_codecs_reject_duplicate_keys_and_boolean_timestamps(raw: bytes) -> None:
    with pytest.raises(RetentionError):
        decode_ack(raw)


def test_prepared_central_tombstone_requires_three_jobs_and_evidence_for_bundle() -> None:
    prepared = plan_retention((_record(delivered=1),), now=10, limit=1)[0]
    raw = prepared.as_json().replace(
        b'"jobIds":["' + b"b" * 64 + b'","' + b"c" * 64 + b'","' + b"d" * 64 + b'"]',
        b'"jobIds":["' + b"b" * 64 + b'"]',
    )
    with pytest.raises(RetentionError):
        decode_prepared(raw)
    early = CommittedTombstone(prepared, 7)
    with pytest.raises(RetentionError):
        decode_committed(early.as_json())


def test_planner_rejects_duplicate_records_and_omits_unidentified_lab_work() -> None:
    with pytest.raises(RetentionError, match="duplicate"):
        plan_retention((_record(), _record()), now=10, limit=2)
    lab = RetentionRecord(
        _event_id("moodle-task-v1:" + "e" * 64, "moodle-assignment-v1:" + "f" * 64),
        "moodle-task-v1:" + "e" * 64,
        "moodle-assignment-v1:" + "f" * 64,
        ExecutionMode.IN_GUEST,
        "ready",
        True,
        1,
        None,
        1,
        None,
        (),
        None,
    )
    assert plan_retention((lab,), now=10, limit=1) == ()


def _central_completion(
    tmp_path: Path, *, revision_mark: str = "a", succeeded: bool = True, now: int = 1
) -> tuple[ApprovalState, str]:
    revision = "moodle-assignment-v1:" + revision_mark * 64
    draft = NotificationDraft(
        "moodle-task-v1:" + "b" * 64,
        revision,
        "Course",
        "C",
        "Assignment",
        0,
        1,
        0,
        0,
        1,
        (),
        1,
    )
    event = MoodleState(tmp_path / f"moodle-{revision_mark}.sqlite3").enqueue(draft, now=now)
    assert event is not None
    state = ApprovalState(tmp_path / "approval.sqlite3")
    buttons = state.prepare(event, now=now)
    state.resolve(buttons.approve, 1, 1, now=now + 1)
    pending = state.claim_work("worker", 60, now=now + 2)
    assert pending is not None and state.mark_ready(pending, now=now + 2, for_execution=True)
    ready = state.claim_work("worker", 60, now=now + 3)
    assert ready is not None
    manifest = {
        "kind": "artifact-manifest-v1",
        "files": [{"path": "report.md", "size": 1, "sha256": "0" * 64}],
        "totals": {"bytes": 1, "files": 1},
    }
    manifest_digest = sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    jobs = ("1" * 64, "2" * 64, "3" * 64)
    provenance = {
        "kind": "moodle-central-provenance-v2",
        "roles": ["central_planner", "central_executor", "central_reviewer"],
        "jobIds": list(jobs),
        "plannerJobId": jobs[0],
        "executorJobId": jobs[1],
        "reviewerJobId": jobs[2],
        "selectedMode": "central",
        "specificationDigest": "4" * 64,
        "preparedInputManifestDigest": "5" * 64,
        "planDigest": "6" * 64,
        "plannerResultDigest": "7" * 64,
        "executorResultDigest": "8" * 64,
        "artifactManifestDigest": manifest_digest,
        "artifactBundleDigest": "9" * 64,
        "reviewerResultDigest": "a" * 64,
        "reviewerAccepted": True,
        "bundleLocator": f"bundles/{'9' * 64}.zip",
        "artifactManifest": manifest,
    }
    assert state.complete_execution(
        ready,
        succeeded=succeeded,
        summary="done",
        report_markdown="done",
        provenance=provenance if succeeded else None,
        now=now + 3,
    )
    return state, revision


def test_real_completion_retention_chain_binds_namespaced_revision_and_delivery(
    tmp_path: Path,
) -> None:
    state, revision = _central_completion(tmp_path)
    pending = state.retention_records(28, 24, 7, 1)
    assert pending[0].revision_digest == revision and pending[0].evidence_eligible_at is None
    prepared = plan_retention(pending, now=28, limit=1)[0]
    assert decode_prepared(prepared.as_json()) == prepared and prepared.bundle_digest is None
    notification = state.pending_execution_notification()
    assert notification is not None
    assert state.mark_execution_notification_delivered(notification, now=30)
    delivered = state.retention_records(38, 24, 7, 1)
    committed = CommittedTombstone(plan_retention(delivered, now=38, limit=1)[0], 38)
    assert decode_committed(committed.as_json()).prepared.revision_digest == revision


@pytest.mark.parametrize("value", [True, False])
def test_retention_records_reject_boolean_arguments(tmp_path: Path, value: bool) -> None:
    state, _ = _central_completion(tmp_path)
    with pytest.raises(ValueError):
        state.retention_records(28, value, 7, 1)
    with pytest.raises(ValueError):
        state.retention_records(28, 24, value, 1)
    with pytest.raises(ValueError):
        state.retention_records(28, 24, 7, value)


@pytest.mark.parametrize("status", ["pending", "lab_pending"])
def test_retention_rejects_impossible_stored_execution_outbox_statuses(
    tmp_path: Path, status: str
) -> None:
    state, _ = _central_completion(tmp_path)
    with sqlite3.connect(state.path) as connection:
        if status == "lab_pending":
            connection.execute(
                "UPDATE work_items SET selected_mode = 'in_guest', lab_handle = 'lab:corrupt', "
                "status = 'lab_pending'"
            )
        else:
            connection.execute("UPDATE work_items SET status = ?", (status,))
    with pytest.raises(ApprovalStateError, match="retention record is corrupt"):
        state.retention_records(28, 24, 7, 1)


def test_decoders_reject_forged_rehashed_exact_shaped_event_identity() -> None:
    prepared = plan_retention((_record(delivered=1),), now=10, limit=1)[0]
    value = json.loads(prepared.as_json())
    value["eventId"] = _event_id(
        "moodle-task-v1:" + "c" * 64, "moodle-assignment-v1:" + "d" * 64
    )
    identity = {key: value[key] for key in value if key not in {"kind", "state", "tombstoneId"}}
    value["tombstoneId"] = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(RetentionError, match="identity"):
        decode_prepared(raw)
    value["state"] = "committed"
    value["committedAt"] = 10
    with pytest.raises(RetentionError, match="identity"):
        decode_committed(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def test_retention_records_do_not_starve_later_successful_central_completion(
    tmp_path: Path,
) -> None:
    _central_completion(tmp_path, revision_mark="b", succeeded=False, now=1)
    state, revision = _central_completion(tmp_path, revision_mark="c", succeeded=True, now=10)
    records = state.retention_records(100, 1, 1, 1)
    assert len(records) == 1
    assert records[0].revision_digest == revision and records[0].succeeded


@pytest.mark.parametrize(
    "payload",
    [
        {"summary": "done", "reportMarkdown": "done"},
        {"succeeded": True, "reportMarkdown": "done"},
        {"succeeded": True, "summary": "done"},
        {"succeeded": "true", "summary": "done", "reportMarkdown": "done"},
        {"succeeded": True, "summary": 1, "reportMarkdown": "done"},
        {"succeeded": True, "summary": "done", "reportMarkdown": 1},
    ],
)
def test_retention_probe_rejects_missing_or_wrong_required_central_outcome_fields(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    state, _ = _central_completion(tmp_path)
    with sqlite3.connect(state.path) as connection:
        connection.execute(
            "UPDATE execution_outbox SET payload = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
    with pytest.raises(ApprovalStateError, match="retention record is corrupt"):
        state.retention_records(100, 1, 1, 1)


@pytest.mark.parametrize(
    "payload",
    [
        '{"succeeded":false,"summary":"done","reportMarkdown":"done","extra":1}',
        '{"succeeded":false,"succeeded":true,"summary":"done","reportMarkdown":"done"}',
        '{"succeeded":true,"succeeded":false,"summary":"done","reportMarkdown":"done"}',
    ],
)
def test_retention_probe_rejects_extra_or_duplicate_central_outcome_keys(
    tmp_path: Path, payload: str
) -> None:
    state, _ = _central_completion(tmp_path)
    with sqlite3.connect(state.path) as connection:
        connection.execute("UPDATE execution_outbox SET payload = ?", (payload,))
    with pytest.raises(ApprovalStateError, match="retention record is corrupt"):
        state.retention_records(100, 1, 1, 1)
