"""Narrow provider-neutral interfaces for future adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from moodle_autotask.domain.models import (
    ExecutionRequest,
    ExecutionResult,
    LabHandle,
    LabProvisionRequest,
    SubmissionIntent,
    TaskId,
)


class LabReadiness(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    reference: str


class AgentRuntime(Protocol):
    """Executes only a capability-limited request and an opaque lab handle."""

    def execute(
        self, request: ExecutionRequest, lab_handle: LabHandle | None
    ) -> ExecutionResult: ...


class LabProvider(Protocol):
    """Idempotent lab operations keyed by the immutable provision request and caller key."""

    def provision(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle: ...

    def reconcile(
        self, request: LabProvisionRequest, *, idempotency_key: str
    ) -> LabHandle | None: ...

    def readiness(self, handle: LabHandle) -> LabReadiness: ...

    def teardown(self, handle: LabHandle, *, idempotency_key: str) -> None: ...


class TaskSubmitter(Protocol):
    def submit(self, intent: SubmissionIntent) -> SubmissionReceipt: ...


class TaskSource(Protocol):
    def discover(self) -> tuple[TaskId, ...]: ...


class ArtifactCollector(Protocol):
    def collect(self, task_id: TaskId) -> ExecutionResult: ...


class NotificationProvider(Protocol):
    def notify(self, task_id: TaskId, message: str) -> None: ...
