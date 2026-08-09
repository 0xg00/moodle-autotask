"""AWS CLI backed, capability-limited ephemeral Windows lab provider."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, cast

from moddle_autotask.domain.models import LabHandle, LabProvisionRequest
from moddle_autotask.ports.contracts import LabReadiness

_APPROVED_INSTANCE_TYPES = frozenset({"t3.large", "m6i.large"})
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-[0-9]$")
_ROLE_ARN_PATTERN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$"
)
_SUBNET_PATTERN = re.compile(r"^subnet-[0-9a-f]{8,17}$")
_SECURITY_GROUP_PATTERN = re.compile(r"^sg-[0-9a-f]{8,17}$")
_INSTANCE_PATTERN = re.compile(r"^i-[0-9a-f]{8,17}$")
_IMAGE_PATTERN = re.compile(r"^ami-[0-9a-f]{8,17}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AwsLabError(RuntimeError):
    """Raised when AWS cannot safely establish the requested lab state."""


class JsonCommandRunner(Protocol):
    def run_json(
        self, arguments: tuple[str, ...], *, extra_environment: Mapping[str, str] | None = None
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class AwsCliJsonRunner:
    executable: str = "aws"
    timeout_seconds: int = 120

    def run_json(
        self, arguments: tuple[str, ...], *, extra_environment: Mapping[str, str] | None = None
    ) -> object:
        environment = os.environ.copy()
        if extra_environment is not None:
            environment.pop("AWS_PROFILE", None)
            environment.pop("AWS_DEFAULT_PROFILE", None)
            environment.update(extra_environment)
        try:
            completed = subprocess.run(
                [self.executable, *arguments, "--no-cli-pager", "--output", "json"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            raise AwsLabError("AWS CLI operation timed out") from error
        if completed.returncode != 0:
            raise AwsLabError(f"AWS CLI operation failed with exit code {completed.returncode}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AwsLabError("AWS CLI returned invalid JSON") from error


@dataclass(frozen=True, slots=True)
class AwsLabConfig:
    region: str
    provisioner_role_arn: str
    subnet_id: str
    security_group_id: str
    instance_profile_name: str
    image_id: str
    project_name: str = "moodle-autotask"
    environment: str = "development"
    instance_type: str = "t3.large"
    root_volume_size_gib: int = 80

    def __post_init__(self) -> None:
        checks = (
            (_REGION_PATTERN.fullmatch(self.region), "invalid AWS region"),
            (
                _ROLE_ARN_PATTERN.fullmatch(self.provisioner_role_arn),
                "invalid provisioner role ARN",
            ),
            (_SUBNET_PATTERN.fullmatch(self.subnet_id), "invalid lab subnet ID"),
            (
                _SECURITY_GROUP_PATTERN.fullmatch(self.security_group_id),
                "invalid lab security group ID",
            ),
            (
                _ID_PATTERN.fullmatch(self.instance_profile_name),
                "invalid lab instance profile name",
            ),
            (_IMAGE_PATTERN.fullmatch(self.image_id), "invalid approved image ID"),
            (_ID_PATTERN.fullmatch(self.project_name), "invalid project name"),
            (_ID_PATTERN.fullmatch(self.environment), "invalid environment name"),
        )
        for result, message in checks:
            if result is None:
                raise ValueError(message)
        if self.instance_type not in _APPROVED_INSTANCE_TYPES:
            raise ValueError("instance type is not approved")
        if not 50 <= self.root_volume_size_gib <= 500:
            raise ValueError("root volume size must be between 50 and 500 GiB")


@dataclass(frozen=True, slots=True, repr=False)
class _Session:
    access_key_id: str
    secret_access_key: str
    session_token: str

    def environment(self) -> dict[str, str]:
        return {
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_SESSION_TOKEN": self.session_token,
        }


class AwsEc2LabProvider:
    """Implements LabProvider using a fixed AWS launch profile and short-lived role sessions."""

    def __init__(self, config: AwsLabConfig, runner: JsonCommandRunner | None = None) -> None:
        self._config = config
        self._runner = runner or AwsCliJsonRunner()

    def provision(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle:
        provision_key = self._provision_key(request, idempotency_key)
        existing = self._reconcile_with_key(provision_key)
        if existing is not None:
            return existing

        session = self._assume_role(provision_key)
        image_id = request.image_reference or self._config.image_id
        if _IMAGE_PATTERN.fullmatch(image_id) is None:
            raise AwsLabError("lab request contains an invalid image ID")
        tags = self._tags(request, provision_key)
        tag_specifications = json.dumps(
            [
                {"ResourceType": "instance", "Tags": tags},
                {"ResourceType": "volume", "Tags": tags},
            ],
            separators=(",", ":"),
        )
        block_devices = json.dumps(
            [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "DeleteOnTermination": True,
                        "Encrypted": True,
                        "VolumeSize": self._config.root_volume_size_gib,
                        "VolumeType": "gp3",
                    },
                }
            ],
            separators=(",", ":"),
        )
        response = self._aws(
            session,
            "ec2",
            "run-instances",
            "--region",
            self._config.region,
            "--image-id",
            image_id,
            "--instance-type",
            self._config.instance_type,
            "--subnet-id",
            self._config.subnet_id,
            "--security-group-ids",
            self._config.security_group_id,
            "--iam-instance-profile",
            f"Name={self._config.instance_profile_name}",
            "--associate-public-ip-address",
            "--metadata-options",
            "HttpEndpoint=enabled,HttpTokens=required,HttpPutResponseHopLimit=1",
            "--block-device-mappings",
            block_devices,
            "--tag-specifications",
            tag_specifications,
            "--client-token",
            provision_key,
            "--count",
            "1",
        )
        instances = self._list_field(response, "Instances")
        if len(instances) != 1:
            raise AwsLabError("AWS did not return exactly one lab instance")
        instance_id = self._required_string(instances[0], "InstanceId")
        if _INSTANCE_PATTERN.fullmatch(instance_id) is None:
            raise AwsLabError("AWS returned an invalid lab instance ID")
        return self._handle(instance_id, provision_key)

    def reconcile(
        self, request: LabProvisionRequest, *, idempotency_key: str
    ) -> LabHandle | None:
        return self._reconcile_with_key(self._provision_key(request, idempotency_key))

    def readiness(self, handle: LabHandle) -> LabReadiness:
        instance_id, provision_key = self._parse_handle(handle)
        instance = self._owned_instance(instance_id, provision_key)
        if instance is None:
            return LabReadiness.FAILED
        state = self._nested_string(instance, "State", "Name")
        if state != "running":
            return LabReadiness.PENDING if state == "pending" else LabReadiness.FAILED
        session = self._assume_role(provision_key)
        response = self._aws(
            session,
            "ssm",
            "describe-instance-information",
            "--region",
            self._config.region,
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
        )
        information = self._list_field(response, "InstanceInformationList")
        if not information:
            return LabReadiness.PENDING
        ping_status = self._required_string(information[0], "PingStatus")
        return LabReadiness.READY if ping_status == "Online" else LabReadiness.PENDING

    def teardown(self, handle: LabHandle, *, idempotency_key: str) -> None:
        self._require_non_blank(idempotency_key, "idempotency key")
        instance_id, provision_key = self._parse_handle(handle)
        instance = self._owned_instance(instance_id, provision_key)
        if instance is None:
            return
        state = self._nested_string(instance, "State", "Name")
        if state in {"shutting-down", "terminated"}:
            return
        session = self._assume_role(provision_key)
        self._aws(
            session,
            "ec2",
            "terminate-instances",
            "--region",
            self._config.region,
            "--instance-ids",
            instance_id,
        )

    def _reconcile_with_key(self, provision_key: str) -> LabHandle | None:
        session = self._assume_role(provision_key)
        response = self._aws(
            session,
            "ec2",
            "describe-instances",
            "--region",
            self._config.region,
            "--filters",
            f"Name=tag:Project,Values={self._config.project_name}",
            f"Name=tag:Environment,Values={self._config.environment}",
            "Name=tag:Role,Values=lab",
            f"Name=tag:ProvisionKey,Values={provision_key}",
            "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down",
        )
        instances = self._reservation_instances(response)
        if len(instances) > 1:
            raise AwsLabError("multiple AWS labs share one provision key")
        if not instances:
            return None
        instance_id = self._required_string(instances[0], "InstanceId")
        if _INSTANCE_PATTERN.fullmatch(instance_id) is None:
            raise AwsLabError("AWS returned an invalid lab instance ID")
        return self._handle(instance_id, provision_key)

    def _owned_instance(self, instance_id: str, provision_key: str) -> dict[str, object] | None:
        session = self._assume_role(provision_key)
        response = self._aws(
            session,
            "ec2",
            "describe-instances",
            "--region",
            self._config.region,
            "--filters",
            f"Name=instance-id,Values={instance_id}",
        )
        instances = self._reservation_instances(response)
        if not instances:
            return None
        if len(instances) != 1:
            raise AwsLabError("AWS returned an ambiguous lab identity")
        tags = {
            self._required_string(tag, "Key"): self._required_string(tag, "Value")
            for tag in self._list_field(instances[0], "Tags")
        }
        expected = {
            "Project": self._config.project_name,
            "Environment": self._config.environment,
            "Role": "lab",
            "ProvisionKey": provision_key,
        }
        if any(tags.get(key) != value for key, value in expected.items()):
            raise AwsLabError("instance does not belong to the requested lab")
        return instances[0]

    def _assume_role(self, provision_key: str) -> _Session:
        response = self._runner.run_json(
            (
                "sts",
                "assume-role",
                "--role-arn",
                self._config.provisioner_role_arn,
                "--role-session-name",
                f"moodle-lab-{provision_key[:16]}",
                "--duration-seconds",
                "3600",
                "--region",
                self._config.region,
            )
        )
        credentials = self._mapping_field(response, "Credentials")
        return _Session(
            access_key_id=self._required_string(credentials, "AccessKeyId"),
            secret_access_key=self._required_string(credentials, "SecretAccessKey"),
            session_token=self._required_string(credentials, "SessionToken"),
        )

    def _aws(self, session: _Session, *arguments: str) -> object:
        return self._runner.run_json(tuple(arguments), extra_environment=session.environment())

    def _tags(self, request: LabProvisionRequest, provision_key: str) -> list[dict[str, str]]:
        return [
            {"Key": "Name", "Value": f"{self._config.project_name}-{self._config.environment}-lab"},
            {"Key": "Project", "Value": self._config.project_name},
            {"Key": "Environment", "Value": self._config.environment},
            {"Key": "ManagedBy", "Value": "moodle-autotask"},
            {"Key": "Role", "Value": "lab"},
            {"Key": "ProvisionKey", "Value": provision_key},
            {"Key": "TaskKey", "Value": self._hash_text(request.task_id.value)},
            {"Key": "WorkflowKey", "Value": self._hash_text(request.workflow_revision.value)},
            {"Key": "SpecificationDigest", "Value": request.specification_digest.value},
        ]

    @staticmethod
    def _provision_key(request: LabProvisionRequest, idempotency_key: str) -> str:
        AwsEc2LabProvider._require_non_blank(idempotency_key, "idempotency key")
        payload = {
            "idempotency_key": idempotency_key,
            "image_reference": request.image_reference,
            "mode": request.requested_mode.value,
            "specification_digest": request.specification_digest.value,
            "task_id": request.task_id.value,
            "workflow_revision": request.workflow_revision.value,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _hash_text(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()

    def _handle(self, instance_id: str, provision_key: str) -> LabHandle:
        return LabHandle(f"aws-ec2:v1:{self._config.region}:{instance_id}:{provision_key}")

    def _parse_handle(self, handle: LabHandle) -> tuple[str, str]:
        parts = handle.value.split(":")
        if len(parts) != 5 or parts[:3] != ["aws-ec2", "v1", self._config.region]:
            raise AwsLabError("lab handle is not valid for this AWS provider")
        instance_id, provision_key = parts[3], parts[4]
        if _INSTANCE_PATTERN.fullmatch(instance_id) is None or _DIGEST_PATTERN.fullmatch(
            provision_key
        ) is None:
            raise AwsLabError("lab handle is malformed")
        return instance_id, provision_key

    @staticmethod
    def _require_non_blank(value: str, name: str) -> None:
        if not value.strip():
            raise ValueError(f"{name} must not be blank")

    @staticmethod
    def _mapping(value: object) -> dict[str, object]:
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise AwsLabError("AWS response has an invalid object")
        return cast(dict[str, object], value)

    @classmethod
    def _mapping_field(cls, value: object, key: str) -> dict[str, object]:
        mapping = cls._mapping(value)
        if key not in mapping:
            raise AwsLabError(f"AWS response is missing {key}")
        return cls._mapping(mapping[key])

    @classmethod
    def _required_string(cls, value: object, key: str) -> str:
        mapping = cls._mapping(value)
        field = mapping.get(key)
        if not isinstance(field, str) or not field:
            raise AwsLabError(f"AWS response has an invalid {key}")
        return field

    @classmethod
    def _nested_string(cls, value: object, outer: str, inner: str) -> str:
        return cls._required_string(cls._mapping_field(value, outer), inner)

    @classmethod
    def _list_field(cls, value: object, key: str) -> list[dict[str, object]]:
        mapping = cls._mapping(value)
        field = mapping.get(key)
        if not isinstance(field, list):
            raise AwsLabError(f"AWS response has an invalid {key}")
        return [cls._mapping(item) for item in field]

    @classmethod
    def _reservation_instances(cls, value: object) -> list[dict[str, object]]:
        instances: list[dict[str, object]] = []
        for reservation in cls._list_field(value, "Reservations"):
            instances.extend(cls._list_field(reservation, "Instances"))
        return instances
