from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from moddle_autotask.adapters.aws.agent_spool import ExecutionProgress, LabCommandExecutor
from moddle_autotask.adapters.aws.artifacts import PreparedArtifact, PreparedAssignment
from moddle_autotask.adapters.aws.image_imports import (
    ImageImportReadiness,
    ImageImportResult,
)
from moddle_autotask.adapters.aws.worker import process_one
from moddle_autotask.adapters.moodle.approval_state import ApprovalState
from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationAttachment,
    NotificationDraft,
    NotificationEvent,
)
from moddle_autotask.adapters.moodle.submission import (
    MoodleSubmissionError,
    PermanentSubmissionOfferError,
)
from moddle_autotask.domain.models import ExecutionMode, LabHandle, LabProvisionRequest
from moddle_autotask.ports.contracts import LabReadiness


@dataclass
class _Provider:
    readiness_value: LabReadiness = LabReadiness.PENDING
    fail_provision: bool = False
    fail_teardown_once: bool = False
    provisions: list[tuple[LabProvisionRequest, str]] = field(default_factory=list)
    teardowns: list[tuple[LabHandle, str]] = field(default_factory=list)

    def provision(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle:
        self.provisions.append((request, idempotency_key))
        if self.fail_provision:
            raise RuntimeError("temporary provider failure")
        return LabHandle("lab:test")

    def reconcile(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle | None:
        del request, idempotency_key
        return None

    def readiness(self, handle: LabHandle) -> LabReadiness:
        assert handle == LabHandle("lab:test")
        return self.readiness_value

    def teardown(self, handle: LabHandle, *, idempotency_key: str) -> None:
        self.teardowns.append((handle, idempotency_key))
        if self.fail_teardown_once:
            self.fail_teardown_once = False
            raise RuntimeError("temporary cleanup failure")


@dataclass
class _Preparer:
    prepared: list[str] = field(default_factory=list)

    def prepare(self, event: NotificationEvent) -> PreparedAssignment:
        task_key = event.task_key
        revision = event.revision_digest
        self.prepared.append(task_key)
        return PreparedAssignment(
            task_key,
            revision,
            (
                PreparedArtifact(
                    "moodle-attachment-v1:" + "c" * 64,
                    "base.ova",
                    123,
                    "d" * 64,
                    "private-bucket",
                    "assignments/base.ova",
                ),
            ),
        )


@dataclass
class _Importer:
    result: ImageImportResult
    ensured: list[str] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)

    def ensure(self, prepared: PreparedAssignment, *, idempotency_key: str) -> ImageImportResult:
        assert prepared.artifacts[0].filename == "base.ova"
        self.ensured.append(idempotency_key)
        return self.result

    def cleanup(self, *, idempotency_key: str) -> None:
        self.cleaned.append(idempotency_key)


@dataclass
class _Broker:
    progress: ExecutionProgress = ExecutionProgress("pending")
    calls: list[ExecutionMode] = field(default_factory=list)

    def step(
        self,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        mode: ExecutionMode,
        lab_handle: LabHandle | None,
        lab_executor: LabCommandExecutor,
    ) -> ExecutionProgress:
        del event, prepared, lab_handle, lab_executor
        self.calls.append(mode)
        return self.progress


@dataclass
class _Notifier:
    calls: list[tuple[NotificationEvent, ExecutionProgress]] = field(default_factory=list)

    def notify(self, event: NotificationEvent, progress: ExecutionProgress) -> None:
        self.calls.append((event, progress))


@dataclass
class _SubmissionNotifier(_Notifier):
    ready: list[object] = field(default_factory=list)
    blocked: list[tuple[NotificationEvent, str]] = field(default_factory=list)

    def notify_submission_ready(self, manifest: object, buttons: object) -> None:
        self.ready.append((manifest, buttons))

    def notify_submission_result(self, notification: object) -> None:
        del notification

    def notify_submission_blocked(self, event: NotificationEvent, reason: str) -> None:
        self.blocked.append((event, reason))


@dataclass
class _SubmissionService:
    offered: list[NotificationEvent] = field(default_factory=list)
    preflight_error: RuntimeError | None = None

    def can_offer_submission(self, event: NotificationEvent) -> None:
        if self.preflight_error is not None:
            raise self.preflight_error
        self.offered.append(event)

    def upload(self, manifest: object) -> int:
        raise AssertionError("draft policy must not upload")

    def save(self, manifest: object, draft_item_id: int) -> None:
        raise AssertionError("draft policy must not save")

    def verify(self, manifest: object) -> object | None:
        raise AssertionError("draft policy must not verify")


@dataclass
class _FailingNotifier:
    calls: int = 0

    def notify(self, event: NotificationEvent, progress: ExecutionProgress) -> None:
        del event, progress
        self.calls += 1
        raise RuntimeError("Telegram temporarily unavailable")


class _InvalidNotifier:
    def notify(self, event: NotificationEvent, progress: ExecutionProgress) -> None:
        del event, progress
        raise ValueError("invalid notification payload")


def _approved(
    tmp_path: Path,
    *,
    lab: bool,
    filename: str = "capture.pcap",
    assignment_id: int | None = None,
    submission_drafts: bool = False,
    requires_submission_statement: bool = False,
) -> tuple[ApprovalState, str, str]:
    attachment = (NotificationAttachment(filename, 123, None, True),) if lab else ()
    event = MoodleState(tmp_path / "moodle.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + "a" * 64,
            "moodle-assignment-v1:" + "b" * 64,
            "Course",
            "M01",
            "Informe" if not lab else "Máquina",
            0,
            100,
            0,
            0,
            1,
            attachment,
            assignment_id,
            submission_drafts,
            requires_submission_statement,
        ),
        now=1,
    )
    assert event is not None
    state = ApprovalState(tmp_path / "approval.sqlite3")
    buttons = state.prepare(event, now=1)
    state.resolve(buttons.approve, 42, 42, now=2)
    return state, event.task_key, event.revision_digest


def test_central_work_becomes_ready_without_provisioning(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=False)
    provider = _Provider()

    cycle = process_one(state, provider, owner="worker", now=10)

    assert cycle.result == "central_ready"
    assert provider.provisions == []
    item = state.work_status(task_key, revision)
    assert item is not None and item.status == "ready" and item.lab_handle is None


def test_central_work_waits_for_agent_then_completes_exact_revision(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=False)
    provider = _Provider()
    broker = _Broker()
    preparer = _Preparer()
    notifier = _Notifier()

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            execution_notifier=notifier,
            now=10,
        ).result
        == "central_ready"
    )
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            execution_notifier=notifier,
            now=11,
        ).result
        == "agent_pending"
    )
    broker.progress = ExecutionProgress("succeeded", "done", "# Informe")
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            execution_notifier=notifier,
            now=26,
        ).result
        == "execution_complete"
    )

    item = state.work_status(task_key, revision)
    assert item is not None and item.status == "cleaned" and item.error_code is None
    assert broker.calls == [ExecutionMode.CENTRAL, ExecutionMode.CENTRAL]
    assert len(notifier.calls) == 1


@pytest.mark.parametrize("policy", ("submission_drafts", "requires_submission_statement"))
def test_submission_policy_never_offers_second_approval(tmp_path: Path, policy: str) -> None:
    state, _, _ = (
        _approved(tmp_path, lab=False, assignment_id=43, submission_drafts=True)
        if policy == "submission_drafts"
        else _approved(tmp_path, lab=False, assignment_id=43, requires_submission_statement=True)
    )
    provider = _Provider()
    notifier = _SubmissionNotifier()
    service = _SubmissionService()
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))

    assert process_one(
        state,
        provider,
        owner="worker",
        artifact_preparer=_Preparer(),
        execution_broker=broker,
        execution_notifier=notifier,
        submission_service=service,
        now=10,
    ).result == "central_ready"
    assert process_one(
        state,
        provider,
        owner="worker",
        artifact_preparer=_Preparer(),
        execution_broker=broker,
        execution_notifier=notifier,
        submission_service=service,
        now=11,
    ).result == "execution_complete"

    assert notifier.ready == []
    assert service.offered == []
    assert notifier.blocked and "declaración" in notifier.blocked[0][1]


def test_submission_preflight_transient_retries_execution_notification(tmp_path: Path) -> None:
    state, _, _ = _approved(tmp_path, lab=False, assignment_id=43)
    provider = _Provider()
    notifier = _SubmissionNotifier()
    service = _SubmissionService(preflight_error=MoodleSubmissionError("Moodle timeout"))
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))

    assert process_one(
        state, provider, owner="worker", artifact_preparer=_Preparer(), execution_broker=broker,
        execution_notifier=notifier, submission_service=service, now=10,
    ).result == "central_ready"
    assert process_one(
        state, provider, owner="worker", artifact_preparer=_Preparer(), execution_broker=broker,
        execution_notifier=notifier, submission_service=service, now=11,
    ).result == "execution_complete"
    assert state.pending_execution_notification() is not None
    assert notifier.ready == [] and notifier.blocked == []

    service.preflight_error = None
    assert process_one(
        state, provider, owner="worker", execution_notifier=notifier,
        submission_service=service, now=12,
    ).result == "idle"
    assert state.pending_execution_notification() is None
    assert len(service.offered) == 1 and len(notifier.ready) == 1


def test_submission_preflight_permanent_rejection_blocks_and_delivers(tmp_path: Path) -> None:
    state, _, _ = _approved(tmp_path, lab=False, assignment_id=43)
    provider = _Provider()
    notifier = _SubmissionNotifier()
    service = _SubmissionService(
        preflight_error=PermanentSubmissionOfferError("Moodle upload capability is not enabled")
    )
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))

    assert process_one(
        state, provider, owner="worker", artifact_preparer=_Preparer(), execution_broker=broker,
        execution_notifier=notifier, submission_service=service, now=10,
    ).result == "central_ready"
    assert process_one(
        state, provider, owner="worker", artifact_preparer=_Preparer(), execution_broker=broker,
        execution_notifier=notifier, submission_service=service, now=11,
    ).result == "execution_complete"
    assert state.pending_execution_notification() is None
    assert notifier.ready == [] and len(notifier.blocked) == 1
    assert "Moodle no habilita" in notifier.blocked[0][1]
    assert process_one(
        state, provider, owner="worker", execution_notifier=notifier,
        submission_service=service, now=12,
    ).result == "idle"
    assert len(notifier.blocked) == 1


def test_completion_delivery_retries_runtime_error_but_propagates_value_error(
    tmp_path: Path,
) -> None:
    state, _, _ = _approved(tmp_path, lab=False)
    provider = _Provider()
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))
    failing = _FailingNotifier()

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            execution_notifier=failing,
            now=10,
        ).result
        == "central_ready"
    )
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            execution_notifier=failing,
            now=11,
        ).result
        == "execution_complete"
    )
    assert state.pending_execution_notification() is not None

    with pytest.raises(ValueError, match="invalid notification payload"):
        process_one(state, provider, owner="worker", execution_notifier=_InvalidNotifier(), now=12)
    assert state.pending_execution_notification() is not None


def test_lab_work_provisions_once_then_waits_for_ssm(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=True)
    provider = _Provider()

    first = process_one(state, provider, owner="worker", now=10)
    second = process_one(state, provider, owner="worker", now=11)
    provider.readiness_value = LabReadiness.READY
    third = process_one(state, provider, owner="worker", now=42)
    before_ttl = process_one(state, provider, owner="worker", now=7241)
    cleanup = process_one(state, provider, owner="worker", now=7242)

    assert first.result == "lab_provisioned"
    assert second.result == "lab_pending"
    assert third.result == "lab_ready"
    assert before_ttl.result == "idle"
    assert cleanup.result == "lab_cleaned"
    assert len(provider.provisions) == 1
    item = state.work_status(task_key, revision)
    assert item is not None and item.status == "cleaned"
    assert item.lab_handle == LabHandle("lab:test")
    assert provider.teardowns == [(LabHandle("lab:test"), "cleanup-" + item.provision_key)]


def test_lab_cleanup_deadline_survives_notification_outage_and_restart(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=True)
    provider = _Provider(readiness_value=LabReadiness.READY)
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))
    failing = _FailingNotifier()
    preparer = _Preparer()

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            execution_notifier=failing,
            now=10,
        ).result
        == "lab_provisioned"
    )
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            execution_notifier=failing,
            now=11,
        ).result
        == "lab_ready"
    )
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            execution_notifier=failing,
            now=12,
        ).result
        == "execution_complete"
    )
    assert failing.calls == 1
    item = state.work_status(task_key, revision)
    assert item is not None and item.status == "ready" and item.error_code == "execution_complete"
    assert state.pending_execution_notification() is not None

    restarted = ApprovalState(tmp_path / "approval.sqlite3")
    assert (
        process_one(
            restarted,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            execution_notifier=failing,
            now=7212,
        ).result
        == "lab_cleaned"
    )
    assert provider.teardowns == [(LabHandle("lab:test"), "cleanup-" + item.provision_key)]
    assert restarted.pending_execution_notification() is not None
    notifier = _Notifier()
    assert (
        process_one(
            restarted, provider, owner="worker", execution_notifier=notifier, now=7213
        ).result
        == "idle"
    )
    assert len(notifier.calls) == 1
    assert restarted.pending_execution_notification() is None
    cleaned = restarted.work_status(task_key, revision)
    assert cleaned is not None and cleaned.status == "cleaned"
    assert broker.calls == [ExecutionMode.HYBRID]


def test_cleanup_retry_preserves_execution_phase_and_deadline_without_reexecution(
    tmp_path: Path,
) -> None:
    state, task_key, revision = _approved(tmp_path, lab=True)
    provider = _Provider(readiness_value=LabReadiness.READY, fail_teardown_once=True)
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))
    preparer = _Preparer()

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            now=10,
        ).result
        == "lab_provisioned"
    )
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            now=11,
        ).result
        == "lab_ready"
    )
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            now=12,
        ).result
        == "execution_complete"
    )
    with state._connect() as connection:
        deadline = connection.execute("SELECT cleanup_due_at FROM work_items").fetchone()[0]
    assert deadline == 7212

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            now=7212,
        ).result
        == "cleanup_retry"
    )
    retried = state.work_status(task_key, revision)
    assert retried is not None and retried.status == "ready"
    assert retried.error_code == "execution_complete"
    with state._connect() as connection:
        assert connection.execute("SELECT cleanup_due_at FROM work_items").fetchone()[0] == deadline

    restarted = ApprovalState(tmp_path / "approval.sqlite3")
    assert (
        process_one(
            restarted,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            now=7452,
        ).result
        == "lab_cleaned"
    )
    assert len(provider.teardowns) == 2
    assert broker.calls == [ExecutionMode.HYBRID]


def test_worker_cli_uses_bounded_lab_runner_and_long_import_runner() -> None:
    source = (
        Path(__file__).parents[1] / "src/moddle_autotask/adapters/aws/worker_cli.py"
    ).read_text(encoding="utf-8")

    assert "runner = AwsCliJsonRunner(timeout_seconds=3600)" in source
    assert "lab_runner = AwsCliJsonRunner(timeout_seconds=30)" in source
    provider = source.split("provider = AwsEc2LabProvider(", 1)[1].split("artifact_preparer =", 1)[
        0
    ]
    assert "lab_runner," in provider


def test_provider_failure_releases_lease_with_bounded_retry(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=True)
    provider = _Provider(fail_provision=True)

    failed = process_one(state, provider, owner="worker", now=10)
    before_delay = process_one(state, provider, owner="worker", now=39)
    provider.fail_provision = False
    retried = process_one(state, provider, owner="worker", now=40)

    assert failed.result == "retry" and before_delay.result == "idle"
    assert retried.result == "lab_provisioned"
    item = state.work_status(task_key, revision)
    assert item is not None and item.status == "lab_pending" and item.attempts == 2
    assert len(provider.provisions) == 2
    assert provider.provisions[0][1] == provider.provisions[1][1]


def test_ova_requires_image_import_without_launching_blank_windows(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=True, filename="base.ova")
    provider = _Provider()

    cycle = process_one(state, provider, owner="worker", now=10)

    assert cycle.result == "image_import_required"
    assert provider.provisions == []
    item = state.work_status(task_key, revision)
    assert item is not None and item.status == "failed" and item.lab_handle is None


def test_ova_import_pending_retries_without_launching(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=True, filename="base.ova")
    provider = _Provider()
    preparer = _Preparer()
    importer = _Importer(ImageImportResult(ImageImportReadiness.PENDING))

    cycle = process_one(
        state,
        provider,
        owner="worker",
        artifact_preparer=preparer,
        image_importer=importer,
        now=10,
    )
    for attempt in range(1, 25):
        cycle = process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            image_importer=importer,
            now=10 + attempt * 61,
        )

    assert cycle.result == "image_import_pending"
    assert provider.provisions == []
    item = state.work_status(task_key, revision)
    assert item is not None and item.status == "pending" and item.attempts == 25
    assert len(preparer.prepared) == 25 and len(importer.ensured) == 25


def test_completed_ova_import_launches_exact_imported_image(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=True, filename="base.ova")
    provider = _Provider()
    importer = _Importer(ImageImportResult(ImageImportReadiness.READY, "ami-0123456789abcdef0"))

    cycle = process_one(
        state,
        provider,
        owner="worker",
        artifact_preparer=_Preparer(),
        image_importer=importer,
        now=10,
    )

    assert cycle.result == "lab_provisioned"
    assert provider.provisions[0][0].image_reference == "ami-0123456789abcdef0"
    item = state.work_status(task_key, revision)
    assert item is not None and item.status == "lab_pending"
