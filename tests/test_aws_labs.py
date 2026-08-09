from __future__ import annotations

import json
import subprocess
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from moddle_autotask.adapters.aws.labs import (
    AwsCliJsonRunner,
    AwsEc2LabProvider,
    AwsLabConfig,
    AwsLabError,
)
from moddle_autotask.domain.models import (
    Digest,
    ExecutionMode,
    LabHandle,
    LabProvisionRequest,
    TaskId,
    WorkflowRevision,
)
from moddle_autotask.ports.contracts import LabReadiness


@dataclass
class _FakeRunner:
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = field(default_factory=list)
    instances: list[dict[str, object]] = field(default_factory=list)
    online: bool = True
    duplicate: bool = False
    command_output: str = ""
    command_output_override: str | None = None
    command_statuses: list[str | None] = field(default_factory=lambda: ["Success"])
    get_command_statuses: list[str | None] = field(default_factory=lambda: ["Success"])

    def run_json(
        self, arguments: tuple[str, ...], *, extra_environment: Mapping[str, str] | None = None
    ) -> object:
        self.calls.append((arguments, extra_environment))
        operation = arguments[:2]
        if operation == ("sts", "assume-role"):
            return {
                "Credentials": {
                    "AccessKeyId": "temporary-access",
                    "SecretAccessKey": "temporary-secret",
                    "SessionToken": "temporary-token",
                }
            }
        if operation == ("ssm", "get-parameter"):
            return {"Parameter": {"Value": "ami-0123456789abcdef0"}}
        if operation == ("ec2", "run-instances"):
            tags_json = arguments[arguments.index("--tag-specifications") + 1]
            tag_specs = json.loads(tags_json)
            assert isinstance(tag_specs, list)
            instance_tags = tag_specs[0]["Tags"]
            self.instances = [
                {
                    "InstanceId": "i-0123456789abcdef0",
                    "State": {"Name": "pending"},
                    "Tags": instance_tags,
                }
            ]
            return {"Instances": self.instances}
        if operation == ("ec2", "describe-instances"):
            selected = list(self.instances)
            instance_filter = next(
                (
                    argument.removeprefix("Name=instance-id,Values=")
                    for argument in arguments
                    if argument.startswith("Name=instance-id,Values=")
                ),
                None,
            )
            if instance_filter is not None:
                selected = [item for item in selected if item["InstanceId"] == instance_filter]
            provision_filter = next(
                (
                    argument.removeprefix("Name=tag:ProvisionKey,Values=")
                    for argument in arguments
                    if argument.startswith("Name=tag:ProvisionKey,Values=")
                ),
                None,
            )
            if provision_filter is not None:
                filtered: list[dict[str, object]] = []
                for item in selected:
                    raw_tags = item.get("Tags")
                    if not isinstance(raw_tags, list):
                        continue
                    if any(
                        isinstance(tag, dict)
                        and tag.get("Key") == "ProvisionKey"
                        and tag.get("Value") == provision_filter
                        for tag in raw_tags
                    ):
                        filtered.append(item)
                selected = filtered
            if self.duplicate and selected:
                selected.append(dict(selected[0]))
            return {"Reservations": [{"Instances": selected}] if selected else []}
        if operation == ("ssm", "describe-instance-information"):
            information = [{"PingStatus": "Online"}] if self.online else []
            return {"InstanceInformationList": information}
        if operation == ("ssm", "send-command"):
            parameters = json.loads(arguments[arguments.index("--parameters") + 1])
            wrapper = parameters["commands"][0]
            execution_key = wrapper.split("$key = '", 1)[1].split("'", 1)[0]
            if self.command_output_override is None:
                self.command_output = json.dumps(
                    {
                        "executionKey": execution_key,
                        "succeeded": True,
                        "transcriptBase64": b64encode(b"verified").decode("ascii"),
                        "truncated": False,
                    },
                    separators=(",", ":"),
                )
            else:
                self.command_output = self.command_output_override
            return {"Command": {"CommandId": "12345678-1234-1234-1234-123456789abc"}}
        if operation == ("ssm", "list-command-invocations"):
            status = self.command_statuses.pop(0) if self.command_statuses else "Success"
            if status is None:
                return {"CommandInvocations": []}
            return {
                "CommandInvocations": [
                    {
                        "CommandId": "12345678-1234-1234-1234-123456789abc",
                        "InstanceId": "i-0123456789abcdef0",
                        "Status": status,
                    }
                ]
            }
        if operation == ("ssm", "get-command-invocation"):
            status = self.get_command_statuses.pop(0) if self.get_command_statuses else "Success"
            if status is None:
                raise AwsLabError("temporary SSM visibility failure")
            return {
                "Status": status,
                "StandardOutputContent": self.command_output,
                "StandardErrorContent": "",
            }
        if operation == ("ec2", "terminate-instances"):
            for instance in self.instances:
                instance["State"] = {"Name": "shutting-down"}
            return {"TerminatingInstances": []}
        raise AssertionError(f"unexpected AWS call: {arguments}")


def _config(**changes: object) -> AwsLabConfig:
    values: dict[str, object] = {
        "region": "eu-south-2",
        "provisioner_role_arn": (
            "arn:aws:iam::123456789012:role/moodle-autotask-development-lab-provisioner"
        ),
        "subnet_id": "subnet-0123456789abcdef0",
        "security_group_id": "sg-0123456789abcdef0",
        "instance_profile_name": "moodle-autotask-development-lab",
        "image_id": "ami-0123456789abcdef0",
    }
    values.update(changes)
    return AwsLabConfig(**values)  # type: ignore[arg-type]


def _request(
    *, task_id: str = "course:assignment:1", image_reference: str | None = None
) -> LabProvisionRequest:
    return LabProvisionRequest(
        task_id=TaskId(task_id),
        workflow_revision=WorkflowRevision("revision-1"),
        requested_mode=ExecutionMode.CENTRAL,
        specification_digest=Digest("a" * 64),
        image_reference=image_reference,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "not-a-region"),
        ("provisioner_role_arn", "arn:aws:iam::123:role/admin"),
        ("subnet_id", "subnet-user-controlled"),
        ("security_group_id", "sg-user-controlled"),
        ("instance_profile_name", "../../admin"),
        ("image_id", "ami-attacker-controlled"),
        ("instance_type", "p5.48xlarge"),
        ("root_volume_size_gib", 501),
    ],
)
def test_config_rejects_unapproved_launch_inputs(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_provision_uses_fixed_profile_hashed_identity_and_imdsv2() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)

    handle = provider.provision(_request(), idempotency_key="approval:one")

    assert handle.value.startswith("aws-ec2:v1:eu-south-2:i-0123456789abcdef0:")
    run_call, environment = next(
        call for call in runner.calls if call[0][:2] == ("ec2", "run-instances")
    )
    assert run_call[run_call.index("--image-id") + 1] == "ami-0123456789abcdef0"
    assert not any(call[0][:2] == ("ssm", "get-parameter") for call in runner.calls)
    assert run_call[run_call.index("--instance-type") + 1] == "t3.large"
    assert run_call[run_call.index("--subnet-id") + 1] == "subnet-0123456789abcdef0"
    assert run_call[run_call.index("--security-group-ids") + 1] == "sg-0123456789abcdef0"
    assert "HttpTokens=required" in run_call[run_call.index("--metadata-options") + 1]
    block_devices = json.loads(run_call[run_call.index("--block-device-mappings") + 1])
    assert block_devices[0]["Ebs"] == {
        "DeleteOnTermination": True,
        "Encrypted": True,
        "VolumeSize": 80,
        "VolumeType": "gp3",
    }
    assert "course:assignment:1" not in " ".join(run_call)
    assert environment == {
        "AWS_ACCESS_KEY_ID": "temporary-access",
        "AWS_SECRET_ACCESS_KEY": "temporary-secret",
        "AWS_SESSION_TOKEN": "temporary-token",
    }


def test_provision_replay_reconciles_without_second_instance() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)

    first = provider.provision(_request(), idempotency_key="same-key")
    second = provider.provision(_request(), idempotency_key="same-key")

    assert second == first
    assert sum(call[0][:2] == ("ec2", "run-instances") for call in runner.calls) == 1


def test_provision_uses_exact_validated_imported_image() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)

    provider.provision(
        _request(image_reference="ami-0fedcba9876543210"), idempotency_key="imported-image"
    )

    run_call = next(call[0] for call in runner.calls if call[0][:2] == ("ec2", "run-instances"))
    assert run_call[run_call.index("--image-id") + 1] == "ami-0fedcba9876543210"
    with pytest.raises(AwsLabError, match="invalid image ID"):
        provider.provision(_request(image_reference="ami-invalid"), idempotency_key="bad-image")


def test_changed_request_does_not_reconcile_existing_lab() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)
    provider.provision(_request(), idempotency_key="same-key")

    result = provider.reconcile(_request(task_id="other-task"), idempotency_key="same-key")

    assert result is None


def test_duplicate_provision_key_fails_closed() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)
    provider.provision(_request(), idempotency_key="same-key")
    runner.duplicate = True

    with pytest.raises(AwsLabError, match="multiple AWS labs"):
        provider.reconcile(_request(), idempotency_key="same-key")


def test_readiness_requires_running_instance_and_online_ssm() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")

    assert provider.readiness(handle) is LabReadiness.PENDING
    runner.instances[0]["State"] = {"Name": "running"}
    runner.online = False
    assert provider.readiness(handle) is LabReadiness.PENDING
    runner.online = True
    assert provider.readiness(handle) is LabReadiness.READY


def test_teardown_validates_ownership_and_is_idempotent() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")
    tags = runner.instances[0]["Tags"]
    assert isinstance(tags, list)
    role_tag = next(tag for tag in tags if tag["Key"] == "Role")
    role_tag["Value"] = "controller"

    with pytest.raises(AwsLabError, match="does not belong"):
        provider.teardown(handle, idempotency_key="cleanup")

    role_tag["Value"] = "lab"
    provider.teardown(handle, idempotency_key="cleanup")
    provider.teardown(handle, idempotency_key="cleanup")
    assert sum(call[0][:2] == ("ec2", "terminate-instances") for call in runner.calls) == 1


def test_powershell_uses_owned_instance_official_document_and_guest_marker() -> None:
    runner = _FakeRunner()
    runner.command_statuses = [None, "Pending", "Success"]
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")

    with patch("moddle_autotask.adapters.aws.labs.time.sleep") as sleep:
        transcript = provider.run_powershell(handle, ("Write-Output 'ok'",), execution_key="f" * 64)

    assert transcript.succeeded and transcript.output == "verified"
    send = next(call[0] for call in runner.calls if call[0][:2] == ("ssm", "send-command"))
    assert sum(call[0][:2] == ("ssm", "send-command") for call in runner.calls) == 1
    polls = [call[0] for call in runner.calls if call[0][:2] == ("ssm", "list-command-invocations")]
    assert len(polls) == 3
    assert all(
        poll[poll.index("--command-id") + 1] == "12345678-1234-1234-1234-123456789abc"
        and poll[poll.index("--instance-id") + 1] == "i-0123456789abcdef0"
        and "--details" in poll
        for poll in polls
    )
    fetches = [call[0] for call in runner.calls if call[0][:2] == ("ssm", "get-command-invocation")]
    assert len(fetches) == 1
    assert (
        fetches[0][fetches[0].index("--command-id") + 1] == "12345678-1234-1234-1234-123456789abc"
    )
    assert fetches[0][fetches[0].index("--instance-id") + 1] == "i-0123456789abcdef0"
    assert sleep.call_count == 2
    assert send[send.index("--document-name") + 1] == "AWS-RunPowerShellScript"
    parameters = json.loads(send[send.index("--parameters") + 1])
    assert parameters["executionTimeout"] == ["1800"]
    wrapper = parameters["commands"][0]
    assert "C:\\ProgramData\\MoodleAutotask\\executions" in wrapper
    assert "Previous execution has an ambiguous state" in wrapper
    assert "f" * 64 in wrapper


def test_powershell_rejects_unknown_listed_command_status() -> None:
    runner = _FakeRunner(command_statuses=["Unknown"])
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")

    with pytest.raises(AwsLabError, match="unknown lab command status"):
        provider.run_powershell(handle, ("Write-Output 'ok'",), execution_key="f" * 64)

    assert sum(call[0][:2] == ("ssm", "send-command") for call in runner.calls) == 1
    assert not any(call[0][:2] == ("ssm", "get-command-invocation") for call in runner.calls)


def test_wait_powershell_resumes_without_send_command() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")
    command_id = "12345678-1234-1234-1234-123456789abc"
    runner.command_output = json.dumps(
        {
            "executionKey": "f" * 64,
            "succeeded": True,
            "transcriptBase64": b64encode(b"verified").decode("ascii"),
            "truncated": False,
        }
    )

    transcript = provider.wait_powershell(handle, command_id, execution_key="f" * 64)

    assert transcript == provider.wait_powershell(handle, command_id, execution_key="f" * 64)
    ssm_calls = [call[0] for call in runner.calls if call[0][0] == "ssm"]
    assert not any(call[:2] == ("ssm", "send-command") for call in ssm_calls)
    assert all(command_id in call and "i-0123456789abcdef0" in call for call in ssm_calls)


@pytest.mark.parametrize(
    "command_id",
    [
        "bad",
        "x" * 36,
        "-" * 36,
        "123456781234-1234-1234-123456789abc",
        "12345678-1234-1234-1234-123456789ABC",
    ],
)
def test_wait_powershell_rejects_invalid_command_id_before_ssm(command_id: str) -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")

    with pytest.raises(ValueError, match="command ID"):
        provider.wait_powershell(handle, command_id, execution_key="f" * 64)
    assert not any(call[0][0] == "ssm" for call in runner.calls)


def test_wait_powershell_stops_at_monotonic_deadline_without_another_poll() -> None:
    runner = _FakeRunner(command_statuses=["Pending"])
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")
    command_id = "12345678-1234-1234-1234-123456789abc"
    clock = [0.0]

    with (
        patch("moddle_autotask.adapters.aws.labs.time.monotonic", lambda: clock[0]),
        patch(
            "moddle_autotask.adapters.aws.labs.time.sleep",
            side_effect=lambda seconds: clock.__setitem__(0, clock[0] + 1800.0),
        ) as sleep,
        pytest.raises(AwsLabError, match="did not finish"),
    ):
        provider.wait_powershell(handle, command_id, execution_key="f" * 64)

    assert sum(call[0][:2] == ("ssm", "list-command-invocations") for call in runner.calls) == 1
    sleep.assert_called_once_with(2)


def test_wait_powershell_preflight_consumes_the_same_monotonic_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _FakeRunner(command_statuses=["Pending"])
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")
    clock = [0.0]
    original_owned_instance = AwsEc2LabProvider._owned_instance

    def delayed_ownership(
        self: AwsEc2LabProvider,
        instance_id: str,
        provision_key: str,
        *,
        deadline: float | None = None,
    ) -> dict[str, object] | None:
        assert deadline == 1800.0
        clock[0] = 1799.0
        return original_owned_instance(self, instance_id, provision_key, deadline=deadline)

    monkeypatch.setattr("moddle_autotask.adapters.aws.labs.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "moddle_autotask.adapters.aws.labs.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(AwsEc2LabProvider, "_owned_instance", delayed_ownership)

    with pytest.raises(AwsLabError, match="did not finish"):
        provider.wait_powershell(
            handle,
            "12345678-1234-1234-1234-123456789abc",
            execution_key="f" * 64,
        )

    assert clock == [1800.0]
    assert sum(call[0][:2] == ("ssm", "list-command-invocations") for call in runner.calls) == 1


def test_wait_powershell_rejects_foreign_owned_handle_before_ssm() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)
    provider.provision(_request(), idempotency_key="same-key")
    foreign_handle = LabHandle("aws-ec2:v1:eu-south-2:i-0123456789abcdef0:" + "f" * 64)

    with pytest.raises(AwsLabError, match="does not belong"):
        provider.wait_powershell(
            foreign_handle,
            "12345678-1234-1234-1234-123456789abc",
            execution_key="f" * 64,
        )

    assert not any(call[0][0] == "ssm" for call in runner.calls)


def test_powershell_retries_list_get_eventual_consistency_without_resending() -> None:
    runner = _FakeRunner(
        command_statuses=["Success", "Cancelling", "Success"],
        get_command_statuses=["Pending", "Success"],
    )
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")

    with patch("moddle_autotask.adapters.aws.labs.time.sleep") as sleep:
        transcript = provider.run_powershell(handle, ("Write-Output 'ok'",), execution_key="f" * 64)

    assert transcript.succeeded and transcript.output == "verified"
    assert sum(call[0][:2] == ("ssm", "send-command") for call in runner.calls) == 1
    assert sum(call[0][:2] == ("ssm", "list-command-invocations") for call in runner.calls) == 3
    assert sum(call[0][:2] == ("ssm", "get-command-invocation") for call in runner.calls) == 2
    assert sleep.call_count == 2


@pytest.mark.parametrize("payload", ["%%%", b64encode(b"x" * 12_001).decode("ascii")])
def test_powershell_rejects_malformed_or_oversize_transcript(payload: str) -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)
    handle = provider.provision(_request(), idempotency_key="same-key")
    runner.command_output_override = json.dumps(
        {
            "executionKey": "f" * 64,
            "succeeded": True,
            "transcriptBase64": payload,
            "truncated": False,
        }
    )

    with pytest.raises(AwsLabError, match="transcript"):
        provider.run_powershell(handle, ("Write-Output 'ok'",), execution_key="f" * 64)


def test_powershell_wrapper_truncates_unicode_with_a_bounded_valid_envelope() -> None:
    from moddle_autotask.adapters.aws.labs import _idempotent_powershell

    text = "😀" * 4_000
    raw = text.encode("utf-8")[:12_000]
    while True:
        try:
            decoded = raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    envelope = json.dumps(
        {
            "executionKey": "f" * 64,
            "succeeded": True,
            "transcriptBase64": b64encode(raw).decode("ascii"),
            "truncated": True,
        },
        separators=(",", ":"),
    )
    wrapper = _idempotent_powershell("f" * 64, b64encode(b"Write-Output ok").decode("ascii"))

    assert decoded == "😀" * 3_000
    assert b64decode(json.loads(envelope)["transcriptBase64"], validate=True) == raw
    assert len(envelope) < 24_000
    assert "$utf8.GetString($bytes)" in wrapper
    assert "GetString($bytes, 0" not in wrapper


@pytest.mark.parametrize(
    "value",
    [
        "aws-ec2:v1:us-east-1:i-0123456789abcdef0:" + "a" * 64,
        "aws-ec2:v1:eu-south-2:i-invalid:" + "a" * 64,
        "aws-ec2:v1:eu-south-2:i-0123456789abcdef0:../../admin",
    ],
)
def test_forged_handle_is_rejected_before_aws(value: str) -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)

    with pytest.raises(AwsLabError, match="handle"):
        provider.readiness(LabHandle(value))

    assert runner.calls == []


def test_blank_idempotency_key_is_rejected_before_aws() -> None:
    runner = _FakeRunner()
    provider = AwsEc2LabProvider(_config(), runner)

    with pytest.raises(ValueError, match="idempotency key"):
        provider.provision(_request(), idempotency_key=" ")

    assert runner.calls == []


def test_cli_timeout_is_reported_without_command_or_environment_details() -> None:
    with patch(
        "moddle_autotask.adapters.aws.labs.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["aws", "operation"], 1),
    ):
        with pytest.raises(AwsLabError, match="timed out") as captured:
            AwsCliJsonRunner(timeout_seconds=1).run_json(
                ("service", "operation"),
                extra_environment={"AWS_SESSION_TOKEN": "SENTINEL_SECRET"},
            )
    assert "SENTINEL_SECRET" not in str(captured.value)
