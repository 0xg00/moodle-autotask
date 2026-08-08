from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from moddle_autotask.application.orchestrator import (
    ApprovalValidationError,
    IdempotencyConflictError,
    TaskOrchestrator,
)
from moddle_autotask.domain.models import (
    ApprovalCheckpoint,
    ApprovalRecord,
    ArtifactReference,
    Digest,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    LabHandle,
    LabProvisionRequest,
    ManifestReference,
    SubmissionIntent,
    TaskId,
    WorkflowRevision,
)
from moddle_autotask.domain.state_machine import InvalidTaskTransitionError, TaskState
from moddle_autotask.ports.contracts import LabReadiness, SubmissionReceipt


@dataclass
class FakeLabProvider:
    provision_calls: int = 0
    fail_next_provision: bool = False
    provisioned_handles: dict[tuple[LabProvisionRequest, str], LabHandle] = field(
        default_factory=dict
    )
    reconcile_calls: int = 0
    fail_next_reconcile: bool = False
    return_none_on_next_reconcile: bool = False
    teardown_keys: set[tuple[LabHandle, str]] = field(default_factory=set)
    teardown_attempts: int = 0
    fail_next_teardown: bool = False

    def provision(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle:
        self.provision_calls += 1
        handle = LabHandle(f"lab:{request.task_id.value}:{idempotency_key}")
        self.provisioned_handles[(request, idempotency_key)] = handle
        if self.fail_next_provision:
            self.fail_next_provision = False
            raise RuntimeError("ambiguous provision failure")
        return handle

    def reconcile(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle | None:
        self.reconcile_calls += 1
        if self.fail_next_reconcile:
            self.fail_next_reconcile = False
            raise RuntimeError("transient reconciliation failure")
        if self.return_none_on_next_reconcile:
            self.return_none_on_next_reconcile = False
            return None
        return self.provisioned_handles[(request, idempotency_key)]

    def readiness(self, handle: LabHandle) -> LabReadiness:
        return LabReadiness.READY

    def teardown(self, handle: LabHandle, *, idempotency_key: str) -> None:
        self.teardown_attempts += 1
        if self.fail_next_teardown:
            self.fail_next_teardown = False
            raise RuntimeError("transient teardown failure")
        self.teardown_keys.add((handle, idempotency_key))


@dataclass
class FakeRuntime:
    result: ExecutionResult
    calls: int = 0

    def execute(self, request: ExecutionRequest, lab_handle: LabHandle | None) -> ExecutionResult:
        self.calls += 1
        return self.result


@dataclass
class FakeSubmitter:
    intents: list[SubmissionIntent] = field(default_factory=list)

    def submit(self, intent: SubmissionIntent) -> SubmissionReceipt:
        self.intents.append(intent)
        return SubmissionReceipt("fake:submission")


def _digest(value: str) -> Digest:
    return Digest.of_json({"value": value})


def _orchestrator() -> tuple[TaskOrchestrator, FakeLabProvider, FakeRuntime, FakeSubmitter]:
    manifest = ManifestReference("memory://manifest", _digest("manifest"))
    runtime = FakeRuntime(ExecutionResult(True, manifest=manifest))
    lab = FakeLabProvider()
    submitter = FakeSubmitter()
    return (
        TaskOrchestrator(TaskId("task-1"), WorkflowRevision("rev-1"), lab, runtime, submitter),
        lab,
        runtime,
        submitter,
    )


def _start_approval(digest: Digest, *, revision: str = "rev-1") -> ApprovalRecord:
    return ApprovalRecord(
        ApprovalCheckpoint.START_WORK,
        TaskId("task-1"),
        WorkflowRevision(revision),
        digest,
        "reviewer-a",
    )


def _advance_to_review(orchestrator: TaskOrchestrator) -> None:
    planning_digest = _digest("plan")
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(planning_digest), expected_digest=planning_digest)
    orchestrator.provision_lab(
        LabProvisionRequest(
            TaskId("task-1"),
            WorkflowRevision("rev-1"),
            ExecutionMode.IN_GUEST,
            planning_digest,
        ),
        idempotency_key="provision-1",
    )
    orchestrator.execute(
        ExecutionRequest(
            TaskId("task-1"),
            WorkflowRevision("rev-1"),
            planning_digest,
            ExecutionMode.IN_GUEST,
            frozenset({"workspace:write"}),
        )
    )


def test_stale_or_mismatched_start_approval_is_rejected() -> None:
    orchestrator, _, _, _ = _orchestrator()
    orchestrator.request_start_approval()

    with pytest.raises(ApprovalValidationError, match="does not match"):
        orchestrator.approve_start(
            _start_approval(_digest("old")), expected_digest=_digest("current")
        )

    with pytest.raises(ApprovalValidationError, match="does not match"):
        orchestrator.approve_start(
            _start_approval(_digest("current"), revision="rev-old"),
            expected_digest=_digest("current"),
        )

    assert orchestrator.state is TaskState.AWAITING_START_APPROVAL


def test_invalid_state_start_approval_is_not_retained() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")

    with pytest.raises(InvalidTaskTransitionError, match="cannot transition"):
        orchestrator.approve_start(
            _start_approval(approved_digest), expected_digest=approved_digest
        )

    orchestrator.request_start_approval()
    with pytest.raises(ApprovalValidationError, match="start approval"):
        orchestrator.provision_lab(
            LabProvisionRequest(
                TaskId("task-1"),
                WorkflowRevision("rev-1"),
                ExecutionMode.IN_GUEST,
                approved_digest,
            ),
            idempotency_key="provision-1",
        )

    assert provider.provision_calls == 0


def test_orchestrator_identity_properties_cannot_be_reassigned() -> None:
    orchestrator, _, _, _ = _orchestrator()

    with pytest.raises(AttributeError):
        orchestrator.task_id = TaskId("other-task")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        orchestrator.workflow_revision = WorkflowRevision("other-revision")  # type: ignore[misc]

    assert orchestrator.task_id == TaskId("task-1")
    assert orchestrator.workflow_revision == WorkflowRevision("rev-1")


def test_invalid_state_execute_does_not_call_runtime() -> None:
    orchestrator, _, runtime, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)

    with pytest.raises(InvalidTaskTransitionError, match="cannot transition"):
        orchestrator.execute(
            ExecutionRequest(
                TaskId("task-1"),
                WorkflowRevision("rev-1"),
                approved_digest,
                ExecutionMode.IN_GUEST,
            )
        )

    assert runtime.calls == 0
    assert orchestrator.state is TaskState.PLANNING


def test_submit_is_blocked_without_matching_approval() -> None:
    orchestrator, _, _, submitter = _orchestrator()
    _advance_to_review(orchestrator)
    intent = _submission_intent()
    orchestrator.request_submit_approval()

    with pytest.raises(ApprovalValidationError, match="requires"):
        orchestrator.submit(intent)

    assert submitter.intents == []
    assert orchestrator.state is TaskState.AWAITING_SUBMIT_APPROVAL


def test_matching_submit_approval_allows_submission() -> None:
    orchestrator, _, _, submitter = _orchestrator()
    _advance_to_review(orchestrator)
    intent = _submission_intent()
    orchestrator.request_submit_approval()
    orchestrator.approve_submission(
        ApprovalRecord(
            ApprovalCheckpoint.SUBMIT,
            TaskId("task-1"),
            WorkflowRevision("rev-1"),
            intent.submission_digest,
            "reviewer-b",
        ),
        intent=intent,
    )

    receipt = orchestrator.submit(intent)

    assert receipt.reference == "fake:submission"
    assert submitter.intents == [intent]
    assert orchestrator.state is TaskState.COMPLETED


def test_submission_intent_must_match_the_exact_approved_intent() -> None:
    orchestrator, _, _, submitter = _orchestrator()
    _advance_to_review(orchestrator)
    intent = _submission_intent()
    orchestrator.request_submit_approval()
    orchestrator.approve_submission(
        ApprovalRecord(
            ApprovalCheckpoint.SUBMIT,
            TaskId("task-1"),
            WorkflowRevision("rev-1"),
            intent.submission_digest,
            "reviewer-b",
        ),
        intent=intent,
    )
    changed_intent = SubmissionIntent(
        intent.task_id,
        intent.workflow_revision,
        intent.submission_digest,
        ManifestReference("memory://changed-manifest", _digest("changed-manifest")),
        (ArtifactReference("memory://changed-artifact", _digest("changed-artifact")),),
    )

    with pytest.raises(ApprovalValidationError, match="differs"):
        orchestrator.submit(changed_intent)

    assert submitter.intents == []
    assert orchestrator.state is TaskState.AWAITING_SUBMIT_APPROVAL


def test_early_submission_approval_is_rejected_and_not_retained() -> None:
    orchestrator, _, _, _ = _orchestrator()
    _advance_to_review(orchestrator)
    intent = _submission_intent()
    approval = ApprovalRecord(
        ApprovalCheckpoint.SUBMIT,
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        intent.submission_digest,
        "reviewer-b",
    )

    with pytest.raises(InvalidTaskTransitionError, match="cannot transition"):
        orchestrator.approve_submission(approval, intent=intent)

    orchestrator.request_submit_approval()
    with pytest.raises(ApprovalValidationError, match="requires"):
        orchestrator.submit(intent)


def test_fake_teardown_is_idempotent_for_a_retried_key() -> None:
    _, provider, _, _ = _orchestrator()
    handle = LabHandle("lab:test")

    provider.teardown(handle, idempotency_key="cleanup-1")
    provider.teardown(handle, idempotency_key="cleanup-1")

    assert provider.teardown_keys == {(handle, "cleanup-1")}


def test_mismatched_provision_digest_does_not_call_provider() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)

    with pytest.raises(ApprovalValidationError, match="start approval"):
        orchestrator.provision_lab(
            LabProvisionRequest(
                TaskId("task-1"),
                WorkflowRevision("rev-1"),
                ExecutionMode.IN_GUEST,
                _digest("different-plan"),
            ),
            idempotency_key="provision-1",
        )

    assert provider.provision_calls == 0
    assert orchestrator.state is TaskState.PLANNING


def test_mismatched_execution_digest_does_not_call_runtime() -> None:
    orchestrator, _, runtime, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)
    orchestrator.provision_lab(
        LabProvisionRequest(
            TaskId("task-1"),
            WorkflowRevision("rev-1"),
            ExecutionMode.IN_GUEST,
            approved_digest,
        ),
        idempotency_key="provision-1",
    )

    with pytest.raises(ApprovalValidationError, match="start approval"):
        orchestrator.execute(
            ExecutionRequest(
                TaskId("task-1"),
                WorkflowRevision("rev-1"),
                _digest("different-plan"),
                ExecutionMode.IN_GUEST,
            )
        )

    assert runtime.calls == 0
    assert orchestrator.state is TaskState.RUNNING


def test_mismatched_retained_start_approval_cannot_provision_or_execute() -> None:
    orchestrator, provider, runtime, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    request = LabProvisionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        ExecutionMode.IN_GUEST,
        approved_digest,
    )
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)
    orchestrator._start_approval = ApprovalRecord(
        ApprovalCheckpoint.START_WORK,
        TaskId("other-task"),
        WorkflowRevision("rev-1"),
        approved_digest,
        "reviewer-a",
    )

    with pytest.raises(ApprovalValidationError, match="does not match"):
        orchestrator.provision_lab(request, idempotency_key="provision-1")

    assert provider.provision_calls == 0
    orchestrator._start_approval = _start_approval(approved_digest)
    orchestrator.provision_lab(request, idempotency_key="provision-1")
    orchestrator._start_approval = ApprovalRecord(
        ApprovalCheckpoint.START_WORK,
        TaskId("task-1"),
        WorkflowRevision("other-revision"),
        approved_digest,
        "reviewer-a",
    )

    with pytest.raises(ApprovalValidationError, match="does not match"):
        orchestrator.execute(
            ExecutionRequest(
                TaskId("task-1"),
                WorkflowRevision("rev-1"),
                approved_digest,
                ExecutionMode.IN_GUEST,
            )
        )

    assert runtime.calls == 0


def test_identical_provision_retry_succeeds_after_ambiguous_failure() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    request = LabProvisionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        ExecutionMode.IN_GUEST,
        approved_digest,
    )
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)
    provider.fail_next_provision = True

    with pytest.raises(RuntimeError, match="ambiguous"):
        orchestrator.provision_lab(request, idempotency_key="provision-1")

    handle = orchestrator.provision_lab(request, idempotency_key="provision-1")

    assert handle == LabHandle("lab:task-1:provision-1")
    assert provider.provision_calls == 2
    assert orchestrator.state is TaskState.RUNNING


def test_changed_provision_retry_is_rejected_without_second_provider_call() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    request = LabProvisionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        ExecutionMode.IN_GUEST,
        approved_digest,
    )
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)
    provider.fail_next_provision = True

    with pytest.raises(RuntimeError, match="ambiguous"):
        orchestrator.provision_lab(request, idempotency_key="provision-1")

    with pytest.raises(IdempotencyConflictError, match="original request"):
        orchestrator.provision_lab(request, idempotency_key="provision-2")
    with pytest.raises(IdempotencyConflictError, match="original request"):
        orchestrator.provision_lab(
            LabProvisionRequest(
                request.task_id,
                request.workflow_revision,
                ExecutionMode.HYBRID,
                request.specification_digest,
            ),
            idempotency_key="provision-1",
        )

    assert provider.provision_calls == 1


def test_successful_provision_replay_returns_retained_handle_without_provider_call() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    request = LabProvisionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        ExecutionMode.IN_GUEST,
        approved_digest,
    )
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)

    original_handle = orchestrator.provision_lab(request, idempotency_key="provision-1")
    replay_handle = orchestrator.provision_lab(request, idempotency_key="provision-1")

    assert replay_handle == original_handle
    assert provider.provision_calls == 1
    assert orchestrator.state is TaskState.RUNNING


def test_changed_successful_provision_replay_is_rejected_without_provider_call() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    request = LabProvisionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        ExecutionMode.IN_GUEST,
        approved_digest,
    )
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)
    orchestrator.provision_lab(request, idempotency_key="provision-1")

    with pytest.raises(IdempotencyConflictError, match="original request"):
        orchestrator.provision_lab(request, idempotency_key="provision-2")
    with pytest.raises(IdempotencyConflictError, match="original request"):
        orchestrator.provision_lab(
            LabProvisionRequest(
                request.task_id,
                request.workflow_revision,
                ExecutionMode.HYBRID,
                request.specification_digest,
            ),
            idempotency_key="provision-1",
        )

    assert provider.provision_calls == 1


def test_cleanup_retries_after_teardown_failure() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    orchestrator.state = TaskState.FAILED
    orchestrator.lab_handle = LabHandle("lab:test")
    provider.fail_next_teardown = True

    with pytest.raises(RuntimeError, match="transient"):
        orchestrator.cleanup(idempotency_key="cleanup-1")

    assert orchestrator.state is TaskState.CLEANING_UP
    orchestrator.cleanup(idempotency_key="cleanup-1")

    assert provider.teardown_attempts == 2
    assert TaskState(orchestrator.state) is TaskState.CLEANED


def test_cleanup_reconciles_an_ambiguous_provision_before_teardown() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    request = LabProvisionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        ExecutionMode.IN_GUEST,
        approved_digest,
    )
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)
    provider.fail_next_provision = True

    with pytest.raises(RuntimeError, match="ambiguous"):
        orchestrator.provision_lab(request, idempotency_key="provision-1")

    orchestrator.cleanup(idempotency_key="cleanup-1")

    assert provider.reconcile_calls == 1
    assert provider.teardown_keys == {(LabHandle("lab:task-1:provision-1"), "cleanup-1")}
    assert orchestrator.state is TaskState.CLEANED


def test_cleanup_completes_when_reconciliation_confirms_no_remote_lab() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    request = LabProvisionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        ExecutionMode.IN_GUEST,
        approved_digest,
    )
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)
    provider.fail_next_provision = True

    with pytest.raises(RuntimeError, match="ambiguous"):
        orchestrator.provision_lab(request, idempotency_key="provision-1")

    provider.return_none_on_next_reconcile = True
    orchestrator.cleanup(idempotency_key="cleanup-1")

    assert provider.reconcile_calls == 1
    assert provider.teardown_attempts == 0
    assert orchestrator.state is TaskState.CLEANED


def test_failed_reconciliation_keeps_cleanup_retryable_with_original_key() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    approved_digest = _digest("approved-plan")
    request = LabProvisionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        ExecutionMode.IN_GUEST,
        approved_digest,
    )
    orchestrator.request_start_approval()
    orchestrator.approve_start(_start_approval(approved_digest), expected_digest=approved_digest)
    provider.fail_next_provision = True

    with pytest.raises(RuntimeError, match="ambiguous"):
        orchestrator.provision_lab(request, idempotency_key="provision-1")

    provider.fail_next_reconcile = True
    with pytest.raises(RuntimeError, match="reconciliation"):
        orchestrator.cleanup(idempotency_key="cleanup-1")
    with pytest.raises(IdempotencyConflictError, match="original"):
        orchestrator.cleanup(idempotency_key="cleanup-2")

    assert provider.reconcile_calls == 1
    assert provider.teardown_attempts == 0
    assert orchestrator.state is TaskState.CLEANING_UP
    orchestrator.cleanup(idempotency_key="cleanup-1")

    assert provider.reconcile_calls == 2
    assert provider.teardown_attempts == 1
    assert TaskState(orchestrator.state) is TaskState.CLEANED


def test_changed_cleanup_retry_is_rejected_without_teardown_call() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    orchestrator.state = TaskState.FAILED
    orchestrator.lab_handle = LabHandle("lab:test")
    provider.fail_next_teardown = True

    with pytest.raises(RuntimeError, match="transient"):
        orchestrator.cleanup(idempotency_key="cleanup-1")
    with pytest.raises(IdempotencyConflictError, match="original"):
        orchestrator.cleanup(idempotency_key="cleanup-2")

    assert provider.teardown_attempts == 1
    assert orchestrator.state is TaskState.CLEANING_UP


def test_cleanup_after_success_with_same_key_is_a_no_op() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    orchestrator.state = TaskState.FAILED
    orchestrator.lab_handle = LabHandle("lab:test")

    orchestrator.cleanup(idempotency_key="cleanup-1")
    orchestrator.cleanup(idempotency_key="cleanup-1")

    assert provider.teardown_attempts == 1
    assert orchestrator.state is TaskState.CLEANED


def test_cleanup_after_success_rejects_a_changed_key_without_teardown_call() -> None:
    orchestrator, provider, _, _ = _orchestrator()
    orchestrator.state = TaskState.FAILED
    orchestrator.lab_handle = LabHandle("lab:test")

    orchestrator.cleanup(idempotency_key="cleanup-1")
    with pytest.raises(IdempotencyConflictError, match="original"):
        orchestrator.cleanup(idempotency_key="cleanup-2")

    assert provider.teardown_attempts == 1
    assert orchestrator.state is TaskState.CLEANED


def _submission_intent() -> SubmissionIntent:
    artifact = ArtifactReference("memory://artifact", _digest("artifact"))
    return SubmissionIntent(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        _digest("submission"),
        ManifestReference("memory://manifest", _digest("manifest")),
        (artifact,),
    )
