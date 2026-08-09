"""Install hardened controller services without embedding secret values."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast


class ControllerServiceError(RuntimeError):
    pass


_REGION = re.compile(r"^[a-z]{2}-[a-z]+-[0-9]$")
_ENVIRONMENT = re.compile(r"^[a-z0-9][a-z0-9-]{1,19}$")
_PROJECT = "moodle-autotask"


@dataclass(frozen=True, slots=True)
class ControllerLabConfig:
    provisioner_role_arn: str
    subnet_id: str
    security_group_id: str
    instance_profile_name: str
    image_id: str
    instance_type: str
    root_volume_size_gib: int
    artifact_bucket: str
    image_importer_role_arn: str
    vmimport_role_name: str


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def install_controller_services(
    root: Path,
    region: str,
    environment: str,
    lab_config: ControllerLabConfig | None = None,
) -> None:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ControllerServiceError("controller root is invalid")
    if not isinstance(region, str) or not _REGION.fullmatch(region):
        raise ControllerServiceError("controller region is invalid")
    if not isinstance(environment, str) or not _ENVIRONMENT.fullmatch(environment):
        raise ControllerServiceError("controller environment is invalid")
    secret_prefix = f"{_PROJECT}/{environment}"
    refresh = _refresh_script(region, secret_prefix)
    scheduler = _scheduler_unit()
    telegram = _telegram_unit()
    _write(root, Path("usr/local/sbin/moodle-autotask-refresh-config"), refresh, 0o750)
    _write(
        root,
        Path("etc/systemd/system/moodle-autotask-scheduler.service"),
        scheduler,
        0o644,
    )
    if lab_config is not None:
        worker = _worker_unit(region, environment, lab_config)
        _write(
            root,
            Path("etc/systemd/system/moodle-autotask-worker.service"),
            worker,
            0o644,
        )
    _write(
        root,
        Path("etc/systemd/system/moodle-autotask-telegram.service"),
        telegram,
        0o644,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(prog="moodle-autotask-controller", allow_abbrev=False)
    parser.add_argument("install", nargs="?")
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", default="development")
    parser.add_argument("--provisioner-role-arn", required=True)
    parser.add_argument("--subnet-id", required=True)
    parser.add_argument("--security-group-id", required=True)
    parser.add_argument("--instance-profile-name", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--artifact-bucket", required=True)
    parser.add_argument("--image-importer-role-arn", required=True)
    parser.add_argument("--vmimport-role-name", required=True)
    parser.add_argument("--instance-type", default="t3.large")
    parser.add_argument("--root-volume-size-gib", type=int, default=80)
    args = parser.parse_args(argv)
    if args.install != "install":
        parser.error("install command is required")
    try:
        effective_user_id = cast(Callable[[], int], getattr(os, "geteuid", lambda: -1))
        if os.name == "nt" or effective_user_id() != 0:
            raise ControllerServiceError("controller installation requires root on POSIX")
        config = ControllerLabConfig(
            args.provisioner_role_arn,
            args.subnet_id,
            args.security_group_id,
            args.instance_profile_name,
            args.image_id,
            args.instance_type,
            args.root_volume_size_gib,
            args.artifact_bucket,
            args.image_importer_role_arn,
            args.vmimport_role_name,
        )
        install_controller_services(Path("/"), args.region, args.environment, config)
        return 0
    except (OSError, RuntimeError, ValueError):
        print("controller service installation failed", file=sys.stderr)
        return 1


def _write(root: Path, relative: Path, content: str, mode: int) -> None:
    target = root / relative
    _reject_indirection(root, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_indirection(root, target.parent)
    if target.exists() or target.is_symlink():
        try:
            existing = target.lstat()
        except OSError as error:
            raise ControllerServiceError("could not inspect controller service path") from error
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise ControllerServiceError("controller service target is unsafe")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        temporary = None
    except OSError as error:
        raise ControllerServiceError("could not install controller service") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _reject_indirection(root: Path, parent: Path) -> None:
    try:
        root_metadata = root.lstat()
        root_resolved = root.resolve(strict=True)
    except OSError as error:
        raise ControllerServiceError("controller root is unavailable") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ControllerServiceError("controller root is unsafe")
    current = parent
    chain: list[Path] = []
    while current != root and current != current.parent:
        chain.append(current)
        current = current.parent
    if current != root:
        raise ControllerServiceError("controller service path escapes root")
    for item in reversed(chain):
        if not item.exists():
            continue
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ControllerServiceError("controller service path is unsafe")
    if root_resolved != root.resolve(strict=True):
        raise ControllerServiceError("controller root changed during installation")


def _refresh_script(region: str, secret_prefix: str) -> str:
    selected_region = shlex.quote(region)
    moodle_secret = shlex.quote(f"{secret_prefix}/moodle-token")
    telegram_secret = shlex.quote(f"{secret_prefix}/telegram-config")
    return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077

install -d -o root -g moodle-autotask -m 0750 /etc/moodle-autotask
exec 9>/run/lock/moodle-autotask-refresh.lock
flock -x 9
temporary_directory="$(mktemp -d /etc/moodle-autotask/.refresh.XXXXXX)"
cleanup() {{
  rm -rf "$temporary_directory"
}}
trap cleanup EXIT

aws secretsmanager get-secret-value \
  --region {selected_region} \
  --secret-id {moodle_secret} \
  --query SecretString \
  --output text >"$temporary_directory/moodle-token.json"
aws secretsmanager get-secret-value \
  --region {selected_region} \
  --secret-id {telegram_secret} \
  --query SecretString \
  --output text >"$temporary_directory/telegram.json"

python3 - "$temporary_directory/moodle-token.json" "$temporary_directory/telegram.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8-sig") as stream:
    moodle = json.load(stream)
if (
    not isinstance(moodle, dict)
    or set(moodle) - {{"baseUrl", "token", "obtainedAt"}}
    or not isinstance(moodle.get("baseUrl"), str)
    or not isinstance(moodle.get("token"), str)
    or ("obtainedAt" in moodle and not isinstance(moodle["obtainedAt"], str))
):
    raise SystemExit("Moodle secret has an invalid shape")

with open(sys.argv[2], encoding="utf-8-sig") as stream:
    telegram = json.load(stream)
if (
    not isinstance(telegram, dict)
    or set(telegram) != {{"botToken", "chatId", "allowedUserId"}}
    or not isinstance(telegram.get("botToken"), str)
    or not re.fullmatch(r"[1-9][0-9]{{5,15}}:[A-Za-z0-9_-]{{30,100}}", telegram["botToken"])
    or not isinstance(telegram.get("chatId"), int)
    or isinstance(telegram["chatId"], bool)
    or not 1 <= telegram["chatId"] < 2**63
    or not isinstance(telegram.get("allowedUserId"), int)
    or isinstance(telegram["allowedUserId"], bool)
    or not 1 <= telegram["allowedUserId"] < 2**63
):
    raise SystemExit("Telegram secret has an invalid shape")
PY

chown moodle-autotask:moodle-autotask \
  "$temporary_directory/moodle-token.json" "$temporary_directory/telegram.json"
chmod 0600 "$temporary_directory/moodle-token.json" "$temporary_directory/telegram.json"
mv -f "$temporary_directory/moodle-token.json" /etc/moodle-autotask/moodle-token.json
mv -f "$temporary_directory/telegram.json" /etc/moodle-autotask/telegram.json
trap - EXIT
rmdir "$temporary_directory"
"""


def _scheduler_unit() -> str:
    command = " ".join(
        (
            "/opt/moodle-autotask/current/venv/bin/moodle-autotask-scheduler",
            "run",
            "--state /var/lib/moodle-autotask/state.sqlite3",
            "--token-file /etc/moodle-autotask/moodle-token.json",
            "--telegram-config-file /etc/moodle-autotask/telegram.json",
            "--approval-state /var/lib/moodle-autotask/approval.sqlite3",
            "--interval-seconds 86400",
        )
    )
    return "\n".join(
        (
            "[Unit]",
            "Description=Moodle Autotask notification scheduler",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "User=moodle-autotask",
            "Group=moodle-autotask",
            "WorkingDirectory=/var/lib/moodle-autotask",
            "ExecStartPre=+/usr/local/sbin/moodle-autotask-refresh-config",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=30",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "PrivateTmp=true",
            "ProtectControlGroups=true",
            "ProtectHome=true",
            "ProtectKernelModules=true",
            "ProtectKernelTunables=true",
            "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/moodle-autotask /etc/moodle-autotask /run/lock",
            "RestrictSUIDSGID=true",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )


def _telegram_unit() -> str:
    command = " ".join(
        (
            "/opt/moodle-autotask/current/venv/bin/moodle-autotask-telegram",
            "run",
            "--config-file /etc/moodle-autotask/telegram.json",
            "--state /var/lib/moodle-autotask/approval.sqlite3",
        )
    )
    return "\n".join(
        (
            "[Unit]",
            "Description=Moodle Autotask Telegram approval receiver",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "User=moodle-autotask",
            "Group=moodle-autotask",
            "WorkingDirectory=/var/lib/moodle-autotask",
            "ExecStartPre=+/usr/local/sbin/moodle-autotask-refresh-config",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=30",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "PrivateTmp=true",
            "ProtectControlGroups=true",
            "ProtectHome=true",
            "ProtectKernelModules=true",
            "ProtectKernelTunables=true",
            "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/moodle-autotask /etc/moodle-autotask /run/lock",
            "RestrictSUIDSGID=true",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )


def _worker_unit(
    region: str, environment: str, config: ControllerLabConfig
) -> str:
    values = (
        region,
        environment,
        config.provisioner_role_arn,
        config.subnet_id,
        config.security_group_id,
        config.instance_profile_name,
        config.image_id,
        config.instance_type,
        config.artifact_bucket,
        config.image_importer_role_arn,
        config.vmimport_role_name,
    )
    if any("\n" in value or "\r" in value or not value for value in values):
        raise ControllerServiceError("worker configuration is invalid")
    if not 50 <= config.root_volume_size_gib <= 500:
        raise ControllerServiceError("worker volume size is invalid")
    command = " ".join(
        (
            "/opt/moodle-autotask/current/venv/bin/moodle-autotask-worker",
            "run",
            "--state /var/lib/moodle-autotask/approval.sqlite3",
            "--token-file /etc/moodle-autotask/moodle-token.json",
            f"--region {shlex.quote(region)}",
            f"--artifact-bucket {shlex.quote(config.artifact_bucket)}",
            f"--image-importer-role-arn {shlex.quote(config.image_importer_role_arn)}",
            f"--vmimport-role-name {shlex.quote(config.vmimport_role_name)}",
            f"--environment {shlex.quote(environment)}",
            f"--provisioner-role-arn {shlex.quote(config.provisioner_role_arn)}",
            f"--subnet-id {shlex.quote(config.subnet_id)}",
            f"--security-group-id {shlex.quote(config.security_group_id)}",
            f"--instance-profile-name {shlex.quote(config.instance_profile_name)}",
            f"--image-id {shlex.quote(config.image_id)}",
            f"--instance-type {shlex.quote(config.instance_type)}",
            f"--root-volume-size-gib {config.root_volume_size_gib}",
            "--interval-seconds 15",
        )
    )
    return "\n".join(
        (
            "[Unit]",
            "Description=Moodle Autotask approved-work lab worker",
            "Wants=network-online.target",
            "After=network-online.target moodle-autotask-telegram.service",
            "",
            "[Service]",
            "Type=simple",
            "User=moodle-autotask",
            "Group=moodle-autotask",
            "WorkingDirectory=/var/lib/moodle-autotask",
            "ExecStartPre=+/usr/local/sbin/moodle-autotask-refresh-config",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=30",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "PrivateTmp=true",
            "ProtectControlGroups=true",
            "ProtectHome=true",
            "ProtectKernelModules=true",
            "ProtectKernelTunables=true",
            "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/moodle-autotask /etc/moodle-autotask /run/lock",
            "RestrictSUIDSGID=true",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )
