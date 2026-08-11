"""Linux behavioral evidence for exact controller health publisher sources."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from moddle_autotask.adapters.aws.controller_service import _health_publisher_script

pytestmark = pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="requires a root POSIX ownership test invocation",
)

_ROOT = Path(__file__).parents[1]
_SERVICES = ("scheduler", "telegram", "worker", "agent")
_GIDS = {"scheduler": 1001, "telegram": 1001, "worker": 1001, "agent": 1002}
_THRESHOLDS = (180, 180, 3900, 2100)


@pytest.mark.skipif(shutil.which("terraform") is None, reason="Terraform is required")
def test_cloud_init_template_renders_bash_parameter_expansions(tmp_path: Path) -> None:
    template = _ROOT / "infra/aws/controller/cloud-init.sh.tftpl"
    main = tmp_path / "main.tf"
    main.write_text(
        "\n".join(
            (
                "output \"user_data\" {",
                "  value = templatefile(" + json.dumps(str(template)) + ", {",
                '    region = "eu-south-2"',
                '    secret_arn = "arn:aws:secretsmanager:eu-south-2:123456789012:secret:moodle"',
                (
                    '    telegram_secret_arn = '
                    '"arn:aws:secretsmanager:eu-south-2:123456789012:secret:telegram"'
                ),
                '    project_name = "moodle-autotask"',
                "    scheduler_interval = 86400",
                '    scheduler_config_base64 = "e30="',
                "  })",
                "}",
                "",
            )
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["terraform", "apply", "-auto-approve", "-input=false", "-no-color"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    rendered = subprocess.run(
        ["terraform", "output", "-raw", "user_data"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    assert 'service="${item%%:*}"; group="${item#*:}"' in rendered


def _chown(path: Path, user: int, group: int) -> None:
    command = getattr(os, "chown", None)
    if command is None:
        pytest.skip("requires POSIX ownership semantics")
    command(path, user, group)


@dataclass
class _PublisherHarness:
    temporary: Path

    def __post_init__(self) -> None:
        self.root = self.temporary / "run" / "moodle-autotask-health"
        self.marker = self.temporary / "var" / "lib" / "moodle-autotask" / "health-enabled"
        self.state = self.temporary / "var" / "lib" / "moodle-autotask" / "health-state"
        self.bin = self.temporary / "bin"
        self.aws_args = self.temporary / "aws-args"
        self.aws_metrics = self.temporary / "aws-metrics"
        self.aws_calls = self.temporary / "aws-calls"
        self.systemctl = self.temporary / "systemctl"
        self.bin.mkdir()
        self.systemctl.mkdir()
        self._write_fakes()
        self.set_services()

    def _write_fakes(self) -> None:
        self._executable(
            "systemctl",
            '#!/usr/bin/env bash\nset -euo pipefail\ncommand="$1"; shift\n'
            'if [ "$command" = show ]; then unit="$1"; else unit="${@: -1}"; fi\n'
            'mapfile -t fields <"$FAKE_SYSTEMCTL_DIR/$unit"\n'
            'case "$command" in\n'
            '  show) printf \'%s\\n\' "${fields[1]}" "${fields[2]}" "${fields[3]}" ;;\n'
            '  is-enabled) [ "${fields[0]}" = enabled ] ;;\n'
            '  is-active) [ "${fields[1]}" = active ] ;;\n'
            "  *) exit 64 ;;\nesac\n",
        )
        self._executable(
            "getent",
            '#!/usr/bin/env bash\nset -euo pipefail\n[ "$1" = group ]\ncase "$2" in\n'
            "  moodle-autotask) printf 'moodle-autotask:x:1001:\\n' ;;\n"
            "  moodle-agent) printf 'moodle-agent:x:1002:\\n' ;;\n  *) exit 2 ;;\nesac\n",
        )
        self._executable(
            "date",
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            '[ "$1" = +%s ]\nprintf \'%s\\n\' "$FAKE_NOW"\n',
        )
        self._executable(
            "curl",
            '#!/usr/bin/env bash\nset -euo pipefail\ncase "$*" in\n'
            "  *'/api/token'*) [ \"${FAKE_CURL_MODE:-ok}\" = token-fail ] && exit 22;\n"
            "    printf token ;;\n"
            "  *'/instance-id'*) case \"${FAKE_CURL_MODE:-ok}\" in\n"
            "    instance-fail) exit 22 ;;\n    invalid-instance) printf not-an-instance ;;\n"
            "    *) printf i-deadbeef ;;\n  esac ;;\n  *) exit 64 ;;\nesac\n",
        )
        self._executable(
            "aws",
            "#!/usr/bin/env bash\nset -euo pipefail\nprintf 'called\\n' >>\"$FAKE_AWS_CALLS\"\n"
            'printf \'%s\\n\' "$@" >"$FAKE_AWS_ARGS"\nfor argument in "$@"; do\n'
            '  case "$argument" in file://*) cp "${argument#file://}" "$FAKE_AWS_METRICS" ;; esac\n'
            'done\nexit "${FAKE_AWS_EXIT:-0}"\n',
        )

    def _executable(self, name: str, content: str) -> None:
        target = self.bin / name
        target.write_text(content, encoding="utf-8")
        target.chmod(0o755)

    def set_services(
        self,
        *,
        enabled: bool = False,
        active: bool = False,
        restarts: int = 0,
        partial_active: str | None = None,
    ) -> None:
        for service in _SERVICES:
            service_active = active or service == partial_active
            state = "active" if service_active else "inactive"
            substate = "running" if service_active else "dead"
            enabled_state = "enabled" if enabled else "disabled"
            (self.systemctl / f"moodle-autotask-{service}.service").write_text(
                f"{enabled_state}\n{state}\n{substate}\n{restarts}\n", encoding="utf-8"
            )

    def precreate_pulses(self, now: int, *, stale_service: str | None = None) -> None:
        self.root.mkdir(parents=True, mode=0o711, exist_ok=True)
        self.root.chmod(0o711)
        for index, service in enumerate(_SERVICES):
            path = self.root / service
            path.touch(exist_ok=True)
            path.chmod(0o620)
            _chown(path, 0, _GIDS[service])
            age = _THRESHOLDS[index] + 1 if service == stale_service else 1
            os.utime(path, (now - age, now - age))

    def enable_expected(self) -> None:
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.touch()
        self.marker.chmod(0o600)

    def generated_source(self) -> str:
        return _health_publisher_script("eu-south-2")

    def bootstrap_source(self) -> str:
        template = (_ROOT / "infra/aws/controller/cloud-init.sh.tftpl").read_text(encoding="utf-8")
        opening = "cat >/usr/local/sbin/${project_name}-health-publish <<'HEALTH'\n"
        literal = template.split(opening, 1)[1].split("\nHEALTH\n", 1)[0]
        assert "${region}" in literal
        return literal.replace("${region}", "eu-south-2").replace("$${", "${")

    def run(
        self,
        source: str,
        *,
        now: int = 10_000,
        curl_mode: str = "ok",
        aws_exit: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        for captured in (self.aws_args, self.aws_metrics, self.aws_calls):
            captured.unlink(missing_ok=True)
        script = self.temporary / "publisher.sh"
        script.write_text(self._substitute_paths(source), encoding="utf-8")
        script.chmod(0o700)
        environment = os.environ | {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_AWS_ARGS": str(self.aws_args),
            "FAKE_AWS_CALLS": str(self.aws_calls),
            "FAKE_AWS_METRICS": str(self.aws_metrics),
            "FAKE_AWS_EXIT": str(aws_exit),
            "FAKE_CURL_MODE": curl_mode,
            "FAKE_NOW": str(now),
            "FAKE_SYSTEMCTL_DIR": str(self.systemctl),
        }
        return subprocess.run(
            ["bash", str(script)], text=True, capture_output=True, env=environment, check=False
        )

    def _substitute_paths(self, source: str) -> str:
        replacements = {
            "root=/run/moodle-autotask-health": f"root={shlex.quote(str(self.root))}",
            "marker=/var/lib/moodle-autotask/health-enabled": (
                f"marker={shlex.quote(str(self.marker))}"
            ),
            "state=/var/lib/moodle-autotask/health-state": f"state={shlex.quote(str(self.state))}",
        }
        for original, replacement in replacements.items():
            if original in source:
                source = source.replace(original, replacement, 1)
        return source

    def calls(self) -> int:
        if not self.aws_calls.exists():
            return 0
        return len(self.aws_calls.read_text(encoding="utf-8").splitlines())

    def metrics(self) -> list[dict[str, object]]:
        return cast(
            list[dict[str, object]], json.loads(self.aws_metrics.read_text(encoding="utf-8"))
        )


def _assert_metric_batch(harness: _PublisherHarness, *, aggregate: int, expected: int) -> None:
    assert harness.calls() == 1
    arguments = harness.aws_args.read_text(encoding="utf-8").splitlines()
    assert arguments[:6] == [
        "cloudwatch",
        "put-metric-data",
        "--region",
        "eu-south-2",
        "--namespace",
        "MoodleAutotask/Controller",
    ]
    assert arguments[6] == "--metric-data" and arguments[7].startswith("file://")
    metrics = harness.metrics()
    assert [metric["MetricName"] for metric in metrics] == [
        *("ServiceStateMatchesExpectation",) * 4,
        "ControllerStateMatchesExpectation",
        "ServicesExpectedRunning",
    ]
    assert len(metrics) == 6
    for metric in metrics:
        dimensions = cast(list[dict[str, str]], metric["Dimensions"])
        assert dimensions[0] == {"Name": "InstanceId", "Value": "i-deadbeef"}
        assert dimensions[1] == {"Name": "Service", "Value": dimensions[1]["Value"]}
        assert dimensions[1]["Value"] in {"aggregate", *_SERVICES}
        assert set(metric) == {"MetricName", "Dimensions", "Value"}
    assert metrics[-2]["Value"] == aggregate
    assert metrics[-1]["Value"] == expected


def test_exact_generated_and_bootstrap_sources_pass_bash_syntax_check(tmp_path: Path) -> None:
    harness = _PublisherHarness(tmp_path)
    for source in (harness.generated_source(), harness.bootstrap_source()):
        result = subprocess.run(
            ["bash", "-n"], input=source, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("publisher", ("generated_source", "bootstrap_source"))
def test_inactive_services_publish_a_healthy_six_metric_batch(
    tmp_path: Path, publisher: str
) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    result = harness.run(getattr(harness, publisher)())
    assert result.returncode == 0, result.stderr
    _assert_metric_batch(harness, aggregate=1, expected=0)
    assert [metric["Value"] for metric in harness.metrics()[:4]] == [1, 1, 1, 1]


@pytest.mark.parametrize("publisher", ("generated_source", "bootstrap_source"))
def test_partial_activity_marks_the_aggregate_unhealthy(tmp_path: Path, publisher: str) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    harness.set_services(partial_active="scheduler")
    result = harness.run(getattr(harness, publisher)())
    assert result.returncode == 0, result.stderr
    _assert_metric_batch(harness, aggregate=0, expected=0)
    assert harness.metrics()[0]["Value"] == 0


def test_generated_expected_active_fresh_stable_restarts_are_healthy(tmp_path: Path) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    harness.enable_expected()
    harness.set_services(enabled=True, active=True)
    source = harness.generated_source()
    first = harness.run(source)
    second = harness.run(source, now=10_060)
    assert first.returncode == second.returncode == 0
    _assert_metric_batch(harness, aggregate=1, expected=1)
    assert [metric["Value"] for metric in harness.metrics()[:4]] == [1, 1, 1, 1]


@pytest.mark.parametrize("stale_service", _SERVICES)
def test_generated_expected_active_rejects_each_stale_pulse(
    tmp_path: Path, stale_service: str
) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000, stale_service=stale_service)
    harness.enable_expected()
    harness.set_services(enabled=True, active=True)
    result = harness.run(harness.generated_source())
    assert result.returncode == 0, result.stderr
    _assert_metric_batch(harness, aggregate=0, expected=1)
    assert harness.metrics()[_SERVICES.index(stale_service)]["Value"] == 0


def test_generated_restart_changes_remain_unhealthy_for_five_minutes_then_recover(
    tmp_path: Path,
) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    harness.enable_expected()
    harness.set_services(enabled=True, active=True, restarts=0)
    source = harness.generated_source()
    assert harness.run(source).returncode == 0
    _assert_metric_batch(harness, aggregate=1, expected=1)
    harness.set_services(enabled=True, active=True, restarts=1)
    harness.precreate_pulses(10_001)
    changed = harness.run(source, now=10_001)
    changed_metrics = harness.metrics()
    harness.precreate_pulses(10_300)
    under_300 = harness.run(source, now=10_300)
    under_300_metrics = harness.metrics()
    harness.precreate_pulses(10_301)
    recovered = harness.run(source, now=10_301)
    assert changed.returncode == under_300.returncode == recovered.returncode == 0
    assert changed_metrics[-2]["Value"] == under_300_metrics[-2]["Value"] == 0
    assert changed_metrics[0]["Value"] == under_300_metrics[0]["Value"] == 0
    _assert_metric_batch(harness, aggregate=1, expected=1)
    assert [metric["Value"] for metric in harness.metrics()[:4]] == [1, 1, 1, 1]


@pytest.mark.parametrize("mutation", ("directory", "wrong-owner", "wrong-mode", "nonempty"))
def test_generated_publishes_an_unhealthy_metric_for_unsafe_regular_pulses(
    tmp_path: Path, mutation: str
) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    path = harness.root / "scheduler"
    if mutation == "directory":
        path.unlink()
        path.mkdir()
    elif mutation == "wrong-owner":
        _chown(path, 1, _GIDS["scheduler"])
    elif mutation == "wrong-mode":
        path.chmod(0o600)
    else:
        path.write_text("not empty", encoding="utf-8")
    result = harness.run(harness.generated_source())
    assert result.returncode == 0, result.stderr
    _assert_metric_batch(harness, aggregate=0, expected=0)
    assert harness.metrics()[0]["Value"] == 0


@pytest.mark.parametrize("publisher", ("generated_source", "bootstrap_source"))
def test_symlinked_pulse_paths_are_rejected_before_publishing(
    tmp_path: Path, publisher: str
) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    path = harness.root / "scheduler"
    path.unlink()
    path.symlink_to(harness.temporary / "outside")
    result = harness.run(getattr(harness, publisher)())
    assert result.returncode != 0
    assert harness.calls() == 0


@pytest.mark.parametrize("state_kind", ("file", "symlink", "unsafe-directory"))
def test_generated_rejects_unsafe_restart_state_before_publishing(
    tmp_path: Path, state_kind: str
) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    harness.state.parent.mkdir(parents=True)
    if state_kind == "file":
        harness.state.touch()
    elif state_kind == "symlink":
        harness.state.symlink_to(harness.temporary / "outside-state")
    else:
        harness.state.mkdir(mode=0o755)
        harness.state.chmod(0o755)
    result = harness.run(harness.generated_source())
    assert result.returncode != 0
    assert harness.calls() == 0


@pytest.mark.parametrize("publisher", ("generated_source", "bootstrap_source"))
@pytest.mark.parametrize("curl_mode", ("token-fail", "instance-fail", "invalid-instance"))
def test_publishers_fail_closed_for_invalid_or_missing_imds(
    tmp_path: Path, publisher: str, curl_mode: str
) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    result = harness.run(getattr(harness, publisher)(), curl_mode=curl_mode)
    assert result.returncode != 0
    assert harness.calls() == 0


@pytest.mark.parametrize("publisher", ("generated_source", "bootstrap_source"))
def test_publishers_propagate_cloudwatch_failure(tmp_path: Path, publisher: str) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    result = harness.run(getattr(harness, publisher)(), aws_exit=12)
    assert result.returncode == 12
    _assert_metric_batch(harness, aggregate=1, expected=0)


def test_generated_rejects_a_malformed_expectation_marker(tmp_path: Path) -> None:
    harness = _PublisherHarness(tmp_path)
    harness.precreate_pulses(10_000)
    harness.marker.parent.mkdir(parents=True)
    harness.marker.write_text("enabled", encoding="utf-8")
    harness.marker.chmod(0o600)
    result = harness.run(harness.generated_source())
    assert result.returncode != 0
    assert harness.calls() == 0
