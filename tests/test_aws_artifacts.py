from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from moddle_autotask.adapters.aws.artifacts import (
    ArtifactPreparationError,
    AwsMoodleArtifactPreparer,
)
from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.downloads import MoodleDownloadReceipt
from moddle_autotask.adapters.moodle.models import (
    MoodleAssignmentSnapshot,
    MoodleAttachment,
)
from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationAttachment,
    NotificationDraft,
    NotificationEvent,
)


@dataclass
class _Loader:
    values: tuple[MoodleAssignmentSnapshot, ...]

    def assignments(self) -> tuple[MoodleAssignmentSnapshot, ...]:
        return self.values


@dataclass
class _Runner:
    corrupt_head: bool = False
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run_json(
        self,
        arguments: tuple[str, ...],
        *,
        extra_environment: Mapping[str, str] | None = None,
    ) -> object:
        assert extra_environment is None
        self.calls.append(arguments)
        if arguments[1] == "put-object":
            body = Path(arguments[arguments.index("--body") + 1])
            assert body.read_bytes() == b"approved ova!"
            return {"ChecksumSHA256": arguments[arguments.index("--checksum-sha256") + 1]}
        checksum = arguments[arguments.index("--key") + 1].split("/")[-2]
        encoded = base64.b64encode(bytes.fromhex(checksum)).decode("ascii")
        return {
            "ContentLength": 13 if not self.corrupt_head else 12,
            "ChecksumSHA256": encoded,
            "Metadata": {"moodle-sha256": checksum},
            "ServerSideEncryption": "AES256",
        }


def _snapshot() -> MoodleAssignmentSnapshot:
    attachment = MoodleAttachment(
        "moodle-attachment-v1:" + "c" * 64,
        "introattachments",
        "base.ova",
        "/",
        "http://127.0.0.1:8000/webservice/pluginfile.php/1/base.ova",
        13,
        1,
        "application/octet-stream",
    )
    return MoodleAssignmentSnapshot(
        "moodle-task-v1:" + "a" * 64,
        "moodle-assignment-v1:" + "b" * 64,
        "http://127.0.0.1:8000",
        1,
        2,
        "ASIX",
        "ASIX-M06",
        3,
        "Lab",
        "Do it",
        0,
        100,
        0,
        0,
        1,
        (attachment,),
    )


def _event(tmp_path: Path, snapshot: MoodleAssignmentSnapshot) -> NotificationEvent:
    draft = NotificationDraft(
        snapshot.task_key,
        snapshot.revision_digest,
        snapshot.course_name,
        snapshot.course_shortname,
        snapshot.title,
        snapshot.allows_submissions_from,
        snapshot.due_date,
        snapshot.cutoff_date,
        snapshot.grading_due_date,
        snapshot.time_modified,
        (NotificationAttachment("base.ova", 13, "application/octet-stream", True),),
    )
    event = MoodleState(tmp_path / "state.sqlite3").enqueue(draft, now=1)
    assert event is not None
    return event


def _download(
    config: MoodleConnectionConfig,
    assignment: MoodleAssignmentSnapshot,
    attachment_key: str,
    output_directory: Path,
    max_size_bytes: int | None = None,
) -> MoodleDownloadReceipt:
    assert config.base_url == assignment.site_url
    assert attachment_key == assignment.attachments[0].attachment_key
    assert max_size_bytes == config.max_download_bytes
    path = output_directory / "base.ova"
    path.write_bytes(b"approved ova!")
    return MoodleDownloadReceipt(path, 13, hashlib.sha256(b"approved ova!").hexdigest())


def test_exact_revision_is_downloaded_and_verified_in_private_prefix(tmp_path: Path) -> None:
    snapshot = _snapshot()
    runner = _Runner()
    preparer = AwsMoodleArtifactPreparer(
        MoodleConnectionConfig(snapshot.site_url, "secret"),
        "moodle-autotask-artifacts-123456789012-eu-south-2",
        "eu-south-2",
        tmp_path / "work",
        runner,
        loader=_Loader((snapshot,)),
        downloader=_download,
    )

    event = _event(tmp_path, snapshot)
    result = preparer.prepare(event)

    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.sha256 == hashlib.sha256(b"approved ova!").hexdigest()
    assert artifact.object_key == (
        f"assignments/{'a' * 64}/{'b' * 64}/{'c' * 64}/"
        f"{artifact.sha256}/base.ova"
    )
    assert [call[1] for call in runner.calls] == ["put-object", "head-object"]
    assert not tuple((tmp_path / "work").iterdir())
    assert preparer.prepare(event) == result
    assert [call[1] for call in runner.calls] == ["put-object", "head-object"]


def test_missing_revision_fails_before_download_or_aws(tmp_path: Path) -> None:
    snapshot = _snapshot()
    runner = _Runner()
    preparer = AwsMoodleArtifactPreparer(
        MoodleConnectionConfig(snapshot.site_url, "secret"),
        "moodle-autotask-artifacts-123456789012-eu-south-2",
        "eu-south-2",
        tmp_path / "work",
        runner,
        loader=_Loader(()),
        downloader=_download,
    )

    with pytest.raises(ArtifactPreparationError, match="revision is unavailable"):
        preparer.prepare(_event(tmp_path, snapshot))
    assert runner.calls == []


def test_staged_integrity_mismatch_fails_closed(tmp_path: Path) -> None:
    snapshot = _snapshot()
    runner = _Runner(corrupt_head=True)
    preparer = AwsMoodleArtifactPreparer(
        MoodleConnectionConfig(snapshot.site_url, "secret"),
        "moodle-autotask-artifacts-123456789012-eu-south-2",
        "eu-south-2",
        tmp_path / "work",
        runner,
        loader=_Loader((snapshot,)),
        downloader=_download,
    )

    with pytest.raises(ArtifactPreparationError, match="integrity"):
        preparer.prepare(_event(tmp_path, snapshot))
