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
    )


def test_installer_writes_exact_hardened_services_and_refresh_script(tmp_path: Path) -> None:
    install_controller_services(tmp_path, "eu-south-2", "development")
    refresh = tmp_path / "usr/local/sbin/moodle-autotask-refresh-config"
    scheduler = tmp_path / "etc/systemd/system/moodle-autotask-scheduler.service"
    telegram = tmp_path / "etc/systemd/system/moodle-autotask-telegram.service"
    refresh_text = refresh.read_text(encoding="utf-8")
    scheduler_text = scheduler.read_text(encoding="utf-8")
    telegram_text = telegram.read_text(encoding="utf-8")

    assert "moodle-autotask/development/moodle-token" in refresh_text
    assert "moodle-autotask/development/telegram-config" in refresh_text
    assert "flock -x" in refresh_text and "umask 077" in refresh_text
    assert "botToken" in refresh_text and "allowedUserId" in refresh_text
    assert "--secret-string" not in refresh_text
    assert "moodle-autotask-scheduler run" in scheduler_text
    assert "--state /var/lib/moodle-autotask/state.sqlite3" in scheduler_text
    assert "--telegram-config-file /etc/moodle-autotask/telegram.json" in scheduler_text
    assert "--approval-state /var/lib/moodle-autotask/approval.sqlite3" in scheduler_text
    assert "moodle-autotask-telegram run" in telegram_text
    for unit in (scheduler_text, telegram_text):
        assert "NoNewPrivileges=true" in unit
        assert "ProtectSystem=strict" in unit
        assert "PrivateDevices=true" in unit
        assert "ReadWritePaths=/var/lib/moodle-autotask /etc/moodle-autotask /run/lock" in unit
        assert "ExecStartPre=+/usr/local/sbin/moodle-autotask-refresh-config" in unit
    if os.name != "nt":
        assert stat.S_IMODE(refresh.stat().st_mode) == 0o750
        assert stat.S_IMODE(scheduler.stat().st_mode) == 0o644
        assert stat.S_IMODE(telegram.stat().st_mode) == 0o644
    bash = shutil.which("bash")
    if bash is not None:
        result = subprocess.run(
            [bash, "-n"], input=refresh_text.encode(), capture_output=True, timeout=10
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
    assert "NoNewPrivileges=true" in text and "ProtectSystem=strict" in text
    assert "ExecStartPre=" not in text


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
