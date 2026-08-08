"""Immutable values that cross the task lifecycle boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from math import isfinite


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("JSON digest values must not contain non-finite floats")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON digest object keys must be strings")
            _validate_json_value(item)
        return
    raise ValueError("JSON digest value is not JSON-compatible")


@dataclass(frozen=True, slots=True)
class TaskId:
    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, "task id")


@dataclass(frozen=True, slots=True)
class WorkflowRevision:
    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, "workflow revision")


@dataclass(frozen=True, slots=True)
class Digest:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 64 or any(char not in "0123456789abcdef" for char in self.value):
            raise ValueError("digest must be a lowercase SHA-256 hexadecimal value")

    @classmethod
    def of_json(cls, value: object) -> Digest:
        _validate_json_value(value)
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return cls(sha256(canonical.encode("utf-8")).hexdigest())


@dataclass(frozen=True, slots=True)
class LabHandle:
    """Opaque lab identity; provider configuration is deliberately excluded."""

    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, "lab handle")


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    uri: str
    digest: Digest

    def __post_init__(self) -> None:
        _require_text(self.uri, "artifact URI")


@dataclass(frozen=True, slots=True)
class ManifestReference:
    uri: str
    digest: Digest

    def __post_init__(self) -> None:
        _require_text(self.uri, "manifest URI")


class ApprovalCheckpoint(StrEnum):
    START_WORK = "start_work"
    SUBMIT = "submit"


class ExecutionMode(StrEnum):
    CENTRAL = "central"
    IN_GUEST = "in_guest"
    HYBRID = "hybrid"
    AUTO = "auto"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    checkpoint: ApprovalCheckpoint
    task_id: TaskId
    workflow_revision: WorkflowRevision
    digest: Digest
    approver: str
    approved: bool = True

    def __post_init__(self) -> None:
        _require_text(self.approver, "approver")


@dataclass(frozen=True, slots=True)
class LabProvisionRequest:
    task_id: TaskId
    workflow_revision: WorkflowRevision
    requested_mode: ExecutionMode
    specification_digest: Digest


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    task_id: TaskId
    workflow_revision: WorkflowRevision
    execution_digest: Digest
    selected_mode: ExecutionMode
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, frozenset):
            object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if self.selected_mode is ExecutionMode.AUTO:
            raise ValueError("execution mode must be selected before runtime execution")
        if any(not capability.strip() for capability in self.capabilities):
            raise ValueError("capabilities must not contain blank values")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    succeeded: bool
    artifacts: tuple[ArtifactReference, ...] = ()
    manifest: ManifestReference | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.artifacts, tuple):
            object.__setattr__(self, "artifacts", tuple(self.artifacts))


@dataclass(frozen=True, slots=True)
class SubmissionIntent:
    task_id: TaskId
    workflow_revision: WorkflowRevision
    submission_digest: Digest
    manifest: ManifestReference
    artifacts: tuple[ArtifactReference, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.artifacts, tuple):
            object.__setattr__(self, "artifacts", tuple(self.artifacts))
