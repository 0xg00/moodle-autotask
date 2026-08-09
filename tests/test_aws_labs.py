from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from moddle_autotask.adapters.aws.labs import AwsEc2LabProvider, AwsLabConfig, AwsLabError
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
    }
    values.update(changes)
    return AwsLabConfig(**values)  # type: ignore[arg-type]


def _request(*, task_id: str = "course:assignment:1") -> LabProvisionRequest:
    return LabProvisionRequest(
        task_id=TaskId(task_id),
        workflow_revision=WorkflowRevision("revision-1"),
        requested_mode=ExecutionMode.CENTRAL,
        specification_digest=Digest("a" * 64),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "not-a-region"),
        ("provisioner_role_arn", "arn:aws:iam::123:role/admin"),
        ("subnet_id", "subnet-user-controlled"),
        ("security_group_id", "sg-user-controlled"),
        ("instance_profile_name", "../../admin"),
        ("image_parameter", "/attacker/ami"),
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
