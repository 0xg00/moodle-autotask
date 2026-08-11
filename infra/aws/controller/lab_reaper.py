"""EventBridge Lambda that caps tagged Moodle Autotask lab lifetime."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Protocol, cast

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

_ACTIVE_OR_RESIDUAL_STATES = frozenset({"pending", "running", "stopping", "stopped"})
_REQUIRED_TAGS = ("Project", "Environment", "ManagedBy", "Role", "ProvisionKey")


class Ec2Client(Protocol):
    def describe_instances(self, **kwargs: object) -> Mapping[str, object]: ...

    def terminate_instances(self, **kwargs: object) -> Mapping[str, object]: ...


class Boto3Module(Protocol):
    def client(self, service_name: str) -> Ec2Client: ...


def _log(event: str, **fields: object) -> None:
    LOGGER.info(json.dumps({"event": event, **fields}, sort_keys=True, separators=(",", ":")))


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def _owned_lab(instance: Mapping[str, object], *, project: str, environment: str) -> bool:
    tags = instance.get("Tags")
    if not isinstance(tags, list):
        return False
    values: dict[str, str] = {}
    duplicates: set[str] = set()
    for tag in tags:
        if not isinstance(tag, Mapping):
            return False
        key, value = tag.get("Key"), tag.get("Value")
        if not isinstance(key, str) or not isinstance(value, str):
            return False
        if key in values:
            duplicates.add(key)
        values[key] = value
    if any(key in duplicates for key in _REQUIRED_TAGS):
        return False
    return (
        values.get("Project") == project
        and values.get("Environment") == environment
        and values.get("ManagedBy") == "moodle-autotask"
        and values.get("Role") == "lab"
        and bool(values.get("ProvisionKey"))
    )


def _eligible_instance(
    instance: Mapping[str, object], *, project: str, environment: str, now: datetime, ttl: timedelta
) -> str | None:
    instance_id = instance.get("InstanceId")
    state = instance.get("State")
    if not isinstance(instance_id, str) or not instance_id:
        return None
    if not isinstance(state, Mapping) or state.get("Name") not in _ACTIVE_OR_RESIDUAL_STATES:
        return None
    launched = _utc(instance.get("LaunchTime"))
    if launched is None or now - launched < ttl:
        return None
    if not _owned_lab(instance, project=project, environment=environment):
        return None
    return instance_id


def reap_instances(
    client: Ec2Client,
    *,
    project: str,
    environment: str,
    hard_ttl_seconds: int,
    max_terminations: int,
    now: datetime,
) -> list[str]:
    """Terminate at most one deterministic batch of stale, independently verified labs."""
    if not project or not environment or not 10800 <= hard_ttl_seconds <= 86400:
        raise ValueError("invalid reaper configuration")
    if not 1 <= max_terminations <= 20:
        raise ValueError("invalid reaper termination cap")
    now_utc = _utc(now)
    if now_utc is None:
        raise ValueError("reaper clock must be timezone-aware")

    filters = [
        {"Name": "tag:Project", "Values": [project]},
        {"Name": "tag:Environment", "Values": [environment]},
        {"Name": "tag:ManagedBy", "Values": ["moodle-autotask"]},
        {"Name": "tag:Role", "Values": ["lab"]},
        {"Name": "tag-key", "Values": ["ProvisionKey"]},
        {"Name": "instance-state-name", "Values": sorted(_ACTIVE_OR_RESIDUAL_STATES)},
    ]
    candidates: list[str] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        request: dict[str, object] = {"Filters": filters}
        if token is not None:
            request["NextToken"] = token
        response = client.describe_instances(**request)
        reservations = response.get("Reservations")
        if not isinstance(reservations, list):
            raise ValueError("malformed DescribeInstances response")
        for reservation in reservations:
            if not isinstance(reservation, Mapping) or not isinstance(
                instances := reservation.get("Instances"), list
            ):
                raise ValueError("malformed DescribeInstances reservation")
            for instance in instances:
                if isinstance(instance, Mapping):
                    instance_id = _eligible_instance(
                        instance,
                        project=project,
                        environment=environment,
                        now=now_utc,
                        ttl=timedelta(seconds=hard_ttl_seconds),
                    )
                    if instance_id is not None:
                        candidates.append(instance_id)
        next_token = response.get("NextToken")
        if next_token is None:
            break
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            raise ValueError("malformed DescribeInstances pagination")
        seen_tokens.add(next_token)
        token = next_token

    selected = sorted(set(candidates))[:max_terminations]
    if selected:
        client.terminate_instances(InstanceIds=selected)
    _log("lab_reaper_complete", examined=len(candidates), terminated=len(selected))
    return selected


def lambda_handler(_event: object, _context: object) -> dict[str, object]:
    try:
        boto3 = cast(Boto3Module, import_module("boto3"))
        terminated = reap_instances(
            boto3.client("ec2"),
            project=os.environ["PROJECT_NAME"],
            environment=os.environ["ENVIRONMENT"],
            hard_ttl_seconds=int(os.environ["LAB_HARD_TTL_SECONDS"]),
            max_terminations=int(os.environ["MAX_TERMINATIONS_PER_RUN"]),
            now=datetime.now(UTC),
        )
    except Exception as error:
        _log("lab_reaper_failed", error=type(error).__name__)
        raise
    return {"terminated": len(terminated)}
