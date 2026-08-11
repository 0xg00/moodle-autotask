"""Exact Moodle revision download and private S3 staging."""

from __future__ import annotations

import base64
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.downloads import MoodleDownloadReceipt, download_attachment
from moddle_autotask.adapters.moodle.models import MoodleAssignmentSnapshot
from moddle_autotask.adapters.moodle.path_safety import assert_no_indirection
from moddle_autotask.adapters.moodle.service import MoodleService
from moddle_autotask.adapters.moodle.state import NotificationEvent

from .labs import JsonCommandRunner

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}-[a-z]+-[0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ArtifactPreparationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedArtifact:
    attachment_key: str
    filename: str
    size_bytes: int
    sha256: str
    bucket: str
    object_key: str


@dataclass(frozen=True, slots=True)
class PreparedAssignment:
    task_key: str
    revision_digest: str
    artifacts: tuple[PreparedArtifact, ...]
    course_name: str = ""
    course_shortname: str = ""
    title: str = ""
    intro: str = ""
    # Bound by the durable approval record immediately before a central agent job
    # is created.  Keeping this optional preserves the existing lab preparer API.
    specification_digest: str = ""


class AssignmentLoader(Protocol):
    def assignments(self) -> tuple[MoodleAssignmentSnapshot, ...]: ...


class AttachmentDownloader(Protocol):
    def __call__(
        self,
        config: MoodleConnectionConfig,
        assignment: MoodleAssignmentSnapshot,
        attachment_key: str,
        output_directory: Path,
        max_size_bytes: int | None = None,
    ) -> MoodleDownloadReceipt: ...


class AwsMoodleArtifactPreparer:
    """Stages only an assignment snapshot that exactly matches the approved revision."""

    def __init__(
        self,
        config: MoodleConnectionConfig,
        bucket: str,
        region: str,
        working_directory: Path,
        runner: JsonCommandRunner,
        *,
        loader: AssignmentLoader | None = None,
        downloader: AttachmentDownloader = download_attachment,
    ) -> None:
        if _BUCKET.fullmatch(bucket) is None or _REGION.fullmatch(region) is None:
            raise ValueError("artifact staging configuration is invalid")
        self._config = config
        self._bucket = bucket
        self._region = region
        self._working_directory = working_directory
        self._runner = runner
        self._loader = loader or MoodleService(config)
        self._downloader = downloader
        self._prepared: dict[tuple[str, str], PreparedAssignment] = {}

    def prepare(self, event: NotificationEvent) -> PreparedAssignment:
        identity = (event.task_key, event.revision_digest)
        cached = self._prepared.get(identity)
        if cached is not None:
            return cached
        matches = tuple(
            item
            for item in self._loader.assignments()
            if item.task_key == event.task_key and item.revision_digest == event.revision_digest
        )
        if len(matches) != 1:
            raise ArtifactPreparationError("approved Moodle revision is unavailable")
        assignment = matches[0]
        expected = tuple(
            (item.filename, item.size_bytes, item.mimetype) for item in assignment.attachments
        )
        advertised = tuple(
            (item.filename, item.size_bytes, item.mimetype) for item in event.attachments
        )
        if expected != advertised:
            raise ArtifactPreparationError("approved attachment metadata does not match Moodle")
        try:
            assert_no_indirection(self._working_directory)
            self._working_directory.mkdir(parents=True, exist_ok=True)
            assert_no_indirection(self._working_directory)
            if not self._working_directory.is_dir():
                raise ArtifactPreparationError("artifact working directory is unsafe")
            with tempfile.TemporaryDirectory(
                prefix="moodle-artifacts-", dir=self._working_directory
            ) as temporary:
                artifacts = tuple(
                    self._stage(assignment, item.attachment_key, Path(temporary))
                    for item in assignment.attachments
                )
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, ArtifactPreparationError):
                raise
            raise ArtifactPreparationError("could not stage approved Moodle artifacts") from error
        prepared = PreparedAssignment(
            event.task_key,
            event.revision_digest,
            artifacts,
            assignment.course_name,
            assignment.course_shortname,
            assignment.title,
            assignment.intro,
        )
        self._prepared[identity] = prepared
        return prepared

    def _stage(
        self, assignment: MoodleAssignmentSnapshot, attachment_key: str, directory: Path
    ) -> PreparedArtifact:
        receipt = self._downloader(
            self._config, assignment, attachment_key, directory, self._config.max_download_bytes
        )
        attachment = next(
            item for item in assignment.attachments if item.attachment_key == attachment_key
        )
        task = _digest(assignment.task_key, "moodle-task-v1")
        revision = _digest(assignment.revision_digest, "moodle-assignment-v1")
        identity = _digest(attachment_key, "moodle-attachment-v1")
        if _DIGEST.fullmatch(receipt.sha256) is None or receipt.size_bytes != attachment.size_bytes:
            raise ArtifactPreparationError("downloaded artifact receipt is invalid")
        object_key = (
            f"assignments/{task}/{revision}/{identity}/{receipt.sha256}/{attachment.filename}"
        )
        checksum = base64.b64encode(bytes.fromhex(receipt.sha256)).decode("ascii")
        response = self._runner.run_json(
            (
                "s3api",
                "put-object",
                "--region",
                self._region,
                "--bucket",
                self._bucket,
                "--key",
                object_key,
                "--body",
                str(receipt.path),
                "--checksum-algorithm",
                "SHA256",
                "--checksum-sha256",
                checksum,
                "--metadata",
                f"moodle-sha256={receipt.sha256}",
                "--server-side-encryption",
                "AES256",
            )
        )
        _mapping(response)
        head = _mapping(
            self._runner.run_json(
                (
                    "s3api",
                    "head-object",
                    "--region",
                    self._region,
                    "--bucket",
                    self._bucket,
                    "--key",
                    object_key,
                    "--checksum-mode",
                    "ENABLED",
                )
            )
        )
        metadata = _mapping(head.get("Metadata"))
        if (
            head.get("ContentLength") != receipt.size_bytes
            or head.get("ChecksumSHA256") != checksum
            or metadata != {"moodle-sha256": receipt.sha256}
            or head.get("ServerSideEncryption") != "AES256"
        ):
            raise ArtifactPreparationError("staged artifact integrity could not be verified")
        return PreparedArtifact(
            attachment_key,
            attachment.filename,
            receipt.size_bytes,
            receipt.sha256,
            self._bucket,
            object_key,
        )


def _digest(value: str, namespace: str) -> str:
    prefix = f"{namespace}:"
    digest = value.removeprefix(prefix)
    if value != prefix + digest or _DIGEST.fullmatch(digest) is None:
        raise ArtifactPreparationError("approved artifact identity is invalid")
    return digest


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ArtifactPreparationError("AWS returned invalid artifact metadata")
    return cast(dict[str, object], value)
