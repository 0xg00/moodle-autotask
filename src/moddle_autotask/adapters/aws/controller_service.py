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
_CODEX_VERSION = "0.147.0"
_CODEX_ARCHIVE_SHA256 = (
    "0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36"
)
_CODEX_BINARY_SHA256 = (
    "cb0a15567e9a60a5820d54b0f6ae86d504dc3805c1eab21a47f70e3eb7b73a40"
)


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
    codex_installer = _codex_installer_script()
    codex_login = _codex_login_unit()
    agent = _agent_unit()
    scheduler = _scheduler_unit()
    telegram = _telegram_unit()
    health = _health_publisher_script(region)
    health_prepare = _health_prepare_script()
    _write(root, Path("usr/local/sbin/moodle-autotask-refresh-config"), refresh, 0o750)
    _write(
        root,
        Path("usr/local/sbin/moodle-autotask-install-codex"),
        codex_installer,
        0o750,
    )
    _write(
        root,
        Path("etc/systemd/system/moodle-autotask-codex-login.service"),
        codex_login,
        0o644,
    )
    _write(
        root,
        Path("etc/systemd/system/moodle-autotask-agent.service"),
        agent,
        0o644,
    )
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
    _write(root, Path("usr/local/sbin/moodle-autotask-health-publish"), health, 0o750)
    _write(root, Path("usr/local/sbin/moodle-autotask-health-prepare"), health_prepare, 0o750)
    _write(
        root,
        Path("etc/systemd/system/moodle-autotask-health.service"),
        _health_unit(),
        0o644,
    )
    _write(
        root,
        Path("etc/systemd/system/moodle-autotask-health.timer"),
        _health_timer_unit(),
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


def _codex_installer_script() -> str:
    archive_name = "codex-x86_64-unknown-linux-musl"
    archive_url = (
        "https://github.com/openai/codex/releases/download/"
        f"rust-v{_CODEX_VERSION}/{archive_name}.tar.gz"
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077

agent_user=moodle-agent
agent_home=/var/lib/moodle-agent
codex_home="$agent_home/.codex"
version={shlex.quote(_CODEX_VERSION)}
archive_url={shlex.quote(archive_url)}
archive_sha256={shlex.quote(_CODEX_ARCHIVE_SHA256)}
binary_sha256={shlex.quote(_CODEX_BINARY_SHA256)}
binary_name={shlex.quote(archive_name)}
install_root="/opt/moodle-autotask/codex/$version"
install_target="$install_root/codex"

if id --user "$agent_user" >/dev/null 2>&1; then
  passwd_entry="$(getent passwd "$agent_user")"
  IFS=: read -r account_name _ account_uid _ _ account_home _ <<<"$passwd_entry"
  test "$account_name" = "$agent_user"
  test "$account_uid" -ne 0
  test "$account_home" = "$agent_home"
else
  useradd --system --home-dir "$agent_home" --create-home \
    --shell /usr/sbin/nologin "$agent_user"
fi
if id -nG "$agent_user" | tr ' ' '\n' | grep -Fxq moodle-autotask; then
  echo 'moodle-agent must not belong to the application secret group' >&2
  exit 1
fi

install -d -o "$agent_user" -g "$agent_user" -m 0700 "$agent_home" "$codex_home"
install -d -o root -g root -m 0755 /var/spool/moodle-autotask
install -d -o moodle-autotask -g "$agent_user" -m 2750 \
  /var/spool/moodle-autotask/jobs
install -d -o "$agent_user" -g moodle-autotask -m 2750 \
  /var/spool/moodle-autotask/results
install -d -o "$agent_user" -g "$agent_user" -m 0700 \
  "$agent_home/workspaces"
if [ -e "$codex_home/auth.json" ] || [ -L "$codex_home/auth.json" ]; then
  test -f "$codex_home/auth.json"
  test ! -L "$codex_home/auth.json"
  test "$(stat -c '%U:%G:%a' "$codex_home/auth.json")" = \
    "$agent_user:$agent_user:600"
fi

temporary_directory="$(mktemp -d /tmp/moodle-autotask-codex.XXXXXX)"
cleanup() {{
  rm -rf "$temporary_directory"
}}
trap cleanup EXIT

if [ ! -f "$install_target" ] || \
   [ "$(sha256sum "$install_target" | cut -d ' ' -f 1)" != "$binary_sha256" ]; then
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --fail --silent --show-error --location "$archive_url" \
    --output "$temporary_directory/codex.tar.gz"
  echo "$archive_sha256  $temporary_directory/codex.tar.gz" \
    | sha256sum --check --strict
  test "$(tar -tzf "$temporary_directory/codex.tar.gz")" = "$binary_name"
  tar -xzf "$temporary_directory/codex.tar.gz" --no-same-owner \
    --no-same-permissions -C "$temporary_directory"
  test -f "$temporary_directory/$binary_name"
  test ! -L "$temporary_directory/$binary_name"
  echo "$binary_sha256  $temporary_directory/$binary_name" \
    | sha256sum --check --strict
  install -d -o root -g root -m 0755 "$install_root"
  install -o root -g root -m 0755 \
    "$temporary_directory/$binary_name" "$install_target"
fi
echo "$binary_sha256  $install_target" | sha256sum --check --strict
if ! command -v bwrap >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends bubblewrap
fi
bwrap_path="$(command -v bwrap)"
test -x "$bwrap_path"
ln -sfn "$install_target" /usr/local/bin/moodle-autotask-codex.next
mv -Tf /usr/local/bin/moodle-autotask-codex.next \
  /usr/local/bin/moodle-autotask-codex

cat >"$temporary_directory/config.toml" <<'CONFIG'
cli_auth_credentials_store = "file"
forced_login_method = "chatgpt"
CONFIG
install -o "$agent_user" -g "$agent_user" -m 0600 \
  "$temporary_directory/config.toml" "$codex_home/config.toml"

cat >"$temporary_directory/requirements.toml" <<'REQUIREMENTS'
allowed_approval_policies = ["never"]
allowed_web_search_modes = ["disabled"]
default_permissions = "moodle-autotask"

[allowed_permission_profiles]
moodle-autotask = true

[permissions.filesystem]
deny_read = ["/var/lib/moodle-agent/.codex", "/etc/moodle-autotask"]

[permissions.moodle-autotask]
extends = ":workspace"

[permissions.moodle-autotask.network]
enabled = false
REQUIREMENTS
if [ -e /etc/codex ] || [ -L /etc/codex ]; then
  test -d /etc/codex
  test ! -L /etc/codex
fi
install -d -o root -g root -m 0755 /etc/codex
install -o root -g root -m 0644 \
  "$temporary_directory/requirements.toml" /etc/codex/requirements.toml
test "$(/usr/local/bin/moodle-autotask-codex --version)" = \
  "codex-cli $version"
"""


def _codex_login_unit() -> str:
    return "\n".join(
        (
            "[Unit]",
            "Description=Moodle Autotask one-time Codex device login",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "User=moodle-agent",
            "Group=moodle-agent",
            "Environment=HOME=/var/lib/moodle-agent",
            "Environment=CODEX_HOME=/var/lib/moodle-agent/.codex",
            "WorkingDirectory=/var/lib/moodle-agent",
            "ExecStart=/usr/local/bin/moodle-autotask-codex login --device-auth",
            "TimeoutStartSec=15min",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "PrivateTmp=true",
            "ProtectControlGroups=true",
            "ProtectHome=true",
            "ProtectKernelModules=true",
            "ProtectKernelTunables=true",
            "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/moodle-agent",
            "IPAddressDeny=169.254.169.254/32",
            "IPAddressDeny=fd00:ec2::254/128",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "RestrictSUIDSGID=true",
            "",
        )
    )


def _health_publisher_script(region: str) -> str:
    selected_region = shlex.quote(region)
    return f'''#!/usr/bin/env bash
set -euo pipefail
umask 077
root=/run/moodle-autotask-health
marker=/var/lib/moodle-autotask/health-enabled
state=/var/lib/moodle-autotask/health-state
services=(scheduler telegram worker agent)
thresholds=(180 180 3900 2100)
safe_dir() {{ test -d "$1" && test ! -L "$1" && test "$(stat -c '%u:%a' "$1")" = "$2"; }}
safe_dir "$root" '0:711'
if [ -e "$state" ] || [ -L "$state" ]; then
  safe_dir "$state" '0:700'
else
  install -d -o root -g root -m 0700 "$state"
fi
expected=0
if [ -e "$marker" ] || [ -L "$marker" ]; then
  test -f "$marker" && test ! -L "$marker" && test ! -s "$marker"
  test "$(stat -c '%u:%a' "$marker")" = '0:600'
  expected=1
fi
token="$(curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \\
  -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \\
  http://169.254.169.254/latest/api/token)"
instance="$(curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \\
  -H "X-aws-ec2-metadata-token: $token" \\
  http://169.254.169.254/latest/meta-data/instance-id)"
[[ "$instance" =~ ^i-[0-9a-f]+$ ]]
now="$(date +%s)"; aggregate=1; metric_data='['
for index in "${{!services[@]}}"; do
  service="${{services[$index]}}"
  threshold="${{thresholds[$index]}}"
  unit="moodle-autotask-$service.service"; healthy=1
  group=moodle-autotask; [ "$service" = agent ] && group=moodle-agent
  pulse="$root/$service"; gid="$(getent group "$group" | cut -d: -f3)"
  test -n "$gid"; test ! -L "$pulse"
  metadata="$(stat -c '%F:%u:%g:%a:%s:%Y' "$pulse" 2>/dev/null || true)"; mtime="${{metadata##*:}}"
  [[ "$metadata" = regular\\ empty\\ file:0:$gid:620:0:* && "$mtime" =~ ^[0-9]+$ ]] || healthy=0
  mapfile -t details < <(systemctl show "$unit" --property=ActiveState \\
    --property=SubState --property=NRestarts --value 2>/dev/null || true)
  restarts="${{details[2]:-invalid}}"
  if [ "$expected" -eq 1 ]; then
    systemctl is-enabled --quiet "$unit" && [ "${{details[0]:-}}" = active ] \\
      && [ "${{details[1]:-}}" = running ] || healthy=0
    [ $((now-mtime)) -le "$threshold" ] 2>/dev/null || healthy=0
    [[ "$restarts" =~ ^[0-9]+$ ]] || healthy=0
    restart_file="$state/$service"; changed=$((now-300))
    if [ -e "$restart_file" ] || [ -L "$restart_file" ]; then
      test -f "$restart_file" && test ! -L "$restart_file" \\
        && test "$(stat -c '%u:%a' "$restart_file")" = '0:600'
      read -r prior changed <"$restart_file" || healthy=0
      [[ "$prior" =~ ^[0-9]+$ && "$changed" =~ ^[0-9]+$ ]] || healthy=0
      [ "$prior" = "$restarts" ] || changed="$now"
    fi
    temporary="$(mktemp "$state/.$service.XXXXXX")"
    printf '%s %s\\n' "$restarts" "$changed" >"$temporary"
    chmod 0600 "$temporary"; chown root:root "$temporary"
    mv -f "$temporary" "$restart_file"
    [ $((now-changed)) -ge 300 ] || healthy=0
  else
    ! systemctl is-enabled --quiet "$unit" && ! systemctl is-active --quiet "$unit" || healthy=0
  fi
  [ "$healthy" -eq 1 ] || aggregate=0
  metric_data+="{{\\\"MetricName\\\":\\\"ServiceStateMatchesExpectation\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"$service\\\"}}],\\\"Value\\\":$healthy}},"
done
metric_data+="{{\\\"MetricName\\\":\\\"ControllerStateMatchesExpectation\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"aggregate\\\"}}],\\\"Value\\\":$aggregate}},{{\\\"MetricName\\\":\\\"ServicesExpectedRunning\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"aggregate\\\"}}],\\\"Value\\\":$expected}}]"
temp="$(mktemp "$root/.metrics.XXXXXX")"; trap 'rm -f "$temp"' EXIT
printf '%s' "$metric_data" >"$temp"
aws cloudwatch put-metric-data --region {selected_region} \\
  --namespace MoodleAutotask/Controller --metric-data "file://$temp"
'''


def _health_prepare_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
root=/run/moodle-autotask-health
if [ -e "$root" ] || [ -L "$root" ]; then
  test -d "$root" && test ! -L "$root" && test "$(stat -c '%u:%a' "$root")" = '0:711'
else
  install -d -o root -g root -m 0711 "$root"
fi
for item in scheduler:moodle-autotask telegram:moodle-autotask \\
  worker:moodle-autotask agent:moodle-agent; do
  name="${item%%:*}"; group="${item#*:}"; path="$root/$name"
  if [ -e "$path" ] || [ -L "$path" ]; then
    test -f "$path" && test ! -L "$path" && test ! -s "$path"
    test "$(stat -c '%u:%G:%a' "$path")" = "0:$group:620"
  else
    install -o root -g "$group" -m 0620 /dev/null "$path"
  fi
done
"""


def _health_unit() -> str:
    return """[Unit]
Description=Moodle Autotask controller health publisher
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
Group=root
ExecStart=/usr/local/sbin/moodle-autotask-health-publish
ExecStartPre=/usr/local/sbin/moodle-autotask-health-prepare
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/run/moodle-autotask-health /var/lib/moodle-autotask
"""


def _health_timer_unit() -> str:
    return """[Unit]
Description=Publish Moodle Autotask controller health every minute

[Timer]
OnBootSec=60s
OnUnitActiveSec=60s
AccuracySec=1s
Unit=moodle-autotask-health.service

[Install]
WantedBy=timers.target
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
            "--request-timeout-seconds 60",
            "--scheduler-config-file /etc/moodle-autotask/scheduler.json",
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
            "ReadWritePaths=/var/lib/moodle-autotask /etc/moodle-autotask /run/lock "
            "/run/moodle-autotask-health",
            "RestrictSUIDSGID=true",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )


def _agent_unit() -> str:
    command = " ".join(
        (
            "/opt/moodle-autotask/current/venv/bin/moodle-autotask-agent",
            "run",
            "--jobs /var/spool/moodle-autotask/jobs",
            "--results /var/spool/moodle-autotask/results",
            "--workspaces /var/lib/moodle-agent/workspaces",
            "--codex /usr/local/bin/moodle-autotask-codex",
            "--interval-seconds 15",
            "--timeout-seconds 1800",
        )
    )
    return "\n".join(
        (
            "[Unit]",
            "Description=Moodle Autotask isolated Codex execution agent",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "User=moodle-agent",
            "Group=moodle-agent",
            "Environment=HOME=/var/lib/moodle-agent",
            "Environment=CODEX_HOME=/var/lib/moodle-agent/.codex",
            "WorkingDirectory=/var/lib/moodle-agent",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=30",
            "UMask=0027",
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "PrivateTmp=true",
            "ProtectControlGroups=true",
            "ProtectHome=true",
            "ProtectKernelModules=true",
            "ProtectKernelTunables=true",
            "ProtectSystem=strict",
            "ReadOnlyPaths=/var/spool/moodle-autotask/jobs /etc/codex",
            "ReadWritePaths=/var/lib/moodle-agent /var/spool/moodle-autotask/results "
            "/run/moodle-autotask-health",
            "IPAddressDeny=169.254.169.254/32",
            "IPAddressDeny=fd00:ec2::254/128",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
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
            "ReadWritePaths=/var/lib/moodle-autotask /etc/moodle-autotask /run/lock "
            "/run/moodle-autotask-health",
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
            "--telegram-config-file /etc/moodle-autotask/telegram.json",
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
            "--agent-jobs /var/spool/moodle-autotask/jobs",
            "--agent-results /var/spool/moodle-autotask/results",
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
            "ReadWritePaths=/var/lib/moodle-autotask /etc/moodle-autotask /run/lock "
            "/var/spool/moodle-autotask/jobs /run/moodle-autotask-health",
            "RestrictSUIDSGID=true",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )
