from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from moddle_autotask.adapters.aws.controller_service import (
    ControllerLabConfig,
    ControllerServiceError,
    install_controller_services,
    main,
)


def _lab_config() -> ControllerLabConfig:
    return ControllerLabConfig(
        "arn:aws:iam::123456789012:role/moodle-autotask-development-lab-provisioner",
        "subnet-0123456789abcdef0",
        "sg-0123456789abcdef0",
        "moodle-autotask-development-lab",
        "ami-0123456789abcdef0",
        "t3.large",
        80,
        "moodle-autotask-artifacts-123456789012-eu-south-2",
        "arn:aws:iam::123456789012:role/moodle-autotask-development-image-importer",
        "moodle-autotask-development-vmimport",
    )


def test_installer_writes_exact_hardened_services_and_refresh_script(tmp_path: Path) -> None:
    install_controller_services(tmp_path, "eu-south-2", "development")
    refresh = tmp_path / "usr/local/sbin/moodle-autotask-refresh-config"
    codex_installer = tmp_path / "usr/local/sbin/moodle-autotask-install-codex"
    codex_login = tmp_path / "etc/systemd/system/moodle-autotask-codex-login.service"
    agent = tmp_path / "etc/systemd/system/moodle-autotask-agent.service"
    scheduler = tmp_path / "etc/systemd/system/moodle-autotask-scheduler.service"
    telegram = tmp_path / "etc/systemd/system/moodle-autotask-telegram.service"
    health = tmp_path / "usr/local/sbin/moodle-autotask-health-publish"
    workspace_setup = tmp_path / "usr/local/sbin/moodle-autotask-workspace-setup"
    health_unit = tmp_path / "etc/systemd/system/moodle-autotask-health.service"
    health_timer = tmp_path / "etc/systemd/system/moodle-autotask-health.timer"
    refresh_text = refresh.read_text(encoding="utf-8")
    scheduler_text = scheduler.read_text(encoding="utf-8")
    telegram_text = telegram.read_text(encoding="utf-8")
    codex_installer_text = codex_installer.read_text(encoding="utf-8")
    codex_login_text = codex_login.read_text(encoding="utf-8")
    agent_text = agent.read_text(encoding="utf-8")
    health_text = health.read_text(encoding="utf-8")
    workspace_setup_text = workspace_setup.read_text(encoding="utf-8")

    assert "moodle-autotask/development/moodle-token" in refresh_text
    assert "moodle-autotask/development/telegram-config" in refresh_text
    assert "flock -x" in refresh_text and "umask 077" in refresh_text
    assert "botToken" in refresh_text and "allowedUserId" in refresh_text
    assert "--secret-string" not in refresh_text
    assert "moodle-autotask-scheduler run" in scheduler_text
    assert "--state /var/lib/moodle-autotask/state.sqlite3" in scheduler_text
    assert "--telegram-config-file /etc/moodle-autotask/telegram.json" in scheduler_text
    assert "--approval-state /var/lib/moodle-autotask/approval.sqlite3" in scheduler_text
    assert "--request-timeout-seconds 60" in scheduler_text
    assert "--scheduler-config-file /etc/moodle-autotask/scheduler.json" in scheduler_text
    assert "ASIX-CAMPAIGN-01" not in scheduler_text
    assert "--max-new-events-per-cycle" not in scheduler_text
    assert "moodle-autotask-telegram run" in telegram_text
    assert "MoodleAutotask/Controller" in health_text
    assert "cloudwatch put-metric-data" in health_text
    assert "ControllerStateMatchesExpectation" in health_text
    assert "ServicesExpectedRunning" in health_text
    assert "ServiceStateMatchesExpectation" in health_text
    for metric in (
        "StorageAdmissionOpen",
        "RootFilesystemFreeBytes",
        "RootFilesystemFreeInodes",
        "WorkspaceFilesystemFreeBytes",
    ):
        assert metric in health_text
    assert '"Value\\\":\\\"storage' in health_text
    assert "12884901888" in health_text and "2147483648" in health_text
    assert "100000" in health_text and "20000" in health_text
    assert "image_root=/var/lib/moodle-autotask-root" in workspace_setup_text
    assert 'image="$image_root/agent-workspaces.img"' in workspace_setup_text
    assert 'candidate="$image_root/.agent-workspaces.img.pending"' in workspace_setup_text
    assert 'safe_directory "$image_root" root:root:700' in workspace_setup_text
    assert 'install -d -o root -g root -m 0700 "$image_root"' in workspace_setup_text
    parent_guard = workspace_setup_text.index(
        'if [ -e "$image_root" ] || [ -L "$image_root" ]; then'
    )
    assert parent_guard < workspace_setup_text.index(
        'if findmnt -rn -o TARGET --target "$workspace"'
    )
    assert parent_guard < workspace_setup_text.index('dd if=/dev/zero of="$candidate"')
    assert "/var/lib/moodle-autotask/agent-workspaces.img" not in workspace_setup_text
    assert "/var/lib/moodle-autotask/.agent-workspaces.img.pending" not in workspace_setup_text
    assert "/var/lib/moodle-autotask-root/agent-workspaces.img" in health_text
    assert "/var/lib/moodle-autotask/agent-workspaces.img" not in health_text
    assert "dd if=/dev/zero" in workspace_setup_text
    assert "count=256 conv=fsync" in workspace_setup_text
    assert "mkfs.ext4 -F -N 100000 -m 6" in workspace_setup_text
    assert "loop,nodev,nosuid" in workspace_setup_text
    assert "test -z \"$(find \"$workspace\" -mindepth 1" in workspace_setup_text
    assert '"root:root:600:$size:$expected_links"' in workspace_setup_text
    assert "stat -c '%U:%G:%a:%h' \"$fstab\")\" = root:root:644:1" in workspace_setup_text
    assert 'safe_image "$image" 1' in workspace_setup_text
    assert 'safe_image "$candidate" 1' in workspace_setup_text
    assert 'safe_image "$image" 2; safe_image "$candidate" 2' in workspace_setup_text
    assert "NRestarts" in health_text and "moodle-autotask-health" in health_text
    assert "ActiveState=*)" in health_text and "SubState=*)" in health_text
    assert "--property=NRestarts --value" not in health_text
    assert "ExecStartPre=/usr/local/sbin/moodle-autotask-health-prepare" in (
        health_unit.read_text(encoding="utf-8")
    )
    assert "OnUnitActiveSec=60s" in health_timer.read_text(encoding="utf-8")
    assert "rust-v0.147.0/codex-x86_64-unknown-linux-musl.tar.gz" in codex_installer_text
    assert "0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36" in (
        codex_installer_text
    )
    assert "cb0a15567e9a60a5820d54b0f6ae86d504dc3805c1eab21a47f70e3eb7b73a40" in (
        codex_installer_text
    )
    assert "cli_auth_credentials_store = \"file\"" in codex_installer_text
    assert "forced_login_method = \"chatgpt\"" in codex_installer_text
    assert "allowed_approval_policies = [\"never\"]" in codex_installer_text
    assert "allowed_web_search_modes = [\"disabled\"]" in codex_installer_text
    assert "default_permissions = \"moodle-autotask\"" in codex_installer_text
    assert 'deny_read = ["/var/lib/moodle-agent/.codex", "/etc/moodle-autotask"]' in (
        codex_installer_text
    )
    assert "install -o root -g root -m 0644" in codex_installer_text
    assert "/etc/codex/requirements.toml" in codex_installer_text
    assert "moodle-agent must not belong to the application secret group" in (
        codex_installer_text
    )
    assert "moodle-autotask must not belong to the agent group" in codex_installer_text
    assert "install_protocol_layout()" in codex_installer_text
    assert "os.stat(name, dir_fd=parent_fd, follow_symlinks=False)" in codex_installer_text
    assert "os.O_NOFOLLOW" in codex_installer_text
    assert "os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)" in codex_installer_text
    assert "(metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)" in (
        codex_installer_text
    )
    assert 'for name in ("committed", "barriers", "locks")' in codex_installer_text
    assert 'results_retention_fd, "acks"' in codex_installer_text
    bundles_call = (
        'install_child(results_fd, "bundles", agent.pw_uid, '
        "controller_group.gr_gid, 0o2750)"
    )
    assert bundles_call in (
        codex_installer_text
    )
    retention_call = (
        'controller_state_fd, "retention", controller.pw_uid, '
        "controller_group.gr_gid, 0o700"
    )
    assert retention_call in (
        codex_installer_text
    )
    assert "if ! command -v bwrap >/dev/null 2>&1; then" in codex_installer_text
    assert "apt-get install -y --no-install-recommends bubblewrap" in codex_installer_text
    assert 'bwrap_path="$(command -v bwrap)"' in codex_installer_text
    assert 'test -x "$bwrap_path"' in codex_installer_text
    bwrap_install = codex_installer_text.split(
        "if ! command -v bwrap >/dev/null 2>&1; then", 1
    )[1].split('bwrap_path="$(command -v bwrap)"', 1)[0]
    assert "apt-get update" in bwrap_install
    assert "apt-get install -y --no-install-recommends bubblewrap" in bwrap_install
    assert codex_installer_text.index('test -x "$bwrap_path"') < codex_installer_text.index(
        'ln -sfn "$install_target"'
    )
    assert "--device-auth" in codex_login_text
    assert "User=moodle-agent" in codex_login_text
    assert "ReadWritePaths=/var/lib/moodle-agent" in codex_login_text
    assert "IPAddressDeny=169.254.169.254/32" in codex_login_text
    assert "IPAddressDeny=fd00:ec2::254/128" in codex_login_text
    assert "/etc/moodle-autotask" not in codex_login_text
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex_login_text
    assert "moodle-autotask-agent run" in agent_text
    assert "User=moodle-agent" in agent_text
    assert "ReadOnlyPaths=/var/spool/moodle-autotask/jobs /etc/codex" in agent_text
    assert "ReadWritePaths=/var/lib/moodle-agent /var/spool/moodle-autotask/results" in (
        agent_text
    )
    assert "RequiresMountsFor=/var/lib/moodle-agent/workspaces" in agent_text
    assert "After=network-online.target local-fs.target" in agent_text
    assert "ExecStartPre=+/usr/local/sbin/moodle-autotask-workspace-setup" in agent_text
    assert "IPAddressDeny=169.254.169.254/32" in agent_text
    for unit in (scheduler_text, telegram_text):
        assert "NoNewPrivileges=true" in unit
        assert "ProtectSystem=strict" in unit
        assert "PrivateDevices=true" in unit
        assert "ReadWritePaths=/var/lib/moodle-autotask /etc/moodle-autotask /run/lock" in unit
        assert "ExecStartPre=+/usr/local/sbin/moodle-autotask-refresh-config" in unit
    if os.name != "nt":
        assert stat.S_IMODE(refresh.stat().st_mode) == 0o750
        assert stat.S_IMODE(codex_installer.stat().st_mode) == 0o750
        assert stat.S_IMODE(codex_login.stat().st_mode) == 0o644
        assert stat.S_IMODE(agent.stat().st_mode) == 0o644
        assert stat.S_IMODE(scheduler.stat().st_mode) == 0o644
        assert stat.S_IMODE(telegram.stat().st_mode) == 0o644
    bash = shutil.which("bash")
    if bash is not None:
        for script in (refresh_text, codex_installer_text, health_text, workspace_setup_text):
            result = subprocess.run(
                [bash, "-n"], input=script.encode(), capture_output=True, timeout=10
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_installer_is_idempotent_and_replaces_only_regular_targets(tmp_path: Path) -> None:
    install_controller_services(tmp_path, "eu-south-2", "development")
    scheduler = tmp_path / "etc/systemd/system/moodle-autotask-scheduler.service"
    original = scheduler.read_bytes()
    scheduler.write_text("tampered", encoding="utf-8")
    install_controller_services(tmp_path, "eu-south-2", "development")
    assert scheduler.read_bytes() == original
    assert not tuple(scheduler.parent.glob(".moodle-autotask-*.service.*"))


def test_installer_writes_hardened_worker_with_fixed_lab_configuration(
    tmp_path: Path,
) -> None:
    install_controller_services(tmp_path, "eu-south-2", "development", _lab_config())
    worker = tmp_path / "etc/systemd/system/moodle-autotask-worker.service"
    text = worker.read_text(encoding="utf-8")
    assert "moodle-autotask-worker run" in text
    assert "--state /var/lib/moodle-autotask/approval.sqlite3" in text
    assert "--provisioner-role-arn arn:aws:iam::123456789012:role/" in text
    assert "--subnet-id subnet-0123456789abcdef0" in text
    assert "--security-group-id sg-0123456789abcdef0" in text
    assert "--instance-profile-name moodle-autotask-development-lab" in text
    assert "--image-id ami-0123456789abcdef0" in text
    assert "--token-file /etc/moodle-autotask/moodle-token.json" in text
    assert "--telegram-config-file /etc/moodle-autotask/telegram.json" in text
    assert "--artifact-bucket moodle-autotask-artifacts-123456789012-eu-south-2" in text
    assert "--image-importer-role-arn arn:aws:iam::123456789012:role/" in text
    assert "--vmimport-role-name moodle-autotask-development-vmimport" in text
    assert "--agent-jobs /var/spool/moodle-autotask/jobs" in text
    assert "--agent-results /var/spool/moodle-autotask/results" in text
    assert "NoNewPrivileges=true" in text and "ProtectSystem=strict" in text
    assert "ExecStartPre=+/usr/local/sbin/moodle-autotask-refresh-config" in text


def test_installer_wires_one_bounded_retention_action_before_normal_work(
    tmp_path: Path,
) -> None:
    install_controller_services(tmp_path, "eu-south-2", "development", _lab_config())
    units = {
        "agent": tmp_path / "etc/systemd/system/moodle-autotask-agent.service",
        "worker": tmp_path / "etc/systemd/system/moodle-autotask-worker.service",
    }
    commands = {
        name: shlex.split(
            next(
                line.removeprefix("ExecStart=")
                for line in unit.read_text(encoding="utf-8").splitlines()
                if line.startswith("ExecStart=")
            )
        )
        for name, unit in units.items()
    }

    agent = commands["agent"]
    worker = commands["worker"]
    assert agent[:2] == [
        "/opt/moodle-autotask/current/venv/bin/moodle-autotask-agent",
        "run",
    ]
    assert worker[:2] == [
        "/opt/moodle-autotask/current/venv/bin/moodle-autotask-worker",
        "run",
    ]
    for command, flag, value in (
        (agent, "--bundles", "/var/spool/moodle-autotask/results/bundles"),
        (agent, "--retention-root", "/var/lib/moodle-agent"),
        (worker, "--retention-controller-private", "/var/lib/moodle-autotask"),
        (worker, "--retention-agent-private", "/var/lib/moodle-agent"),
        (worker, "--retention-workspaces", "/var/lib/moodle-agent/workspaces"),
        (worker, "--retention-bundles", "/var/spool/moodle-autotask/results/bundles"),
        (worker, "--retention-scratch-ttl", "86400"),
        (worker, "--retention-evidence-ttl", "604800"),
        (worker, "--retention-candidate-limit", "1024"),
        (worker, "--retention-scan-limit", "1024"),
    ):
        assert command.count(flag) == 1
        assert command[command.index(flag) + 1] == value


def test_installer_rejects_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.write_text("keep", encoding="utf-8")
    service = tmp_path / "etc/systemd/system/moodle-autotask-scheduler.service"
    service.parent.mkdir(parents=True)
    try:
        service.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(ControllerServiceError, match="target is unsafe"):
        install_controller_services(tmp_path, "eu-south-2", "development")
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("region", "environment"),
    (
        ("EU-south-2", "development"),
        ("eu-south-2;id", "development"),
        ("eu-south-2", "Development"),
        ("eu-south-2", "../production"),
    ),
)
def test_installer_rejects_untrusted_render_values(
    tmp_path: Path, region: str, environment: str
) -> None:
    with pytest.raises(ControllerServiceError):
        install_controller_services(tmp_path, region, environment)
    assert not (tmp_path / "etc").exists()


def test_cli_rejects_unknown_options_without_echoing_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "SENTINEL_CONTROLLER_VALUE"
    with pytest.raises(SystemExit) as error:
        main(["install", "--region", "eu-south-2", "--root", sentinel])
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert sentinel not in captured.out and sentinel not in captured.err


def _layout_function(source: str, invocation: str) -> str:
    match = re.search(
        rf"({re.escape(invocation)}\(\) \{{\n.*?\n\}})\n{re.escape(invocation)}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{invocation} must remain an executable root installer"
    return match.group(1)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is required")
def test_root_protocol_installers_are_safe_idempotent_and_preserve_contents(
    tmp_path: Path,
) -> None:
    """Exercise the one canonical release installer in an isolated POSIX root."""
    install_controller_services(tmp_path, "eu-south-2", "development")
    upgrade = (tmp_path / "usr/local/sbin/moodle-autotask-install-codex").read_text(
        encoding="utf-8"
    )
    upgrade_function = _layout_function(upgrade, "install_protocol_layout")
    harness = tmp_path / "protocol-harness.sh"
    (tmp_path / "sitecustomize.py").write_text(
        "\n".join(
            (
                "import os",
                "",
                "_original_open = os.open",
                "_swapped = False",
                "",
                "def _open(path, flags, mode=0o777, *, dir_fd=None):",
                "    global _swapped",
                "    target = os.environ.get('PROTOCOL_RACE_TARGET')",
                "    sentinel = os.environ.get('PROTOCOL_RACE_SENTINEL')",
                "    replacement = os.environ.get('PROTOCOL_RACE_REPLACEMENT')",
                "    if (",
                "        target and sentinel and not _swapped and path == '.retention'",
                "        and dir_fd is not None",
                "    ):",
                "        parent = os.readlink(f'/proc/self/fd/{dir_fd}')",
                "        if parent == target:",
                "            source = os.path.join(parent, '.retention')",
                "            os.rename(source, source + '.attacker-saved')",
                "            if replacement:",
                "                os.rename(replacement, source)",
                "            else:",
                "                os.symlink(sentinel, source)",
                "            _swapped = True",
                "    return _original_open(path, flags, mode, dir_fd=dir_fd)",
                "",
                "os.open = _open",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    harness.write_text(
        "\n".join(
            (
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "groupadd --system moodle-autotask",
                "groupadd --system moodle-agent",
                "useradd --system --gid moodle-autotask --home-dir /nonexistent "
                "--shell /usr/sbin/nologin moodle-autotask",
                "useradd --system --gid moodle-agent --home-dir /nonexistent "
                "--shell /usr/sbin/nologin moodle-agent",
                "controller_user=moodle-autotask",
                "agent_user=moodle-agent",
                upgrade_function,
                "install_protocol_layout",
                "metadata() { stat -c '%U:%G:%a' \"$1\"; }",
                "for path in /var/spool/moodle-autotask/jobs "
                "/var/spool/moodle-autotask/jobs/.retention "
                "/var/spool/moodle-autotask/jobs/.retention/committed "
                "/var/spool/moodle-autotask/jobs/.retention/barriers "
                "/var/spool/moodle-autotask/jobs/.retention/locks; do",
                "  test \"$(metadata \"$path\")\" = moodle-autotask:moodle-agent:2750",
                "done",
                "for path in /var/spool/moodle-autotask/results "
                "/var/spool/moodle-autotask/results/.retention "
                "/var/spool/moodle-autotask/results/.retention/acks "
                "/var/spool/moodle-autotask/results/bundles; do",
                "  test \"$(metadata \"$path\")\" = moodle-agent:moodle-autotask:2750",
                "done",
                "test \"$(metadata /var/lib/moodle-autotask/retention)\" "
                "= moodle-autotask:moodle-autotask:700",
                "! runuser -u moodle-agent -- test -r /var/lib/moodle-autotask/retention",
                "! id -nG moodle-agent | tr ' ' '\\n' | grep -Fxq moodle-autotask",
                "! id -nG moodle-autotask | tr ' ' '\\n' | grep -Fxq moodle-agent",
                "runuser -u moodle-agent -- test -r /var/spool/moodle-autotask/jobs",
                "! runuser -u moodle-agent -- test -w /var/spool/moodle-autotask/jobs",
                "runuser -u moodle-agent -- sh -c ': > "
                "/var/spool/moodle-autotask/results/.retention/acks/agent'",
                "runuser -u moodle-autotask -- sh -c ': > "
                "/var/spool/moodle-autotask/jobs/.retention/committed/controller'",
                "runuser -u moodle-autotask -- test -r "
                "/var/spool/moodle-autotask/results/.retention/acks",
                "! runuser -u moodle-autotask -- test -w "
                "/var/spool/moodle-autotask/results/.retention/acks",
                "runuser -u moodle-autotask -- test -r "
                "/var/spool/moodle-autotask/results/bundles",
                "! runuser -u moodle-autotask -- test -w "
                "/var/spool/moodle-autotask/results/bundles",
                "runuser -u moodle-agent -- sh -c 'umask 0027; : > "
                "/var/spool/moodle-autotask/results/bundles/"
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.zip; "
                ": > /var/spool/moodle-autotask/results/bundles/.publish.lock; "
                ": > /var/spool/moodle-autotask/results/.results.publish.lock'",
                "for path in /var/spool/moodle-autotask/results/bundles/"
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.zip "
                "/var/spool/moodle-autotask/results/bundles/.publish.lock "
                "/var/spool/moodle-autotask/results/.results.publish.lock; do",
                "  test \"$(metadata \"$path\")\" = moodle-agent:moodle-autotask:640",
                "done",
                "printf jobs > /var/spool/moodle-autotask/jobs/unrelated",
                "printf results > /var/spool/moodle-autotask/results/unrelated",
                "chown root:root /var/spool/moodle-autotask/jobs/.retention/committed",
                "chmod 0700 /var/spool/moodle-autotask/jobs/.retention/committed",
                "chown root:root /var/spool/moodle-autotask/results/.retention/acks",
                "chmod 0700 /var/spool/moodle-autotask/results/.retention/acks",
                "chown root:root /var/spool/moodle-autotask/results/bundles",
                "chmod 0700 /var/spool/moodle-autotask/results/bundles",
                "chown root:root /var/lib/moodle-autotask/retention",
                "chmod 0755 /var/lib/moodle-autotask/retention",
                "install_protocol_layout",
                "test \"$(metadata /var/spool/moodle-autotask/jobs/.retention/committed)\" "
                "= moodle-autotask:moodle-agent:2750",
                "test \"$(metadata /var/spool/moodle-autotask/results/.retention/acks)\" "
                "= moodle-agent:moodle-autotask:2750",
                "test \"$(metadata /var/spool/moodle-autotask/results/bundles)\" "
                "= moodle-agent:moodle-autotask:2750",
                "test \"$(metadata /var/lib/moodle-autotask/retention)\" "
                "= moodle-autotask:moodle-autotask:700",
                "test \"$(cat /var/spool/moodle-autotask/jobs/unrelated)\" = jobs",
                "test \"$(cat /var/spool/moodle-autotask/results/unrelated)\" = results",
                "mkdir /tmp/protocol-sentinel",
                "chmod 0700 /tmp/protocol-sentinel",
                "printf untouched > /tmp/protocol-sentinel/marker",
                "if PROTOCOL_RACE_TARGET=/var/spool/moodle-autotask/jobs "
                "PROTOCOL_RACE_SENTINEL=/tmp/protocol-sentinel "
                "install_protocol_layout; then exit 1; fi",
                "test -L /var/spool/moodle-autotask/jobs/.retention",
                "test \"$(cat /tmp/protocol-sentinel/marker)\" = untouched",
                "test \"$(metadata /tmp/protocol-sentinel)\" = root:root:700",
                "rm /var/spool/moodle-autotask/jobs/.retention",
                "mv /var/spool/moodle-autotask/jobs/.retention.attacker-saved "
                "/var/spool/moodle-autotask/jobs/.retention",
                "mkdir /tmp/protocol-replacement",
                "chmod 0700 /tmp/protocol-replacement",
                "printf unchanged > /tmp/protocol-replacement/marker",
                "if PROTOCOL_RACE_TARGET=/var/spool/moodle-autotask/jobs "
                "PROTOCOL_RACE_SENTINEL=/tmp/protocol-sentinel "
                "PROTOCOL_RACE_REPLACEMENT=/tmp/protocol-replacement "
                "install_protocol_layout; then exit 1; fi",
                "test -d /var/spool/moodle-autotask/jobs/.retention",
                "test \"$(cat /var/spool/moodle-autotask/jobs/.retention/marker)\" = unchanged",
                "test \"$(metadata /var/spool/moodle-autotask/jobs/.retention)\" = root:root:700",
                "mv /var/spool/moodle-autotask/jobs/.retention /tmp/protocol-replacement",
                "mv /var/spool/moodle-autotask/jobs/.retention.attacker-saved "
                "/var/spool/moodle-autotask/jobs/.retention",
                "mv /var/spool/moodle-autotask/jobs/.retention/locks /tmp/locks",
                "ln -s /tmp /var/spool/moodle-autotask/jobs/.retention/locks",
                "if install_protocol_layout; then exit 1; fi",
                "test -L /var/spool/moodle-autotask/jobs/.retention/locks",
                "rm /var/spool/moodle-autotask/jobs/.retention/locks",
                "mv /tmp/locks /var/spool/moodle-autotask/jobs/.retention/locks",
                "mv /var/spool/moodle-autotask/results/.retention/acks /tmp/acks",
                "printf rejected > /var/spool/moodle-autotask/results/.retention/acks",
                "if install_protocol_layout; then exit 1; fi",
                "test \"$(cat /var/spool/moodle-autotask/results/.retention/acks)\" = rejected",
                "rm /var/spool/moodle-autotask/results/.retention/acks",
                "mv /tmp/acks /var/spool/moodle-autotask/results/.retention/acks",
                "install_protocol_layout",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{tmp_path.resolve().as_posix()}:/harness:ro",
            "-e",
            "PYTHONPATH=/harness",
            "python:3.12-slim",
            "bash",
            "/harness/protocol-harness.sh",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
