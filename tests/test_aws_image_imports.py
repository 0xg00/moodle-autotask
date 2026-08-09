from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from moddle_autotask.adapters.aws.artifacts import PreparedArtifact, PreparedAssignment
from moddle_autotask.adapters.aws.image_imports import (
    AwsImageImportConfig,
    AwsImageImporter,
    ImageImportError,
    ImageImportReadiness,
)


def _tags(provision_key: str, role: str) -> list[dict[str, str]]:
    return [
        {"Key": "Project", "Value": "moodle-autotask"},
        {"Key": "Environment", "Value": "development"},
        {"Key": "ManagedBy", "Value": "moodle-autotask"},
        {"Key": "Role", "Value": role},
        {"Key": "ProvisionKey", "Value": provision_key},
    ]


@dataclass
class _Runner:
    completed: bool = False
    corrupt_source: bool = False
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = field(default_factory=list)
    source: dict[str, str] | None = None

    def run_json(
        self,
        arguments: tuple[str, ...],
        *,
        extra_environment: Mapping[str, str] | None = None,
    ) -> object:
        self.calls.append((arguments, extra_environment))
        if arguments[:2] == ("sts", "assume-role"):
            assert extra_environment is None
            return {
                "Credentials": {
                    "AccessKeyId": "A",
                    "SecretAccessKey": "B",
                    "SessionToken": "C",
                }
            }
        assert extra_environment == {
            "AWS_ACCESS_KEY_ID": "A",
            "AWS_SECRET_ACCESS_KEY": "B",
            "AWS_SESSION_TOKEN": "C",
        }
        operation = arguments[1]
        if operation == "import-image":
            containers = json.loads(arguments[arguments.index("--disk-containers") + 1])
            self.source = containers[0]["UserBucket"]
            return {"ImportTaskId": "import-ami-0123456789abcdef0"}
        if operation == "describe-import-image-tasks":
            provision_key = "d" * 64
            assert self.source is not None
            source = self.source
            if self.corrupt_source:
                source = {"S3Bucket": source["S3Bucket"], "S3Key": "wrong.ova"}
            result: dict[str, object] = {
                "ImportTaskId": "import-ami-0123456789abcdef0",
                "Status": "completed" if self.completed else "active",
                "Tags": _tags(provision_key, "image-import"),
                "SnapshotDetails": [
                    {
                        "Format": "ova",
                        "UserBucket": source,
                        **({"SnapshotId": "snap-0123456789abcdef0"} if self.completed else {}),
                    }
                ],
            }
            if self.completed:
                result["ImageId"] = "ami-0123456789abcdef0"
            return {"ImportImageTasks": [result]}
        if operation == "create-tags":
            return {}
        if operation == "describe-images" and "--image-ids" in arguments:
            return {
                "Images": [
                    {
                        "ImageId": "ami-0123456789abcdef0",
                        "Tags": _tags("d" * 64, "lab-image"),
                        "BlockDeviceMappings": [
                            {"Ebs": {"SnapshotId": "snap-0123456789abcdef0"}}
                        ],
                    }
                ]
            }
        if operation == "describe-images":
            return {"Images": []}
        raise AssertionError(arguments)


def _prepared(filename: str = "base.ova") -> PreparedAssignment:
    return PreparedAssignment(
        "moodle-task-v1:" + "a" * 64,
        "moodle-assignment-v1:" + "b" * 64,
        (
            PreparedArtifact(
                "moodle-attachment-v1:" + "c" * 64,
                filename,
                13,
                "e" * 64,
                "moodle-autotask-artifacts-123456789012-eu-south-2",
                "assignments/a/b/c/e/base.ova",
            ),
        ),
    )


def _importer(runner: _Runner) -> AwsImageImporter:
    return AwsImageImporter(
        AwsImageImportConfig(
            "eu-south-2",
            "arn:aws:iam::123456789012:role/moodle-autotask-development-image-importer",
            "moodle-autotask-development-vmimport",
        ),
        runner,
    )


def test_active_import_is_pending_and_uses_idempotent_encrypted_private_ova() -> None:
    runner = _Runner()

    result = _importer(runner).ensure(_prepared(), idempotency_key="d" * 64)

    assert result.readiness is ImageImportReadiness.PENDING and result.image_id is None
    command = next(call for call, _ in runner.calls if call[1] == "import-image")
    assert "--client-token" in command
    assert command[command.index("--license-type") + 1] == "BYOL"
    assert "--encrypted" in command
    assert command[command.index("--role-name") + 1].endswith("-vmimport")


def test_completed_import_tags_and_verifies_image_and_snapshot() -> None:
    runner = _Runner(completed=True)

    result = _importer(runner).ensure(_prepared(), idempotency_key="d" * 64)

    assert result.readiness is ImageImportReadiness.READY
    assert result.image_id == "ami-0123456789abcdef0"
    assert [call[1] for call, _ in runner.calls][-2:] == ["create-tags", "describe-images"]


def test_import_rejects_wrong_s3_source_and_non_ova_before_launch() -> None:
    with pytest.raises(ImageImportError, match="staged artifact"):
        _importer(_Runner(corrupt_source=True)).ensure(
            _prepared(), idempotency_key="d" * 64
        )
    runner = _Runner()
    with pytest.raises(ImageImportError, match="exactly one OVA"):
        _importer(runner).ensure(_prepared("base.vmdk"), idempotency_key="d" * 64)
    assert runner.calls == []


def test_cleanup_is_idempotent_when_no_owned_image_exists() -> None:
    runner = _Runner()
    _importer(runner).cleanup(idempotency_key="d" * 64)
    assert [call[1] for call, _ in runner.calls] == ["assume-role", "describe-images"]
