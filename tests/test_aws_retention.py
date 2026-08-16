from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from moodle_autotask.adapters.aws import central_protocol
from moodle_autotask.adapters.aws.retention import (
    AgentRetentionAck,
    CommittedTombstone,
    PreparedTombstone,
    RetentionError,
    decode_ack,
    decode_committed,
    decode_prepared,
    plan_retention,
)
from moodle_autotask.adapters.moodle.approval_state import (
    ApprovalState,
    ApprovalStateError,
    RetentionRecord,
)
from moodle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationDraft,
    _event_id,
)
from moodle_autotask.domain.models import ExecutionMode, LabHandle


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
        result_digests=("1" * 64, "2" * 64, "3" * 64),
    )


def test_planner_never_uses_delivery_age_without_proof() -> None:
    assert plan_retention((_record(),), now=9, limit=1) == ()
    pending = plan_retention((_record(),), now=10, limit=1)
    assert pending[0].bundle_digest is None
    delivered = plan_retention((_record(delivered=20),), now=27, limit=2)
    assert [plan.target_phase for plan in delivered] == ["scratch", "evidence"]
    assert delivered[1].bundle_digest == "e" * 64


def test_lab_planner_emits_one_family_bound_scratch_tombstone() -> None:
    task = "moodle-task-v1:" + "8" * 64
    revision = "moodle-assignment-v1:" + "9" * 64
    plan_id, report_id, dispatch_digest = "1" * 64, "2" * 64, "3" * 64
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.IN_GUEST,
        "cleaned",
        False,
        1,
        2,
        10,
        20,
        (plan_id,),
        None,
        "lab",
        (plan_id, report_id),
        report_id,
        dispatch_digest,
        ("4" * 64,),
    )
    prepared = plan_retention((record,), now=20, limit=2)
    assert len(prepared) == 1
    assert prepared[0].execution_family == "lab"
    assert prepared[0].job_ids == (plan_id,)
    assert prepared[0].barrier_ids == (plan_id, report_id)
    assert prepared[0].dispatch_id == report_id
    assert decode_prepared(prepared[0].as_json()) == prepared[0]
    assert b'"executionFamily":"lab"' in prepared[0].as_json()
    assert b'"executionFamily"' not in plan_retention((_record(),), now=10, limit=1)[
        0
    ].as_json()


def test_phase_scoped_plans_have_stable_scratch_identity_and_exact_shapes() -> None:
    before_delivery = plan_retention((_record(),), now=10, limit=2)
    after_delivery = plan_retention((_record(delivered=20),), now=27, limit=2)

    assert len(before_delivery) == 1
    scratch, evidence = after_delivery
    assert scratch == before_delivery[0]
    assert scratch.target_phase == "scratch"
    assert scratch.job_ids == ("b" * 64, "c" * 64, "d" * 64)
    assert scratch.bundle_digest is None
    assert evidence.target_phase == "evidence"
    assert evidence.job_ids == ()
    assert evidence.bundle_digest == "e" * 64


@pytest.mark.parametrize(
    ("replace_from", "replace_to"),
    [
        (b'"targetPhase":"scratch"', b'"targetPhase":"evidence"'),
        (b'"jobIds":[]', b'"jobIds":["' + b"b" * 64 + b'"]'),
        (b'"bundleDigest":null', b'"bundleDigest":"' + b"e" * 64 + b'"'),
    ],
)
def test_phase_scoped_prepared_codec_rejects_cross_phase_shapes(
    replace_from: bytes, replace_to: bytes
) -> None:
    scratch, evidence = plan_retention((_record(delivered=1),), now=10, limit=2)
    source = evidence.as_json() if replace_from == b'"jobIds":[]' else scratch.as_json()
    with pytest.raises(RetentionError):
        decode_prepared(source.replace(replace_from, replace_to))


def test_completed_filter_is_exact_and_applied_before_limit() -> None:
    first = _record(delivered=1)
    second = replace(_record(delivered=1), task_key="moodle-task-v1:" + "c" * 64)
    second = replace(second, event_id=_event_id(second.task_key, second.revision_digest))
    planned = plan_retention(
        (first, second),
        now=10,
        limit=1,
        completed=lambda candidate: candidate.event_id == first.event_id,
    )
    assert len(planned) == 1 and planned[0].event_id == second.event_id
    with pytest.raises(RetentionError, match="predicate"):
        plan_retention((first,), now=10, limit=1, completed=lambda _: cast(bool, 1))
    with pytest.raises(RetentionError, match="predicate"):
        plan_retention(
            (first,),
            now=10,
            limit=1,
            completed=lambda _: (_ for _ in ()).throw(ValueError()),
        )


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
    provenance: dict[str, object] = {
        "kind": "moodle-central-provenance-v2",
        "roles": ["central_planner", "central_executor", "central_reviewer"],
        "jobIds": list(jobs),
        "plannerJobId": jobs[0],
        "executorJobId": jobs[1],
        "reviewerJobId": jobs[2],
        "selectedMode": "central",
        "specificationDigest": ready.item.specification_digest.value,
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
    assert pending[0].result_digests == ("7" * 64, "8" * 64, "a" * 64)
    prepared = plan_retention(pending, now=28, limit=1)[0]
    assert decode_prepared(prepared.as_json()) == prepared and prepared.bundle_digest is None
    assert prepared.result_digests == pending[0].result_digests
    notification = state.pending_execution_notification()
    assert notification is not None
    assert state.mark_execution_notification_delivered(notification, now=30)
    delivered = state.retention_records(38, 24, 7, 1)
    committed = CommittedTombstone(plan_retention(delivered, now=38, limit=1)[0], 38)
    assert decode_committed(committed.as_json()).prepared.revision_digest == revision


def test_lab_completion_flows_from_sqlite_to_family_bound_retention_plan(
    tmp_path: Path,
) -> None:
    revision = "moodle-assignment-v1:" + "6" * 64
    event = MoodleState(tmp_path / "moodle-lab.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + "5" * 64,
            revision,
            "Course",
            "C",
            "Laboratorio",
            0,
            1,
            0,
            0,
            1,
            (),
            1,
        ),
        now=1,
    )
    assert event is not None
    state = ApprovalState(tmp_path / "approval-lab.sqlite3")
    buttons = state.prepare(event, now=1)
    state.resolve(buttons.approve, 1, 1, now=2)
    pending = state.claim_work("worker", 60, now=3)
    assert pending is not None
    assert state.record_lab(pending, LabHandle("lab:test"), now=3)
    lab_pending = state.claim_work("worker", 60, now=4)
    assert lab_pending is not None
    assert state.mark_ready(lab_pending, now=4, for_execution=True)
    ready = state.claim_work("worker", 60, now=5)
    assert ready is not None
    plan_id = "7" * 64
    provenance: dict[str, object] = {
        "kind": "moodle-lab-provenance-v1",
        "selectedMode": "hybrid",
        "taskKey": event.task_key,
        "revisionDigest": event.revision_digest,
        "specificationDigest": ready.item.specification_digest.value,
        "phases": ["lab_plan"],
        "jobIds": [plan_id],
        "barrierIds": [plan_id],
        "resultDigests": ["8" * 64],
        "terminalStatus": "failed",
        "dispatch": None,
    }
    assert state.complete_execution(
        ready,
        succeeded=False,
        summary="failed",
        report_markdown="",
        provenance=provenance,
        now=5,
    )
    cleanup = state.claim_work("worker", 60, now=6)
    assert cleanup is not None and state.mark_cleaned(cleanup, now=6)

    records = state.retention_records(10, 1, 7, 1)
    assert len(records) == 1 and records[0].execution_family == "lab"
    assert records[0].result_digests == ("8" * 64,)
    prepared = plan_retention(records, now=10, limit=1)[0]
    assert prepared.execution_family == "lab"
    assert prepared.job_ids == (plan_id,)
    assert prepared.barrier_ids == (plan_id,)
    assert decode_prepared(prepared.as_json()) == prepared


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


def test_sqlite_retention_completion_replay_filters_exact_phase(tmp_path: Path) -> None:
    state, _revision = _central_completion(tmp_path)
    records = state.retention_records(10, 1, 1, 1)
    scratch = plan_retention(records, now=10, limit=1)[0]

    state.record_retention_completions((scratch,), completed_at=11)
    state.record_retention_completions((scratch,), completed_at=12)
    assert state.retention_records(12, 1, 1, 1) == ()
    with pytest.raises(ApprovalStateError, match="conflicts"):
        state.record_retention_completions(
            (replace(scratch, tombstone_id="f" * 64),), completed_at=12
        )


def test_sqlite_completion_ledger_does_not_starve_later_retention_record(tmp_path: Path) -> None:
    limit = 3
    for index, marker in enumerate(("b", "c", "d"), start=1):
        _central_completion(tmp_path, revision_mark=marker, now=index)
    state, later_revision = _central_completion(tmp_path, revision_mark="e", now=10)

    old_records = state.retention_records(20, 1, 1, limit)
    assert len(old_records) == limit
    old_plans = plan_retention(old_records, now=20, limit=limit)
    state.record_retention_completions(old_plans, completed_at=21)
    with state._connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM retention_completions").fetchone()
        assert count == (limit,)

    restarted = ApprovalState(state.path)
    actionable = restarted.retention_records(22, 1, 1, limit)
    assert len(actionable) == 1
    assert actionable[0].revision_digest == later_revision
    assert plan_retention(actionable, now=22, limit=1)[0].revision_digest == later_revision


def test_retention_records_exclude_1024_completed_rows_before_sql_limit(tmp_path: Path) -> None:
    state, later_revision = _central_completion(tmp_path, revision_mark="e", now=2_000)
    with state._connect() as connection:
        template_event, template_outcome = connection.execute(
            "SELECT r.payload, o.payload FROM requests r "
            "JOIN execution_outbox o ON o.event_id = r.event_id"
        ).fetchone()
        connection.execute("BEGIN IMMEDIATE")
        for index in range(1_024):
            task = f"moodle-task-v1:{index:064x}"
            revision = f"moodle-assignment-v1:{index + 2_048:064x}"
            event_id = _event_id(task, revision)
            event = json.loads(template_event)
            event.update({"event_id": event_id, "task_key": task, "revision_digest": revision})
            connection.execute(
                "INSERT INTO requests(event_id, task_key, revision_digest, payload, "
                "delivery_state, decision, decided_by, decided_at, chat_id, message_id, "
                "created_at) "
                "VALUES (?, ?, ?, ?, 'prepared', 'approved', 1, 1, NULL, NULL, ?)",
                (
                    event_id,
                    task,
                    revision,
                    json.dumps(event, sort_keys=True, separators=(",", ":")),
                    index,
                ),
            )
            connection.execute(
                "INSERT INTO work_items(event_id, selected_mode, specification_digest, "
                "provision_key, status, lab_handle, attempts, available_at, lease_owner, "
                "lease_token, lease_expires_at, "
                "error_code, created_at, updated_at, cleanup_due_at) "
                "VALUES (?, 'central', ?, ?, 'cleaned', NULL, 1, ?, NULL, NULL, NULL, "
                "'execution_complete', ?, ?, NULL)",
                (event_id, "a" * 64, f"old-{index}", index, index, index),
            )
            connection.execute(
                "INSERT INTO execution_outbox(event_id, payload, delivered_at, created_at) "
                "VALUES (?, ?, NULL, ?)",
                (event_id, template_outcome, index),
            )
            connection.execute(
                "INSERT INTO retention_completions(event_id, target_phase, tombstone_id, "
                "completed_at) "
                "VALUES (?, 'scratch', ?, ?)",
                (
                    event_id,
                    sha256(f"completed:{index}".encode()).hexdigest(),
                    index,
                ),
            )
        connection.execute("COMMIT")

    records = state.retention_records(5_000, 1, 1, 1_024)

    assert len(records) == 1
    assert records[0].revision_digest == later_revision


def _complete_terminal_v3(
    tmp_path: Path,
    *,
    prefix: int,
    terminal_status: str,
    bundle: bool,
    provenance: bool,
    completion_succeeded: bool = False,
) -> ApprovalState:
    revision = "moodle-assignment-v1:" + "e" * 64
    draft = NotificationDraft(
        "moodle-task-v1:" + "d" * 64,
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
    event = MoodleState(tmp_path / "moodle.sqlite3").enqueue(draft, now=1)
    assert event is not None
    state = ApprovalState(tmp_path / "approval.sqlite3")
    buttons = state.prepare(event, now=1)
    state.resolve(buttons.approve, 1, 1, now=2)
    pending = state.claim_work("worker", 60, now=3)
    assert pending is not None and state.mark_ready(pending, now=3, for_execution=True)
    ready = state.claim_work("worker", 60, now=4)
    assert ready is not None
    value: dict[str, object] | None = None
    if provenance:
        job_ids = [str(index + 1) * 64 for index in range(prefix)]
        value = {
            "kind": "moodle-central-provenance-v3",
            "selectedMode": "central",
            "eventId": event.event_id,
            "taskKey": event.task_key,
            "revisionDigest": event.revision_digest,
            "specificationDigest": ready.item.specification_digest.value,
            "preparedInputManifestDigest": "6" * 64,
            "roles": list(central_protocol.CENTRAL_ROLES[:prefix]),
            "jobIds": job_ids,
            "terminalRole": (
                central_protocol.CENTRAL_ROLES[prefix]
                if terminal_status == "budget_error"
                else central_protocol.CENTRAL_ROLES[prefix - 1]
            ),
            "terminalStatus": terminal_status,
            "resultDigests": [str(index + 7) * 64 for index in range(prefix)],
        }
        if bundle:
            bundle_digest = "a" * 64
            manifest = {
                "kind": "artifact-manifest-v1",
                "files": [{"path": "report.md", "size": 1, "sha256": "b" * 64}],
                "totals": {"files": 1, "bytes": 1},
            }
            value |= {
                "artifactManifest": manifest,
                "artifactManifestDigest": central_protocol.canonical_digest(manifest),
                "artifactBundleDigest": bundle_digest,
                "bundleLocator": f"bundles/{bundle_digest}.zip",
            }
        assert central_protocol.validate_terminal_provenance(value) == value
    if completion_succeeded:
        with pytest.raises(ApprovalStateError, match="provenance"):
            state.complete_execution(
                ready,
                succeeded=True,
                summary="failed",
                report_markdown="",
                provenance=value,
                now=4,
            )
    else:
        assert state.complete_execution(
            ready,
            succeeded=False,
            summary="failed",
            report_markdown="",
            provenance=value,
            now=4,
        )

    return state


def test_complete_execution_rejects_success_with_terminal_central_v3(tmp_path: Path) -> None:
    _complete_terminal_v3(
        tmp_path,
        prefix=1,
        terminal_status="failed",
        bundle=False,
        provenance=True,
        completion_succeeded=True,
    )


@pytest.mark.parametrize(
    ("prefix", "terminal_status", "bundle", "provenance"),
    [
        (1, "failed", False, True),
        (2, "failed", False, True),
        (3, "failed", True, True),
        (3, "rejected", True, True),
        (1, "budget_error", False, True),
        (0, "failed", False, False),
    ],
    ids=(
        "planner-failed",
        "executor-failed",
        "reviewer-failed",
        "reviewer-rejected",
        "budget-after-planner",
        "initial-budget-no-artifact",
    ),
)
def test_retention_records_include_terminal_v3_prefixes(
    tmp_path: Path, prefix: int, terminal_status: str, bundle: bool, provenance: bool
) -> None:
    state = _complete_terminal_v3(
        tmp_path,
        prefix=prefix,
        terminal_status=terminal_status,
        bundle=bundle,
        provenance=provenance,
    )

    records = state.retention_records(10, 1, 1, 1)
    if not provenance:
        assert records == ()
        assert plan_retention(records, now=10, limit=1) == ()
        return
    assert len(records) == 1
    expected_ids = tuple(str(index + 1) * 64 for index in range(prefix))
    assert records[0].central_job_ids == expected_ids
    assert records[0].result_digests == tuple(
        str(index + 7) * 64 for index in range(prefix)
    )
    assert records[0].bundle_digest == ("a" * 64 if bundle else None)
    before_delivery = plan_retention(records, now=10, limit=2)
    assert [(item.target_phase, item.job_ids, item.bundle_digest) for item in before_delivery] == [
        ("scratch", expected_ids, None)
    ]

    notification = state.pending_execution_notification()
    assert notification is not None
    assert state.mark_execution_notification_delivered(notification, now=20)
    delivered = state.retention_records(22, 1, 1, 1)
    plans = plan_retention(delivered, now=22, limit=2)
    expected_phases: list[tuple[str, tuple[str, ...], str | None]] = [
        ("scratch", expected_ids, None)
    ]
    if bundle:
        expected_phases.append(("evidence", (), "a" * 64))
    actual_phases = [(item.target_phase, item.job_ids, item.bundle_digest) for item in plans]
    assert actual_phases == expected_phases
    for completed in plans:
        completed_id = completed.tombstone_id

        def completed_receipt(candidate: PreparedTombstone, done: str = completed_id) -> bool:
            return candidate.tombstone_id == done

        remaining = plan_retention(
            delivered,
            now=22,
            limit=1,
            completed=completed_receipt,
        )
        assert all(item.tombstone_id != completed.tombstone_id for item in remaining)


@pytest.mark.parametrize(
    "hostile",
    [
        "bundle-on-planner-failure",
        "bundle-on-executor-failure",
        "bundle-on-executor-budget",
        "missing-bundle-after-executor-success",
        "wrong-manifest-digest",
        "wrong-bundle-digest",
        "wrong-bundle-locator",
        "wrong-terminal-role",
        "wrong-terminal-status",
    ],
)
def test_approval_state_retention_rejects_hostile_terminal_v3_tuples(
    tmp_path: Path, hostile: str
) -> None:
    if hostile == "bundle-on-planner-failure":
        state = _complete_terminal_v3(
            tmp_path, prefix=1, terminal_status="failed", bundle=False, provenance=True
        )
    elif hostile == "bundle-on-executor-failure":
        state = _complete_terminal_v3(
            tmp_path, prefix=2, terminal_status="failed", bundle=False, provenance=True
        )
    elif hostile == "bundle-on-executor-budget":
        state = _complete_terminal_v3(
            tmp_path, prefix=1, terminal_status="budget_error", bundle=False, provenance=True
        )
    else:
        state = _complete_terminal_v3(
            tmp_path, prefix=2, terminal_status="budget_error", bundle=True, provenance=True
        )
    with state._connect() as connection:
        row = connection.execute("SELECT payload FROM execution_outbox").fetchone()
        assert row is not None
        payload = json.loads(row[0])
        provenance = cast(dict[str, object], payload["provenance"])
        if hostile.startswith("bundle-on-"):
            manifest = {
                "kind": "artifact-manifest-v1",
                "files": [{"path": "report.md", "size": 1, "sha256": "b" * 64}],
                "totals": {"files": 1, "bytes": 1},
            }
            digest = "a" * 64
            provenance.update(
                {
                    "artifactManifest": manifest,
                    "artifactManifestDigest": central_protocol.canonical_digest(manifest),
                    "artifactBundleDigest": digest,
                    "bundleLocator": f"bundles/{digest}.zip",
                }
            )
        elif hostile == "missing-bundle-after-executor-success":
            for key in (
                "artifactManifest",
                "artifactManifestDigest",
                "artifactBundleDigest",
                "bundleLocator",
            ):
                del provenance[key]
        elif hostile == "wrong-manifest-digest":
            provenance["artifactManifestDigest"] = "f" * 64
        elif hostile == "wrong-bundle-digest":
            provenance["artifactBundleDigest"] = "f" * 64
        elif hostile == "wrong-bundle-locator":
            provenance["bundleLocator"] = "bundles/other.zip"
        elif hostile == "wrong-terminal-role":
            provenance["terminalRole"] = "central_executor"
        else:
            provenance["terminalStatus"] = "failed"
        connection.execute(
            "UPDATE execution_outbox SET payload = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
    with pytest.raises(ApprovalStateError, match="execution provenance is invalid"):
        state.retention_records(10, 1, 1, 1)


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
