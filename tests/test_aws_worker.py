from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import pytest

from moddle_autotask.adapters.aws.agent_spool import ExecutionProgress, LabCommandExecutor
from moddle_autotask.adapters.aws.artifacts import PreparedArtifact, PreparedAssignment
from moddle_autotask.adapters.aws.completion import TelegramExecutionNotifier
from moddle_autotask.adapters.aws.image_imports import (
    ImageImportReadiness,
    ImageImportResult,
)
from moddle_autotask.adapters.aws.input_transfer import GuestInputReady
from moddle_autotask.adapters.aws.worker import process_one
from moddle_autotask.adapters.moodle import approval_state
from moddle_autotask.adapters.moodle.approval_state import ApprovalState, SubmissionManifest
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
from moddle_autotask.adapters.moodle.telegram import TelegramConfig
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
        if mode is ExecutionMode.CENTRAL and self.progress.status == "succeeded":
            return ExecutionProgress(
                self.progress.status,
                self.progress.summary,
                self.progress.report_markdown,
                self.progress.provenance or _central_provenance(),
            )
        return self.progress


@dataclass
class _Transfer:
    calls: list[ExecutionMode] = field(default_factory=list)

    def ensure(
        self,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        handle: LabHandle,
        executor: object,
        *,
        excluded_attachment_keys: frozenset[str] = frozenset(),
    ) -> GuestInputReady:
        del event, handle, executor, excluded_attachment_keys
        self.calls.append(ExecutionMode.HYBRID)
        return GuestInputReady("e" * 64, None, ())


def _central_provenance() -> dict[str, object]:
    manifest = {
        "kind": "artifact-manifest-v1",
        "files": [{"path": "report.md", "size": 1, "sha256": "0" * 64}],
        "totals": {"bytes": 1, "files": 1},
    }
    manifest_digest = sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    planner, executor, reviewer = ("a" * 64, "b" * 64, "c" * 64)
    return {
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


def _bundled_central_provenance(root: Path) -> dict[str, object]:
    bundles = root / "bundles"
    bundles.mkdir(parents=True)
    artifact = b"verified evidence\n"
    manifest = {
        "kind": "artifact-manifest-v1",
        "files": [
            {"path": "report.md", "size": len(artifact), "sha256": sha256(artifact).hexdigest()}
        ],
        "totals": {"files": 1, "bytes": len(artifact)},
    }
    temporary = bundles / "bundle.zip"
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("report.md", artifact)
    digest = sha256(temporary.read_bytes()).hexdigest()
    temporary.rename(bundles / f"{digest}.zip")
    provenance = _central_provenance()
    provenance["artifactManifest"] = manifest
    provenance["artifactManifestDigest"] = sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    provenance["artifactBundleDigest"] = digest
    provenance["bundleLocator"] = f"bundles/{digest}.zip"
    return provenance


@dataclass
class _Notifier:
    calls: list[tuple[NotificationEvent, ExecutionProgress]] = field(default_factory=list)

    def notify(self, event: NotificationEvent, progress: ExecutionProgress) -> None:
        self.calls.append((event, progress))


@dataclass
class _TelegramTransport:
    fail_zip: bool = False
    documents: list[str] = field(default_factory=list)

    def send_message(self, chat_id: int, text: str, buttons: object = None) -> int:
        del chat_id, text, buttons
        return 1

    def send_document(self, chat_id: int, filename: str, content: bytes, caption: str) -> int:
        del chat_id, content, caption
        self.documents.append(filename)
        if self.fail_zip and filename.endswith(".zip"):
            raise RuntimeError("zip delivery failed")
        return 1


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

    def save(self, manifest: SubmissionManifest, draft_item_id: int) -> None:
        raise AssertionError("draft policy must not save")

    def verify(self, manifest: object) -> object | None:
        raise AssertionError("draft policy must not verify")

    def verify_draft(self, manifest: object) -> object | None:
        raise AssertionError("draft policy must not verify")

    def finalize(self, manifest: object) -> None:
        raise AssertionError("draft policy must not finalize")


@dataclass(frozen=True)
class _Receipt:
    reference: str = "moodle-submission:91"


@dataclass
class _LifecycleSubmissionService:
    """Stateful Moodle double: remote status only changes at the real boundaries."""

    remote_status: str = "new"
    fail_at: str | None = None
    expire_lease_state: ApprovalState | None = None
    upload_calls: int = 0
    save_calls: int = 0
    verify_draft_calls: int = 0
    finalize_calls: int = 0
    verify_calls: int = 0

    def can_offer_submission(self, event: NotificationEvent) -> None:
        del event

    def upload(self, manifest: object) -> int:
        del manifest
        self.upload_calls += 1
        if self.fail_at == "upload":
            raise RuntimeError("upload crashed")
        return 17

    def save(self, manifest: SubmissionManifest, draft_item_id: int) -> None:
        assert draft_item_id == 17
        self.save_calls += 1
        if self.fail_at == "save_after_submit":
            self.remote_status = "submitted"
            raise RuntimeError("save response lost")
        if self.fail_at == "save_after_draft":
            self.remote_status = "draft"
            raise RuntimeError("save response lost")
        if self.fail_at == "save":
            raise RuntimeError("save crashed")
        if self.fail_at == "save_no_remote":
            return
        self.remote_status = "draft" if manifest.event.submission_drafts else "submitted"

    def verify_draft(self, manifest: object) -> object | None:
        del manifest
        self.verify_draft_calls += 1
        if self.fail_at == "verify_draft":
            raise RuntimeError("draft verification crashed")
        return _Receipt() if self.remote_status == "draft" else None

    def finalize(self, manifest: object) -> None:
        del manifest
        self.finalize_calls += 1
        if self.fail_at == "finalize_after_submit":
            self.remote_status = "submitted"
            raise RuntimeError("finalize response lost")
        if self.fail_at == "finalize":
            raise RuntimeError("finalize crashed")
        assert self.remote_status == "draft"
        self.remote_status = "submitted"

    def verify(self, manifest: object) -> object | None:
        del manifest
        self.verify_calls += 1
        if self.fail_at == "verify":
            raise RuntimeError("submitted verification crashed")
        if self.expire_lease_state is not None:
            with self.expire_lease_state._connect() as connection:
                connection.execute("UPDATE submissions SET lease_expires_at = 10")
        return _Receipt() if self.remote_status == "submitted" else None


def _submission_approved(
    tmp_path: Path, *, drafts: bool = False, statement: bool = False
) -> tuple[ApprovalState, NotificationEvent]:
    event = MoodleState(tmp_path / "submission-moodle.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + "e" * 64,
            "moodle-assignment-v1:" + "f" * 64,
            "Course",
            "M01",
            "Report",
            0,
            100,
            0,
            0,
            1,
            (),
            43,
            drafts,
            statement,
            "<p>I accept the submission statement.</p>" if statement else "",
            1 if statement else 0,
        ),
        now=1,
    )
    assert event is not None
    state = ApprovalState(tmp_path / "submission-approval.sqlite3")
    state.prepare(event, now=1)
    _manifest, buttons = state.prepare_submission(event, "done", "# Report", now=2)
    assert state.resolve_submission(buttons.submit, 42, 42, now=3)[1] == "approved"
    return state, event


def _submission_row(state: ApprovalState) -> tuple[str, int | None, str | None]:
    with state._connect() as connection:
        row = connection.execute(
            "SELECT status, draft_item_id, error_code FROM submissions"
        ).fetchone()
    assert row is not None
    return str(row[0]), row[1], row[2]


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
    assert notifier.calls[0][1].provenance == _central_provenance()


def test_central_bundle_delivery_retries_after_zip_failure_without_marking_outbox(
    tmp_path: Path,
) -> None:
    state, _, _ = _approved(tmp_path, lab=False)
    provider = _Provider()
    broker = _Broker()
    transport = _TelegramTransport(fail_zip=True)
    bundles_root = tmp_path / "trusted-results"
    notifier = TelegramExecutionNotifier(
        TelegramConfig("123456:abcdefghijklmnopqrstuvwxyzABCDE", 1, 1),
        transport,
        bundles_root / "bundles",
    )

    assert process_one(state, provider, owner="worker", now=10).result == "central_ready"
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            execution_notifier=notifier,
            now=11,
        ).result
        == "agent_pending"
    )
    broker.progress = ExecutionProgress(
        "succeeded",
        "done",
        "# Informe\nEvidence verified.",
        _bundled_central_provenance(bundles_root),
    )
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            execution_notifier=notifier,
            now=26,
        ).result
        == "execution_complete"
    )
    assert transport.documents[0].endswith(".md")
    assert transport.documents[1].endswith(".zip")
    assert state.pending_execution_notification() is not None

    transport.fail_zip = False
    assert (
        process_one(state, provider, owner="worker", execution_notifier=notifier, now=27).result
        == "idle"
    )
    assert state.pending_execution_notification() is None
    assert transport.documents[-1].endswith(".zip")


def test_statement_without_drafts_never_offers_second_approval(tmp_path: Path) -> None:
    state, _, _ = _approved(
        tmp_path, lab=False, assignment_id=43, requires_submission_statement=True
    )
    provider = _Provider()
    notifier = _SubmissionNotifier()
    service = _SubmissionService()
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            execution_notifier=notifier,
            submission_service=service,
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
            execution_notifier=notifier,
            submission_service=service,
            now=11,
        ).result
        == "execution_complete"
    )

    assert notifier.ready == []
    assert service.offered == []
    assert notifier.blocked and "declaración" in notifier.blocked[0][1]


def test_submission_preflight_transient_retries_execution_notification(tmp_path: Path) -> None:
    state, _, _ = _approved(tmp_path, lab=False, assignment_id=43)
    provider = _Provider()
    notifier = _SubmissionNotifier()
    service = _SubmissionService(preflight_error=MoodleSubmissionError("Moodle timeout"))
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            execution_notifier=notifier,
            submission_service=service,
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
            execution_notifier=notifier,
            submission_service=service,
            now=11,
        ).result
        == "execution_complete"
    )
    assert state.pending_execution_notification() is not None
    assert notifier.ready == [] and notifier.blocked == []

    service.preflight_error = None
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            execution_notifier=notifier,
            submission_service=service,
            now=12,
        ).result
        == "idle"
    )
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

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            execution_notifier=notifier,
            submission_service=service,
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
            execution_notifier=notifier,
            submission_service=service,
            now=11,
        ).result
        == "execution_complete"
    )
    assert state.pending_execution_notification() is None
    assert notifier.ready == [] and len(notifier.blocked) == 1
    assert "Moodle no habilita" in notifier.blocked[0][1]
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            execution_notifier=notifier,
            submission_service=service,
            now=12,
        ).result
        == "idle"
    )
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
    preparer = _Preparer()
    transfer = _Transfer()

    first = process_one(state, provider, owner="worker", now=10)
    second = process_one(state, provider, owner="worker", now=11)
    provider.readiness_value = LabReadiness.READY
    third = process_one(
        state,
        provider,
        owner="worker",
        artifact_preparer=preparer,
        guest_input_transfer=transfer,
        now=42,
    )
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


@pytest.mark.parametrize("mode", (ExecutionMode.HYBRID, ExecutionMode.IN_GUEST))
def test_noncentral_ready_without_transfer_retries_before_broker_dispatch(
    tmp_path: Path, mode: ExecutionMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approval_state, "_select_mode", lambda event: mode)
    state, _task_key, _revision = _approved(tmp_path, lab=True)
    provider = _Provider(readiness_value=LabReadiness.READY)
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))
    preparer = _Preparer()
    assert process_one(state, provider, owner="worker", now=10).result == "lab_provisioned"

    cycle = process_one(
        state,
        provider,
        owner="worker",
        artifact_preparer=preparer,
        execution_broker=broker,
        now=11,
    )

    assert cycle.result == "retry" and broker.calls == []


@pytest.mark.parametrize("mode", (ExecutionMode.HYBRID, ExecutionMode.IN_GUEST))
def test_noncentral_empty_transfer_is_bound_before_dispatch(
    tmp_path: Path, mode: ExecutionMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approval_state, "_select_mode", lambda event: mode)
    state, _task_key, _revision = _approved(tmp_path, lab=True)
    provider = _Provider(readiness_value=LabReadiness.READY)
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))
    transfer = _Transfer()

    assert process_one(state, provider, owner="worker", now=10).result == "lab_provisioned"
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            guest_input_transfer=transfer,
            now=11,
        ).result
        == "lab_ready"
    )
    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=_Preparer(),
            execution_broker=broker,
            guest_input_transfer=transfer,
            now=12,
        ).result
        == "execution_complete"
    )
    assert transfer.calls == [ExecutionMode.HYBRID, ExecutionMode.HYBRID]
    assert broker.calls == [mode]


def test_lab_cleanup_deadline_survives_notification_outage_and_restart(tmp_path: Path) -> None:
    state, task_key, revision = _approved(tmp_path, lab=True)
    provider = _Provider(readiness_value=LabReadiness.READY)
    broker = _Broker(ExecutionProgress("succeeded", "done", "# Informe"))
    failing = _FailingNotifier()
    preparer = _Preparer()
    transfer = _Transfer()

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            execution_notifier=failing,
            guest_input_transfer=transfer,
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
            guest_input_transfer=transfer,
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
            guest_input_transfer=transfer,
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
    transfer = _Transfer()

    assert (
        process_one(
            state,
            provider,
            owner="worker",
            artifact_preparer=preparer,
            execution_broker=broker,
            guest_input_transfer=transfer,
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
            guest_input_transfer=transfer,
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
            guest_input_transfer=transfer,
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


def test_direct_submission_is_uploaded_saved_verified_and_receipted_once(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path)
    service = _LifecycleSubmissionService(remote_status="submitted")

    cycle = process_one(state, _Provider(), owner="worker", submission_service=service, now=10)

    assert cycle.result == "submission_confirmed"
    assert _submission_row(state) == ("submitted", 17, None)
    assert (service.upload_calls, service.save_calls, service.verify_calls) == (1, 1, 1)


def test_draft_submission_requires_verified_draft_then_finalizes_once(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True, statement=True)
    service = _LifecycleSubmissionService()

    assert (
        process_one(state, _Provider(), owner="worker", submission_service=service, now=10).result
        == "submission_confirmed"
    )
    assert _submission_row(state) == ("submitted", 17, None)
    assert (
        service.upload_calls,
        service.save_calls,
        service.verify_draft_calls,
        service.finalize_calls,
        service.verify_calls,
    ) == (1, 1, 2, 1, 2)


def test_stale_saving_claim_recovers_by_verifying_draft_without_reuploading(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True)
    claim = state.claim_submission("first", 6, now=10)
    assert claim is not None and state.record_submission_draft(claim, 17, now=10)
    service = _LifecycleSubmissionService(remote_status="draft")

    cycle = process_one(state, _Provider(), owner="second", submission_service=service, now=16)

    assert cycle.result == "submission_confirmed"
    assert _submission_row(state) == ("submitted", 17, None)
    assert (service.upload_calls, service.save_calls, service.verify_draft_calls) == (0, 0, 2)


def test_stale_finalizing_claim_reconciles_without_reupload_or_resave(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True)
    claim = state.claim_submission("first", 6, now=10)
    assert claim is not None
    saving = state.record_submission_draft(claim, 17, now=10)
    assert saving is not None and state.record_submission_finalizing(saving, now=10)
    service = _LifecycleSubmissionService(remote_status="draft")

    cycle = process_one(state, _Provider(), owner="second", submission_service=service, now=16)

    assert cycle.result == "submission_confirmed"
    assert _submission_row(state) == ("submitted", 17, None)
    assert (
        service.upload_calls,
        service.save_calls,
        service.finalize_calls,
        service.verify_calls,
    ) == (0, 0, 1, 2)


def test_upload_crash_never_creates_a_durable_draft(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path)
    service = _LifecycleSubmissionService(fail_at="upload")

    assert (
        process_one(state, _Provider(), owner="worker", submission_service=service, now=10).result
        == "submission_failed"
    )
    assert _submission_row(state) == ("failed", None, "submission_failed")
    assert service.upload_calls == 1 and service.save_calls == 0


def test_save_response_loss_reconciles_exact_submitted_receipt(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path)
    service = _LifecycleSubmissionService(fail_at="save_after_submit")

    assert (
        process_one(state, _Provider(), owner="worker", submission_service=service, now=10).result
        == "submission_confirmed"
    )
    assert _submission_row(state) == ("submitted", 17, None)
    assert (service.upload_calls, service.save_calls, service.verify_calls) == (1, 1, 1)


def test_draft_save_response_loss_recovers_without_duplicate_upload_or_save(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True)
    service = _LifecycleSubmissionService(fail_at="save_after_draft")

    assert (
        process_one(state, _Provider(), owner="worker", submission_service=service, now=10).result
        == "submission_confirmed"
    )
    assert _submission_row(state) == ("submitted", 17, None)
    assert (
        service.upload_calls,
        service.save_calls,
        service.verify_draft_calls,
        service.verify_calls,
    ) == (1, 1, 2, 2)
    assert (service.upload_calls, service.save_calls, service.finalize_calls) == (1, 1, 1)


def test_unverified_direct_submission_fails_closed_after_save(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path)
    service = _LifecycleSubmissionService(fail_at="save_no_remote")

    assert (
        process_one(state, _Provider(), owner="worker", submission_service=service, now=10).result
        == "submission_unverified"
    )
    assert _submission_row(state) == ("failed", 17, "submission_unverified")


def test_unverified_draft_fails_closed_before_finalization(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True)
    service = _LifecycleSubmissionService(fail_at="save_no_remote")

    assert (
        process_one(state, _Provider(), owner="worker", submission_service=service, now=10).result
        == "submission_draft_unverified"
    )
    assert _submission_row(state) == ("failed", 17, "submission_draft_unverified")
    assert service.finalize_calls == 0


def test_finalization_response_loss_reconciles_submitted_remote_state(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True)
    claim = state.claim_submission("first", 6, now=10)
    assert claim is not None
    saving = state.record_submission_draft(claim, 17, now=10)
    assert saving is not None and state.record_submission_finalizing(saving, now=10)
    service = _LifecycleSubmissionService(remote_status="draft", fail_at="finalize_after_submit")

    cycle = process_one(state, _Provider(), owner="recover", submission_service=service, now=16)

    assert cycle.result == "submission_confirmed"
    assert _submission_row(state) == ("submitted", 17, None)
    assert service.finalize_calls == 1 and service.verify_calls == 2


def test_finalization_error_with_exact_draft_fails_closed_without_second_finalize(
    tmp_path: Path,
) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True)
    claim = state.claim_submission("first", 6, now=10)
    assert claim is not None
    saving = state.record_submission_draft(claim, 17, now=10)
    assert saving is not None and state.record_submission_finalizing(saving, now=10)
    service = _LifecycleSubmissionService(remote_status="draft", fail_at="finalize")

    assert (
        process_one(state, _Provider(), owner="recover", submission_service=service, now=16).result
        == "submission_ambiguous"
    )
    assert _submission_row(state) == ("failed", 17, "submission_ambiguous")
    assert service.finalize_calls == 1 and service.verify_draft_calls == 2


def test_recovered_finalizing_changed_draft_fails_closed_without_finalizing(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True)
    claim = state.claim_submission("first", 6, now=10)
    assert claim is not None
    saving = state.record_submission_draft(claim, 17, now=10)
    assert saving is not None and state.record_submission_finalizing(saving, now=10)
    service = _LifecycleSubmissionService(remote_status="changed_draft")

    cycle = process_one(state, _Provider(), owner="recover", submission_service=service, now=16)

    assert cycle.result == "submission_draft_unverified"
    assert _submission_row(state) == ("failed", 17, "submission_draft_unverified")
    assert service.finalize_calls == 0


def test_submitted_verification_crash_fails_closed(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path)
    service = _LifecycleSubmissionService(fail_at="verify")

    assert (
        process_one(state, _Provider(), owner="worker", submission_service=service, now=10).result
        == "submission_failed"
    )
    assert _submission_row(state) == ("failed", 17, "submission_failed")


def test_completion_ownership_loss_never_reports_a_submitted_receipt(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path)
    service = _LifecycleSubmissionService(expire_lease_state=state)

    cycle = process_one(state, _Provider(), owner="worker", submission_service=service, now=10)

    assert cycle.result == "submission_ownership_lost"
    assert _submission_row(state) == ("saving", 17, None)


def test_expired_submission_lease_causes_no_duplicate_remote_mutation(tmp_path: Path) -> None:
    state, _event = _submission_approved(tmp_path, drafts=True)
    claim = state.claim_submission("first", 6, now=10)
    assert claim is not None
    saving = state.record_submission_draft(claim, 17, now=10)
    assert saving is not None and state.record_submission_finalizing(saving, now=10)
    service = _LifecycleSubmissionService(remote_status="submitted")

    assert (
        process_one(state, _Provider(), owner="recover", submission_service=service, now=16).result
        == "submission_confirmed"
    )
    assert (
        process_one(state, _Provider(), owner="recover", submission_service=service, now=17).result
        == "idle"
    )
    assert (service.upload_calls, service.save_calls, service.finalize_calls) == (0, 0, 0)
