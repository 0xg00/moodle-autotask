"""Durable bridge from an exact human approval to an idempotent AWS lab."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from moddle_autotask.adapters.moodle.approval_state import (
    ApprovalState,
    ApprovalStateError,
    WorkClaim,
)
from moddle_autotask.adapters.moodle.state import NotificationEvent
from moddle_autotask.domain.models import (
    ExecutionMode,
    LabProvisionRequest,
    TaskId,
    WorkflowRevision,
)
from moddle_autotask.ports.contracts import LabProvider, LabReadiness

from .artifacts import PreparedAssignment
from .image_imports import ImageImportReadiness, ImageImportResult


class ArtifactPreparer(Protocol):
    def prepare(self, event: NotificationEvent) -> PreparedAssignment: ...


class ImageImporter(Protocol):
    def ensure(
        self, prepared: PreparedAssignment, *, idempotency_key: str
    ) -> ImageImportResult: ...

    def cleanup(self, *, idempotency_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerCycle:
    result: str
    mode: ExecutionMode | None = None


def process_one(
    state: ApprovalState,
    provider: LabProvider,
    *,
    owner: str,
    artifact_preparer: ArtifactPreparer | None = None,
    image_importer: ImageImporter | None = None,
    lease_seconds: int = 300,
    now: int | None = None,
) -> WorkerCycle:
    claim = state.claim_work(owner, lease_seconds, now=now)
    if claim is None:
        return WorkerCycle("idle")
    try:
        return _process_claim(
            state,
            provider,
            claim,
            artifact_preparer=artifact_preparer,
            image_importer=image_importer,
            now=now,
        )
    except ApprovalStateError:
        raise
    except (RuntimeError, ValueError):
        if not state.retry_work(
            claim, "provider_failed", _retry_delay(claim.item.attempts), now=now
        ):
            return WorkerCycle("ownership_lost", claim.item.selected_mode)
        return WorkerCycle("retry", claim.item.selected_mode)


def _process_claim(
    state: ApprovalState,
    provider: LabProvider,
    claim: WorkClaim,
    *,
    artifact_preparer: ArtifactPreparer | None,
    image_importer: ImageImporter | None,
    now: int | None,
) -> WorkerCycle:
    item = claim.item
    if item.status in {"ready", "failed"} and item.lab_handle is not None:
        provider.teardown(item.lab_handle, idempotency_key=f"cleanup-{item.provision_key}")
        if _requires_image_import(item.event):
            if image_importer is None:
                raise RuntimeError("image importer is unavailable during cleanup")
            image_importer.cleanup(idempotency_key=item.provision_key)
        if not state.mark_cleaned(claim, now=now):
            return WorkerCycle("ownership_lost", item.selected_mode)
        return WorkerCycle("lab_cleaned", item.selected_mode)
    if item.status == "pending":
        if item.selected_mode is ExecutionMode.CENTRAL:
            if not state.mark_ready(claim, now=now):
                return WorkerCycle("ownership_lost", item.selected_mode)
            return WorkerCycle("central_ready", item.selected_mode)
        image_id: str | None = None
        if _requires_image_import(item.event):
            if artifact_preparer is None or image_importer is None:
                if not state.fail_work(claim, "image_import_required", now=now):
                    return WorkerCycle("ownership_lost", item.selected_mode)
                return WorkerCycle("image_import_required", item.selected_mode)
            prepared = artifact_preparer.prepare(item.event)
            imported = image_importer.ensure(prepared, idempotency_key=item.provision_key)
            if imported.readiness is ImageImportReadiness.PENDING:
                if not state.retry_work(
                    claim,
                    "image_import_pending",
                    60,
                    now=now,
                    exhaustible=False,
                ):
                    return WorkerCycle("ownership_lost", item.selected_mode)
                return WorkerCycle("image_import_pending", item.selected_mode)
            image_id = imported.image_id
            if image_id is None:
                raise RuntimeError("completed image import has no image ID")
        elif artifact_preparer is not None:
            artifact_preparer.prepare(item.event)
        request = LabProvisionRequest(
            TaskId(item.event.task_key),
            WorkflowRevision(item.event.revision_digest),
            item.selected_mode,
            item.specification_digest,
            image_reference=image_id,
        )
        handle = provider.provision(request, idempotency_key=item.provision_key)
        if not state.record_lab(claim, handle, now=now):
            return WorkerCycle("ownership_lost", item.selected_mode)
        return WorkerCycle("lab_provisioned", item.selected_mode)

    if item.status != "lab_pending" or item.lab_handle is None:
        raise ApprovalStateError("claimed work has an invalid state")
    readiness = provider.readiness(item.lab_handle)
    if readiness is LabReadiness.READY:
        if not state.mark_ready(claim, now=now):
            return WorkerCycle("ownership_lost", item.selected_mode)
        return WorkerCycle("lab_ready", item.selected_mode)
    if readiness is LabReadiness.FAILED:
        if not state.retry_work(
            claim, "lab_failed", _retry_delay(item.attempts), now=now
        ):
            return WorkerCycle("ownership_lost", item.selected_mode)
        return WorkerCycle("retry", item.selected_mode)
    if not state.retry_work(claim, "lab_pending", 30, now=now):
        return WorkerCycle("ownership_lost", item.selected_mode)
    return WorkerCycle("lab_pending", item.selected_mode)


def _retry_delay(attempts: int) -> int:
    exponent: int = min(max(attempts - 1, 0), 6)
    delays = (30, 60, 120, 240, 480, 960, 1800)
    return delays[exponent]


def _requires_image_import(event: NotificationEvent) -> bool:
    return any(
        attachment.filename.lower().endswith(
            (".ova", ".ovf", ".vdi", ".vmdk", ".vhd", ".vhdx")
        )
        for attachment in event.attachments
    )
