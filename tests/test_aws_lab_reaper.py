from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SOURCE = ROOT / "infra" / "aws" / "controller" / "lab_reaper.py"
_SPEC = importlib.util.spec_from_file_location("lab_reaper", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
lab_reaper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lab_reaper)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class _Ec2:
    def __init__(self, pages: list[dict[str, object]] | Exception) -> None:
        self.pages = pages
        self.requests: list[dict[str, object]] = []
        self.terminated: list[list[str]] = []

    def describe_instances(self, **kwargs: object) -> dict[str, object]:
        self.requests.append(kwargs)
        if isinstance(self.pages, Exception):
            raise self.pages
        return self.pages[len(self.requests) - 1]

    def terminate_instances(self, **kwargs: object) -> dict[str, object]:
        instance_ids = kwargs["InstanceIds"]
        if not isinstance(instance_ids, list):
            raise AssertionError("invalid termination request")
        if not all(isinstance(item, str) for item in instance_ids):
            raise AssertionError("invalid termination request")
        self.terminated.append([item for item in instance_ids if isinstance(item, str)])
        return {}


class _FailingTerminationEc2(_Ec2):
    def terminate_instances(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        raise RuntimeError("termination unavailable")


def _instance(
    instance_id: str,
    *,
    launch_time: datetime = NOW - timedelta(hours=4),
    state: str = "running",
    tags: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "InstanceId": instance_id,
        "LaunchTime": launch_time,
        "State": {"Name": state},
        "Tags": tags
        or [
            {"Key": "Project", "Value": "project"},
            {"Key": "Environment", "Value": "development"},
            {"Key": "ManagedBy", "Value": "moodle-autotask"},
            {"Key": "Role", "Value": "lab"},
            {"Key": "ProvisionKey", "Value": "digest"},
        ],
    }


def _page(*instances: dict[str, object], token: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"Reservations": [{"Instances": list(instances)}]}
    if token is not None:
        result["NextToken"] = token
    return result


def _reap(client: _Ec2, **kwargs: object) -> list[str]:
    parameters: dict[str, object] = {
        "project": "project",
        "environment": "development",
        "hard_ttl_seconds": 4 * 60 * 60,
        "max_terminations": 20,
        "now": NOW,
        **kwargs,
    }
    return cast(
        list[str],
        lab_reaper.reap_instances(client, **parameters),
    )


def test_reaper_paginates_filters_defensively_orders_and_caps() -> None:
    client = _Ec2(
        [
            _page(_instance("i-c"), _instance("i-a"), token="next"),
            _page(_instance("i-b"), _instance("i-ignored", state="terminated")),
        ]
    )

    assert _reap(client, max_terminations=2) == ["i-a", "i-b"]
    assert client.terminated == [["i-a", "i-b"]]
    assert client.requests[1]["NextToken"] == "next"
    assert client.requests[0]["Filters"] == [
        {"Name": "tag:Project", "Values": ["project"]},
        {"Name": "tag:Environment", "Values": ["development"]},
        {"Name": "tag:ManagedBy", "Values": ["moodle-autotask"]},
        {"Name": "tag:Role", "Values": ["lab"]},
        {"Name": "tag-key", "Values": ["ProvisionKey"]},
        {
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopped", "stopping"],
        },
    ]


def test_reaper_hard_ttl_boundary_and_timezone_are_exact() -> None:
    client = _Ec2(
        [
            _page(
                _instance(
                    "i-before", launch_time=NOW - timedelta(hours=4) + timedelta(microseconds=1)
                ),
                _instance("i-equal", launch_time=NOW - timedelta(hours=4)),
                _instance("i-after", launch_time=NOW - timedelta(hours=4, microseconds=1)),
                _instance(
                    "i-offset",
                    launch_time=(NOW - timedelta(hours=4)).astimezone(timezone(timedelta(hours=2))),
                ),
            )
        ]
    )

    assert _reap(client) == ["i-after", "i-equal", "i-offset"]
    assert client.terminated == [["i-after", "i-equal", "i-offset"]]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item["Tags"].__setitem__(0, {"Key": "Project", "Value": "other"}),
        lambda item: item["Tags"].pop(),
        lambda item: item.__setitem__("State", {"Name": "shutting-down"}),
        lambda item: item.__setitem__("LaunchTime", NOW.replace(tzinfo=None)),
        lambda item: item["Tags"].__setitem__(4, {"Key": "ProvisionKey", "Value": ""}),
        lambda item: item.__setitem__(
            "Tags", item["Tags"] + [{"Key": "ProvisionKey", "Value": "duplicate"}]
        ),
    ],
)
def test_reaper_rejects_tag_state_and_time_mismatches(mutate: Any) -> None:
    item = _instance("i-nope")
    mutate(item)
    client = _Ec2([_page(item)])

    assert _reap(client) == []
    assert client.terminated == []


def test_reaper_fails_closed_before_termination_on_malformed_or_ec2_failure() -> None:
    malformed = _Ec2([{"Reservations": "not-a-list"}])
    with pytest.raises(ValueError, match="malformed"):
        _reap(malformed)
    assert malformed.terminated == []

    unavailable = _Ec2(RuntimeError("unavailable"))
    with pytest.raises(RuntimeError, match="unavailable"):
        _reap(unavailable)
    assert unavailable.terminated == []

    termination_failure = _FailingTerminationEc2([_page(_instance("i-stale"))])
    with pytest.raises(RuntimeError, match="termination unavailable"):
        _reap(termination_failure)
    assert termination_failure.terminated == []


def test_reaper_import_is_offline_and_rejects_bad_pagination() -> None:
    client = _Ec2([_page(_instance("i-a"), token="same"), _page(token="same")])
    with pytest.raises(ValueError, match="pagination"):
        _reap(client)
    assert client.terminated == []
