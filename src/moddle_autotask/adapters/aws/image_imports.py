"""Idempotent AWS VM Import/Export adapter for a single approved OVA."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import cast

from .artifacts import PreparedArtifact, PreparedAssignment
from .labs import JsonCommandRunner, _Session

_ROLE = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$")
_ROLE_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$"
)
_REGION = re.compile(r"^[a-z]{2}-[a-z]+-[0-9]$")
_IMPORT_TASK = re.compile(r"^import-ami-[0-9a-f]{8,17}$")
_IMAGE = re.compile(r"^ami-[0-9a-f]{8,17}$")
_SNAPSHOT = re.compile(r"^snap-[0-9a-f]{8,17}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VIRTUAL_DISKS = (".ova", ".ovf", ".vdi", ".vmdk", ".vhd", ".vhdx")


class ImageImportError(RuntimeError):
    pass


class ImageImportReadiness(StrEnum):
    PENDING = "pending"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class ImageImportResult:
    readiness: ImageImportReadiness
    image_id: str | None = None

    def __post_init__(self) -> None:
        if (self.readiness is ImageImportReadiness.READY) != (self.image_id is not None):
            raise ValueError("image import result is inconsistent")


@dataclass(frozen=True, slots=True)
class AwsImageImportConfig:
    region: str
    importer_role_arn: str
    vmimport_role_name: str
    project_name: str = "moodle-autotask"
    environment: str = "development"

    def __post_init__(self) -> None:
        if (
            _REGION.fullmatch(self.region) is None
            or _ROLE_ARN.fullmatch(self.importer_role_arn) is None
            or _ROLE.fullmatch(self.vmimport_role_name) is None
            or not self.project_name
            or not self.environment
        ):
            raise ValueError("image import configuration is invalid")


class AwsImageImporter:
    def __init__(self, config: AwsImageImportConfig, runner: JsonCommandRunner) -> None:
        self._config = config
        self._runner = runner

    def ensure(
        self, prepared: PreparedAssignment, *, idempotency_key: str
    ) -> ImageImportResult:
        artifact = _selected_ova(prepared)
        token = _import_token(idempotency_key, artifact)
        session = self._assume_role(token)
        tags = self._tags(idempotency_key, "image-import")
        started = _mapping(
            self._aws(
                session,
                "ec2",
                "import-image",
                "--region",
                self._config.region,
                "--client-token",
                token,
                "--description",
                f"moodle-autotask:{idempotency_key}",
                "--role-name",
                self._config.vmimport_role_name,
                "--license-type",
                "BYOL",
                "--encrypted",
                "--disk-containers",
                json.dumps(
                    [
                        {
                            "Format": "OVA",
                            "UserBucket": {
                                "S3Bucket": artifact.bucket,
                                "S3Key": artifact.object_key,
                            },
                        }
                    ],
                    separators=(",", ":"),
                ),
                "--tag-specifications",
                json.dumps(
                    [{"ResourceType": "import-image-task", "Tags": tags}],
                    separators=(",", ":"),
                ),
            )
        )
        task_id = _required_string(started, "ImportTaskId")
        if _IMPORT_TASK.fullmatch(task_id) is None:
            raise ImageImportError("AWS returned an invalid import task ID")
        described = self._aws(
            session,
            "ec2",
            "describe-import-image-tasks",
            "--region",
            self._config.region,
            "--import-task-ids",
            task_id,
        )
        tasks = _list_field(described, "ImportImageTasks")
        if len(tasks) != 1:
            raise ImageImportError("AWS did not return exactly one image import task")
        task = tasks[0]
        self._validate_task(task, artifact, idempotency_key)
        status = task.get("Status")
        if status == "active":
            return ImageImportResult(ImageImportReadiness.PENDING)
        if status != "completed" or not isinstance(task.get("ImageId"), str):
            raise ImageImportError("VM image import failed")
        image_id = cast(str, task["ImageId"])
        if _IMAGE.fullmatch(image_id) is None:
            raise ImageImportError("AWS returned an invalid imported image ID")
        snapshot_ids = _snapshot_ids(task)
        ownership = self._tags(idempotency_key, "lab-image")
        self._aws(
            session,
            "ec2",
            "create-tags",
            "--region",
            self._config.region,
            "--resources",
            image_id,
            *snapshot_ids,
            "--tags",
            json.dumps(ownership, separators=(",", ":")),
        )
        self._verify_image(session, image_id, snapshot_ids, idempotency_key)
        return ImageImportResult(ImageImportReadiness.READY, image_id)

    def cleanup(self, *, idempotency_key: str) -> None:
        _require_digest(idempotency_key, "image import idempotency key")
        session = self._assume_role(idempotency_key)
        response = self._aws(
            session,
            "ec2",
            "describe-images",
            "--region",
            self._config.region,
            "--owners",
            "self",
            "--filters",
            f"Name=tag:Project,Values={self._config.project_name}",
            f"Name=tag:Environment,Values={self._config.environment}",
            "Name=tag:Role,Values=lab-image",
            f"Name=tag:ProvisionKey,Values={idempotency_key}",
        )
        images = _list_field(response, "Images")
        if len(images) > 1:
            raise ImageImportError("multiple imported images share one provision key")
        if not images:
            return
        image_id = _required_string(images[0], "ImageId")
        if _IMAGE.fullmatch(image_id) is None:
            raise ImageImportError("AWS returned an invalid imported image ID")
        snapshots = _image_snapshots(images[0])
        self._aws(
            session,
            "ec2",
            "deregister-image",
            "--region",
            self._config.region,
            "--image-id",
            image_id,
        )
        for snapshot_id in snapshots:
            self._aws(
                session,
                "ec2",
                "delete-snapshot",
                "--region",
                self._config.region,
                "--snapshot-id",
                snapshot_id,
            )

    def _validate_task(
        self, task: dict[str, object], artifact: PreparedArtifact, idempotency_key: str
    ) -> None:
        task_id = _required_string(task, "ImportTaskId")
        if _IMPORT_TASK.fullmatch(task_id) is None:
            raise ImageImportError("AWS returned an invalid import task ID")
        tags = _tags(task)
        expected = {
            item["Key"]: item["Value"]
            for item in self._tags(idempotency_key, "image-import")
        }
        if any(tags.get(key) != value for key, value in expected.items()):
            raise ImageImportError("image import task ownership could not be verified")
        details = _list_field(task, "SnapshotDetails")
        if len(details) != 1 or str(details[0].get("Format", "")).lower() != "ova":
            raise ImageImportError("image import task does not match the approved OVA")
        bucket = _mapping(details[0].get("UserBucket"))
        if bucket != {"S3Bucket": artifact.bucket, "S3Key": artifact.object_key}:
            raise ImageImportError("image import task does not match the staged artifact")

    def _verify_image(
        self, session: _Session, image_id: str, snapshots: tuple[str, ...], idempotency_key: str
    ) -> None:
        response = self._aws(
            session,
            "ec2",
            "describe-images",
            "--region",
            self._config.region,
            "--image-ids",
            image_id,
        )
        images = _list_field(response, "Images")
        expected = {
            item["Key"]: item["Value"]
            for item in self._tags(idempotency_key, "lab-image")
        }
        actual = {} if len(images) != 1 else _tags(images[0])
        if len(images) != 1 or any(actual.get(key) != value for key, value in expected.items()):
            raise ImageImportError("imported image ownership could not be verified")
        if set(_image_snapshots(images[0])) != set(snapshots):
            raise ImageImportError("imported image snapshots do not match import task")

    def _assume_role(self, token: str) -> _Session:
        response = self._runner.run_json(
            (
                "sts",
                "assume-role",
                "--role-arn",
                self._config.importer_role_arn,
                "--role-session-name",
                f"moodle-import-{token[:16]}",
                "--duration-seconds",
                "3600",
                "--region",
                self._config.region,
            )
        )
        credentials = _mapping_field(response, "Credentials")
        return _Session(
            _required_string(credentials, "AccessKeyId"),
            _required_string(credentials, "SecretAccessKey"),
            _required_string(credentials, "SessionToken"),
        )

    def _aws(self, session: _Session, *arguments: str) -> object:
        return self._runner.run_json(tuple(arguments), extra_environment=session.environment())

    def _tags(self, provision_key: str, role: str) -> list[dict[str, str]]:
        _require_digest(provision_key, "image import idempotency key")
        return [
            {"Key": "Project", "Value": self._config.project_name},
            {"Key": "Environment", "Value": self._config.environment},
            {"Key": "ManagedBy", "Value": "moodle-autotask"},
            {"Key": "Role", "Value": role},
            {"Key": "ProvisionKey", "Value": provision_key},
        ]


def _selected_ova(prepared: PreparedAssignment) -> PreparedArtifact:
    virtual = tuple(
        item for item in prepared.artifacts if item.filename.lower().endswith(_VIRTUAL_DISKS)
    )
    if len(virtual) != 1 or not virtual[0].filename.lower().endswith(".ova"):
        raise ImageImportError("exactly one OVA is required for image import")
    return virtual[0]


def _import_token(idempotency_key: str, artifact: PreparedArtifact) -> str:
    _require_digest(idempotency_key, "image import idempotency key")
    return sha256(
        f"moodle-image-import-v1\0{idempotency_key}\0{artifact.sha256}".encode()
    ).hexdigest()


def _require_digest(value: str, label: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ImageImportError(f"{label} is invalid")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ImageImportError("AWS returned an invalid image import object")
    return cast(dict[str, object], value)


def _mapping_field(value: object, key: str) -> dict[str, object]:
    mapping = _mapping(value)
    if key not in mapping:
        raise ImageImportError(f"AWS response is missing {key}")
    return _mapping(mapping[key])


def _required_string(value: object, key: str) -> str:
    field = _mapping(value).get(key)
    if not isinstance(field, str) or not field:
        raise ImageImportError(f"AWS response has an invalid {key}")
    return field


def _list_field(value: object, key: str) -> list[dict[str, object]]:
    field = _mapping(value).get(key)
    if not isinstance(field, list):
        raise ImageImportError(f"AWS response has an invalid {key}")
    return [_mapping(item) for item in field]


def _tags(value: object) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _list_field(value, "Tags"):
        key = _required_string(item, "Key")
        field = _required_string(item, "Value")
        if key in result:
            raise ImageImportError("AWS returned duplicate image import tags")
        result[key] = field
    return result


def _snapshot_ids(task: Mapping[str, object]) -> tuple[str, ...]:
    result = tuple(
        _required_string(item, "SnapshotId")
        for item in _list_field(task, "SnapshotDetails")
    )
    if (
        not result
        or any(_SNAPSHOT.fullmatch(item) is None for item in result)
        or len(set(result)) != len(result)
    ):
        raise ImageImportError("AWS returned invalid imported snapshots")
    return result


def _image_snapshots(image: Mapping[str, object]) -> tuple[str, ...]:
    snapshots: list[str] = []
    for device in _list_field(image, "BlockDeviceMappings"):
        ebs = _mapping(device.get("Ebs"))
        snapshot = _required_string(ebs, "SnapshotId")
        if _SNAPSHOT.fullmatch(snapshot) is None:
            raise ImageImportError("AWS returned an invalid image snapshot")
        snapshots.append(snapshot)
    if not snapshots or len(set(snapshots)) != len(snapshots):
        raise ImageImportError("AWS returned invalid image snapshots")
    return tuple(snapshots)
