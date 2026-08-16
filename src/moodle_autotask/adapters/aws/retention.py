"""Pure retention planning codecs; Phase A deliberately performs no filesystem mutation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from moodle_autotask.adapters.moodle.approval_state import RetentionRecord
from moodle_autotask.adapters.moodle.state import _event_id
from moodle_autotask.domain.models import ExecutionMode

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^moodle-assignment-v1:[0-9a-f]{64}$")
_EVENT = re.compile(r"^moodle-notification-event-v1:[0-9a-f]{64}$")
_TASK = re.compile(r"^moodle-task-v1:[0-9a-f]{64}$")
_MAX_TOMBSTONE_BYTES = 16_384


class RetentionError(RuntimeError):
    """Raised when durable retention metadata is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    scratch_ttl: int = 24 * 3600
    evidence_ttl: int = 7 * 24 * 3600

    def __post_init__(self) -> None:
        if type(self.scratch_ttl) is not int or not 1 <= self.scratch_ttl <= 90 * 24 * 3600:
            raise ValueError("scratch retention TTL is invalid")
        if type(self.evidence_ttl) is not int or not 1 <= self.evidence_ttl <= 90 * 24 * 3600:
            raise ValueError("evidence retention TTL is invalid")


@dataclass(frozen=True, slots=True)
class PreparedTombstone:
    tombstone_id: str
    event_id: str
    task_key: str
    revision_digest: str
    target_phase: str
    eligible_at: int
    job_ids: tuple[str, ...]
    bundle_digest: str | None
    execution_family: str = "central"
    barrier_ids: tuple[str, ...] = ()
    dispatch_id: str | None = None
    dispatch_digest: str | None = None
    result_digests: tuple[str, ...] = ()

    def as_json(self) -> bytes:
        return _encode("prepared", self._fields())

    def _fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "tombstoneId": self.tombstone_id,
            "eventId": self.event_id,
            "taskKey": self.task_key,
            "revisionDigest": self.revision_digest,
            "targetPhase": self.target_phase,
            "eligibleAt": self.eligible_at,
            "jobIds": list(self.job_ids),
            "bundleDigest": self.bundle_digest,
            "resultDigests": list(self.result_digests),
        }
        if self.execution_family == "lab":
            fields.update(
                {
                    "executionFamily": "lab",
                    "barrierIds": list(self.barrier_ids),
                    "dispatchId": self.dispatch_id,
                    "dispatchDigest": self.dispatch_digest,
                }
            )
        return fields


@dataclass(frozen=True, slots=True)
class CommittedTombstone:
    prepared: PreparedTombstone
    committed_at: int

    def as_json(self) -> bytes:
        return _encode("committed", {**self.prepared._fields(), "committedAt": self.committed_at})


@dataclass(frozen=True, slots=True)
class AgentRetentionAck:
    tombstone_id: str
    committed_at: int
    acknowledged_at: int

    def as_json(self) -> bytes:
        return _encode(
            "ack",
            {
                "tombstoneId": self.tombstone_id,
                "committedAt": self.committed_at,
                "acknowledgedAt": self.acknowledged_at,
            },
        )


def plan_retention(
    records: tuple[RetentionRecord, ...],
    *,
    now: int,
    limit: int,
    completed: Callable[[PreparedTombstone], bool] | None = None,
) -> tuple[PreparedTombstone, ...]:
    """Plan central scratch cleanup, adding a bundle only after proven delivery age."""
    if type(now) is not int or now < 0 or type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("retention planner arguments are invalid")
    plans: list[PreparedTombstone] = []
    seen: set[str] = set()
    for record in records:
        _validate_record(record)
        if record.event_id in seen:
            raise RetentionError("retention records contain duplicate events")
        seen.add(record.event_id)
        if (
            record.work_status != "cleaned"
            or now < record.scratch_eligible_at
            or not record.central_job_ids
        ):
            continue
        if record.execution_family == "lab":
            identity_fields = {
                "eventId": record.event_id,
                "taskKey": record.task_key,
                "revisionDigest": record.revision_digest,
                "targetPhase": "scratch",
                "eligibleAt": record.scratch_eligible_at,
                "jobIds": list(record.central_job_ids),
                "bundleDigest": None,
                "executionFamily": "lab",
                "barrierIds": list(record.barrier_ids),
                "dispatchId": record.dispatch_id,
                "dispatchDigest": record.dispatch_digest,
                "resultDigests": list(record.result_digests),
            }
            candidate = PreparedTombstone(
                hashlib.sha256(_canonical(identity_fields)).hexdigest(),
                record.event_id,
                record.task_key,
                record.revision_digest,
                "scratch",
                record.scratch_eligible_at,
                record.central_job_ids,
                None,
                "lab",
                record.barrier_ids,
                record.dispatch_id,
                record.dispatch_digest,
                record.result_digests,
            )
            if completed is not None:
                try:
                    done = completed(candidate)
                except Exception as error:
                    raise RetentionError("retention completion predicate failed") from error
                if type(done) is not bool:
                    raise RetentionError("retention completion predicate is invalid")
                if done:
                    continue
            plans.append(candidate)
            if len(plans) >= limit:
                return tuple(plans)
            continue
        phases: list[tuple[str, int, tuple[str, ...], str | None]] = [
            ("scratch", record.scratch_eligible_at, record.central_job_ids, None)
        ]
        if (
            record.delivered_at is not None
            and record.evidence_eligible_at is not None
            and now >= record.evidence_eligible_at
            and record.bundle_digest is not None
        ):
            phases.append(("evidence", record.evidence_eligible_at, (), record.bundle_digest))
        for phase, eligible, jobs, bundle in phases:
            identity_fields = {
                "eventId": record.event_id,
                "taskKey": record.task_key,
                "revisionDigest": record.revision_digest,
                "targetPhase": phase,
                "eligibleAt": eligible,
                "jobIds": list(jobs),
                "bundleDigest": bundle,
                "resultDigests": list(record.result_digests),
            }
            candidate = PreparedTombstone(
                hashlib.sha256(_canonical(identity_fields)).hexdigest(),
                record.event_id,
                record.task_key,
                record.revision_digest,
                phase,
                eligible,
                jobs,
                bundle,
                result_digests=record.result_digests,
            )
            if completed is not None:
                try:
                    done = completed(candidate)
                except Exception as error:
                    raise RetentionError("retention completion predicate failed") from error
                if type(done) is not bool:
                    raise RetentionError("retention completion predicate is invalid")
                if done:
                    continue
            plans.append(candidate)
            if len(plans) >= limit:
                return tuple(plans)
    return tuple(plans)


def decode_prepared(raw: bytes) -> PreparedTombstone:
    fields = _decode(raw, "prepared", (_prepared_keys(), _lab_prepared_keys()))
    value = _prepared_from_fields(fields)
    if raw != value.as_json():
        raise RetentionError("retention metadata is not canonical")
    return value


def decode_committed(raw: bytes) -> CommittedTombstone:
    fields = _decode(
        raw,
        "committed",
        (
            _prepared_keys() | {"committedAt"},
            _lab_prepared_keys() | {"committedAt"},
        ),
    )
    prepared = _prepared_from_fields(fields)
    committed = fields.get("committedAt")
    if type(committed) is not int or committed < prepared.eligible_at:
        raise RetentionError("committed tombstone is invalid")
    value = CommittedTombstone(prepared, committed)
    if raw != value.as_json():
        raise RetentionError("retention metadata is not canonical")
    return value


def decode_ack(raw: bytes) -> AgentRetentionAck:
    fields = _decode(raw, "ack", {"tombstoneId", "committedAt", "acknowledgedAt"})
    tombstone, committed, acknowledged = (
        fields.get("tombstoneId"),
        fields.get("committedAt"),
        fields.get("acknowledgedAt"),
    )
    if (
        not isinstance(tombstone, str)
        or _DIGEST.fullmatch(tombstone) is None
        or type(committed) is not int
        or type(acknowledged) is not int
        or committed < 0
        or acknowledged < committed
    ):
        raise RetentionError("retention acknowledgement is invalid")
    value = AgentRetentionAck(tombstone, committed, acknowledged)
    if raw != value.as_json():
        raise RetentionError("retention metadata is not canonical")
    return value


def _prepared_keys() -> set[str]:
    return {
        "tombstoneId",
        "eventId",
        "taskKey",
        "revisionDigest",
        "targetPhase",
        "eligibleAt",
        "jobIds",
        "bundleDigest",
        "resultDigests",
    }


def _lab_prepared_keys() -> set[str]:
    return _prepared_keys() | {
        "executionFamily",
        "barrierIds",
        "dispatchId",
        "dispatchDigest",
    }


def _prepared_from_fields(fields: dict[str, object]) -> PreparedTombstone:
    tombstone = fields.get("tombstoneId")
    event, task, revision = (
        fields.get("eventId"),
        fields.get("taskKey"),
        fields.get("revisionDigest"),
    )
    phase, eligible, jobs, bundle = (
        fields.get("targetPhase"),
        fields.get("eligibleAt"),
        fields.get("jobIds"),
        fields.get("bundleDigest"),
    )
    family = fields.get("executionFamily", "central")
    barriers = fields.get("barrierIds", [])
    dispatch_id = fields.get("dispatchId")
    dispatch_digest = fields.get("dispatchDigest")
    result_digests = fields.get("resultDigests")
    if (
        not isinstance(tombstone, str)
        or _DIGEST.fullmatch(tombstone) is None
        or not isinstance(event, str)
        or _EVENT.fullmatch(event) is None
        or not isinstance(task, str)
        or _TASK.fullmatch(task) is None
        or not isinstance(revision, str)
        or _REVISION.fullmatch(revision) is None
        or phase not in {"scratch", "evidence"}
        or type(eligible) is not int
        or eligible < 0
        or not isinstance(jobs, list)
        or (phase == "scratch" and not 1 <= len(jobs) <= 3)
        or (phase == "evidence" and len(jobs) != 0)
        or not all(isinstance(job, str) and _DIGEST.fullmatch(job) for job in jobs)
        or len(set(cast(list[str], jobs))) != len(jobs)
        or not isinstance(result_digests, list)
        or not 1 <= len(result_digests) <= 3
        or not all(
            isinstance(item, str) and _DIGEST.fullmatch(item)
            for item in result_digests
        )
        or (phase == "scratch" and len(result_digests) != len(jobs))
        or (
            (phase == "scratch" and bundle is not None)
            or (
                phase == "evidence"
                and (not isinstance(bundle, str) or _DIGEST.fullmatch(bundle) is None)
            )
        )
        or family not in {"central", "lab"}
        or not isinstance(barriers, list)
        or not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in barriers)
        or len(set(cast(list[str], barriers))) != len(barriers)
        or (
            family == "central"
            and (barriers or dispatch_id is not None or dispatch_digest is not None)
        )
        or (
            family == "lab"
            and (
                phase != "scratch"
                or bundle is not None
                or not len(jobs) <= len(barriers) <= 2
                or cast(list[str], barriers)[: len(cast(list[str], jobs))]
                != cast(list[str], jobs)
                or (dispatch_id is None) != (dispatch_digest is None)
                or (
                    dispatch_id is not None
                    and (
                        not isinstance(dispatch_id, str)
                        or _DIGEST.fullmatch(dispatch_id) is None
                        or dispatch_id != cast(list[str], barriers)[-1]
                        or not isinstance(dispatch_digest, str)
                        or _DIGEST.fullmatch(dispatch_digest) is None
                    )
                )
            )
        )
    ):
        raise RetentionError("prepared tombstone is invalid")
    canonical_fields = {
        "eventId": event,
        "taskKey": task,
        "revisionDigest": revision,
        "targetPhase": phase,
        "eligibleAt": eligible,
        "jobIds": jobs,
        "bundleDigest": bundle,
        "resultDigests": result_digests,
    }
    if family == "lab":
        canonical_fields.update(
            {
                "executionFamily": "lab",
                "barrierIds": barriers,
                "dispatchId": dispatch_id,
                "dispatchDigest": dispatch_digest,
            }
        )
    if hashlib.sha256(_canonical(canonical_fields)).hexdigest() != tombstone:
        raise RetentionError("prepared tombstone identity is invalid")
    if event != _event_id(task, revision):
        raise RetentionError("prepared tombstone identity is invalid")
    return PreparedTombstone(
        tombstone,
        event,
        task,
        revision,
        phase,
        eligible,
        tuple(cast(list[str], jobs)),
        cast(str | None, bundle),
        family,
        tuple(cast(list[str], barriers)),
        cast(str | None, dispatch_id),
        cast(str | None, dispatch_digest),
        tuple(cast(list[str], result_digests)),
    )


def _encode(state: str, fields: dict[str, object]) -> bytes:
    return _canonical({"kind": "moodle-retention-v1", "state": state, **fields})


def _decode(
    raw: bytes, state: str, expected: set[str] | tuple[set[str], ...]
) -> dict[str, object]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= _MAX_TOMBSTONE_BYTES:
        raise RetentionError("retention metadata is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionError("retention metadata is invalid") from error
    expected_sets = (expected,) if isinstance(expected, set) else expected
    if not isinstance(value, dict) or set(value) not in tuple(
        {"kind", "state"} | item for item in expected_sets
    ):
        raise RetentionError("retention metadata is invalid")
    if value.get("kind") != "moodle-retention-v1" or value.get("state") != state:
        raise RetentionError("retention metadata is invalid")
    return value


def _no_duplicate_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if not isinstance(key, str) or key in value:
            raise RetentionError("retention metadata is invalid")
        value[key] = item
    return value


def _validate_record(record: RetentionRecord) -> None:
    if not isinstance(record, RetentionRecord):
        raise RetentionError("retention record is invalid")
    if (
        _EVENT.fullmatch(record.event_id) is None
        or _TASK.fullmatch(record.task_key) is None
        or _REVISION.fullmatch(record.revision_digest) is None
        or record.event_id != _event_id(record.task_key, record.revision_digest)
        or type(record.selected_mode) is not ExecutionMode
        or record.work_status not in {"ready", "failed", "cleaned"}
        or type(record.succeeded) is not bool
        or type(record.outbox_created_at) is not int
        or type(record.scratch_eligible_at) is not int
        or record.outbox_created_at < 0
        or record.scratch_eligible_at < record.outbox_created_at
        or (record.delivered_at is None) != (record.evidence_eligible_at is None)
        or (record.delivered_at is not None and type(record.delivered_at) is not int)
        or (
            record.evidence_eligible_at is not None and type(record.evidence_eligible_at) is not int
        )
    ):
        raise RetentionError("retention record is inconsistent")
    if (
        record.delivered_at is not None
        and record.evidence_eligible_at is not None
        and (
            record.delivered_at < record.outbox_created_at
            or record.evidence_eligible_at < record.delivered_at
        )
    ):
        raise RetentionError("retention record is inconsistent")
    central = record.selected_mode.value == "central"
    if central and record.work_status != "cleaned":
        raise RetentionError("retention record is inconsistent")
    if type(record.central_job_ids) is not tuple:
        raise RetentionError("retention record is inconsistent")
    if (
        type(record.result_digests) is not tuple
        or len(record.result_digests) != len(record.central_job_ids)
        or not all(type(item) is str and _DIGEST.fullmatch(item) for item in record.result_digests)
    ):
        raise RetentionError("retention record is inconsistent")
    if central and record.execution_family != "central":
        raise RetentionError("retention record is inconsistent")
    if central and record.succeeded:
        if (
            len(record.central_job_ids) != 3
            or not all(type(job) is str for job in record.central_job_ids)
            or len(set(record.central_job_ids)) != 3
            or not all(_DIGEST.fullmatch(job) for job in record.central_job_ids)
            or type(record.bundle_digest) is not str
            or _DIGEST.fullmatch(record.bundle_digest) is None
        ):
            raise RetentionError("retention record is inconsistent")
    elif central:
        if (
            not 1 <= len(record.central_job_ids) <= 3
            or not all(type(job) is str for job in record.central_job_ids)
            or len(set(record.central_job_ids)) != len(record.central_job_ids)
            or not all(_DIGEST.fullmatch(job) for job in record.central_job_ids)
            or (
                record.bundle_digest is not None
                and (
                    type(record.bundle_digest) is not str
                    or _DIGEST.fullmatch(record.bundle_digest) is None
                )
            )
        ):
            raise RetentionError("retention record is inconsistent")
    elif record.execution_family == "lab":
        if (
            record.selected_mode.value not in {"hybrid", "in_guest"}
            or record.work_status != "cleaned"
            or not 1 <= len(record.central_job_ids) <= 2
            or not all(
                type(job) is str and _DIGEST.fullmatch(job)
                for job in record.central_job_ids
            )
            or type(record.barrier_ids) is not tuple
            or not len(record.central_job_ids) <= len(record.barrier_ids) <= 2
            or record.barrier_ids[: len(record.central_job_ids)] != record.central_job_ids
            or len(set(record.barrier_ids)) != len(record.barrier_ids)
            or not all(type(job) is str and _DIGEST.fullmatch(job) for job in record.barrier_ids)
            or record.bundle_digest is not None
            or (record.dispatch_id is None) != (record.dispatch_digest is None)
            or (
                record.dispatch_id is not None
                and (
                    record.dispatch_id != record.barrier_ids[-1]
                    or _DIGEST.fullmatch(record.dispatch_id) is None
                    or not isinstance(record.dispatch_digest, str)
                    or _DIGEST.fullmatch(record.dispatch_digest) is None
                )
            )
        ):
            raise RetentionError("retention record is inconsistent")
    elif record.central_job_ids or record.bundle_digest is not None:
        raise RetentionError("retention record is inconsistent")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
