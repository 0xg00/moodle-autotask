"""Minimal side-effecting application boundary for one task lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field

from moodle_autotask.domain.models import (
    ApprovalCheckpoint,
    ApprovalRecord,
    Digest,
    ExecutionRequest,
    ExecutionResult,
    LabHandle,
    LabProvisionRequest,
    SubmissionIntent,
    TaskId,
    WorkflowRevision,
)
from moodle_autotask.domain.state_machine import TaskState, transition
from moodle_autotask.ports.contracts import (
    AgentRuntime,
    LabProvider,
    LabReadiness,
    SubmissionReceipt,
    TaskSubmitter,
)


class ApprovalValidationError(ValueError):
    """Raised when an approval cannot authorize the exact requested action."""


class LabNotReadyError(RuntimeError):
    """Raised when a provider does not report a provisioned lab as ready."""


class IdempotencyConflictError(ValueError):
    """Raised when a retry changes the request or key bound to an in-flight action."""


@dataclass(slots=True, init=False)
class TaskOrchestrator:
    """Coordinates validated state into port calls; it contains no provider implementation."""

    _task_id: TaskId = field(repr=False)
    _workflow_revision: WorkflowRevision = field(repr=False)
    lab_provider: LabProvider
    agent_runtime: AgentRuntime
    task_submitter: TaskSubmitter
    state: TaskState = TaskState.DISCOVERED
    lab_handle: LabHandle | None = None
    _start_approval: ApprovalRecord | None = field(default=None, init=False, repr=False)
    _submit_approval: ApprovalRecord | None = field(default=None, init=False, repr=False)
    _provision_request: LabProvisionRequest | None = field(default=None, init=False, repr=False)
    _provision_idempotency_key: str | None = field(default=None, init=False, repr=False)
    _cleanup_idempotency_key: str | None = field(default=None, init=False, repr=False)
    _approved_submission_intent: SubmissionIntent | None = field(default=None, repr=False)

    def __init__(
        self,
        task_id: TaskId,
        workflow_revision: WorkflowRevision,
        lab_provider: LabProvider,
        agent_runtime: AgentRuntime,
        task_submitter: TaskSubmitter,
        state: TaskState = TaskState.DISCOVERED,
        lab_handle: LabHandle | None = None,
    ) -> None:
        self._task_id = task_id
        self._workflow_revision = workflow_revision
        self.lab_provider = lab_provider
        self.agent_runtime = agent_runtime
        self.task_submitter = task_submitter
        self.state = state
        self.lab_handle = lab_handle
        self._start_approval = None
        self._submit_approval = None
        self._provision_request = None
        self._provision_idempotency_key = None
        self._cleanup_idempotency_key = None
        self._approved_submission_intent = None

    @property
    def task_id(self) -> TaskId:
        """The task identity is fixed when this orchestrator is created."""
        return self._task_id

    @property
    def workflow_revision(self) -> WorkflowRevision:
        """The workflow revision is fixed when this orchestrator is created."""
        return self._workflow_revision

    def request_start_approval(self) -> None:
        self.state = transition(self.state, TaskState.AWAITING_START_APPROVAL)

    def approve_start(self, approval: ApprovalRecord, *, expected_digest: Digest) -> None:
        next_state = transition(self.state, TaskState.PLANNING)
        self._validate_approval(approval, ApprovalCheckpoint.START_WORK, expected_digest)
        self._start_approval = approval
        self.state = next_state

    def provision_lab(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle:
        if request.task_id != self.task_id or request.workflow_revision != self.workflow_revision:
            raise ApprovalValidationError(
                "provision request is for a different task or workflow revision"
        )
        self._require_start_approval(request.specification_digest)
        if self.state is TaskState.PLANNING:
            self._provision_request = request
            self._provision_idempotency_key = idempotency_key
            self.state = transition(self.state, TaskState.PROVISIONING)
        elif self.state is TaskState.PROVISIONING:
            if (
                request != self._provision_request
                or idempotency_key != self._provision_idempotency_key
            ):
                raise IdempotencyConflictError(
                    "provision retry must use the original request and key"
                )
        elif self.state is TaskState.RUNNING:
            if (
                request != self._provision_request
                or idempotency_key != self._provision_idempotency_key
            ):
                raise IdempotencyConflictError(
                    "provision replay must use the original request and key"
                )
            if self.lab_handle is None:
                raise RuntimeError("successful provision has no retained lab handle")
            return self.lab_handle
        else:
            transition(self.state, TaskState.PROVISIONING)
        self.lab_handle = self.lab_provider.provision(request, idempotency_key=idempotency_key)
        if self.lab_provider.readiness(self.lab_handle) is not LabReadiness.READY:
            self.state = transition(self.state, TaskState.FAILED)
            raise LabNotReadyError("provisioned lab is not ready")
        self.state = transition(self.state, TaskState.RUNNING)
        return self.lab_handle

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.task_id != self.task_id or request.workflow_revision != self.workflow_revision:
            raise ApprovalValidationError(
                "execution request is for a different task or workflow revision"
            )
        self._require_start_approval(request.execution_digest)
        self.state = transition(self.state, TaskState.COLLECTING)
        result = self.agent_runtime.execute(request, self.lab_handle)
        if not result.succeeded:
            self.state = transition(self.state, TaskState.FAILED)
            return result
        self.state = transition(self.state, TaskState.REVIEW_READY)
        return result

    def request_submit_approval(self) -> None:
        self.state = transition(self.state, TaskState.AWAITING_SUBMIT_APPROVAL)

    def approve_submission(self, approval: ApprovalRecord, *, intent: SubmissionIntent) -> None:
        transition(self.state, TaskState.SUBMITTING)
        self._validate_approval(approval, ApprovalCheckpoint.SUBMIT, intent.submission_digest)
        if intent.task_id != self.task_id or intent.workflow_revision != self.workflow_revision:
            raise ApprovalValidationError(
                "submission intent is for a different task or workflow revision"
            )
        self._submit_approval = approval
        self._approved_submission_intent = intent

    def submit(self, intent: SubmissionIntent) -> SubmissionReceipt:
        if self._submit_approval is None or self._approved_submission_intent is None:
            raise ApprovalValidationError("submission requires a matching submit approval")
        if intent != self._approved_submission_intent:
            raise ApprovalValidationError(
                "submission intent differs from the approved immutable intent"
            )
        self._validate_approval(
            self._submit_approval, ApprovalCheckpoint.SUBMIT, intent.submission_digest
        )
        if intent.task_id != self.task_id or intent.workflow_revision != self.workflow_revision:
            raise ApprovalValidationError(
                "submission intent is for a different task or workflow revision"
            )
        self.state = transition(self.state, TaskState.SUBMITTING)
        receipt = self.task_submitter.submit(intent)
        self.state = transition(self.state, TaskState.COMPLETED)
        return receipt

    def cleanup(self, *, idempotency_key: str) -> None:
        if self.state is TaskState.CLEANED:
            if idempotency_key != self._cleanup_idempotency_key:
                raise IdempotencyConflictError(
                    "cleanup retry must use the original idempotency key"
                )
            return
        if self.state is TaskState.CLEANING_UP:
            if idempotency_key != self._cleanup_idempotency_key:
                raise IdempotencyConflictError(
                    "cleanup retry must use the original idempotency key"
                )
        else:
            self.state = transition(self.state, TaskState.CLEANING_UP)
            self._cleanup_idempotency_key = idempotency_key
        if self.lab_handle is None and self._provision_request is not None:
            if self._provision_idempotency_key is None:
                raise RuntimeError("in-flight provision has no idempotency key for reconciliation")
            self.lab_handle = self.lab_provider.reconcile(
                self._provision_request,
                idempotency_key=self._provision_idempotency_key,
            )
        if self.lab_handle is not None:
            self.lab_provider.teardown(self.lab_handle, idempotency_key=idempotency_key)
        self.state = transition(self.state, TaskState.CLEANED)

    def _require_start_approval(self, expected_digest: Digest) -> None:
        if self._start_approval is None:
            raise ApprovalValidationError("start approval does not match the approved plan digest")
        try:
            self._validate_approval(
                self._start_approval, ApprovalCheckpoint.START_WORK, expected_digest
            )
        except ApprovalValidationError as error:
            raise ApprovalValidationError(
                "start approval does not match the approved plan digest"
            ) from error

    def _validate_approval(
        self,
        approval: ApprovalRecord,
        checkpoint: ApprovalCheckpoint,
        expected_digest: Digest,
    ) -> None:
        if (
            not approval.approved
            or approval.checkpoint is not checkpoint
            or approval.task_id != self.task_id
            or approval.workflow_revision != self.workflow_revision
            or approval.digest != expected_digest
        ):
            raise ApprovalValidationError("approval does not match the immutable task action")
