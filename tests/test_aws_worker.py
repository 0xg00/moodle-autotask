from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from moddle_autotask.adapters.aws.worker import process_one
from moddle_autotask.adapters.moodle.approval_state import ApprovalState
from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationAttachment,
    NotificationDraft,
)
from moddle_autotask.domain.models import LabHandle, LabProvisionRequest
from moddle_autotask.ports.contracts import LabReadiness


@dataclass
class _Provider:
    readiness_value: LabReadiness = LabReadiness.PENDING
    fail_provision: bool = False
    provisions: list[tuple[LabProvisionRequest, str]] = field(default_factory=list)
    teardowns: list[tuple[LabHandle, str]] = field(default_factory=list)

    def provision(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle:
        self.provisions.append((request, idempotency_key))
        if self.fail_provision:
            raise RuntimeError("temporary provider failure")
        return LabHandle("lab:test")

    def reconcile(
        self, request: LabProvisionRequest, *, idempotency_key: str
    ) -> LabHandle | None:
        del request, idempotency_key
        return None

    def readiness(self, handle: LabHandle) -> LabReadiness:
        assert handle == LabHandle("lab:test")
        return self.readiness_value

    def teardown(self, handle: LabHandle, *, idempotency_key: str) -> None:
        self.teardowns.append((handle, idempotency_key))


def _approved(
    tmp_path: Path, *, lab: bool, filename: str = "capture.pcap"
) -> tuple[ApprovalState, str, str]:
    attachment = (
        (NotificationAttachment(filename, 123, None, True),) if lab else ()
    )
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
    assert provider.teardowns == [
        (LabHandle("lab:test"), "cleanup-" + item.provision_key)
    ]


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
