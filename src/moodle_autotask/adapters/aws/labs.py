"""AWS CLI backed, capability-limited ephemeral Windows lab provider."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from base64 import b64decode, b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, cast

from moodle_autotask.domain.models import LabHandle, LabProvisionRequest
from moodle_autotask.ports.contracts import LabReadiness

_APPROVED_INSTANCE_TYPES = frozenset({"t3.large", "m6i.large"})
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-[0-9]$")
_ROLE_ARN_PATTERN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$"
)
_SUBNET_PATTERN = re.compile(r"^subnet-[0-9a-f]{8,17}$")
_SECURITY_GROUP_PATTERN = re.compile(r"^sg-[0-9a-f]{8,17}$")
_INSTANCE_PATTERN = re.compile(r"^i-[0-9a-f]{8,17}$")
_VOLUME_PATTERN = re.compile(r"^vol-[0-9a-f]{8,17}$")
_IMAGE_PATTERN = re.compile(r"^ami-[0-9a-f]{8,17}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMAND_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PENDING_COMMAND_STATUSES = frozenset({"Pending", "InProgress", "Delayed", "Cancelling"})
_TERMINAL_COMMAND_STATUSES = frozenset(
    {
        "Success",
        "Cancelled",
        "Failed",
        "TimedOut",
        "AccessDenied",
        "DeliveryTimedOut",
        "ExecutionTimedOut",
        "Undeliverable",
        "InvalidPlatform",
        "Terminated",
    }
)
_COMMAND_TIMEOUT_SECONDS = 1800
_COMMAND_POLL_SECONDS = 2
_MAX_SSM_TRANSCRIPT_BYTES = 12_000
_TEARDOWN_TIMEOUT_SECONDS = 300
_TEARDOWN_POLL_SECONDS = 2


class AwsLabError(RuntimeError):
    """Raised when AWS cannot safely establish the requested lab state."""


@dataclass(frozen=True, slots=True)
class LabTranscript:
    succeeded: bool
    output: str


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

    def run_text(self, arguments: tuple[str, ...]) -> str:
        """Run the one AWS CLI command whose safe result is a presigned URL.

        Callers must keep this value transient; unlike ``run_json`` it is not
        suitable for any durable payload.
        """
        try:
            completed = subprocess.run(
                [self.executable, *arguments, "--no-cli-pager"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise AwsLabError("AWS CLI operation timed out") from error
        if completed.returncode != 0:
            raise AwsLabError(f"AWS CLI operation failed with exit code {completed.returncode}")
        return completed.stdout


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

    def __init__(
        self,
        config: AwsLabConfig,
        runner: JsonCommandRunner | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._runner = runner or AwsCliJsonRunner(timeout_seconds=30)
        self._clock = clock
        self._sleeper = sleeper

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

    def reconcile(self, request: LabProvisionRequest, *, idempotency_key: str) -> LabHandle | None:
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
        deadline = self._clock() + _TEARDOWN_TIMEOUT_SECONDS
        instance = self._owned_instance(instance_id, provision_key)
        if instance is not None:
            state = self._nested_string(instance, "State", "Name")
            if state not in {"shutting-down", "terminated"}:
                if state not in {"pending", "running", "stopping", "stopped"}:
                    raise AwsLabError("AWS returned an invalid lab instance state")
                try:
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
                except AwsLabError as error:
                    # A lost terminate response is ambiguous.  Reconcile it below; if AWS did
                    # not actually start termination, preserve the original provider error.
                    try:
                        self._wait_for_teardown(instance_id, provision_key, deadline)
                    except AwsLabError:
                        raise error from None
                    return
        self._wait_for_teardown(instance_id, provision_key, deadline)

    def _wait_for_teardown(self, instance_id: str, provision_key: str, deadline: float) -> None:
        while True:
            self._require_before_teardown_deadline(deadline)
            instance = self._owned_instance(instance_id, provision_key)
            volumes = self._owned_volumes(provision_key)
            instance_finished = instance is None
            if instance is not None:
                state = self._nested_string(instance, "State", "Name")
                if state not in {
                    "pending",
                    "running",
                    "stopping",
                    "stopped",
                    "shutting-down",
                    "terminated",
                }:
                    raise AwsLabError("AWS returned an invalid lab instance state")
                instance_finished = state == "terminated"
            volumes_finished = all(
                self._required_string(volume, "State") == "deleted" for volume in volumes
            )
            if instance_finished and volumes_finished:
                return
            self._sleep_until_teardown_deadline(deadline)

    def run_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> LabTranscript:
        command_id = self.dispatch_powershell(handle, commands, execution_key=execution_key)
        return self.wait_powershell(handle, command_id, execution_key=execution_key)

    def run_ephemeral_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> LabTranscript:
        """Run a one-shot command without writing its source into the guest filesystem.

        This is reserved for short-lived bearer URLs.  It intentionally returns
        no SSM command output because HTTP/AWS errors may reflect the URL.
        """
        if _DIGEST_PATTERN.fullmatch(execution_key) is None:
            raise ValueError("lab execution key is invalid")
        if (
            not isinstance(commands, tuple)
            or not commands
            or len(commands) > 32
            or not all(isinstance(command, str) and command.strip() for command in commands)
        ):
            raise ValueError("lab PowerShell commands are invalid")
        source = "\n".join(commands)
        if len(source.encode("utf-8")) > 24 * 1024:
            raise ValueError("lab PowerShell commands are too large")
        instance_id, provision_key = self._parse_handle(handle)
        if self._owned_instance(instance_id, provision_key) is None:
            raise AwsLabError("lab instance is unavailable")
        session = self._assume_role(provision_key)
        response = self._aws(
            session,
            "ssm",
            "send-command",
            "--region",
            self._config.region,
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunPowerShellScript",
            "--parameters",
            json.dumps({"commands": [source], "executionTimeout": ["1800"]}, separators=(",", ":")),
            "--timeout-seconds",
            "1800",
        )
        command_id = self._required_string(self._mapping_field(response, "Command"), "CommandId")
        if _COMMAND_ID_PATTERN.fullmatch(command_id) is None:
            raise AwsLabError("AWS returned an invalid SSM command ID")
        deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            invocations = self._list_field(
                self._aws(
                    session,
                    "ssm",
                    "list-command-invocations",
                    "--region",
                    self._config.region,
                    "--command-id",
                    command_id,
                    "--instance-id",
                    instance_id,
                    deadline=deadline,
                ),
                "CommandInvocations",
            )
            if not invocations:
                self._sleep_until_command_deadline(deadline)
                continue
            if len(invocations) != 1:
                raise AwsLabError("AWS returned an ambiguous lab command invocation")
            invocation = invocations[0]
            if (
                self._required_string(invocation, "CommandId") != command_id
                or self._required_string(invocation, "InstanceId") != instance_id
            ):
                raise AwsLabError("AWS returned an invalid lab command invocation")
            status = self._required_string(invocation, "Status")
            if status in _PENDING_COMMAND_STATUSES:
                self._sleep_until_command_deadline(deadline)
                continue
            if status not in _TERMINAL_COMMAND_STATUSES:
                raise AwsLabError("AWS returned an unknown lab command status")
            return LabTranscript(status == "Success", "")
        raise AwsLabError("lab command did not finish")

    def dispatch_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> str:
        if _DIGEST_PATTERN.fullmatch(execution_key) is None:
            raise ValueError("lab execution key is invalid")
        if (
            not isinstance(commands, tuple)
            or not commands
            or len(commands) > 32
            or not all(isinstance(command, str) and command.strip() for command in commands)
        ):
            raise ValueError("lab PowerShell commands are invalid")
        source = "\n".join(commands)
        if len(source.encode("utf-8")) > 24 * 1024:
            raise ValueError("lab PowerShell commands are too large")
        instance_id, provision_key = self._parse_handle(handle)
        if self._owned_instance(instance_id, provision_key) is None:
            raise AwsLabError("lab instance is unavailable")
        session = self._assume_role(provision_key)
        encoded = b64encode(source.encode("utf-16-le")).decode("ascii")
        wrapper = _idempotent_powershell(execution_key, encoded)
        parameters = json.dumps(
            {"commands": [wrapper], "executionTimeout": ["1800"]}, separators=(",", ":")
        )
        response = self._aws(
            session,
            "ssm",
            "send-command",
            "--region",
            self._config.region,
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunPowerShellScript",
            "--parameters",
            parameters,
            "--timeout-seconds",
            "1800",
        )
        command = self._mapping_field(response, "Command")
        command_id = self._required_string(command, "CommandId")
        if _COMMAND_ID_PATTERN.fullmatch(command_id) is None:
            raise AwsLabError("AWS returned an invalid SSM command ID")
        return command_id

    def wait_powershell(
        self, handle: LabHandle, command_id: str, *, execution_key: str
    ) -> LabTranscript:
        deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
        if _DIGEST_PATTERN.fullmatch(execution_key) is None:
            raise ValueError("lab execution key is invalid")
        if _COMMAND_ID_PATTERN.fullmatch(command_id) is None:
            raise ValueError("lab SSM command ID is invalid")
        instance_id, provision_key = self._parse_handle(handle)
        if self._owned_instance(instance_id, provision_key, deadline=deadline) is None:
            raise AwsLabError("lab instance is unavailable")
        return self._wait_powershell(
            self._assume_role(provision_key, deadline=deadline),
            instance_id,
            command_id,
            execution_key,
            deadline,
        )

    def _wait_powershell(
        self,
        session: _Session,
        instance_id: str,
        command_id: str,
        execution_key: str,
        deadline: float,
    ) -> LabTranscript:
        while time.monotonic() < deadline:
            invocations = self._list_field(
                self._aws(
                    session,
                    "ssm",
                    "list-command-invocations",
                    "--region",
                    self._config.region,
                    "--command-id",
                    command_id,
                    "--instance-id",
                    instance_id,
                    "--details",
                    deadline=deadline,
                ),
                "CommandInvocations",
            )
            if not invocations:
                self._sleep_until_command_deadline(deadline)
                continue
            if len(invocations) != 1:
                raise AwsLabError("AWS returned an ambiguous lab command invocation")
            invocation = invocations[0]
            if (
                self._required_string(invocation, "CommandId") != command_id
                or self._required_string(invocation, "InstanceId") != instance_id
            ):
                raise AwsLabError("AWS returned an invalid lab command invocation")
            status = self._required_string(invocation, "Status")
            if status in _PENDING_COMMAND_STATUSES:
                self._sleep_until_command_deadline(deadline)
                continue
            if status not in _TERMINAL_COMMAND_STATUSES:
                raise AwsLabError("AWS returned an unknown lab command status")
            if time.monotonic() >= deadline:
                break
            try:
                fetched = self._aws(
                    session,
                    "ssm",
                    "get-command-invocation",
                    "--region",
                    self._config.region,
                    "--command-id",
                    command_id,
                    "--instance-id",
                    instance_id,
                    deadline=deadline,
                )
            except AwsLabError:
                self._sleep_until_command_deadline(deadline)
                continue
            invocation = self._mapping(fetched)
            fetched_status = self._required_string(invocation, "Status")
            if fetched_status in _PENDING_COMMAND_STATUSES:
                self._sleep_until_command_deadline(deadline)
                continue
            if fetched_status != status:
                self._sleep_until_command_deadline(deadline)
                continue
            output = invocation.get("StandardOutputContent", "")
            error_output = invocation.get("StandardErrorContent", "")
            if not isinstance(output, str) or not isinstance(error_output, str):
                raise AwsLabError("AWS returned invalid lab command output")
            combined = output + (("\n" + error_output) if error_output else "")
            if len(combined.encode("utf-8")) > 2 * 1024 * 1024:
                raise AwsLabError("lab command output is too large")
            if status != "Success":
                return LabTranscript(False, combined)
            try:
                payload = self._mapping(json.loads(output))
            except json.JSONDecodeError as error:
                raise AwsLabError("lab command returned invalid transcript JSON") from error
            if set(payload) != {"executionKey", "succeeded", "transcriptBase64", "truncated"}:
                raise AwsLabError("lab command transcript is invalid")
            if payload.get("executionKey") != execution_key:
                raise AwsLabError("lab command transcript identity is invalid")
            succeeded = payload.get("succeeded")
            encoded_transcript = payload.get("transcriptBase64")
            truncated = payload.get("truncated")
            if (
                not isinstance(succeeded, bool)
                or not isinstance(encoded_transcript, str)
                or not isinstance(truncated, bool)
            ):
                raise AwsLabError("lab command transcript is invalid")
            try:
                transcript = b64decode(encoded_transcript, validate=True).decode("utf-8", "strict")
            except (UnicodeDecodeError, ValueError) as error:
                raise AwsLabError("lab command transcript is invalid") from error
            if len(transcript.encode("utf-8")) > _MAX_SSM_TRANSCRIPT_BYTES:
                raise AwsLabError("lab command transcript is too large")
            if truncated:
                transcript += "\n[transcript truncated]"
            return LabTranscript(succeeded, transcript)
        raise AwsLabError("lab command did not finish")

    @staticmethod
    def _sleep_until_command_deadline(deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(_COMMAND_POLL_SECONDS, remaining))

    def _sleep_until_teardown_deadline(self, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining > 0:
            self._sleeper(min(_TEARDOWN_POLL_SECONDS, remaining))

    def _require_before_teardown_deadline(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise AwsLabError("lab teardown did not finish")

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

    def _owned_instance(
        self, instance_id: str, provision_key: str, *, deadline: float | None = None
    ) -> dict[str, object] | None:
        session = self._assume_role(provision_key, deadline=deadline)
        response = self._aws(
            session,
            "ec2",
            "describe-instances",
            "--region",
            self._config.region,
            "--filters",
            f"Name=instance-id,Values={instance_id}",
            deadline=deadline,
        )
        instances = self._reservation_instances(response)
        if not instances:
            return None
        if len(instances) != 1:
            raise AwsLabError("AWS returned an ambiguous lab identity")
        tags = self._tag_values(instances[0])
        expected = self._ownership_tags(provision_key)
        if any(tags.get(key) != value for key, value in expected.items()):
            raise AwsLabError("instance does not belong to the requested lab")
        return instances[0]

    def _owned_volumes(self, provision_key: str) -> list[dict[str, object]]:
        session = self._assume_role(provision_key)
        response = self._aws(
            session,
            "ec2",
            "describe-volumes",
            "--region",
            self._config.region,
            "--filters",
            f"Name=tag:Project,Values={self._config.project_name}",
            f"Name=tag:Environment,Values={self._config.environment}",
            "Name=tag:Role,Values=lab",
            f"Name=tag:ProvisionKey,Values={provision_key}",
        )
        volumes = self._list_field(response, "Volumes")
        expected = self._ownership_tags(provision_key)
        for volume in volumes:
            volume_id = self._required_string(volume, "VolumeId")
            if _VOLUME_PATTERN.fullmatch(volume_id) is None:
                raise AwsLabError("AWS returned an invalid lab volume ID")
            tags = self._tag_values(volume)
            if any(tags.get(key) != value for key, value in expected.items()):
                raise AwsLabError("volume does not belong to the requested lab")
        return volumes

    def _ownership_tags(self, provision_key: str) -> dict[str, str]:
        return {
            "Project": self._config.project_name,
            "Environment": self._config.environment,
            "Role": "lab",
            "ProvisionKey": provision_key,
        }

    def _assume_role(self, provision_key: str, *, deadline: float | None = None) -> _Session:
        self._require_before_deadline(deadline)
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

    def _aws(self, session: _Session, *arguments: str, deadline: float | None = None) -> object:
        self._require_before_deadline(deadline)
        return self._runner.run_json(tuple(arguments), extra_environment=session.environment())

    @staticmethod
    def _require_before_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise AwsLabError("lab command did not finish")

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
        if (
            _INSTANCE_PATTERN.fullmatch(instance_id) is None
            or _DIGEST_PATTERN.fullmatch(provision_key) is None
        ):
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
    def _tag_values(cls, value: object) -> dict[str, str]:
        tags: dict[str, str] = {}
        for tag in cls._list_field(value, "Tags"):
            key = cls._required_string(tag, "Key")
            if key in tags:
                raise AwsLabError("AWS response has duplicate resource tags")
            tags[key] = cls._required_string(tag, "Value")
        return tags

    @classmethod
    def _reservation_instances(cls, value: object) -> list[dict[str, object]]:
        instances: list[dict[str, object]] = []
        for reservation in cls._list_field(value, "Reservations"):
            instances.extend(cls._list_field(reservation, "Instances"))
        return instances


def _idempotent_powershell(execution_key: str, encoded_source: str) -> str:
    return "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            "$root = 'C:\\ProgramData\\MoodleAutotask\\executions'",
            f"$key = '{execution_key}'",
            "$done = Join-Path $root ($key + '.json')",
            "$running = Join-Path $root ($key + '.running')",
            "New-Item -ItemType Directory -Force -Path $root | Out-Null",
            "if (Test-Path -LiteralPath $done -PathType Leaf) {",
            "  [Console]::Out.Write((Get-Content -LiteralPath $done -Raw))",
            "  exit 0",
            "}",
            "if (Test-Path -LiteralPath $running) {",
            "  Write-Error 'Previous execution has an ambiguous state'",
            "  exit 70",
            "}",
            "New-Item -ItemType File -Path $running -ErrorAction Stop | Out-Null",
            "$scriptPath = Join-Path $root ($key + '.ps1')",
            "$source = \"`$ErrorActionPreference = 'Stop'`r`n\" + "
            "[Text.Encoding]::Unicode.GetString("
            f"[Convert]::FromBase64String('{encoded_source}'))",
            "[IO.File]::WriteAllText($scriptPath, $source, [Text.Encoding]::Unicode)",
            "$text = (& powershell.exe -NoLogo -NoProfile -NonInteractive "
            "-ExecutionPolicy Bypass -File $scriptPath 2>&1 | Out-String)",
            "$ok = ($LASTEXITCODE -eq 0)",
            "$utf8 = New-Object System.Text.UTF8Encoding($false, $true)",
            "$bytes = $utf8.GetBytes($text)",
            "$truncated = $bytes.Length -gt 12000",
            "if ($truncated) { $bytes = [byte[]]$bytes[0..11999]; while ($bytes.Length) { "
            "try { $utf8.GetString($bytes) | Out-Null; break } catch "
            "[System.Text.DecoderFallbackException] { if ($bytes.Length -eq 1) { "
            "$bytes = [byte[]]@() } else { $bytes = [byte[]]$bytes[0..($bytes.Length - 2)] } } } }",
            "$payload = @{ executionKey = $key; succeeded = $ok; transcriptBase64 = "
            "[Convert]::ToBase64String($bytes); truncated = $truncated } "
            "| ConvertTo-Json -Compress",
            "if ($payload.Length -gt 24000) { throw 'Lab transcript envelope is too large' }",
            "$temporary = $done + '.tmp'",
            "[IO.File]::WriteAllText($temporary, $payload, (New-Object Text.UTF8Encoding($false)))",
            "Move-Item -LiteralPath $temporary -Destination $done -Force",
            "Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue",
            "Remove-Item -LiteralPath $running -Force",
            "[Console]::Out.Write($payload)",
            "exit 0",
        )
    )
