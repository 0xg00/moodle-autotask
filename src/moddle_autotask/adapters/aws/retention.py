"""Pure retention planning codecs; Phase A deliberately performs no filesystem mutation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import cast

from moddle_autotask.adapters.moodle.approval_state import RetentionRecord
from moddle_autotask.adapters.moodle.state import _event_id
from moddle_autotask.domain.models import ExecutionMode

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
    scratch_eligible_at: int
    evidence_eligible_at: int | None
    job_ids: tuple[str, ...]
    bundle_digest: str | None

    def as_json(self) -> bytes:
        return _encode("prepared", self._fields())

    def _fields(self) -> dict[str, object]:
        return {
            "tombstoneId": self.tombstone_id,
            "eventId": self.event_id,
            "taskKey": self.task_key,
            "revisionDigest": self.revision_digest,
            "scratchEligibleAt": self.scratch_eligible_at,
            "evidenceEligibleAt": self.evidence_eligible_at,
            "jobIds": list(self.job_ids),
            "bundleDigest": self.bundle_digest,
        }


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
    records: tuple[RetentionRecord, ...], *, now: int, limit: int
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
            record.selected_mode.value != "central"
            or record.work_status != "cleaned"
            or now < record.scratch_eligible_at
            or len(record.central_job_ids) != 3
        ):
            continue
        bundle = (
            record.bundle_digest
            if (
                record.delivered_at is not None
                and record.evidence_eligible_at is not None
                and now >= record.evidence_eligible_at
            )
            else None
        )
        identity_fields = {
            "eventId": record.event_id,
            "taskKey": record.task_key,
            "revisionDigest": record.revision_digest,
            "scratchEligibleAt": record.scratch_eligible_at,
            "evidenceEligibleAt": record.evidence_eligible_at,
            "jobIds": list(record.central_job_ids),
            "bundleDigest": bundle,
        }
        tombstone_id = hashlib.sha256(_canonical(identity_fields)).hexdigest()
        plans.append(
            PreparedTombstone(
                tombstone_id,
                record.event_id,
                record.task_key,
                record.revision_digest,
                record.scratch_eligible_at,
                record.evidence_eligible_at,
                record.central_job_ids,
                bundle,
            )
        )
        if len(plans) >= limit:
            break
    return tuple(plans)


def decode_prepared(raw: bytes) -> PreparedTombstone:
    fields = _decode(raw, "prepared", _prepared_keys())
    return _prepared_from_fields(fields)


def decode_committed(raw: bytes) -> CommittedTombstone:
    fields = _decode(raw, "committed", _prepared_keys() | {"committedAt"})
    prepared = _prepared_from_fields(fields)
    committed = fields.get("committedAt")
    if (
        type(committed) is not int
        or committed < prepared.scratch_eligible_at
        or (
            prepared.bundle_digest is not None
            and (prepared.evidence_eligible_at is None or committed < prepared.evidence_eligible_at)
        )
    ):
        raise RetentionError("committed tombstone is invalid")
    return CommittedTombstone(prepared, committed)


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
    return AgentRetentionAck(tombstone, committed, acknowledged)


def _prepared_keys() -> set[str]:
    return {
        "tombstoneId",
        "eventId",
        "taskKey",
        "revisionDigest",
        "scratchEligibleAt",
        "evidenceEligibleAt",
        "jobIds",
        "bundleDigest",
    }


def _prepared_from_fields(fields: dict[str, object]) -> PreparedTombstone:
    tombstone = fields.get("tombstoneId")
    event, task, revision = (
        fields.get("eventId"),
        fields.get("taskKey"),
        fields.get("revisionDigest"),
    )
    scratch, evidence, jobs, bundle = (
        fields.get("scratchEligibleAt"),
        fields.get("evidenceEligibleAt"),
        fields.get("jobIds"),
        fields.get("bundleDigest"),
    )
    if (
        not isinstance(tombstone, str)
        or _DIGEST.fullmatch(tombstone) is None
        or not isinstance(event, str)
        or _EVENT.fullmatch(event) is None
        or not isinstance(task, str)
        or _TASK.fullmatch(task) is None
        or not isinstance(revision, str)
        or _REVISION.fullmatch(revision) is None
        or type(scratch) is not int
        or scratch < 0
        or (evidence is not None and (type(evidence) is not int or evidence < 0))
        or not isinstance(jobs, list)
        or len(jobs) != 3
        or not all(isinstance(job, str) and _DIGEST.fullmatch(job) for job in jobs)
        or len(set(cast(list[str], jobs))) != len(jobs)
        or (
            bundle is not None
            and (not isinstance(bundle, str) or _DIGEST.fullmatch(bundle) is None)
        )
        or (bundle is not None and evidence is None)
    ):
        raise RetentionError("prepared tombstone is invalid")
    canonical_fields = {
        "eventId": event,
        "taskKey": task,
        "revisionDigest": revision,
        "scratchEligibleAt": scratch,
        "evidenceEligibleAt": evidence,
        "jobIds": jobs,
        "bundleDigest": bundle,
    }
    if hashlib.sha256(_canonical(canonical_fields)).hexdigest() != tombstone:
        raise RetentionError("prepared tombstone identity is invalid")
    if event != _event_id(task, revision):
        raise RetentionError("prepared tombstone identity is invalid")
    return PreparedTombstone(
        tombstone,
        event,
        task,
        revision,
        scratch,
        evidence,
        tuple(cast(list[str], jobs)),
        bundle,
    )


def _encode(state: str, fields: dict[str, object]) -> bytes:
    return _canonical({"kind": "moodle-retention-v1", "state": state, **fields})


def _decode(raw: bytes, state: str, expected: set[str]) -> dict[str, object]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= _MAX_TOMBSTONE_BYTES:
        raise RetentionError("retention metadata is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionError("retention metadata is invalid") from error
    if not isinstance(value, dict) or set(value) != {"kind", "state"} | expected:
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
            record.evidence_eligible_at is not None
            and type(record.evidence_eligible_at) is not int
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
    elif record.central_job_ids or record.bundle_digest is not None:
        raise RetentionError("retention record is inconsistent")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
