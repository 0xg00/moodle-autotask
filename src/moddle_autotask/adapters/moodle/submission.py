"""Explicit, verified Moodle assignment-file submission boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .approval_state import SubmissionManifest
from .client import MoodleClient, MoodleClientError
from .config import MoodleConnectionConfig
from .service import (
    MoodleRequiredFunctionCapabilityError,
    MoodleService,
    MoodleServiceError,
    MoodleUploadCapabilityError,
)
from .state import NotificationEvent


class MoodleSubmissionError(RuntimeError):
    """Sanitized failure; never carries a Moodle token or remote body."""


class PermanentSubmissionOfferError(MoodleSubmissionError):
    """A successful Moodle response conclusively forbids this exact offer."""


class UnsupportedSubmissionPolicyError(PermanentSubmissionOfferError):
    """Permanent: this assignment needs a Moodle confirmation flow we do not expose."""


class MoodleSubmissionCaller(Protocol):
    def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object: ...

    def upload_draft_file(self, filename: str, content: bytes) -> int: ...

    def pluginfile_digest(self, file_url: str, expected_size: int) -> str: ...


class MoodleSubmissionSite(Protocol):
    def verified_site_url(
        self,
        required_functions: frozenset[str] = frozenset(),
        *,
        require_uploadfiles: bool = False,
    ) -> str: ...

    def assignments(self) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True)
class MoodleSubmissionReceipt:
    submission_id: int

    @property
    def reference(self) -> str:
        return f"moodle-submission:{self.submission_id}"


class MoodleSubmissionClient:
    """Uses upload.php then the official mod_assign_save_submission service.

    The caller persists the returned draft item id before calling ``save`` so a
    restart can verify rather than blindly saving a second copy.
    """

    def __init__(
        self,
        config: MoodleConnectionConfig,
        client: MoodleSubmissionCaller | None = None,
        service: MoodleSubmissionSite | None = None,
    ) -> None:
        self.config = config
        self.client = client or MoodleClient(config)
        self.service = service or MoodleService(config, self.client)
        self._verified = False
        self._finalization_verified = False

    def upload(self, manifest: SubmissionManifest) -> int:
        self._verify_service()
        self._reject_unsupported_policy(manifest)
        self._assert_current_manifest(manifest)
        self._assert_file_policy(manifest)
        try:
            return self.client.upload_draft_file(
                manifest.filename, manifest.report_markdown.encode("utf-8")
            )
        except MoodleClientError as error:
            raise MoodleSubmissionError("could not upload Moodle submission draft") from error

    def save(self, manifest: SubmissionManifest, draft_item_id: int) -> None:
        self._verify_service()
        self._reject_unsupported_policy(manifest)
        # This is the final pre-mutation check.  A changed, replaced, or
        # deleted assignment invalidates the human approval rather than being
        # silently submitted to a different revision.
        self._assert_current_manifest(manifest)
        if (
            not isinstance(draft_item_id, int)
            or isinstance(draft_item_id, bool)
            or draft_item_id <= 0
        ):
            raise MoodleSubmissionError("Moodle draft item identity is invalid")
        try:
            result = self.client.call(
                "mod_assign_save_submission",
                {
                    "assignmentid": manifest.event.assignment_id or 0,
                    "plugindata[files_filemanager]": draft_item_id,
                },
            )
        except MoodleClientError as error:
            raise MoodleSubmissionError("could not save Moodle submission") from error
        if result != []:
            raise MoodleSubmissionError("Moodle save submission response is invalid")

    def finalize(self, manifest: SubmissionManifest) -> None:
        """Submit a verified Moodle draft using the documented consent parameter."""
        self._verify_service(finalization=True)
        self._assert_current_manifest(manifest)
        if not manifest.event.submission_drafts:
            return
        try:
            result = self.client.call(
                "mod_assign_submit_for_grading",
                {
                    "assignmentid": manifest.event.assignment_id or 0,
                    "acceptsubmissionstatement": 1
                    if manifest.event.requires_submission_statement
                    else 0,
                },
            )
        except MoodleClientError as error:
            raise MoodleSubmissionError("could not finalize Moodle submission") from error
        if result == []:
            return
        # Moodle may have completed the transition while returning a warning.
        # Reconcile only by the exact, file-digest-bound submitted status.
        if self.verify(manifest) is None:
            raise MoodleSubmissionError("Moodle finalization response is invalid")

    def can_offer_submission(self, event: NotificationEvent) -> None:
        if not isinstance(event, NotificationEvent):
            raise MoodleSubmissionError("Moodle submission event is invalid")
        self._reject_unsupported_policy(event)
        self._verify_service(finalization=event.submission_drafts)
        self._assert_current_event(event)
        self._preflight(event)

    def verify(self, manifest: SubmissionManifest) -> MoodleSubmissionReceipt | None:
        return self._verify_status(manifest, "submitted")

    def verify_draft(self, manifest: SubmissionManifest) -> MoodleSubmissionReceipt | None:
        return self._verify_status(manifest, "draft")

    def _verify_status(
        self, manifest: SubmissionManifest, expected_status: str
    ) -> MoodleSubmissionReceipt | None:
        self._verify_service()
        try:
            raw = self.client.call(
                "mod_assign_get_submission_status", {"assignid": manifest.event.assignment_id or 0}
            )
        except MoodleClientError as error:
            raise MoodleSubmissionError("could not verify Moodle submission") from error
        result = _submission_receipt(raw, manifest, expected_status)
        if result is None:
            return None
        receipt, file_url = result
        try:
            digest = self.client.pluginfile_digest(
                file_url, len(manifest.report_markdown.encode("utf-8"))
            )
        except MoodleClientError as error:
            raise MoodleSubmissionError("could not verify Moodle submission file") from error
        if digest != manifest.report_digest:
            raise MoodleSubmissionError("Moodle submission file digest differs")
        return receipt

    def _verify_service(self, *, finalization: bool = False) -> None:
        if self._verified and (not finalization or self._finalization_verified):
            return
        try:
            self.service.verified_site_url(
                frozenset(
                    {"mod_assign_save_submission", "mod_assign_get_submission_status"}
                    | ({"mod_assign_submit_for_grading"} if finalization else set())
                ),
                require_uploadfiles=True,
            )
        except (MoodleRequiredFunctionCapabilityError, MoodleUploadCapabilityError) as error:
            raise PermanentSubmissionOfferError(
                "Moodle upload capability is not enabled"
            ) from error
        except MoodleServiceError as error:
            raise MoodleSubmissionError("could not verify Moodle submission service") from error
        self._verified = True
        self._finalization_verified = self._finalization_verified or finalization

    def _assert_current_manifest(self, manifest: SubmissionManifest) -> None:
        self._assert_current_event(manifest.event)

    def _assert_file_policy(self, manifest: SubmissionManifest) -> None:
        matches = [
            item
            for item in self.service.assignments()
            if getattr(item, "assignment_id", None) == manifest.event.assignment_id
        ]
        if len(matches) != 1:
            raise PermanentSubmissionOfferError("approved Moodle assignment no longer exists")
        assignment = matches[0]
        size = len(manifest.report_markdown.encode("utf-8"))
        if size > getattr(assignment, "file_submission_max_bytes", 2 * 1024 * 1024):
            raise PermanentSubmissionOfferError("Moodle assignment file size limit is too small")
        filetypes = getattr(assignment, "file_submission_filetypes", ".md")
        if (
            isinstance(filetypes, str)
            and filetypes.strip()
            and ".md" not in {item.strip().lower() for item in filetypes.replace(",", " ").split()}
        ):
            raise PermanentSubmissionOfferError("Moodle assignment does not accept Markdown files")

    def _assert_current_event(self, event: object) -> None:
        try:
            matches = [
                item
                for item in self.service.assignments()
                if getattr(item, "assignment_id", None) == getattr(event, "assignment_id", None)
            ]
        except MoodleServiceError as error:
            raise MoodleSubmissionError("could not revalidate Moodle assignment") from error
        if len(matches) != 1:
            raise PermanentSubmissionOfferError("approved Moodle assignment no longer exists")
        current = matches[0]
        if (
            getattr(current, "task_key", None) != getattr(event, "task_key", None)
            or getattr(current, "revision_digest", None) != getattr(event, "revision_digest", None)
            or getattr(current, "assignment_id", None) != getattr(event, "assignment_id", None)
        ):
            raise PermanentSubmissionOfferError("approved Moodle assignment revision changed")

    def _preflight(self, event: NotificationEvent) -> None:
        matches = [
            item
            for item in self.service.assignments()
            if getattr(item, "assignment_id", None) == event.assignment_id
        ]
        if len(matches) != 1:
            raise PermanentSubmissionOfferError("approved Moodle assignment no longer exists")
        assignment = matches[0]
        if any(
            bool(getattr(assignment, name, False)) for name in ("team_submission", "no_submissions")
        ):
            raise PermanentSubmissionOfferError(
                "Moodle assignment submission mode is not supported"
            )
        if not bool(getattr(assignment, "file_submission_enabled", True)):
            raise PermanentSubmissionOfferError("Moodle file submission plugin is disabled")
        if getattr(assignment, "file_submission_max_files", 1) != 1:
            raise PermanentSubmissionOfferError("Moodle assignment does not permit one exact file")
        if getattr(assignment, "file_submission_max_bytes", 2 * 1024 * 1024) < 1:
            raise PermanentSubmissionOfferError("Moodle assignment file size is invalid")
        if event.requires_submission_statement and not event.submission_statement:
            raise PermanentSubmissionOfferError("Moodle submission statement is missing")
        try:
            raw = self.client.call(
                "mod_assign_get_submission_status", {"assignid": event.assignment_id or 0}
            )
        except MoodleClientError as error:
            raise MoodleSubmissionError("could not preflight Moodle submission") from error
        if not isinstance(raw, Mapping):
            raise MoodleSubmissionError("Moodle submission preflight is invalid")
        last_attempt = raw.get("lastattempt")
        if not isinstance(last_attempt, Mapping):
            raise MoodleSubmissionError("Moodle submission preflight is invalid")
        summary = raw.get("gradingsummary")
        if summary is not None and not isinstance(summary, Mapping):
            raise MoodleSubmissionError("Moodle submission preflight is invalid")
        for name, required in (
            ("submissionsenabled", True),
            ("locked", False),
            ("canedit", True),
            ("caneditowner", True),
        ):
            value = last_attempt.get(name)
            if not isinstance(value, bool):
                raise MoodleSubmissionError("Moodle submission preflight is invalid")
            if value is not required:
                raise PermanentSubmissionOfferError("Moodle submission is not currently editable")
        # ``cansubmit`` is deliberately not a prerequisite: Moodle reports it
        # false for normal direct-save assignments that do not use a separate
        # submit-for-grading transition.  Any material submission object,
        # however, is an existing attempt and must never be overwritten.
        for name in ("submission", "teamsubmission"):
            existing = last_attempt.get(name)
            if existing is not None and not isinstance(existing, Mapping):
                raise MoodleSubmissionError("Moodle submission preflight is invalid")
            if isinstance(existing, Mapping):
                raise PermanentSubmissionOfferError("Moodle assignment already has a submission")

    @staticmethod
    def _reject_unsupported_policy(manifest: SubmissionManifest | NotificationEvent) -> None:
        event = manifest.event if isinstance(manifest, SubmissionManifest) else manifest
        if not event.submission_drafts and event.requires_submission_statement:
            raise UnsupportedSubmissionPolicyError(
                "Moodle assignment requires a student submission statement"
            )


def _submission_receipt(
    raw: object, manifest: SubmissionManifest, expected_status: str = "submitted"
) -> tuple[MoodleSubmissionReceipt, str] | None:
    if (
        not isinstance(raw, Mapping)
        or set(raw)
        - {
            "lastattempt",
            "feedback",
            "previousattempts",
            "assignmentdata",
            "gradingsummary",
            "warnings",
        }
        or "assignmentdata" not in raw
        or "warnings" not in raw
    ):
        raise MoodleSubmissionError("Moodle submission status is invalid")
    if (
        not (
            isinstance(raw["assignmentdata"], Mapping)
            or (isinstance(raw["assignmentdata"], list) and not raw["assignmentdata"])
        )
        or not isinstance(raw["warnings"], list)
        or raw["warnings"]
        or ("gradingsummary" in raw and not isinstance(raw["gradingsummary"], Mapping))
        or ("lastattempt" in raw and not isinstance(raw["lastattempt"], Mapping))
        or ("feedback" in raw and not isinstance(raw["feedback"], Mapping))
        or ("previousattempts" in raw and not isinstance(raw["previousattempts"], list))
    ):
        raise MoodleSubmissionError("Moodle submission status is invalid")
    last_attempt = raw.get("lastattempt")
    if last_attempt is None:
        return None
    submission = last_attempt.get("submission")
    if not isinstance(submission, Mapping):
        return None
    submission_id = submission.get("id")
    status = submission.get("status")
    plugins = submission.get("plugins")
    if (
        not isinstance(submission_id, int)
        or isinstance(submission_id, bool)
        or submission_id <= 0
        or status != expected_status
        or not isinstance(plugins, list)
    ):
        return None
    relevant_areas: list[Mapping[object, object]] = []
    for plugin in plugins:
        if not isinstance(plugin, Mapping):
            raise MoodleSubmissionError("Moodle submission file is invalid")
        if plugin.get("type") != "file":
            continue
        areas = plugin.get("fileareas")
        if not isinstance(areas, list):
            raise MoodleSubmissionError("Moodle submission file is invalid")
        for area in areas:
            if not isinstance(area, Mapping):
                raise MoodleSubmissionError("Moodle submission file is invalid")
            if area.get("area") != "submission_files":
                continue
            relevant_areas.append(area)
    if len(relevant_areas) != 1:
        raise MoodleSubmissionError("Moodle submission file layout is invalid")
    files = relevant_areas[0].get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], Mapping):
        raise MoodleSubmissionError("Moodle submission file layout is invalid")
    item = files[0]
    size = item.get("filesize")
    file_url = item.get("fileurl")
    if (
        item.get("filename") != manifest.filename
        or type(size) is not int
        or size != len(manifest.report_markdown.encode("utf-8"))
        or not isinstance(file_url, str)
    ):
        raise MoodleSubmissionError("Moodle submission file differs")
    return MoodleSubmissionReceipt(submission_id), file_url
