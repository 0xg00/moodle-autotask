"""Linux behavioral checks for the literal remote guards in ``aws-deploy.ps1``."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="remote guards are POSIX shell")

_ROOT = Path(__file__).parents[1]
_DEPLOY = _ROOT / "scripts" / "aws-deploy.ps1"
_SERVICES = ("scheduler", "telegram", "worker", "agent")
_CONTROLLER_FILES = (
    ("usr/local/sbin/moodle-autotask-refresh-config", 0o750),
    ("usr/local/sbin/moodle-autotask-install-codex", 0o750),
    ("usr/local/sbin/moodle-autotask-health-publish", 0o750),
    ("usr/local/sbin/moodle-autotask-health-prepare", 0o750),
    ("etc/codex/requirements.toml", 0o644),
    ("etc/systemd/system/moodle-autotask-codex-login.service", 0o644),
    ("etc/systemd/system/moodle-autotask-agent.service", 0o644),
    ("etc/systemd/system/moodle-autotask-scheduler.service", 0o644),
    ("etc/systemd/system/moodle-autotask-worker.service", 0o644),
    ("etc/systemd/system/moodle-autotask-telegram.service", 0o644),
    ("etc/systemd/system/moodle-autotask-health.service", 0o644),
    ("etc/systemd/system/moodle-autotask-health.timer", 0o644),
)


def _function_payload(name: str) -> str:
    source = _DEPLOY.read_text(encoding="utf-8")
    match = re.search(
        rf"function {re.escape(name)} \{{.*?return \(?@'\n(.*?)\n'@",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{name} must keep a literal remote shell payload"
    return match.group(1)


def _action_literals(action: str, following: str) -> list[str]:
    source = _DEPLOY.read_text(encoding="utf-8")
    block = source.split(f"if ($Action -eq '{action}') {{", 1)[1].split(following, 1)[0]
    values = re.findall(r"(?m)^\s*'((?:''|[^'])*)',?\s*$", block)
    assert values and values[0] == "set -eu"
    return [value.replace("''", "'") for value in values]


def _activate_payload() -> str:
    commands = _action_literals("Activate", "if ($Action -eq 'Deactivate')")
    guard = _function_payload("New-ActivationGuardCommand")
    # The guard variable is deliberately spliced in immediately after ``set -eu``.
    source = _DEPLOY.read_text(encoding="utf-8")
    activation = source.split("if ($Action -eq 'Activate')", 1)[1].split(
        "if ($Action -eq 'Deactivate')", 1
    )[0]
    assert activation.index("$activationGuardCommand") < activation.index(
        "moodle-autotask-refresh-config"
    )
    return "\n".join((commands[0], guard, *commands[1:]))


def _deactivate_payload() -> str:
    return "\n".join(_action_literals("Deactivate", "$gitStatus ="))


def _scheduler_guard_payload(config: Path) -> str:
    return _function_payload("New-SchedulerConfigGuardCommand").replace(
        "__CONFIG_PATH__", str(config)
    )


def _release_guard_payload(release: Path) -> str:
    return _function_payload("New-ReleaseGuardCommand").replace(
        "__RELEASE_ROOT__", f"/opt/moodle-autotask/releases/{release.name}"
    )


def _deploy_order_contract() -> str:
    source = _DEPLOY.read_text(encoding="utf-8")
    scheduler = source.index("$schedulerConfigGuardCommand = New-SchedulerConfigGuardCommand")
    controller = source.index("$controllerInstallGuardCommand = New-ControllerInstallGuardCommand")
    end = source.index("Send-ControllerCommand -TargetInstanceId $controllerInstanceId", controller)
    assert scheduler < controller < end
    command_block = source[end : source.index("    'set -eu'", end) + 20_000]
    required = (
        "$releaseGuardCommand",
        "release_was_present",
        "$controllerInstallGuardCommand",
        "$schedulerConfigGuardCommand",
        "systemctl stop moodle-autotask-scheduler.service",
        "systemctl stop moodle-autotask-telegram.service",
        "systemctl stop moodle-autotask-worker.service",
        "systemctl stop moodle-autotask-agent.service",
        "$schedulerConfigInstallCommand",
        "current.next",
        "mv -Tf /opt/moodle-autotask/current.next /opt/moodle-autotask/current",
        "moodle-autotask-controller' install",
        "systemctl daemon-reload",
        "systemctl start moodle-autotask-scheduler.service",
        "systemctl start moodle-autotask-telegram.service",
        "systemctl start moodle-autotask-worker.service",
        "systemctl enable --now moodle-autotask-agent.service",
        "moodle-autotask-health-prepare",
        "activation_started=$(date +%s)",
        "health-enabled",
        "systemctl enable --now moodle-autotask-health.timer",
        "cleanup_controller_install",
    )
    offsets = [command_block.index(value) for value in required]
    assert offsets == sorted(offsets)
    return command_block


@dataclass
class _RemoteHarness:
    temporary: Path

    def __post_init__(self) -> None:
        self.root = self.temporary / "root"
        self.bin = self.temporary / "bin"
        self.state = self.temporary / "state"
        self.root.mkdir()
        self.bin.mkdir()
        self.state.mkdir()
        self._fake("systemctl", self._systemctl())
        self._fake("mktemp", self._once("/usr/bin/mktemp"))
        self._fake("cp", self._once("/bin/cp"))
        self._fake("mv", self._once("/bin/mv"))
        self._fake(
            "fault",
            "#!/usr/bin/env bash\n"
            'test "${FAKE_FAIL:-}" != "$1" || exit 91\n',
        )
        self._fake(
            "sleep", '#!/usr/bin/env bash\ntouch "$FAKE_ROOT/run/moodle-autotask-health/"*\n'
        )
        self._prepare_paths()

    def _fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def _once(command: str) -> str:
        return (
            "#!/usr/bin/env bash\nset -eu\n"
            'if [ "${FAKE_FAIL:-}" = "$(basename "$0")" ] && '
            '[ ! -e "$FAKE_STATE/failed-$FAKE_FAIL" ]; then '
            'touch "$FAKE_STATE/failed-$FAKE_FAIL"; exit 91; fi\n'
            f'exec {command} "$@"\n'
        )

    @staticmethod
    def _systemctl() -> str:
        return """#!/usr/bin/env bash
set -eu
command="$1"; shift
unit_for() {
  unit=$(printf '%s' "${1#moodle-autotask-}" | sed 's/\\.service$//')
  if [ "$unit" = health.timer ]; then printf timer; else printf '%s' "$unit"; fi
}
failed() {
  key="$1"
  [ "${FAKE_FAIL:-}" = "$key" ] && [ ! -e "$FAKE_STATE/failed-$key" ] \\
    && touch "$FAKE_STATE/failed-$key"
}
case "$command" in
  is-active) unit=$(unit_for "${@: -1}"); test -f "$FAKE_STATE/active-$unit" ;;
  is-enabled) unit=$(unit_for "${@: -1}"); test -f "$FAKE_STATE/enabled-$unit" ;;
  show)
    unit=$(unit_for "$1")
    if test -f "$FAKE_STATE/active-$unit"; then state=active; substate=running
    else state=inactive; substate=dead; fi
    case " $* " in
      *' ActiveState '*) printf '%s\n' "$state" ;;
      *' SubState '*) printf '%s\n' "$substate" ;;
      *) printf '%s\n%s\n' "$state" "$substate" ;;
    esac ;;
  cat) test -f "$FAKE_STATE/unit-agent" ;;
  daemon-reload) if failed daemon-reload; then exit 92; fi ;;
  start|stop|enable|disable)
    now=false; if [ "${1:-}" = --now ]; then now=true; shift; fi
    if failed "$command"; then exit 92; fi
    for service in "$@"; do
      unit=$(unit_for "$service")
      if failed "$command-$unit"; then exit 92; fi
      case "$command" in
        start) touch "$FAKE_STATE/active-$unit" ;;
        stop) rm -f "$FAKE_STATE/active-$unit" ;;
        enable)
          touch "$FAKE_STATE/enabled-$unit"
          if [ "$now" = true ]; then touch "$FAKE_STATE/active-$unit"; fi ;;
        disable) rm -f "$FAKE_STATE/enabled-$unit" ;;
      esac
    done ;;
  *) exit 64 ;;
esac
"""

    def _prepare_paths(self) -> None:
        for directory in (
            "var/lib/moodle-autotask",
            "run/moodle-autotask-health",
            "opt/moodle-autotask/current/venv/bin",
            "usr/local/sbin",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for service in _SERVICES:
            pulse = self.root / "run/moodle-autotask-health" / service
            pulse.touch()
            pulse.chmod(0o620)
            executable = (
                self.root / "opt/moodle-autotask/current/venv/bin" / (f"moodle-autotask-{service}")
            )
            executable.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            executable.chmod(0o755)
        for name in ("refresh-config", "health-publish", "health-prepare"):
            target = self.root / "usr/local/sbin" / f"moodle-autotask-{name}"
            if name == "health-prepare":
                target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            else:
                target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)

    def set_states(
        self,
        active: set[str],
        enabled: set[str],
        *,
        timer: tuple[bool, bool],
        agent_unit: bool = True,
    ) -> None:
        for item in self.state.glob("active-*"):
            item.unlink()
        for item in self.state.glob("enabled-*"):
            item.unlink()
        for service in active:
            (self.state / f"active-{service}").touch()
        for service in enabled:
            (self.state / f"enabled-{service}").touch()
        if timer[0]:
            (self.state / "active-timer").touch()
        if timer[1]:
            (self.state / "enabled-timer").touch()
        if agent_unit:
            (self.state / "unit-agent").touch()
        else:
            (self.state / "unit-agent").unlink(missing_ok=True)

    def render(self, payload: str) -> str:
        production_release = r"^/opt/moodle-autotask/releases/[0-9a-f]{64}$"
        harness_release = (
            rf"^{re.escape(str(self.root / 'opt/moodle-autotask/releases'))}/[0-9a-f]{{64}}$"
        )
        payload = payload.replace(production_release, "__HARNESS_RELEASE_PATH__")
        pairs = {
            "/var/lib/moodle-autotask": self.root / "var/lib/moodle-autotask",
            "/run/moodle-autotask-health": self.root / "run/moodle-autotask-health",
            "/opt/moodle-autotask": self.root / "opt/moodle-autotask",
            "/usr/local/sbin": self.root / "usr/local/sbin",
            "/etc/systemd/system": self.root / "etc/systemd/system",
            "/etc/codex": self.root / "etc/codex",
        }
        for original, replacement in pairs.items():
            payload = payload.replace(original, str(replacement))
        return payload.replace("__HARNESS_RELEASE_PATH__", harness_release)

    def run(self, payload: str, *, failure: str = "") -> subprocess.CompletedProcess[str]:
        environment = os.environ | {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_FAIL": failure,
            "FAKE_ROOT": str(self.root),
            "FAKE_STATE": str(self.state),
        }
        return subprocess.run(
            ["bash", "-c", self.render(payload)],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
            timeout=15,
        )

    def snapshot(self) -> tuple[frozenset[str], frozenset[str]]:
        active = frozenset(
            path.name.removeprefix("active-") for path in self.state.glob("active-*")
        )
        enabled = frozenset(
            path.name.removeprefix("enabled-") for path in self.state.glob("enabled-*")
        )
        return active, enabled


def _deploy_fixture(
    harness: _RemoteHarness,
    *,
    current: bool = True,
    controller_files: bool = True,
    config: bool = True,
    marker: bool = True,
) -> tuple[Path, dict[Path, tuple[bytes, int]]]:
    current_path = harness.root / "opt/moodle-autotask/current"
    shutil.rmtree(current_path)
    release_root = harness.root / "opt/moodle-autotask/releases"
    old_release = release_root / ("a" * 64)
    new_release = release_root / ("b" * 64)
    for release in (old_release, new_release):
        (release / "venv/bin").mkdir(parents=True)
    if current:
        current_path.symlink_to(old_release, target_is_directory=True)
    expected: dict[Path, tuple[bytes, int]] = {}
    if controller_files:
        for index, (relative, mode) in enumerate(_CONTROLLER_FILES):
            target = harness.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            contents = f"old-controller-{index}".encode()
            target.write_bytes(contents)
            target.chmod(mode)
            expected[target] = (contents, mode)
    else:
        for relative, _ in _CONTROLLER_FILES:
            (harness.root / relative).unlink(missing_ok=True)
    scheduler_config = harness.root / "etc/moodle-autotask/scheduler.json"
    scheduler_config.parent.mkdir(parents=True, exist_ok=True)
    if config:
        scheduler_config.write_bytes(b'{"courseShortnames":["old"],"maxNewEventsPerCycle":1}')
        scheduler_config.chmod(0o640)
        expected[scheduler_config] = (scheduler_config.read_bytes(), 0o640)
    health_marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    if marker:
        health_marker.write_bytes(b"")
        health_marker.chmod(0o600)
        expected[health_marker] = (b"", 0o600)
    return new_release, expected


def _post_guard_payload(
    harness: _RemoteHarness,
    new_release: Path,
    config: Path,
) -> str:
    current = "/opt/moodle-autotask/current"
    next_current = "/opt/moodle-autotask/current.next"
    deployed_release = f"/opt/moodle-autotask/releases/{new_release.name}"
    lines = [
        "set -eu",
        _function_payload("New-ControllerInstallGuardCommand"),
        _scheduler_guard_payload(config),
        'if [ "$scheduler_was_active" = true ]; then systemctl stop '
        "moodle-autotask-scheduler.service; fi",
        'if [ "$telegram_was_active" = true ]; then systemctl stop '
        "moodle-autotask-telegram.service; fi",
        'if [ "$worker_was_active" = true ]; then systemctl stop '
        "moodle-autotask-worker.service; fi",
        'if [ "$agent_was_active" = true ]; then systemctl stop moodle-autotask-agent.service; fi',
        f"printf '%s' new-config > {shlex.quote(str(config))}",
        f"chmod 640 {shlex.quote(str(config))}",
        f"ln -sfn {shlex.quote(deployed_release)} {shlex.quote(next_current)}",
        f"mv -Tf {shlex.quote(next_current)} {shlex.quote(current)}",
        "fault after-current-switch",
    ]
    for relative, mode in _CONTROLLER_FILES:
        target = f"/{relative}"
        if relative.endswith("moodle-autotask-health-prepare"):
            lines.append(f"printf '#!/usr/bin/env bash\\nexit 0\\n' > {shlex.quote(target)}")
        else:
            lines.append(f"printf '%s' new-{shlex.quote(relative)} > {shlex.quote(target)}")
        lines.append(f"chmod {mode:o} {shlex.quote(target)}")
    lines.extend(
        (
            "fault after-controller-files",
            "systemctl daemon-reload",
            'if [ "$scheduler_was_active" = true ]; then systemctl start '
            "moodle-autotask-scheduler.service; fi",
            'if [ "$telegram_was_active" = true ]; then systemctl start '
            "moodle-autotask-telegram.service; fi",
            'if [ "$worker_was_active" = true ]; then systemctl start '
            "moodle-autotask-worker.service; fi",
            'if [ "$agent_was_active" = true ]; then systemctl enable --now '
            "moodle-autotask-agent.service; fi",
            'if [ "$legacy_three_was_active" = true ]; then systemctl enable --now '
            "moodle-autotask-agent.service; fault agent-legacy-enable; fi",
            'if [ "$health_marker_was_present" = true ] || { '
            '[ "$health_marker_was_present" = false ] && '
            '[ "$scheduler_was_active" = true ] && '
            '[ "$telegram_was_active" = true ] && '
            '[ "$worker_was_active" = true ] && { '
            '[ "$agent_was_active" = true ] || '
            '[ "$legacy_three_was_active" = true ]; }; }; then',
            "  /usr/local/sbin/moodle-autotask-health-prepare",
            "  for pulse in scheduler telegram worker agent; do",
            '    path="/run/moodle-autotask-health/$pulse"',
            '    test -f "$path" && test ! -L "$path" && test ! -s "$path"',
            '    touch -d @0 -- "$path"',
            "  done",
            "  activation_started=$(date +%s)",
            "  sleep 0",
            "  ready=true",
            "  for pulse in scheduler telegram worker agent; do",
            '    systemctl is-enabled --quiet "moodle-autotask-$pulse.service"',
            '    test "$(systemctl show "moodle-autotask-$pulse.service" '
            '-p ActiveState --value)" = active',
            '    test "$(systemctl show "moodle-autotask-$pulse.service" '
            '-p SubState --value)" = running',
            '    test "$(stat -c %Y "/run/moodle-autotask-health/$pulse")" '
            '-ge "$activation_started"',
            "  done",
            '  "$ready"',
            "  fault pulse-timeout",
            "  temporary=$(mktemp /var/lib/moodle-autotask/.health-enabled.XXXXXX)",
            '  chmod 0600 "$temporary"',
            '  chown root:root "$temporary"',
            '  mv -f "$temporary" /var/lib/moodle-autotask/health-enabled',
            "  fault marker-replacement",
            "  systemctl enable --now moodle-autotask-health.timer",
            "fi",
            "systemctl enable --now moodle-autotask-health.timer",
            "fault health-timer-enable",
            "trap - EXIT",
            "cleanup_controller_install",
            'rm -f "$scheduler_config_candidate" "$scheduler_config_backup"',
            'rm -f "$health_marker_candidate" "$health_marker_backup"',
        )
    )
    return "\n".join(lines)


def _assert_no_deploy_backups(harness: _RemoteHarness) -> None:
    parent = harness.root / "var/lib/moodle-autotask"
    for pattern in (".controller-install.*", ".scheduler.*", ".health-marker.*"):
        assert not tuple(parent.glob(pattern))
    assert not tuple(harness.root.rglob(".restore.*"))
    assert not (harness.root / "opt/moodle-autotask/current.next").exists()
    assert not (harness.root / "opt/moodle-autotask/current.restore").exists()


@pytest.mark.parametrize("marker_present", (False, True))
@pytest.mark.parametrize("active,enabled", [({"scheduler", "worker"}, {"scheduler", "telegram"})])
def test_activate_exact_payload_enables_all_and_creates_the_marker(
    tmp_path: Path, marker_present: bool, active: set[str], enabled: set[str]
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(active, enabled, timer=(False, True))
    marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    if marker_present:
        marker.touch()
        marker.chmod(0o600)
    result = harness.run(_activate_payload())
    assert result.returncode == 0, result.stderr
    assert set(_SERVICES).issubset(harness.snapshot()[0])
    assert set(_SERVICES).issubset(harness.snapshot()[1])
    assert {"timer"}.issubset(harness.snapshot()[0]) and {"timer"}.issubset(harness.snapshot()[1])
    assert marker.read_bytes() == b"" and marker.stat().st_mode & 0o777 == 0o600
    assert not tuple(marker.parent.glob(".activation-marker.*"))


@pytest.mark.parametrize(
    "failure",
    ("enable", "start", "mktemp", "cp", "mv"),
)
def test_activate_failures_restore_the_exact_prior_service_and_timer_state(
    tmp_path: Path, failure: str
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(
        {"scheduler", "worker", "timer"}, {"scheduler", "telegram", "timer"}, timer=(True, True)
    )
    marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    marker.touch()
    marker.chmod(0o600)
    prior = harness.snapshot()
    result = harness.run(_activate_payload(), failure=failure)
    assert result.returncode != 0 and "activated-environment" not in result.stdout
    assert harness.snapshot() == prior
    assert marker.read_bytes() == b"" and marker.stat().st_mode & 0o777 == 0o600
    assert not tuple(marker.parent.glob(".activation-marker.*"))


@pytest.mark.parametrize("malformed", ("content", "symlink"))
def test_activate_rejects_malformed_or_symlinked_marker_without_mutation(
    tmp_path: Path, malformed: str
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(set(), set(), timer=(False, False))
    marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    if malformed == "content":
        marker.write_bytes(b"bad")
    else:
        target = harness.temporary / "marker-target"
        target.write_bytes(b"keep")
        marker.symlink_to(target)
    prior = harness.snapshot()
    result = harness.run(_activate_payload())
    assert result.returncode != 0 and harness.snapshot() == prior
    assert marker.is_symlink() or marker.read_bytes() == b"bad"


@pytest.mark.parametrize("failure", ("stop", "disable"))
def test_deactivate_failure_retains_marker_and_has_no_success_echo(
    tmp_path: Path, failure: str
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(set(_SERVICES), set(_SERVICES), timer=(True, True))
    marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    marker.touch()
    marker.chmod(0o600)
    result = harness.run(_deactivate_payload(), failure=failure)
    assert result.returncode != 0 and "services-deactivated" not in result.stdout
    assert marker.exists() and marker.stat().st_mode & 0o777 == 0o600


def test_deactivate_exact_payload_disables_services_then_removes_marker(tmp_path: Path) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(set(_SERVICES), set(_SERVICES), timer=(True, True))
    marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    marker.touch()
    marker.chmod(0o600)
    result = harness.run(_deactivate_payload())
    assert result.returncode == 0 and "services-deactivated" in result.stdout
    active, enabled = harness.snapshot()
    assert not set(_SERVICES) & active and not set(_SERVICES) & enabled and not marker.exists()


def test_literal_guard_contracts_remain_tied_to_the_deploy_payload() -> None:
    source = _DEPLOY.read_text(encoding="utf-8")
    for required in (
        "restore_activation()",
        "restore_scheduler_config()",
        "restore_controller_install()",
        'touch -d @0 -- "$path"',
        "systemctl enable --now moodle-autotask-health.timer",
        "ln -sfn '$releaseRoot' /opt/moodle-autotask/current.next",
    ):
        assert required in source
    assert "activation_started" in _activate_payload()
    assert "health_marker_backup_complete" in _function_payload("New-SchedulerConfigGuardCommand")
    assert "controller_guard_complete" in _function_payload("New-ControllerInstallGuardCommand")


def test_deploy_guard_chain_restores_a_first_deploy_to_exact_absence(tmp_path: Path) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(set(), set(), timer=(False, False))
    shutil.rmtree(harness.root / "opt/moodle-autotask/current")
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    payload = "\n".join(
        (
            "set -eu",
            _function_payload("New-ControllerInstallGuardCommand"),
            _scheduler_guard_payload(config),
            "false",
        )
    )
    result = harness.run(payload)
    assert result.returncode != 0
    assert not config.exists()
    assert not (harness.root / "opt/moodle-autotask/current").exists()
    assert not tuple((harness.root / "var/lib/moodle-autotask").glob(".controller-install.*"))
    assert not tuple((harness.root / "var/lib/moodle-autotask").glob(".scheduler.*"))
    assert not tuple((harness.root / "var/lib/moodle-autotask").glob(".health-marker.*"))
    assert harness.snapshot() == (frozenset(), frozenset())


@pytest.mark.parametrize("failure", ("mktemp", "cp"))
def test_deploy_guard_backup_failures_are_nonzero_and_leave_no_candidates(
    tmp_path: Path, failure: str
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states({"scheduler"}, {"scheduler"}, timer=(False, False))
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"old-config")
    config.chmod(0o640)
    marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    marker.touch()
    marker.chmod(0o600)
    payload = "\n".join(
        (
            "set -eu",
            _function_payload("New-ControllerInstallGuardCommand"),
            _scheduler_guard_payload(config),
            "false",
        )
    )
    prior = harness.snapshot()
    result = harness.run(payload, failure=failure)
    assert result.returncode != 0
    assert config.read_bytes() == b"old-config" and config.stat().st_mode & 0o777 == 0o640
    assert marker.read_bytes() == b"" and marker.stat().st_mode & 0o777 == 0o600
    assert harness.snapshot() == prior
    parent = harness.root / "var/lib/moodle-autotask"
    assert not tuple(parent.glob(".controller-install.*"))
    assert not tuple(parent.glob(".scheduler.*"))
    assert not tuple(parent.glob(".health-marker.*"))


def test_controller_guard_copies_and_restores_all_twelve_owned_files(tmp_path: Path) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states({"scheduler"}, {"scheduler"}, timer=(True, True))
    shutil.rmtree(harness.root / "opt/moodle-autotask/current")
    expected: dict[Path, tuple[bytes, int]] = {}
    for index, (relative, mode) in enumerate(_CONTROLLER_FILES):
        target = harness.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        previous_contents = f"previous-{index}".encode()
        target.write_bytes(previous_contents)
        target.chmod(mode)
        expected[target] = (previous_contents, mode)
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    payload = "\n".join(
        (
            "set -eu",
            _function_payload("New-ControllerInstallGuardCommand"),
            _scheduler_guard_payload(config),
            "false",
        )
    )
    result = harness.run(payload)
    assert result.returncode != 0
    for target, (contents, mode) in expected.items():
        assert target.read_bytes() == contents
        assert target.stat().st_mode & 0o777 == mode


def test_deploy_guard_rejects_malformed_config_and_marker_before_state_change(
    tmp_path: Path,
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states({"scheduler", "telegram"}, {"scheduler"}, timer=(True, True))
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    config.parent.mkdir(parents=True)
    target = harness.temporary / "outside-config"
    target.write_bytes(b"outside")
    config.symlink_to(target)
    marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    marker.write_bytes(b"malformed")
    prior = harness.snapshot()
    payload = "\n".join(
        (
            "set -eu",
            _function_payload("New-ControllerInstallGuardCommand"),
            _scheduler_guard_payload(config),
            "false",
        )
    )
    result = harness.run(payload)
    assert result.returncode != 0 and harness.snapshot() == prior
    assert config.is_symlink() and marker.read_bytes() == b"malformed"


@pytest.mark.parametrize(
    ("failure", "legacy"),
    (
        ("after-current-switch", False),
        ("after-controller-files", False),
        ("daemon-reload", False),
        ("start-scheduler", False),
        ("start-telegram", False),
        ("start-worker", False),
        ("enable-agent", False),
        ("agent-legacy-enable", True),
        ("pulse-timeout", False),
        ("marker-replacement", False),
    ),
)
def test_post_guard_deploy_failures_restore_the_complete_prior_machine(
    tmp_path: Path, failure: str, legacy: bool
) -> None:
    harness = _RemoteHarness(tmp_path)
    if legacy:
        active = {"scheduler", "telegram", "worker"}
        enabled = {"scheduler", "telegram", "worker"}
        harness.set_states(active, enabled, timer=(False, True), agent_unit=False)
    else:
        active = set(_SERVICES)
        enabled = {"scheduler", "worker"}
        harness.set_states(active, enabled, timer=(False, True))
    new_release, expected = _deploy_fixture(harness)
    previous_current = os.readlink(harness.root / "opt/moodle-autotask/current")
    prior = harness.snapshot()
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    result = harness.run(_post_guard_payload(harness, new_release, config), failure=failure)
    assert result.returncode != 0
    assert os.readlink(harness.root / "opt/moodle-autotask/current") == previous_current
    assert harness.snapshot() == prior
    for target, (contents, mode) in expected.items():
        assert target.read_bytes() == contents
        assert target.stat().st_mode & 0o777 == mode
    _assert_no_deploy_backups(harness)


def test_post_guard_canonical_timer_enable_failure_restores_an_inactive_deployment(
    tmp_path: Path,
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(set(), {"worker"}, timer=(False, False))
    new_release, expected = _deploy_fixture(harness, marker=False)
    previous_current = os.readlink(harness.root / "opt/moodle-autotask/current")
    prior = harness.snapshot()
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    result = harness.run(_post_guard_payload(harness, new_release, config), failure="enable-timer")
    assert result.returncode != 0
    assert os.readlink(harness.root / "opt/moodle-autotask/current") == previous_current
    assert harness.snapshot() == prior
    for target, (contents, mode) in expected.items():
        assert target.read_bytes() == contents
        assert target.stat().st_mode & 0o777 == mode
    assert not (harness.root / "var/lib/moodle-autotask/health-enabled").exists()
    _assert_no_deploy_backups(harness)


def test_post_guard_first_deploy_failure_restores_all_absent_resources(tmp_path: Path) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(set(), set(), timer=(False, False))
    new_release, _ = _deploy_fixture(
        harness, current=False, controller_files=False, config=False, marker=False
    )
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    payload = _post_guard_payload(harness, new_release, config)
    result = harness.run(payload, failure="after-controller-files")
    assert result.returncode != 0
    assert not (harness.root / "opt/moodle-autotask/current").exists()
    assert not config.exists()
    assert not (harness.root / "var/lib/moodle-autotask/health-enabled").exists()
    for relative, _ in _CONTROLLER_FILES:
        assert not (harness.root / relative).exists()
    assert harness.snapshot() == (frozenset(), frozenset())
    _assert_no_deploy_backups(harness)


def test_legacy_three_success_creates_marker_after_agent_upgrade_and_fresh_pulses(
    tmp_path: Path,
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(
        {"scheduler", "telegram", "worker"},
        {"scheduler", "telegram", "worker"},
        timer=(False, False),
        agent_unit=False,
    )
    new_release, _ = _deploy_fixture(harness, marker=False)
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    result = harness.run(_post_guard_payload(harness, new_release, config))
    marker = harness.root / "var/lib/moodle-autotask/health-enabled"
    assert result.returncode == 0, result.stderr
    assert marker.read_bytes() == b"" and marker.stat().st_mode & 0o777 == 0o600
    assert harness.snapshot() == (
        frozenset({"scheduler", "telegram", "worker", "agent", "timer"}),
        frozenset({"scheduler", "telegram", "worker", "agent", "timer"}),
    )
    _assert_no_deploy_backups(harness)


def test_legacy_three_pulse_timeout_leaves_the_marker_absent(tmp_path: Path) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(
        {"scheduler", "telegram", "worker"},
        {"scheduler", "telegram", "worker"},
        timer=(False, False),
        agent_unit=False,
    )
    new_release, _ = _deploy_fixture(harness, marker=False)
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    result = harness.run(_post_guard_payload(harness, new_release, config), failure="pulse-timeout")
    assert result.returncode != 0
    assert not (harness.root / "var/lib/moodle-autotask/health-enabled").exists()
    _assert_no_deploy_backups(harness)


def test_inactive_deploy_success_keeps_marker_absent_and_enables_the_health_timer(
    tmp_path: Path,
) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(set(), {"worker"}, timer=(False, False))
    new_release, _ = _deploy_fixture(harness, marker=False)
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    result = harness.run(_post_guard_payload(harness, new_release, config))
    assert result.returncode == 0, result.stderr
    assert not (harness.root / "var/lib/moodle-autotask/health-enabled").exists()
    assert harness.snapshot() == (
        frozenset({"timer"}),
        frozenset({"worker", "timer"}),
    )
    _assert_no_deploy_backups(harness)


def test_deploy_literal_segment_keeps_every_recovery_fault_boundary_ordered() -> None:
    command_block = _deploy_order_contract()
    controller_guard = command_block.index("$controllerInstallGuardCommand")
    scheduler_guard = command_block.index("$schedulerConfigGuardCommand")
    first_stop = command_block.index("systemctl stop moodle-autotask-scheduler.service")
    timer_enable = command_block.rindex("systemctl enable --now moodle-autotask-health.timer")
    trap_disarm = command_block.index("trap - EXIT; cleanup_controller_install")
    assert controller_guard < scheduler_guard < first_stop
    assert timer_enable < trap_disarm
    for boundary in (
        'controller_guard_dir=""',
        'cp -p "$path" "$controller_guard_dir/$index"',
        "scheduler_config_candidate=$(mktemp",
        "scheduler_config_prior_absent=true",
        "health_marker_candidate=$(mktemp",
        "systemctl stop moodle-autotask-scheduler.service",
        "New-SchedulerConfigInstallCommand",
        "current.next",
        "moodle-autotask-controller' install",
        "systemctl daemon-reload",
        "systemctl start moodle-autotask-scheduler.service",
        "systemctl enable --now moodle-autotask-agent.service",
        "activation_started=$(date +%s)",
        "temporary=$(mktemp /var/lib/moodle-autotask/.health-enabled.XXXXXX)",
        "systemctl enable --now moodle-autotask-health.timer",
    ):
        assert boundary in _DEPLOY.read_text(encoding="utf-8") or boundary in command_block


@pytest.mark.parametrize(
    "failure",
    ("after-release-root", "after-venv", "after-pip", "after-release-help"),
)
def test_release_guard_restores_the_exact_release_set_before_later_guards(
    tmp_path: Path, failure: str
) -> None:
    harness = _RemoteHarness(tmp_path)
    release_parent = harness.root / "opt/moodle-autotask/releases"
    old_release = release_parent / ("a" * 64)
    new_release = release_parent / ("b" * 64)
    (old_release / "venv/bin").mkdir(parents=True)
    current = harness.root / "opt/moodle-autotask/current"
    shutil.rmtree(current)
    current.symlink_to(old_release, target_is_directory=True)
    prior_current = os.readlink(current)
    remote_release = f"/opt/moodle-autotask/releases/{new_release.name}"
    commands = [
        "set -eu",
        _release_guard_payload(new_release),
        f"install -d -o root -g root -m 0755 {shlex.quote(remote_release)}",
        "fault after-release-root",
        f"install -d -o root -g root -m 0755 {shlex.quote(remote_release + '/venv/bin')}",
        "fault after-venv",
    ]
    for service in _SERVICES:
        executable = f"{remote_release}/venv/bin/moodle-autotask-{service}"
        commands.extend(
            (
                f"printf '#!/usr/bin/env bash\\nexit 0\\n' > {shlex.quote(executable)}",
                f"chmod 0755 {shlex.quote(executable)}",
            )
        )
    commands.extend(("fault after-pip", "fault after-release-help"))

    result = harness.run("\n".join(commands), failure=failure)

    assert result.returncode != 0
    assert sorted(path.name for path in release_parent.iterdir()) == ["a" * 64]
    assert os.readlink(current) == prior_current


def test_release_guard_reuses_only_a_canonical_digest_release(tmp_path: Path) -> None:
    harness = _RemoteHarness(tmp_path)
    release_parent = harness.root / "opt/moodle-autotask/releases"
    old_release = release_parent / ("a" * 64)
    release = release_parent / ("b" * 64)
    for root in (old_release, release):
        (root / "venv/bin").mkdir(parents=True)
    for service in _SERVICES:
        executable = release / "venv/bin" / f"moodle-autotask-{service}"
        executable.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    current = harness.root / "opt/moodle-autotask/current"
    shutil.rmtree(current)
    current.symlink_to(old_release, target_is_directory=True)
    sentinel = harness.temporary / "release-mutated"
    payload = "\n".join(
        (
            "set -eu",
            _release_guard_payload(release),
            'test "$release_was_present" = true',
            (
                'if [ "$release_was_present" = false ]; then touch '
                + shlex.quote(str(sentinel))
                + "; fi"
            ),
            "fault after-canonical-validation",
        )
    )

    result = harness.run(payload, failure="after-canonical-validation")

    assert result.returncode != 0
    assert not sentinel.exists()
    assert release.is_dir() and not release.is_symlink()


def test_release_guard_fails_closed_for_an_incomplete_existing_digest(tmp_path: Path) -> None:
    harness = _RemoteHarness(tmp_path)
    release = harness.root / "opt/moodle-autotask/releases" / ("b" * 64)
    release.mkdir(parents=True)

    result = harness.run("\n".join(("set -eu", _release_guard_payload(release), "true")))

    assert result.returncode != 0
    assert release.is_dir() and not release.is_symlink()


def test_release_guard_removes_a_new_release_after_a_later_guard_failure(tmp_path: Path) -> None:
    harness = _RemoteHarness(tmp_path)
    harness.set_states(set(), set(), timer=(False, False))
    release_parent = harness.root / "opt/moodle-autotask/releases"
    old_release = release_parent / ("a" * 64)
    new_release = release_parent / ("b" * 64)
    (old_release / "venv/bin").mkdir(parents=True)
    current = harness.root / "opt/moodle-autotask/current"
    shutil.rmtree(current)
    current.symlink_to(old_release, target_is_directory=True)
    remote_release = f"/opt/moodle-autotask/releases/{new_release.name}"
    config = harness.root / "etc/moodle-autotask/scheduler.json"
    commands = [
        "set -eu",
        _release_guard_payload(new_release),
        f"install -d -o root -g root -m 0755 {shlex.quote(remote_release + '/venv/bin')}",
    ]
    for service in _SERVICES:
        executable = f"{remote_release}/venv/bin/moodle-autotask-{service}"
        commands.extend(
            (
                f"printf '#!/usr/bin/env bash\\nexit 0\\n' > {shlex.quote(executable)}",
                f"chmod 0755 {shlex.quote(executable)}",
            )
        )
    commands.extend(
        (
            _function_payload("New-ControllerInstallGuardCommand"),
            _scheduler_guard_payload(config),
            f"ln -sfn {shlex.quote(remote_release)} /opt/moodle-autotask/current.next",
            "mv -Tf /opt/moodle-autotask/current.next /opt/moodle-autotask/current",
            "fault after-current-switch",
        )
    )

    result = harness.run("\n".join(commands), failure="after-current-switch")

    assert result.returncode != 0
    assert sorted(path.name for path in release_parent.iterdir()) == ["a" * 64]
    assert os.readlink(current) == str(old_release)
