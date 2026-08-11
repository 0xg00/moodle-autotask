from __future__ import annotations

import os
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
    health_unit = tmp_path / "etc/systemd/system/moodle-autotask-health.service"
    health_timer = tmp_path / "etc/systemd/system/moodle-autotask-health.timer"
    refresh_text = refresh.read_text(encoding="utf-8")
    scheduler_text = scheduler.read_text(encoding="utf-8")
    telegram_text = telegram.read_text(encoding="utf-8")
    codex_installer_text = codex_installer.read_text(encoding="utf-8")
    codex_login_text = codex_login.read_text(encoding="utf-8")
    agent_text = agent.read_text(encoding="utf-8")
    health_text = health.read_text(encoding="utf-8")

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
    assert "NRestarts" in health_text and "moodle-autotask-health" in health_text
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
        for script in (refresh_text, codex_installer_text):
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
