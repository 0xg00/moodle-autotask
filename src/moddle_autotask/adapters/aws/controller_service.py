"""Install hardened controller services without embedding secret values."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import subprocess
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
_CODEX_PACKAGE_ARCHIVE_SHA256 = (
    "bd758d53d56e41dc65e045f4589df79a038ed197a011adcb52a258e6ad64cfda"
)
_CODEX_BINARY_SHA256 = (
    "cb0a15567e9a60a5820d54b0f6ae86d504dc3805c1eab21a47f70e3eb7b73a40"
)
_CODEX_CODE_MODE_HOST_SHA256 = (
    "00ecf5d040865b97884c488883abd342581c2a432debe7a54e4646bceee3d2d6"
)
_CODEX_PACKAGE_METADATA_SHA256 = (
    "00f66f11cc7d5c4133d500b4aae6ed4975608d6b040eefd56dc1ff343566e8cf"
)
_CODEX_RG_SHA256 = (
    "e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849"
)
_CODEX_BWRAP_SHA256 = (
    "77360cb751ccedc5971391444ac86a8a33c15b04d6b4a6fe45f5d25496e62c4c"
)
_CODEX_ZSH_SHA256 = (
    "67faaaa89242c4a332e16e508a1977cffc24bf7fca31d4411cdfd101f3831ef3"
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
    workspace_setup = _workspace_setup_script()
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
    _write(root, Path("usr/local/sbin/moodle-autotask-workspace-setup"), workspace_setup, 0o750)
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
        subprocess.run(
            ["/usr/local/sbin/moodle-autotask-workspace-setup"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1800,
        )
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
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
    archive_name = "codex-package-x86_64-unknown-linux-musl"
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
archive_sha256={shlex.quote(_CODEX_PACKAGE_ARCHIVE_SHA256)}
binary_sha256={shlex.quote(_CODEX_BINARY_SHA256)}
host_sha256={shlex.quote(_CODEX_CODE_MODE_HOST_SHA256)}
metadata_sha256={shlex.quote(_CODEX_PACKAGE_METADATA_SHA256)}
rg_sha256={shlex.quote(_CODEX_RG_SHA256)}
bwrap_sha256={shlex.quote(_CODEX_BWRAP_SHA256)}
zsh_sha256={shlex.quote(_CODEX_ZSH_SHA256)}
install_parent=/opt/moodle-autotask/codex
install_root="$install_parent/package-$version"
install_target="$install_root/bin/codex"

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
if id -nG moodle-autotask | tr ' ' '\n' | grep -Fxq "$agent_user"; then
  echo 'moodle-autotask must not belong to the agent group' >&2
  exit 1
fi

install_protocol_layout() {{
  python3 - moodle-autotask "$agent_user" <<'PY'
import grp
import os
import pwd
import stat
import sys

controller_name, agent_name = sys.argv[1:]
try:
    controller = pwd.getpwnam(controller_name)
    agent = pwd.getpwnam(agent_name)
    controller_group = grp.getgrnam(controller_name)
    agent_group = grp.getgrnam(agent_name)
except KeyError as error:
    raise SystemExit("protocol account is unavailable") from error

if controller.pw_uid == 0 or agent.pw_uid == 0:
    raise SystemExit("protocol account is unsafe")


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def open_existing_child(parent_fd: int, name: str, *, trusted_ancestor: bool) -> int:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise SystemExit("protocol ancestor is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit("protocol path is unsafe")
    if trusted_ancestor and (
        metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise SystemExit("protocol ancestor is unsafe")
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISDIR(opened.st_mode)
        ):
            raise SystemExit("protocol path is unsafe")
        if trusted_ancestor and (
            opened.st_uid != 0 or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise SystemExit("protocol ancestor is unsafe")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def install_child(parent_fd: int, name: str, uid: int, gid: int, mode: int) -> int:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit("protocol path is unsafe")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit("protocol path is unsafe")
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISDIR(opened.st_mode)
        ):
            raise SystemExit("protocol path is unsafe")
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise SystemExit("protocol metadata is unsafe")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


descriptors: list[int] = []
try:
    root_fd = os.open("/", _DIRECTORY_FLAGS)
    descriptors.append(root_fd)
    root_metadata = os.fstat(root_fd)
    if root_metadata.st_uid != 0 or stat.S_IMODE(root_metadata.st_mode) & 0o022:
        raise SystemExit("protocol ancestor is unsafe")
    var_fd = open_existing_child(root_fd, "var", trusted_ancestor=True)
    descriptors.append(var_fd)
    spool_fd = open_existing_child(var_fd, "spool", trusted_ancestor=True)
    descriptors.append(spool_fd)
    protocol_fd = install_child(spool_fd, "moodle-autotask", 0, 0, 0o755)
    descriptors.append(protocol_fd)
    jobs_fd = install_child(
        protocol_fd, "jobs", controller.pw_uid, agent_group.gr_gid, 0o2750
    )
    descriptors.append(jobs_fd)
    jobs_retention_fd = install_child(
        jobs_fd, ".retention", controller.pw_uid, agent_group.gr_gid, 0o2750
    )
    descriptors.append(jobs_retention_fd)
    for name in ("committed", "barriers", "locks"):
        descriptors.append(
            install_child(
                jobs_retention_fd, name, controller.pw_uid, agent_group.gr_gid, 0o2750
            )
        )
    results_fd = install_child(
        protocol_fd, "results", agent.pw_uid, controller_group.gr_gid, 0o2750
    )
    descriptors.append(results_fd)
    descriptors.append(
        install_child(results_fd, "bundles", agent.pw_uid, controller_group.gr_gid, 0o2750)
    )
    results_retention_fd = install_child(
        results_fd, ".retention", agent.pw_uid, controller_group.gr_gid, 0o2750
    )
    descriptors.append(results_retention_fd)
    descriptors.append(
        install_child(
            results_retention_fd, "acks", agent.pw_uid, controller_group.gr_gid, 0o2750
        )
    )
    lib_fd = open_existing_child(var_fd, "lib", trusted_ancestor=True)
    descriptors.append(lib_fd)
    controller_state_fd = install_child(
        lib_fd, "moodle-autotask", controller.pw_uid, controller_group.gr_gid, 0o750
    )
    descriptors.append(controller_state_fd)
    descriptors.append(
        install_child(
            controller_state_fd, "retention", controller.pw_uid, controller_group.gr_gid, 0o700
        )
    )
finally:
    for descriptor in reversed(descriptors):
        os.close(descriptor)
PY
}}
install_protocol_layout

install -d -o "$agent_user" -g "$agent_user" -m 0700 "$agent_home" "$codex_home"
install -d -o "$agent_user" -g "$agent_user" -m 0700 \
  "$agent_home/workspaces"
if [ -e "$codex_home/auth.json" ] || [ -L "$codex_home/auth.json" ]; then
  test -f "$codex_home/auth.json"
  test ! -L "$codex_home/auth.json"
  test "$(stat -c '%U:%G:%a' "$codex_home/auth.json")" = \
    "$agent_user:$agent_user:600"
fi

temporary_directory="$(mktemp -d /tmp/moodle-autotask-codex.XXXXXX)"
package_candidate=
cleanup() {{
  if [ -n "$package_candidate" ]; then
    rm -rf -- "$package_candidate"
  fi
  rm -rf "$temporary_directory"
}}
trap cleanup EXIT

validate_package() {{
  local root="$1"
  test -d "$root" && test ! -L "$root" || return 1
  test "$(stat -c '%U:%G:%a' "$root")" = root:root:755 || return 1
  local expected_inventory
  expected_inventory="$(printf '%s\n' \
    bin bin/codex bin/codex-code-mode-host codex-package.json codex-path \
    codex-path/rg codex-resources codex-resources/bwrap codex-resources/zsh \
    codex-resources/zsh/bin codex-resources/zsh/bin/zsh | sort)"
  test "$(find -P "$root" -mindepth 1 -printf '%P\n' | sort)" = \
    "$expected_inventory" || return 1
  local directory
  for directory in bin codex-path codex-resources codex-resources/zsh \
    codex-resources/zsh/bin; do
    test -d "$root/$directory" && test ! -L "$root/$directory" || return 1
    test "$(stat -c '%U:%G:%a' "$root/$directory")" = root:root:755 || return 1
  done
  local executable
  for executable in bin/codex bin/codex-code-mode-host codex-path/rg \
    codex-resources/bwrap codex-resources/zsh/bin/zsh; do
    test -f "$root/$executable" && test ! -L "$root/$executable" || return 1
    test "$(stat -c '%U:%G:%a:%h' "$root/$executable")" = root:root:755:1 \
      || return 1
  done
  test -f "$root/codex-package.json" && test ! -L "$root/codex-package.json" \
    || return 1
  test "$(stat -c '%U:%G:%a:%h' "$root/codex-package.json")" = root:root:644:1 \
    || return 1
  echo "$binary_sha256  $root/bin/codex" | sha256sum --check --strict --status \
    || return 1
  echo "$host_sha256  $root/bin/codex-code-mode-host" \
    | sha256sum --check --strict --status || return 1
  echo "$metadata_sha256  $root/codex-package.json" \
    | sha256sum --check --strict --status || return 1
  echo "$rg_sha256  $root/codex-path/rg" | sha256sum --check --strict --status \
    || return 1
  echo "$bwrap_sha256  $root/codex-resources/bwrap" \
    | sha256sum --check --strict --status || return 1
  echo "$zsh_sha256  $root/codex-resources/zsh/bin/zsh" \
    | sha256sum --check --strict --status || return 1
}}

test -d /opt && test ! -L /opt
test "$(stat -c '%U:%G:%a' /opt)" = root:root:755
for trusted_directory in /opt/moodle-autotask "$install_parent"; do
  if [ -e "$trusted_directory" ] || [ -L "$trusted_directory" ]; then
    test -d "$trusted_directory"
    test ! -L "$trusted_directory"
    test "$(stat -c '%U:%G:%a' "$trusted_directory")" = root:root:755
  else
    mkdir -- "$trusted_directory"
    chown root:root "$trusted_directory"
    chmod 0755 "$trusted_directory"
  fi
  test "$(stat -c '%U:%G:%a' "$trusted_directory")" = root:root:755
done

if [ -e "$install_root" ] || [ -L "$install_root" ]; then
  validate_package "$install_root"
else
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --fail --silent --show-error --location "$archive_url" \
    --output "$temporary_directory/codex.tar.gz"
  echo "$archive_sha256  $temporary_directory/codex.tar.gz" \
    | sha256sum --check --strict
  python3 - "$temporary_directory/codex.tar.gz" \
    "$temporary_directory/extracted" "$binary_sha256" "$host_sha256" \
    "$metadata_sha256" "$rg_sha256" "$bwrap_sha256" "$zsh_sha256" <<'PY'
import hashlib
import os
from pathlib import Path
import sys
import tarfile

archive_path, extraction_path, *hashes = sys.argv[1:]
expected_files = dict(
    zip(
        (
            "bin/codex",
            "bin/codex-code-mode-host",
            "codex-package.json",
            "codex-path/rg",
            "codex-resources/bwrap",
            "codex-resources/zsh/bin/zsh",
        ),
        zip(
            hashes,
            (258278208, 49682360, 205, 5408904, 529776, 898480),
            strict=True,
        ),
        strict=True,
    )
)
expected_directories = set(
    (
        "bin",
        "codex-path",
        "codex-resources",
        "codex-resources/zsh",
        "codex-resources/zsh/bin",
    )
)
expected_names = expected_directories | set(expected_files)
destination_root = Path(extraction_path)
destination_root.mkdir(mode=0o700)

with tarfile.open(archive_path, mode="r:gz") as archive:
    members = archive.getmembers()
    seen = set()
    by_name = dict()
    for member in members:
        name = member.name.rstrip("/")
        if name in seen or name not in expected_names or member.name.startswith("/"):
            raise SystemExit("Codex package inventory is invalid")
        seen.add(name)
        by_name[name] = member
        if name in expected_directories:
            if not member.isdir():
                raise SystemExit("Codex package directory is invalid")
        elif not member.isreg() or member.linkname:
            raise SystemExit("Codex package file is invalid")
    if seen != expected_names:
        raise SystemExit("Codex package inventory is incomplete")

    for name, (expected_hash, expected_size) in expected_files.items():
        member = by_name[name]
        if member.size != expected_size:
            raise SystemExit("Codex package file size is invalid")
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit("Codex package file is unavailable")
        target = destination_root.joinpath(*name.split("/"))
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        digest = hashlib.sha256()
        remaining = expected_size
        try:
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise SystemExit("Codex package file is truncated")
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                remaining -= len(block)
            if source.read(1):
                raise SystemExit("Codex package file is oversized")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            source.close()
        if digest.hexdigest() != expected_hash:
            raise SystemExit("Codex package file digest is invalid")
PY
  package_candidate="$(mktemp -d "$install_parent/.package-$version.XXXXXX")"
  chmod 0755 "$package_candidate"
  install -d -o root -g root -m 0755 \
    "$package_candidate/bin" "$package_candidate/codex-path" \
    "$package_candidate/codex-resources" \
    "$package_candidate/codex-resources/zsh" \
    "$package_candidate/codex-resources/zsh/bin"
  install -o root -g root -m 0644 \
    "$temporary_directory/extracted/codex-package.json" \
    "$package_candidate/codex-package.json"
  install -o root -g root -m 0755 \
    "$temporary_directory/extracted/bin/codex" \
    "$temporary_directory/extracted/bin/codex-code-mode-host" \
    "$package_candidate/bin/"
  install -o root -g root -m 0755 \
    "$temporary_directory/extracted/codex-path/rg" \
    "$package_candidate/codex-path/rg"
  install -o root -g root -m 0755 \
    "$temporary_directory/extracted/codex-resources/bwrap" \
    "$package_candidate/codex-resources/bwrap"
  install -o root -g root -m 0755 \
    "$temporary_directory/extracted/codex-resources/zsh/bin/zsh" \
    "$package_candidate/codex-resources/zsh/bin/zsh"
  validate_package "$package_candidate"
  test ! -e "$install_root" && test ! -L "$install_root"
  mv -T "$package_candidate" "$install_root"
  package_candidate=
fi
validate_package "$install_root"
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
mount_value() {{
  column="$1"
  target="$2"
  case "$column" in TARGET|SOURCE|FSTYPE|OPTIONS|UUID) ;; *) return 1 ;; esac
  values="$(findmnt -rn -o "$column" --target "$target" 2>/dev/null | sort -u)" || return 1
  test -n "$values" || return 1
  test "$(printf '%s\\n' "$values" | wc -l)" -eq 1 || return 1
  printf '%s' "$values"
}}
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
  active_state=""; sub_state=""; restarts=""
  active_seen=false; sub_seen=false; restarts_seen=false
  details="$(systemctl show "$unit" --property=ActiveState --property=SubState \\
    --property=NRestarts 2>/dev/null || true)"
  while IFS= read -r detail || [ -n "$detail" ]; do
    case "$detail" in
      ActiveState=*)
        [ "$active_seen" = false ] || healthy=0
        active_state="${{detail#ActiveState=}}"; active_seen=true
        ;;
      SubState=*)
        [ "$sub_seen" = false ] || healthy=0
        sub_state="${{detail#SubState=}}"; sub_seen=true
        ;;
      NRestarts=*)
        [ "$restarts_seen" = false ] || healthy=0
        restarts="${{detail#NRestarts=}}"; restarts_seen=true
        ;;
      *) healthy=0 ;;
    esac
  done <<<"$details"
  [ "$active_seen" = true ] && [ "$sub_seen" = true ] && [ "$restarts_seen" = true ] || healthy=0
  [[ "$active_state" =~ ^[a-z]+$ && "$sub_state" =~ ^[a-z-]+$ \\
    && "$restarts" =~ ^[0-9]+$ ]] || healthy=0
  if [ "$expected" -eq 1 ]; then
    systemctl is-enabled --quiet "$unit" && [ "$active_state" = active ] \\
      && [ "$sub_state" = running ] || healthy=0
    [ $((now-mtime)) -le "$threshold" ] 2>/dev/null || healthy=0
    if [[ "$restarts" =~ ^[0-9]+$ ]]; then
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
    fi
  else
    ! systemctl is-enabled --quiet "$unit" && ! systemctl is-active --quiet "$unit" || healthy=0
  fi
  [ "$healthy" -eq 1 ] || aggregate=0
  metric_data+="{{\\\"MetricName\\\":\\\"ServiceStateMatchesExpectation\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"$service\\\"}}],\\\"Value\\\":$healthy}},"
done
storage=1; root_bytes=0; root_inodes=0; workspace_bytes=0
read -r root_block root_available root_inodes <<<"$(stat -f -c '%S %a %d' / 2>/dev/null || true)"
if [[ "$root_block" =~ ^[1-9][0-9]*$ && "$root_available" =~ ^[0-9]+$ ]]; then
  [[ "$root_inodes" =~ ^[0-9]+$ ]] || storage=0
  root_bytes=$((root_block * root_available))
else
  storage=0; root_inodes=0
fi
[ "$root_bytes" -ge 12884901888 ] 2>/dev/null || storage=0
[ "$root_inodes" -ge 100000 ] 2>/dev/null || storage=0
for item in \
  /var/lib/moodle-autotask=moodle-autotask:moodle-autotask:750 \
  /var/lib/moodle-autotask-root=root:root:700 \
  /var/lib/moodle-autotask/retention=moodle-autotask:moodle-autotask:700 \
  /var/spool/moodle-autotask/jobs=moodle-autotask:moodle-agent:2750 \
  /var/spool/moodle-autotask/jobs/.retention=moodle-autotask:moodle-agent:2750 \
  /var/spool/moodle-autotask/results=moodle-agent:moodle-autotask:2750 \
  /var/spool/moodle-autotask/results/bundles=moodle-agent:moodle-autotask:2750; do
  path="${{item%%=*}}"; expected_metadata="${{item#*=}}"
  metadata="$(stat -c '%U:%G:%a' "$path" 2>/dev/null || true)"
  test -d "$path" && test ! -L "$path" && [ "$metadata" = "$expected_metadata" ] || storage=0
done
workspace=/var/lib/moodle-agent/workspaces; image=/var/lib/moodle-autotask-root/agent-workspaces.img
mount_target="$(mount_value TARGET "$workspace" 2>/dev/null || true)"
mount_source="$(mount_value SOURCE "$workspace" 2>/dev/null || true)"
mount_type="$(mount_value FSTYPE "$workspace" 2>/dev/null || true)"
mount_options="$(mount_value OPTIONS "$workspace" 2>/dev/null || true)"
mount_uuid="$(mount_value UUID "$workspace" 2>/dev/null || true)"
image_uuid="$(blkid -s UUID -o value "$image" 2>/dev/null || true)"
loop_backing="$(losetup --noheadings --output BACK-FILE "$mount_source" 2>/dev/null || true)"
loop_backing="$(tr -d ' ' <<<"$loop_backing")"
workspace_metadata="$(stat -c '%U:%G:%a' "$workspace" 2>/dev/null || true)"
if [ "$mount_target" = "$workspace" ] && [[ "$mount_source" =~ ^/dev/loop[0-9]+$ ]] \
  && [ "$loop_backing" = "$image" ] && [ "$mount_type" = ext4 ] \
  && [ "$mount_uuid" = "$image_uuid" ] && [[ ",$mount_options," == *,nodev,* ]] \
  && [[ ",$mount_options," == *,nosuid,* ]] \
  && [ "$workspace_metadata" = moodle-agent:moodle-agent:700 ]; then
  workspace_stats="$(stat -f -c '%S %a %d' "$workspace" 2>/dev/null || true)"
  read -r workspace_block workspace_available workspace_inodes <<<"$workspace_stats"
  if [[ "$workspace_block" =~ ^[1-9][0-9]*$ && "$workspace_available" =~ ^[0-9]+$ ]]; then
    [[ "$workspace_inodes" =~ ^[0-9]+$ ]] || storage=0
    workspace_bytes=$((workspace_block * workspace_available))
  else
    storage=0
  fi
else
  storage=0
fi
[ "$workspace_bytes" -ge 2147483648 ] 2>/dev/null || storage=0
[ "${{workspace_inodes:-0}}" -ge 20000 ] 2>/dev/null || storage=0
metric_data+="{{\\\"MetricName\\\":\\\"ControllerStateMatchesExpectation\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"aggregate\\\"}}],\\\"Value\\\":$aggregate}},{{\\\"MetricName\\\":\\\"ServicesExpectedRunning\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"aggregate\\\"}}],\\\"Value\\\":$expected}},{{\\\"MetricName\\\":\\\"StorageAdmissionOpen\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"storage\\\"}}],\\\"Value\\\":$storage}},{{\\\"MetricName\\\":\\\"RootFilesystemFreeBytes\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"storage\\\"}}],\\\"Value\\\":$root_bytes}},{{\\\"MetricName\\\":\\\"RootFilesystemFreeInodes\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"storage\\\"}}],\\\"Value\\\":$root_inodes}},{{\\\"MetricName\\\":\\\"WorkspaceFilesystemFreeBytes\\\",\\\"Dimensions\\\":[{{\\\"Name\\\":\\\"InstanceId\\\",\\\"Value\\\":\\\"$instance\\\"}},{{\\\"Name\\\":\\\"Service\\\",\\\"Value\\\":\\\"storage\\\"}}],\\\"Value\\\":$workspace_bytes}}]"
temp="$(mktemp "$root/.metrics.XXXXXX")"; trap 'rm -f "$temp"' EXIT
printf '%s' "$metric_data" >"$temp"
aws cloudwatch put-metric-data --region {selected_region} \\
  --namespace MoodleAutotask/Controller --metric-data "file://$temp"
'''


def _workspace_setup_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
umask 077

lock_path=/run/moodle-autotask-workspace-setup.lock
if [ -z "${MOODLE_AUTOTASK_WORKSPACE_LOCK_FD:-}" ]; then
  exec python3 - "$0" "$@" <<'PY'
import fcntl
import os
import stat
import sys

lock_path = "/run/moodle-autotask-workspace-setup.lock"
parent = os.lstat(os.path.dirname(lock_path))
if (
    not stat.S_ISDIR(parent.st_mode)
    or parent.st_uid != 0
    or parent.st_gid != 0
    or stat.S_IMODE(parent.st_mode) != 0o755
):
    raise SystemExit("workspace lock parent is unsafe")
flags = os.O_RDWR | os.O_NOFOLLOW
try:
    descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    descriptor = os.open(lock_path, flags)
metadata = os.fstat(descriptor)
current = os.lstat(lock_path)
identity = lambda item: (item.st_dev, item.st_ino)
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != 0
    or metadata.st_gid != 0
    or stat.S_IMODE(metadata.st_mode) != 0o600
    or metadata.st_nlink != 1
    or identity(metadata) != identity(current)
):
    raise SystemExit("workspace lock is unsafe")
fcntl.flock(descriptor, fcntl.LOCK_EX)
current = os.lstat(lock_path)
if identity(metadata) != identity(current):
    raise SystemExit("workspace lock changed while acquiring it")
os.set_inheritable(descriptor, True)
environment = os.environ.copy()
environment["MOODLE_AUTOTASK_WORKSPACE_LOCK_FD"] = str(descriptor)
os.execve("/bin/bash", ["bash", sys.argv[1], *sys.argv[2:]], environment)
PY
fi
python3 - "$lock_path" "$MOODLE_AUTOTASK_WORKSPACE_LOCK_FD" <<'PY'
import fcntl
import os
import stat
import sys

lock_path = sys.argv[1]
try:
    descriptor = int(sys.argv[2])
except ValueError as error:
    raise SystemExit("workspace lock descriptor is invalid") from error
metadata = os.fstat(descriptor)
current = os.lstat(lock_path)
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != 0
    or metadata.st_gid != 0
    or stat.S_IMODE(metadata.st_mode) != 0o600
    or metadata.st_nlink != 1
    or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
):
    raise SystemExit("workspace lock is unsafe")
fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY
unset MOODLE_AUTOTASK_WORKSPACE_LOCK_FD

image_root=/var/lib/moodle-autotask-root
image="$image_root/agent-workspaces.img"
candidate="$image_root/.agent-workspaces.img.pending"
state="$image_root/agent-workspaces.state"
state_candidate="$image_root/.agent-workspaces.state.pending"
backup="$image_root/legacy-workspaces.pending"
workspace=/var/lib/moodle-agent/workspaces
staging=/run/moodle-autotask-workspace-migration
fstab=/etc/fstab
size=17179869184
allocation_slack=67108864

safe_directory() {
  test -d "$1" && test ! -L "$1" && test "$(stat -c '%U:%G:%a' "$1")" = "$2"
}
mount_value() {
  column="$1"
  target="$2"
  case "$column" in TARGET|SOURCE|FSTYPE|OPTIONS|UUID) ;; *) return 1 ;; esac
  values="$(findmnt -rn -o "$column" --target "$target" 2>/dev/null | sort -u)" || return 1
  test -n "$values" || return 1
  test "$(printf '%s\\n' "$values" | wc -l)" -eq 1 || return 1
  printf '%s' "$values"
}
safe_image() {
  target="$1"
  expected_links="$2"
  test -f "$target" && test ! -L "$target"
  test "$(stat -c '%U:%G:%a:%s:%h' "$target")" = \
    "root:root:600:$size:$expected_links"
  blocks="$(stat -c '%b' "$target")"
  [[ "$blocks" =~ ^[0-9]+$ ]] \
    && [ $((blocks * 512)) -ge $((size - allocation_slack)) ]
  test "$(blkid -s TYPE -o value "$target" 2>/dev/null || true)" = ext4
  uuid="$(blkid -s UUID -o value "$target" 2>/dev/null || true)"
  [[ "$uuid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
  tune2fs_data="$(tune2fs -l "$target" 2>/dev/null)"
  inode_count="$(awk -F: '/^Inode count:/ {gsub(/ /, "", $2); print $2}' <<<"$tune2fs_data")"
  block_count="$(awk -F: '/^Block count:/ {gsub(/ /, "", $2); print $2}' <<<"$tune2fs_data")"
  reserved_line="$(grep -F 'Reserved block count:' <<<"$tune2fs_data")"
  reserved_blocks="$(awk -F: '{gsub(/ /, "", $2); print $2}' <<<"$reserved_line")"
  [[ "$inode_count" =~ ^[0-9]+$ && "$inode_count" -ge 100000 ]]
  [[ "$block_count" =~ ^[1-9][0-9]*$ && "$reserved_blocks" =~ ^[0-9]+$ ]]
  reserved_floor=$((block_count * 6 / 100))
  reserved_ceil=$(((block_count * 6 + 99) / 100))
  [ "$reserved_blocks" -ge "$reserved_floor" ] && [ "$reserved_blocks" -le "$reserved_ceil" ]
}
validate_fstab() {
  safe_fstab
  expected="$image $workspace ext4 loop,nodev,nosuid 0 2"
  entries="$(awk -v target="$workspace" '$1 !~ /^#/ && $2 == target {print}' "$fstab")"
  [ "$entries" = "$expected" ]
}
safe_fstab() {
  test -f "$fstab" && test ! -L "$fstab"
  test "$(stat -c '%U:%G:%a:%h' "$fstab")" = root:root:644:1
}
validate_mount_base() {
  safe_image "$image" 1
  test "$(mount_value TARGET "$workspace" 2>/dev/null || true)" = "$workspace"
  source="$(mount_value SOURCE "$workspace" 2>/dev/null || true)"
  [[ "$source" =~ ^/dev/loop[0-9]+$ ]]
  test "$(losetup --noheadings --output BACK-FILE "$source" | tr -d ' ')" = "$image"
  test "$(mount_value FSTYPE "$workspace")" = ext4
  options="$(mount_value OPTIONS "$workspace")"
  [[ ",$options," == *,nodev,* && ",$options," == *,nosuid,* ]]
  test "$(mount_value UUID "$workspace")" = "$(blkid -s UUID -o value "$image")"
}
validate_mount() {
  validate_mount_base
  safe_directory "$workspace" moodle-agent:moodle-agent:700
  test ! -e "$workspace/lost+found" && test ! -L "$workspace/lost+found"
  validate_fstab
}
validate_staging_mount() {
  test "$(mount_value TARGET "$staging" 2>/dev/null || true)" = "$staging"
  source="$(mount_value SOURCE "$staging")"
  [[ "$source" =~ ^/dev/loop[0-9]+$ ]]
  test "$(losetup --noheadings --output BACK-FILE "$source" | tr -d ' ')" = "$image"
  test "$(mount_value FSTYPE "$staging")" = ext4
  options="$(mount_value OPTIONS "$staging")"
  [[ ",$options," == *,nodev,* && ",$options," == *,nosuid,* ]]
}
tree_digest() {
  python3 - "$1" "$2" <<'PY'
import hashlib
import os
import pwd
import stat
import sys

root = os.fsencode(sys.argv[1])
excluded = os.fsencode(sys.argv[2]) if sys.argv[2] else None
agent = pwd.getpwnam("moodle-agent")
digest = hashlib.sha256()


def add(value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def walk(directory: bytes, relative: bytes) -> None:
    with os.scandir(directory) as entries:
        ordered = sorted(entries, key=lambda entry: os.fsencode(entry.name))
    for entry in ordered:
        name = os.fsencode(entry.name)
        if not relative and excluded is not None and name == excluded:
            continue
        path = os.path.join(directory, name)
        child = name if not relative else relative + b"/" + name
        metadata = os.lstat(path)
        if metadata.st_uid != agent.pw_uid or metadata.st_gid != agent.pw_gid:
            raise SystemExit("workspace tree ownership is unsafe")
        if stat.S_ISDIR(metadata.st_mode):
            kind = b"d"
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            kind = b"f"
        else:
            raise SystemExit("workspace tree type is unsafe")
        add(child)
        add(kind)
        add(str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
        add(str(metadata.st_uid).encode("ascii"))
        add(str(metadata.st_gid).encode("ascii"))
        if kind == b"d":
            walk(path, child)
        else:
            add(str(metadata.st_size).encode("ascii"))
            content = hashlib.sha256()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                before = os.fstat(descriptor)
                while chunk := os.read(descriptor, 1024 * 1024):
                    content.update(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            current = os.lstat(path)
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise SystemExit("workspace tree changed during validation")
            add(content.digest())


walk(root, b"")
print(digest.hexdigest())
PY
}
safe_state_node() {
  target="$1"
  test -f "$target" && test ! -L "$target"
  test "$(stat -c '%U:%G:%a:%h' "$target")" = root:root:600:1
}
load_state() {
  migration_phase=none
  migration_digest=
  migration_uuid=
  if [ -e "$state" ] || [ -L "$state" ]; then
    safe_state_node "$state"
    mapfile -t state_lines <"$state"
    [ "${#state_lines[@]}" -eq 4 ]
    [ "${state_lines[0]}" = version=1 ]
    uuid_pattern='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    [[ "${state_lines[1]}" =~ ^uuid=($uuid_pattern)$ ]]
    migration_uuid="${BASH_REMATCH[1]}"
    [[ "${state_lines[2]}" =~ ^phase=(copying|copied|active)$ ]]
    migration_phase="${BASH_REMATCH[1]}"
    [[ "${state_lines[3]}" =~ ^digest=([0-9a-f]{64})$ ]]
    migration_digest="${BASH_REMATCH[1]}"
    test "$migration_uuid" = "$(blkid -s UUID -o value "$image")"
  fi
}
write_state() {
  next_phase="$1"
  next_digest="$2"
  [[ "$next_phase" =~ ^(copying|copied|active)$ ]]
  [[ "$next_digest" =~ ^[0-9a-f]{64}$ ]]
  image_uuid="$(blkid -s UUID -o value "$image")"
  [[ "$image_uuid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
  if [ -e "$state_candidate" ] || [ -L "$state_candidate" ]; then
    safe_state_node "$state_candidate"
  else
    (umask 077; : >"$state_candidate")
    chown root:root "$state_candidate"; chmod 0600 "$state_candidate"
  fi
  printf 'version=1\nuuid=%s\nphase=%s\ndigest=%s\n' \
    "$image_uuid" "$next_phase" "$next_digest" >"$state_candidate"
  sync -f "$state_candidate"
  mv -f "$state_candidate" "$state"
  sync -f "$image_root"
  load_state
  [ "$migration_phase" = "$next_phase" ]
  [ "$migration_digest" = "$next_digest" ]
}
remove_lost_found() {
  root="$1"
  if [ -e "$root/lost+found" ] || [ -L "$root/lost+found" ]; then
    test -d "$root/lost+found" && test ! -L "$root/lost+found"
    test "$(stat -c '%U:%G:%a:%h' "$root/lost+found")" = root:root:700:2
    test -z "$(find "$root/lost+found" -mindepth 1 -maxdepth 1 -print -quit)"
    rmdir "$root/lost+found"
    sync -f "$root"
  fi
}
cleanup_backup() {
  if [ -e "$backup" ] || [ -L "$backup" ]; then
    safe_directory "$backup" moodle-agent:moodle-agent:700
    tree_digest "$backup" '' >/dev/null
    find "$backup" -xdev -mindepth 1 -depth -delete
    rmdir "$backup"
    sync -f "$image_root"
  fi
}
ensure_fstab() {
  safe_fstab
  if awk -v target="$workspace" '$1 !~ /^#/ && $2 == target {found=1} END {exit !found}' \
    "$fstab"; then
    validate_fstab
  else
    temporary="$(mktemp /etc/.fstab.moodle-autotask.XXXXXX)"
    trap 'rm -f "$temporary"' EXIT
    cat "$fstab" >"$temporary"
    printf '%s %s ext4 loop,nodev,nosuid 0 2\n' "$image" "$workspace" >>"$temporary"
    chown root:root "$temporary"; chmod 0644 "$temporary"; mv -f "$temporary" "$fstab"
    trap - EXIT
    validate_fstab
  fi
}

if [ -e "$image_root" ] || [ -L "$image_root" ]; then
  safe_directory "$image_root" root:root:700
else
  install -d -o root -g root -m 0700 "$image_root"
fi
test -d /var/lib/moodle-agent && test ! -L /var/lib/moodle-agent
test "$(stat -c '%U:%G' /var/lib/moodle-agent)" = moodle-agent:moodle-agent
agent_home_mode="$(stat -c '%a' /var/lib/moodle-agent)"
[[ "$agent_home_mode" = 700 || "$agent_home_mode" = 755 ]]
chmod 0700 /var/lib/moodle-agent
workspace_mounted=false
if [ "$(mount_value TARGET "$workspace" 2>/dev/null || true)" = "$workspace" ]; then
  workspace_mounted=true
fi
if [ -e "$image" ] || [ -L "$image" ]; then
  if [ -e "$candidate" ] || [ -L "$candidate" ]; then
    safe_image "$image" 2; safe_image "$candidate" 2
    test "$(stat -c '%d:%i:%h' "$image")" = "$(stat -c '%d:%i:%h' "$candidate")"
    test "$(stat -c '%h' "$image")" = 2
    rm -f "$candidate"; sync -f "$(dirname "$image")"
  fi
  safe_image "$image" 1
else
  if [ -e "$candidate" ] || [ -L "$candidate" ]; then
    safe_image "$candidate" 1
  else
    available="$(df -B1 --output=avail "$(dirname "$image")" | tail -n 1 | tr -d ' ')"
    [[ "$available" =~ ^[0-9]+$ ]] && [ "$available" -ge $((size + 12884901888)) ]
    dd if=/dev/zero of="$candidate" bs=64M count=256 conv=fsync status=none
    chown root:root "$candidate"; chmod 0600 "$candidate"
    mkfs.ext4 -F -E nodiscard -N 100000 -m 6 "$candidate" >/dev/null
    formatted_blocks="$(tune2fs -l "$candidate" 2>/dev/null | \
      awk -F: '/^Block count:/ {gsub(/ /, "", $2); print $2}')"
    [[ "$formatted_blocks" =~ ^[1-9][0-9]*$ ]]
    tune2fs -r $(((formatted_blocks * 6 + 99) / 100)) "$candidate" >/dev/null
    sync -f "$candidate"; sync -f "$(dirname "$candidate")"
    safe_image "$candidate" 1
  fi
  ln "$candidate" "$image"
  test "$(stat -c '%h' "$candidate")" = 2
  sync -f "$(dirname "$image")"
  rm -f "$candidate"
  test ! -e "$candidate" && test ! -L "$candidate"
  sync -f "$(dirname "$image")"
  safe_image "$image" 1
fi
load_state
if [ "$workspace_mounted" = true ]; then
  validate_mount_base
  remove_lost_found "$workspace"
  chown moodle-agent:moodle-agent "$workspace"; chmod 0700 "$workspace"
  mounted_digest="$(tree_digest "$workspace" '')"
  if [ "$migration_phase" = none ]; then
    if [ -e "$backup" ] || [ -L "$backup" ]; then
      safe_directory "$backup" moodle-agent:moodle-agent:700
      [ "$(tree_digest "$backup" '')" = "$mounted_digest" ]
    fi
    ensure_fstab
    validate_mount
    write_state active "$mounted_digest"
  elif [ "$migration_phase" = copied ]; then
    [ "$mounted_digest" = "$migration_digest" ]
    ensure_fstab
    validate_mount
    write_state active "$migration_digest"
  else
    [ "$migration_phase" = active ]
    validate_mount
  fi
  cleanup_backup
  exit 0
fi

if [ "$migration_phase" = active ]; then
  if [ -e "$workspace" ] || [ -L "$workspace" ]; then
    safe_directory "$workspace" moodle-agent:moodle-agent:700
    test -z "$(find "$workspace" -mindepth 1 -maxdepth 1 -print -quit)"
  else
    install -d -o moodle-agent -g moodle-agent -m 0700 "$workspace"
  fi
  ensure_fstab
  mount "$workspace"
  validate_mount_base
  remove_lost_found "$workspace"
  chown moodle-agent:moodle-agent "$workspace"; chmod 0700 "$workspace"
  validate_mount
  cleanup_backup
  exit 0
fi

if [ "$migration_phase" = none ] || [ "$migration_phase" = copying ]; then
  test ! -e "$backup" && test ! -L "$backup"
  if [ -e "$workspace" ] || [ -L "$workspace" ]; then
    safe_directory "$workspace" moodle-agent:moodle-agent:700
  else
    install -d -o moodle-agent -g moodle-agent -m 0700 "$workspace"
  fi
fi
if [ -e "$staging" ] || [ -L "$staging" ]; then
  safe_directory "$staging" root:root:700
else
  install -d -o root -g root -m 0700 "$staging"
fi
if [ "$(mount_value TARGET "$staging" 2>/dev/null || true)" = "$staging" ]; then
  validate_staging_mount
else
  mount -o loop,nodev,nosuid "$image" "$staging"
  validate_staging_mount
fi
chown root:root "$staging"; chmod 0700 "$staging"
safe_directory "$staging" root:root:700
cleanup_staging() {
  result=$?
  trap - EXIT
  if [ "$(mount_value TARGET "$staging" 2>/dev/null || true)" = "$staging" ]; then
    umount "$staging" || true
  fi
  exit "$result"
}
trap cleanup_staging EXIT

if [ "$migration_phase" = none ]; then
  remove_lost_found "$staging"
  test -z "$(find "$staging" -xdev -mindepth 1 -maxdepth 1 -print -quit)"
  migration_digest="$(tree_digest "$workspace" '')"
  write_state copying "$migration_digest"
fi
if [ "$migration_phase" = copying ]; then
  [ "$(tree_digest "$workspace" '')" = "$migration_digest" ]
  remove_lost_found "$staging"
  find "$staging" -xdev -mindepth 1 -depth -delete
  test -z "$(find "$staging" -xdev -mindepth 1 -maxdepth 1 -print -quit)"
  cp -a -- "$workspace/." "$staging/"
  chown root:root "$staging"; chmod 0700 "$staging"
  test "$(tree_digest "$staging" '')" = "$migration_digest"
  sync -f "$staging"
  write_state copied "$migration_digest"
fi
[ "$migration_phase" = copied ]
[ "$(tree_digest "$staging" '')" = "$migration_digest" ]
if [ -e "$backup" ] || [ -L "$backup" ]; then
  safe_directory "$backup" moodle-agent:moodle-agent:700
  [ "$(tree_digest "$backup" '')" = "$migration_digest" ]
  if [ -e "$workspace" ] || [ -L "$workspace" ]; then
    safe_directory "$workspace" moodle-agent:moodle-agent:700
    test -z "$(find "$workspace" -mindepth 1 -maxdepth 1 -print -quit)"
  else
    install -d -o moodle-agent -g moodle-agent -m 0700 "$workspace"
  fi
else
  safe_directory "$workspace" moodle-agent:moodle-agent:700
  [ "$(tree_digest "$workspace" '')" = "$migration_digest" ]
  test "$(stat -c '%d' "$workspace")" = "$(stat -c '%d' "$image_root")"
  mv -T "$workspace" "$backup"
  sync -f /var/lib/moodle-agent; sync -f "$image_root"
  safe_directory "$backup" moodle-agent:moodle-agent:700
  [ "$(tree_digest "$backup" '')" = "$migration_digest" ]
  install -d -o moodle-agent -g moodle-agent -m 0700 "$workspace"
fi
umount "$staging"
trap - EXIT
rmdir "$staging"
ensure_fstab
mount "$workspace"
validate_mount_base
remove_lost_found "$workspace"
chown moodle-agent:moodle-agent "$workspace"; chmod 0700 "$workspace"
validate_mount
write_state active "$migration_digest"
cleanup_backup
"""


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
            "--bundles /var/spool/moodle-autotask/results/bundles",
            "--retention-root /var/lib/moodle-agent",
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
            "After=network-online.target local-fs.target",
            "RequiresMountsFor=/var/lib/moodle-agent/workspaces",
            "",
            "[Service]",
            "Type=simple",
            "User=moodle-agent",
            "Group=moodle-agent",
            "Environment=HOME=/var/lib/moodle-agent",
            "Environment=CODEX_HOME=/var/lib/moodle-agent/.codex",
            "WorkingDirectory=/var/lib/moodle-agent",
            "ExecStartPre=+/usr/local/sbin/moodle-autotask-workspace-setup",
            "TimeoutStartSec=30min",
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
            "--retention-controller-private /var/lib/moodle-autotask",
            "--retention-agent-private /var/lib/moodle-agent",
            "--retention-workspaces /var/lib/moodle-agent/workspaces",
            "--retention-bundles /var/spool/moodle-autotask/results/bundles",
            "--retention-scratch-ttl 86400",
            "--retention-evidence-ttl 604800",
            "--retention-candidate-limit 1024",
            "--retention-scan-limit 1024",
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
