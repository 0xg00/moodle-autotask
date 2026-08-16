"""Durable bridge from an exact human approval to an idempotent AWS lab."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, cast, runtime_checkable

from moddle_autotask.adapters.moodle.approval_state import (
    ApprovalState,
    ApprovalStateError,
    SubmissionClaim,
    SubmissionManifest,
    WorkClaim,
)
from moddle_autotask.adapters.moodle.state import NotificationEvent
from moddle_autotask.adapters.moodle.submission import (
    PermanentSubmissionOfferError,
    UnsupportedSubmissionPolicyError,
)
from moddle_autotask.domain.models import (
    ExecutionMode,
    LabHandle,
    LabProvisionRequest,
    TaskId,
    WorkflowRevision,
)
from moddle_autotask.ports.contracts import LabProvider, LabReadiness

from . import central_protocol
from .agent_spool import ExecutionBroker, ExecutionProgress, LabCommandExecutor
from .artifacts import PreparedAssignment
from .image_imports import ImageImportReadiness, ImageImportResult
from .input_transfer import GuestCommandExecutor, GuestInputReady
from .storage_quota import StorageCapacityError


class ArtifactPreparer(Protocol):
    def prepare(self, event: NotificationEvent) -> PreparedAssignment: ...


class ImageImporter(Protocol):
    def ensure(
        self, prepared: PreparedAssignment, *, idempotency_key: str
    ) -> ImageImportResult: ...

    def cleanup(self, *, idempotency_key: str) -> None: ...


class ExecutionNotifier(Protocol):
    def notify(self, event: NotificationEvent, progress: ExecutionProgress) -> None: ...


class GuestInputTransferer(Protocol):
    def ensure(
        self,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        handle: LabHandle,
        executor: GuestCommandExecutor,
        *,
        excluded_attachment_keys: frozenset[str] = frozenset(),
    ) -> GuestInputReady: ...


@runtime_checkable
class SubmissionNotifier(Protocol):
    def notify_submission_ready(self, manifest: object, buttons: object) -> None: ...

    def notify_submission_result(self, notification: object) -> None: ...

    def notify_submission_blocked(self, event: NotificationEvent, reason: str) -> None: ...


class SubmissionService(Protocol):
    def can_offer_submission(self, event: NotificationEvent) -> None: ...

    def upload(self, manifest: SubmissionManifest) -> int: ...

    def save(self, manifest: SubmissionManifest, draft_item_id: int) -> None: ...

    def verify_draft(self, manifest: SubmissionManifest) -> object | None: ...

    def finalize(self, manifest: SubmissionManifest) -> None: ...

    def verify(self, manifest: SubmissionManifest) -> object | None: ...


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
    execution_broker: ExecutionBroker | None = None,
    execution_notifier: ExecutionNotifier | None = None,
    guest_input_transfer: GuestInputTransferer | None = None,
    submission_service: SubmissionService | None = None,
    lease_seconds: int = 300,
    now: int | None = None,
) -> WorkerCycle:
    claim = state.claim_work(owner, lease_seconds, now=now)
    if claim is None:
        _deliver_pending_notification(state, execution_notifier, submission_service, now)
        submission_cycle = _process_submission(state, submission_service, owner, lease_seconds, now)
        if submission_cycle is not None:
            _deliver_pending_notification(state, execution_notifier, submission_service, now)
            return submission_cycle
        return WorkerCycle("idle")
    try:
        cycle = _process_claim(
            state,
            provider,
            claim,
            artifact_preparer=artifact_preparer,
            image_importer=image_importer,
            execution_broker=execution_broker,
            execution_notifier=execution_notifier,
            guest_input_transfer=guest_input_transfer,
            now=now,
        )
        _deliver_pending_notification(state, execution_notifier, submission_service, now)
        return cycle
    except ApprovalStateError:
        raise
    except StorageCapacityError:
        if not state.retry_work(
            claim,
            "storage_capacity",
            _retry_delay(claim.item.attempts),
            now=now,
            exhaustible=False,
        ):
            return WorkerCycle("ownership_lost", claim.item.selected_mode)
        return WorkerCycle("storage_capacity", claim.item.selected_mode)
    except (OSError, RuntimeError, ValueError):
        if _is_cleanup_claim(claim, execution_broker):
            error_code = (
                "execution_complete"
                if claim.item.error_code == "execution_complete"
                else "cleanup_failed"
            )
            if not state.retry_work(
                claim,
                error_code,
                _retry_delay(claim.item.attempts),
                now=now,
                exhaustible=False,
            ):
                return WorkerCycle("ownership_lost", claim.item.selected_mode)
            return WorkerCycle("cleanup_retry", claim.item.selected_mode)
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
    execution_broker: ExecutionBroker | None,
    execution_notifier: ExecutionNotifier | None,
    guest_input_transfer: GuestInputTransferer | None,
    now: int | None,
) -> WorkerCycle:
    item = claim.item
    cleanup_ready = (
        item.status == "ready"
        and item.lab_handle is not None
        and (execution_broker is None or item.error_code == "execution_complete")
    )
    if (item.status == "failed" and item.lab_handle is not None) or cleanup_ready:
        provider.teardown(
            cast(LabHandle, item.lab_handle),
            idempotency_key=f"cleanup-{item.provision_key}",
        )
        if _requires_image_import(item.event):
            if image_importer is None:
                raise RuntimeError("image importer is unavailable during cleanup")
            image_importer.cleanup(idempotency_key=item.provision_key)
        if not state.mark_cleaned(claim, now=now):
            return WorkerCycle("ownership_lost", item.selected_mode)
        return WorkerCycle("lab_cleaned", item.selected_mode)
    if item.status == "pending":
        if item.selected_mode is ExecutionMode.CENTRAL:
            try:
                central_protocol.validate_declared_prepared_input_envelope(
                    tuple(
                        (attachment.filename, attachment.size_bytes)
                        for attachment in item.event.attachments
                        if not attachment.is_lab_artifact
                    )
                )
            except central_protocol.CentralProtocolError:
                if not state.fail_work(claim, "central_input_envelope_invalid", now=now):
                    return WorkerCycle("ownership_lost", item.selected_mode)
                return WorkerCycle("central_input_envelope_invalid", item.selected_mode)
            if not state.mark_ready(claim, now=now, for_execution=execution_broker is not None):
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

    if item.status == "ready" and execution_broker is not None:
        if artifact_preparer is None:
            raise RuntimeError("artifact preparer is unavailable during execution")
        prepared = replace(
            artifact_preparer.prepare(item.event),
            specification_digest=item.specification_digest.value,
        )
        if item.selected_mode is not ExecutionMode.CENTRAL:
            prepared = _with_guest_inputs(
                item.event,
                prepared,
                cast(LabHandle, item.lab_handle),
                cast(GuestCommandExecutor, provider),
                guest_input_transfer,
            )
        progress = execution_broker.step(
            item.event,
            prepared,
            item.selected_mode,
            item.lab_handle,
            cast(LabCommandExecutor, provider),
        )
        if progress.status == "pending":
            if not state.retry_work(claim, "agent_pending", 15, now=now, exhaustible=False):
                return WorkerCycle("ownership_lost", item.selected_mode)
            return WorkerCycle("agent_pending", item.selected_mode)
        if progress.status == "failed":
            if not state.complete_execution(
                claim,
                succeeded=False,
                summary=progress.summary,
                report_markdown=progress.report_markdown,
                provenance=progress.provenance,
                now=now,
            ):
                return WorkerCycle("ownership_lost", item.selected_mode)
            return WorkerCycle("execution_failed", item.selected_mode)
        if not state.complete_execution(
            claim,
            succeeded=True,
            summary=progress.summary,
            report_markdown=progress.report_markdown,
            provenance=progress.provenance,
            now=now,
        ):
            return WorkerCycle("ownership_lost", item.selected_mode)
        return WorkerCycle("execution_complete", item.selected_mode)

    if item.status != "lab_pending" or item.lab_handle is None:
        raise ApprovalStateError("claimed work has an invalid state")
    readiness = provider.readiness(item.lab_handle)
    if readiness is LabReadiness.READY:
        if item.selected_mode is not ExecutionMode.CENTRAL:
            if artifact_preparer is None:
                raise RuntimeError("artifact preparer is unavailable during guest input transfer")
            prepared = replace(
                artifact_preparer.prepare(item.event),
                specification_digest=item.specification_digest.value,
            )
            _with_guest_inputs(
                item.event,
                prepared,
                item.lab_handle,
                cast(GuestCommandExecutor, provider),
                guest_input_transfer,
            )
        if not state.mark_ready(claim, now=now, for_execution=execution_broker is not None):
            return WorkerCycle("ownership_lost", item.selected_mode)
        return WorkerCycle("lab_ready", item.selected_mode)
    if readiness is LabReadiness.FAILED:
        if not state.retry_work(claim, "lab_failed", _retry_delay(item.attempts), now=now):
            return WorkerCycle("ownership_lost", item.selected_mode)
        return WorkerCycle("retry", item.selected_mode)
    if not state.retry_work(claim, "lab_pending", 30, now=now, exhaustible=False):
        return WorkerCycle("ownership_lost", item.selected_mode)
    return WorkerCycle("lab_pending", item.selected_mode)


def _with_guest_inputs(
    event: NotificationEvent,
    prepared: PreparedAssignment,
    handle: LabHandle,
    executor: GuestCommandExecutor,
    transferer: GuestInputTransferer | None,
) -> PreparedAssignment:
    if transferer is None:
        raise RuntimeError("guest input transfer is unavailable")
    ready = transferer.ensure(
        event,
        prepared,
        handle,
        executor,
        excluded_attachment_keys=_direct_import_attachment_keys(event, prepared),
    )
    return replace(
        prepared,
        guest_input_transfer_digest=ready.transfer_digest,
        guest_input_paths=ready.guest_paths,
    )


def _is_cleanup_claim(claim: WorkClaim, execution_broker: ExecutionBroker | None) -> bool:
    item = claim.item
    return (item.status == "failed" and item.lab_handle is not None) or (
        item.status == "ready"
        and item.lab_handle is not None
        and (execution_broker is None or item.error_code == "execution_complete")
    )


def _retry_delay(attempts: int) -> int:
    exponent: int = min(max(attempts - 1, 0), 6)
    delays = (30, 60, 120, 240, 480, 960, 1800)
    return delays[exponent]


def _deliver_pending_notification(
    state: ApprovalState,
    notifier: ExecutionNotifier | None,
    submission_service: SubmissionService | None,
    now: int | None,
) -> None:
    notification = state.pending_execution_notification()
    if notification is not None and notifier is not None:
        progress = ExecutionProgress(
            "succeeded" if notification.succeeded else "failed",
            notification.summary,
            notification.report_markdown,
            notification.provenance,
        )
        try:
            notifier.notify(notification.event, progress)
            if notification.succeeded and notification.event.assignment_id is not None:
                _offer_submission_approval(state, notifier, submission_service, notification, now)
        except RuntimeError:
            return
        state.mark_execution_notification_delivered(notification, now=now)
    if notifier is None or not isinstance(notifier, SubmissionNotifier):
        return
    submission = state.pending_submission_notification()
    if submission is None:
        return
    try:
        notifier.notify_submission_result(submission)
    except RuntimeError:
        return
    state.mark_submission_notification_delivered(submission, now=now)


def _offer_submission_approval(
    state: ApprovalState,
    notifier: ExecutionNotifier,
    service: SubmissionService | None,
    notification: object,
    now: int | None,
) -> None:
    event = getattr(notification, "event", None)
    if not isinstance(event, NotificationEvent) or not isinstance(notifier, SubmissionNotifier):
        return
    if not event.submission_drafts and event.requires_submission_statement:
        notifier.notify_submission_blocked(
            event, "la actividad exige la declaración de entrega del alumno"
        )
        return
    if service is None:
        raise RuntimeError("Moodle submission service is unavailable")
    try:
        service.can_offer_submission(event)
    except UnsupportedSubmissionPolicyError:
        notifier.notify_submission_blocked(
            event, "la actividad exige la declaración de entrega del alumno"
        )
        return
    except PermanentSubmissionOfferError:
        notifier.notify_submission_blocked(
            event, "Moodle no habilita una entrega verificable para esta revisión"
        )
        return
    manifest, buttons = state.prepare_submission(
        event,
        getattr(notification, "summary", ""),
        getattr(notification, "report_markdown", ""),
        now=now,
    )
    notifier.notify_submission_ready(manifest, buttons)


def _process_submission(
    state: ApprovalState,
    service: SubmissionService | None,
    owner: str,
    lease_seconds: int,
    now: int | None,
) -> WorkerCycle | None:
    if service is None:
        return None
    claim = state.claim_submission(owner, lease_seconds, now=now)
    if claim is None:
        return None
    try:
        if claim.phase == "saving":
            if claim.manifest.event.submission_drafts:
                draft = service.verify_draft(claim.manifest)
                if draft is None:
                    state.fail_submission(claim, "submission_draft_unverified", now=now)
                    return WorkerCycle("submission_draft_unverified")
                finalizing = state.record_submission_finalizing(claim, now=now)
                if finalizing is None:
                    return WorkerCycle("submission_ownership_lost")
                return _finalize_submission(state, service, finalizing, now)
            receipt = service.verify(claim.manifest)
            if receipt is None:
                state.fail_submission(claim, "submission_ambiguous", now=now)
                return WorkerCycle("submission_ambiguous")
            reference = getattr(receipt, "reference", None)
            if not isinstance(reference, str) or not reference:
                raise RuntimeError("submission receipt is invalid")
            if not state.complete_submission(claim, reference, now=now):
                return WorkerCycle("submission_ownership_lost")
            return WorkerCycle("submission_confirmed")
        if claim.phase == "finalizing":
            return _finalize_submission(state, service, claim, now)
        draft_item_id = service.upload(claim.manifest)
        persisted = state.record_submission_draft(claim, draft_item_id, now=now)
        if persisted is None:
            return WorkerCycle("submission_ownership_lost")
        try:
            service.save(persisted.manifest, draft_item_id)
        except RuntimeError:
            if persisted.manifest.event.submission_drafts:
                draft = service.verify_draft(persisted.manifest)
                if draft is not None:
                    finalizing = state.record_submission_finalizing(persisted, now=now)
                    if finalizing is None:
                        return WorkerCycle("submission_ownership_lost")
                    return _finalize_submission(state, service, finalizing, now)
                state.fail_submission(persisted, "submission_ambiguous", now=now)
                return WorkerCycle("submission_ambiguous")
            receipt = service.verify(persisted.manifest)
            if receipt is not None:
                reference = getattr(receipt, "reference", None)
                if isinstance(reference, str) and reference:
                    if not state.complete_submission(persisted, reference, now=now):
                        return WorkerCycle("submission_ownership_lost")
                    return WorkerCycle("submission_confirmed")
            state.fail_submission(persisted, "submission_ambiguous", now=now)
            return WorkerCycle("submission_ambiguous")
        if persisted.manifest.event.submission_drafts:
            draft = service.verify_draft(persisted.manifest)
            if draft is None:
                state.fail_submission(persisted, "submission_draft_unverified", now=now)
                return WorkerCycle("submission_draft_unverified")
            finalizing = state.record_submission_finalizing(persisted, now=now)
            if finalizing is None:
                return WorkerCycle("submission_ownership_lost")
            return _finalize_submission(state, service, finalizing, now)
        receipt = service.verify(persisted.manifest)
        if receipt is None:
            state.fail_submission(persisted, "submission_unverified", now=now)
            return WorkerCycle("submission_unverified")
        reference = getattr(receipt, "reference", None)
        if not isinstance(reference, str) or not reference:
            raise RuntimeError("submission receipt is invalid")
        if not state.complete_submission(persisted, reference, now=now):
            return WorkerCycle("submission_ownership_lost")
        return WorkerCycle("submission_confirmed")
    except (ApprovalStateError, RuntimeError, ValueError):
        # Never retry an uncertain Moodle save automatically; durable state and
        # the next operator notification make the boundary explicit.
        if state.fail_submission(claim, "submission_failed", now=now):
            return WorkerCycle("submission_failed")
        return WorkerCycle("submission_ownership_lost")


def _finalize_submission(
    state: ApprovalState,
    service: SubmissionService,
    claim: SubmissionClaim,
    now: int | None,
) -> WorkerCycle:
    # A previous worker may have completed Moodle's transition before crashing.
    # Reconcile first so recovery never submits twice.
    receipt = service.verify(claim.manifest)
    if receipt is not None:
        reference = getattr(receipt, "reference", None)
        if not isinstance(reference, str) or not reference:
            raise RuntimeError("submission receipt is invalid")
        if not state.complete_submission(claim, reference, now=now):
            return WorkerCycle("submission_ownership_lost")
        return WorkerCycle("submission_confirmed")
    draft = service.verify_draft(claim.manifest)
    if draft is None:
        state.fail_submission(claim, "submission_draft_unverified", now=now)
        return WorkerCycle("submission_draft_unverified")
    try:
        service.finalize(claim.manifest)
    except RuntimeError as error:
        # The final Moodle response can be lost after its side effect.
        # Reconcile only a digest-bound submitted receipt; an exact draft
        # is still ambiguous and must not be finalized again automatically.
        receipt = service.verify(claim.manifest)
        if receipt is not None:
            reference = getattr(receipt, "reference", None)
            if not isinstance(reference, str) or not reference:
                raise RuntimeError("submission receipt is invalid") from error
            if not state.complete_submission(claim, reference, now=now):
                return WorkerCycle("submission_ownership_lost")
            return WorkerCycle("submission_confirmed")
        if service.verify_draft(claim.manifest) is not None:
            state.fail_submission(claim, "submission_ambiguous", now=now)
            return WorkerCycle("submission_ambiguous")
        raise
    receipt = service.verify(claim.manifest)
    if receipt is None:
        state.fail_submission(claim, "submission_unverified", now=now)
        return WorkerCycle("submission_unverified")
    reference = getattr(receipt, "reference", None)
    if not isinstance(reference, str) or not reference:
        raise RuntimeError("submission receipt is invalid")
    if not state.complete_submission(claim, reference, now=now):
        return WorkerCycle("submission_ownership_lost")
    return WorkerCycle("submission_confirmed")


def _requires_image_import(event: NotificationEvent) -> bool:
    return any(
        attachment.filename.lower().endswith((".ova", ".ovf", ".vdi", ".vmdk", ".vhd", ".vhdx"))
        for attachment in event.attachments
    )


def _direct_import_attachment_keys(
    event: NotificationEvent, prepared: PreparedAssignment
) -> frozenset[str]:
    """Select the exact direct-AMI source; transfer policy is topology, not suffix, based."""
    if not _requires_image_import(event):
        return frozenset()
    selected = tuple(item for item in prepared.artifacts if item.filename.lower().endswith(".ova"))
    if len(selected) != 1:
        raise RuntimeError("direct image import has no exact OVA input")
    return frozenset({selected[0].attachment_key})
