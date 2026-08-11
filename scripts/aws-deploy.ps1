[CmdletBinding()]
param(
    [ValidateSet('Deploy', 'Status', 'Activate', 'Deactivate', 'CodexLogin', 'CodexSmoke')]
    [string]$Action = 'Deploy',

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9]{12}$')]
    [string]$AccountId,

    [ValidatePattern('^[a-z]{2}-[a-z]+-[0-9]$')]
    [string]$Region = 'eu-south-2',

    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
    [string]$Profile = 'moodle-autotask',

    [ValidatePattern('^[a-z0-9][a-z0-9-]{1,19}$')]
    [string]$Environment = 'development',

    [ValidateSet('t3.large', 'm6i.large')]
    [string]$LabInstanceType = 't3.large',

    [ValidateRange(50, 500)]
    [int]$LabRootVolumeSizeGiB = 80,

    [ValidatePattern('^$|^i-[0-9a-f]{8,17}$')]
    [string]$InstanceId = '',

    [string[]]$CourseShortname = @(),

    [switch]$AllCourses,

    [int]$MaxNewEventsPerCycle = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:AWS_CLI_FILE_ENCODING = 'UTF-8'
$env:AWS_CLI_OUTPUT_ENCODING = 'UTF-8'
$env:AWS_PAGER = ''

function New-SchedulerConfigJson {
    param(
        [string[]]$SelectedCourseShortnames,

        [Parameter(Mandatory)]
        [bool]$UseAllCourses,

        [Parameter(Mandatory)]
        [int]$EventCap
    )

    if (($UseAllCourses -and $SelectedCourseShortnames.Count -gt 0) -or
        (-not $UseAllCourses -and $SelectedCourseShortnames.Count -eq 0)) {
        throw 'Deploy requires exactly one scheduler scope: -CourseShortname or -AllCourses.'
    }
    if ($EventCap -lt 1 -or $EventCap -gt 100) {
        throw 'Deploy requires -MaxNewEventsPerCycle from 1 through 100.'
    }
    if ($SelectedCourseShortnames.Count -gt 64) {
        throw 'At most 64 course shortnames are allowed.'
    }
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
    $totalUtf8Bytes = 0
    foreach ($shortname in $SelectedCourseShortnames) {
        if ($null -eq $shortname) {
            throw 'Course shortnames must be non-empty and free of ASCII controls.'
        }
        try {
            $shortnameBytes = $strictUtf8.GetByteCount($shortname)
        }
        catch [ArgumentException] {
            throw 'Course shortnames must be valid UTF-8 text.'
        }
        if ($shortnameBytes -eq 0 -or $shortnameBytes -gt 255 -or
            $shortname -match '[\x00-\x1F\x7F]') {
            throw 'Course shortnames must be non-empty and free of ASCII controls.'
        }
        if (-not $seen.Add($shortname)) {
            throw 'Course shortnames must be exactly unique.'
        }
        $totalUtf8Bytes += $shortnameBytes
    }
    if ($totalUtf8Bytes -gt 2048) {
        throw 'Course shortnames exceed the 2048 UTF-8 byte limit.'
    }
    if ($UseAllCourses) {
        $json = [ordered]@{ allCourses = $true; maxNewEventsPerCycle = $EventCap } |
            ConvertTo-Json -Compress
    }
    else {
        $json = [ordered]@{ courseShortnames = @($SelectedCourseShortnames); maxNewEventsPerCycle = $EventCap } |
            ConvertTo-Json -Compress
    }
    if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 16384) {
        throw 'Scheduler configuration exceeds the 16 KiB limit.'
    }
    return $json
}

function New-SchedulerConfigInstallCommand {
    param(
        [Parameter(Mandatory)]
        [ValidatePattern('^[A-Za-z0-9+/]+={0,2}$')]
        [string]$Base64Config
    )

    return @"
python3 - '$Base64Config' <<'PY'
import base64
import grp
import json
import os
import stat
import sys
import tempfile

encoded = sys.argv[1]
target = "/etc/moodle-autotask/scheduler.json"
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result
try:
    raw = base64.b64decode(encoded, validate=True)
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
    raise SystemExit("invalid scheduler configuration") from error
keys = set(value) if isinstance(value, dict) else set()
courses = value.get("courseShortnames") if isinstance(value, dict) else None
all_courses = value.get("allCourses") if isinstance(value, dict) else None
cap = value.get("maxNewEventsPerCycle") if isinstance(value, dict) else None
valid_courses = (
    keys == {"courseShortnames", "maxNewEventsPerCycle"}
    and isinstance(courses, list) and courses
    and len(courses) <= 64
    and all(isinstance(item, str) and item and len(item.encode("utf-8")) <= 255 and not any(ord(char) <= 0x1f or ord(char) == 0x7f for char in item) for item in courses)
    and sum(len(item.encode("utf-8")) for item in courses) <= 2048
    and len(set(courses)) == len(courses)
)
if not (valid_courses or (keys == {"allCourses", "maxNewEventsPerCycle"} and all_courses is True)):
    raise SystemExit("scheduler scope must be explicit and unambiguous")
if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 100:
    raise SystemExit("invalid scheduler event cap")
if len(raw) > 16 * 1024:
    raise SystemExit("scheduler configuration is too large")
try:
    old = os.lstat(target)
except FileNotFoundError:
    old = None
if old is not None and (not stat.S_ISREG(old.st_mode) or stat.S_ISLNK(old.st_mode)):
    raise SystemExit("scheduler config target is unsafe")
directory = os.path.dirname(target)
parent = os.lstat(directory)
if (
    not stat.S_ISDIR(parent.st_mode)
    or stat.S_ISLNK(parent.st_mode)
    or parent.st_uid != 0
    or stat.S_IMODE(parent.st_mode) & 0o022
):
    raise SystemExit("scheduler config directory is unsafe")
fd, temporary = tempfile.mkstemp(prefix=".scheduler.", dir=directory)
try:
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temporary, 0, grp.getgrnam("moodle-autotask").gr_gid)
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
"@
}

function New-SchedulerConfigGuardCommand {
    param(
        [ValidatePattern('^/[A-Za-z0-9._/-]+$')]
        [string]$ConfigPath = '/etc/moodle-autotask/scheduler.json'
    )

    return (@'
scheduler_was_active=false; telegram_was_active=false; worker_was_active=false; agent_was_active=false; scheduler_was_enabled=false; telegram_was_enabled=false; worker_was_enabled=false; agent_was_enabled=false; agent_unit_was_present=false; legacy_three_was_active=false; scheduler_config_backup=""; scheduler_config_candidate=""; scheduler_config_backup_complete=false; scheduler_config_prior_absent=false; health_marker=/var/lib/moodle-autotask/health-enabled; health_marker_was_present=false; health_marker_prior_absent=false; health_marker_backup=""; health_marker_candidate=""; health_marker_backup_complete=false
if systemctl is-active --quiet moodle-autotask-scheduler.service; then scheduler_was_active=true; fi
if systemctl is-active --quiet moodle-autotask-telegram.service; then telegram_was_active=true; fi
if systemctl is-active --quiet moodle-autotask-worker.service; then worker_was_active=true; fi
if systemctl cat moodle-autotask-agent.service >/dev/null 2>&1; then agent_unit_was_present=true; fi
if systemctl is-active --quiet moodle-autotask-agent.service; then agent_was_active=true; fi
if systemctl is-enabled --quiet moodle-autotask-scheduler.service; then scheduler_was_enabled=true; fi
if systemctl is-enabled --quiet moodle-autotask-telegram.service; then telegram_was_enabled=true; fi
if systemctl is-enabled --quiet moodle-autotask-worker.service; then worker_was_enabled=true; fi
if systemctl is-enabled --quiet moodle-autotask-agent.service; then agent_was_enabled=true; fi
if [ "$agent_unit_was_present" = false ] && [ "$scheduler_was_active" = true ] && [ "$telegram_was_active" = true ] && [ "$worker_was_active" = true ]; then legacy_three_was_active=true; fi
restore_scheduler_config() { result=$?; trap - EXIT; systemctl stop moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service || true; if [ -n "$scheduler_config_candidate" ]; then rm -f "$scheduler_config_candidate" || true; fi; if [ -n "$health_marker_candidate" ]; then rm -f "$health_marker_candidate" || true; fi; if [ "$scheduler_config_backup_complete" = true ]; then mv -f "$scheduler_config_backup" __CONFIG_PATH__ || true; elif [ "$scheduler_config_prior_absent" = true ]; then rm -f __CONFIG_PATH__ || true; fi; for service in scheduler telegram worker agent; do eval "enabled=\$${service}_was_enabled"; if [ "$enabled" = true ]; then systemctl enable "moodle-autotask-$service.service" || true; else systemctl disable "moodle-autotask-$service.service" || true; fi; done; restore_controller_install || true; if [ "$health_marker_was_present" = true ] && [ "$health_marker_backup_complete" = true ]; then mv -f "$health_marker_backup" "$health_marker" || true; elif [ "$health_marker_prior_absent" = true ]; then rm -f "$health_marker" || true; fi; if [ "$scheduler_was_active" = true ]; then systemctl start moodle-autotask-scheduler.service || true; fi; if [ "$telegram_was_active" = true ]; then systemctl start moodle-autotask-telegram.service || true; fi; if [ "$worker_was_active" = true ]; then systemctl start moodle-autotask-worker.service || true; fi; if [ "$agent_was_active" = true ]; then systemctl start moodle-autotask-agent.service || true; fi; cleanup_controller_install || true; restore_release_set || true; exit "$result"; }; trap restore_scheduler_config EXIT
if [ -e "$health_marker" ] || [ -L "$health_marker" ]; then test -f "$health_marker" && test ! -L "$health_marker" && test ! -s "$health_marker" && test "$(stat -c '%u:%a' "$health_marker")" = 0:600 || exit 1; health_marker_was_present=true; health_marker_candidate=$(mktemp /var/lib/moodle-autotask/.health-marker.previous.XXXXXX); cp -p "$health_marker" "$health_marker_candidate"; health_marker_backup="$health_marker_candidate"; health_marker_candidate=""; health_marker_backup_complete=true; else health_marker_prior_absent=true; fi
if [ -e __CONFIG_PATH__ ] || [ -L __CONFIG_PATH__ ]; then test -f __CONFIG_PATH__ && test ! -L __CONFIG_PATH__ || exit 1; scheduler_config_candidate=$(mktemp "$(dirname __CONFIG_PATH__)/.scheduler.candidate.XXXXXX"); cp -p __CONFIG_PATH__ "$scheduler_config_candidate"; scheduler_config_backup="$scheduler_config_candidate"; scheduler_config_candidate=""; scheduler_config_backup_complete=true; else scheduler_config_prior_absent=true; fi
'@).Replace('__CONFIG_PATH__', $ConfigPath)
}

function New-ControllerInstallGuardCommand {
    return @'
if ! declare -F restore_release_set >/dev/null; then restore_release_set() { :; }; fi
controller_guard_dir=""; controller_guard_complete=false; controller_current=""; controller_current_present=false; health_timer_unit_was_present=false; health_timer_was_enabled=false; health_timer_was_active=false
controller_paths=(/usr/local/sbin/moodle-autotask-refresh-config /usr/local/sbin/moodle-autotask-install-codex /usr/local/sbin/moodle-autotask-health-publish /usr/local/sbin/moodle-autotask-health-prepare /etc/codex/requirements.toml /etc/systemd/system/moodle-autotask-codex-login.service /etc/systemd/system/moodle-autotask-agent.service /etc/systemd/system/moodle-autotask-scheduler.service /etc/systemd/system/moodle-autotask-worker.service /etc/systemd/system/moodle-autotask-telegram.service /etc/systemd/system/moodle-autotask-health.service /etc/systemd/system/moodle-autotask-health.timer)
controller_modes=(750 750 750 750 644 644 644 644 644 644 644 644)
if [ -e /opt/moodle-autotask/current ] || [ -L /opt/moodle-autotask/current ]; then test -L /opt/moodle-autotask/current; controller_current="$(readlink /opt/moodle-autotask/current)"; [[ "$controller_current" =~ ^/opt/moodle-autotask/releases/[0-9a-f]{64}$ ]] && test -d "$controller_current" && test ! -L "$controller_current" || exit 1; controller_current_present=true; fi
if systemctl cat moodle-autotask-health.timer >/dev/null 2>&1; then health_timer_unit_was_present=true; fi
if systemctl is-enabled --quiet moodle-autotask-health.timer; then health_timer_was_enabled=true; fi
if systemctl is-active --quiet moodle-autotask-health.timer; then health_timer_was_active=true; fi
controller_guard_dir="$(mktemp -d /var/lib/moodle-autotask/.controller-install.previous.XXXXXX)"; chmod 0700 "$controller_guard_dir"; chown root:root "$controller_guard_dir"
cleanup_controller_guard_candidate() { test -d "$controller_guard_dir" && test ! -L "$controller_guard_dir" && test "$(stat -c '%u:%a' "$controller_guard_dir")" = 0:700 || return; for index in "${!controller_paths[@]}"; do rm -f "$controller_guard_dir/$index" "$controller_guard_dir/$index.present"; done; rmdir "$controller_guard_dir" || true; }; trap 'cleanup_controller_guard_candidate; restore_release_set' EXIT
for index in "${!controller_paths[@]}"; do path="${controller_paths[$index]}"; mode="${controller_modes[$index]}"; if [ -e "$path" ] || [ -L "$path" ]; then test -f "$path" && test ! -L "$path" && test "$(stat -c '%u:%a:%h' "$path")" = "0:$mode:1" || exit 1; cp -p "$path" "$controller_guard_dir/$index"; cmp -s "$path" "$controller_guard_dir/$index" && test "$(stat -c '%u:%a' "$controller_guard_dir/$index")" = "0:$mode" || exit 1; printf 1 >"$controller_guard_dir/$index.present"; else printf 0 >"$controller_guard_dir/$index.present"; fi; done
controller_guard_complete=true
trap - EXIT
restore_controller_install() { if [ "$controller_guard_complete" = true ]; then if [ "$health_timer_unit_was_present" = false ]; then systemctl stop moodle-autotask-health.timer 2>/dev/null || true; systemctl disable moodle-autotask-health.timer 2>/dev/null || true; fi; for index in "${!controller_paths[@]}"; do path="${controller_paths[$index]}"; present="$(cat "$controller_guard_dir/$index.present")"; if [ "$present" = 1 ]; then if [ -e "$path" ] || [ -L "$path" ]; then test -f "$path" && test ! -L "$path" || return 1; fi; temporary="$(mktemp "$(dirname "$path")/.restore.XXXXXX")"; cp -p "$controller_guard_dir/$index" "$temporary"; mv -f "$temporary" "$path"; else if [ -e "$path" ] || [ -L "$path" ]; then test -f "$path" && test ! -L "$path" || return 1; rm -f "$path"; fi; fi; done; if [ "$controller_current_present" = true ]; then ln -sfn "$controller_current" /opt/moodle-autotask/current.restore; mv -Tf /opt/moodle-autotask/current.restore /opt/moodle-autotask/current; else rm -f /opt/moodle-autotask/current; fi; systemctl daemon-reload || return 1; if [ "$health_timer_unit_was_present" = true ]; then if [ "$health_timer_was_enabled" = true ]; then systemctl enable moodle-autotask-health.timer || return 1; else systemctl disable moodle-autotask-health.timer || return 1; fi; if [ "$health_timer_was_active" = true ]; then systemctl start moodle-autotask-health.timer || return 1; else systemctl stop moodle-autotask-health.timer || return 1; fi; fi; fi; }
cleanup_controller_install() { cleanup_controller_guard_candidate; }
'@
}

function New-ReleaseGuardCommand {
    param(
        [ValidatePattern('^/[A-Za-z0-9._/-]+$')]
        [string]$ReleaseRoot
    )

    return (@'
release_root=__RELEASE_ROOT__; release_parent="$(dirname "$release_root")"; release_was_present=false; release_parent_was_present=false; release_current=""; release_current_present=false; release_set_before=""
release_set() { find "$release_parent" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort; }
restore_release_set() { if [ "$release_was_present" = false ] && { [ -e "$release_root" ] || [ -L "$release_root" ]; }; then test -d "$release_root" && test ! -L "$release_root" && test "$(stat -c '%u:%g:%a' "$release_root")" = 0:0:755 || return 1; rm -rf -- "$release_root"; fi; if [ "$release_parent_was_present" = false ]; then rmdir "$release_parent" 2>/dev/null || true; fi; if [ "$release_current_present" = true ]; then ln -sfn "$release_current" /opt/moodle-autotask/current.release-restore; mv -Tf /opt/moodle-autotask/current.release-restore /opt/moodle-autotask/current; else rm -f /opt/moodle-autotask/current; fi; if [ -d "$release_parent" ]; then test "$(release_set)" = "$release_set_before"; else test -z "$release_set_before"; fi; }
restore_release_on_exit() { result=$?; trap - EXIT; restore_release_set || true; exit "$result"; }
if [ -e "$release_parent" ] || [ -L "$release_parent" ]; then test -d "$release_parent" && test ! -L "$release_parent" && test "$(stat -c '%u:%g:%a' "$release_parent")" = 0:0:755 || exit 1; release_parent_was_present=true; release_set_before="$(release_set)"; while IFS= read -r release_name; do [ -z "$release_name" ] && continue; [[ "$release_name" =~ ^[0-9a-f]{64}$ ]] || exit 1; release_path="$release_parent/$release_name"; test -d "$release_path" && test ! -L "$release_path" && test "$(stat -c '%u:%g:%a' "$release_path")" = 0:0:755 || exit 1; done <<<"$release_set_before"; fi
if [ -e /opt/moodle-autotask/current ] || [ -L /opt/moodle-autotask/current ]; then test -L /opt/moodle-autotask/current; release_current="$(readlink /opt/moodle-autotask/current)"; [[ "$release_current" =~ ^/opt/moodle-autotask/releases/[0-9a-f]{64}$ ]] && test -d "$release_current" && test ! -L "$release_current" || exit 1; release_current_present=true; fi
if [ -e "$release_root" ] || [ -L "$release_root" ]; then test -d "$release_root" && test ! -L "$release_root" && test "$(stat -c '%u:%g:%a' "$release_root")" = 0:0:755 || exit 1; test -d "$release_root/venv" && test ! -L "$release_root/venv" && test "$(stat -c '%u:%g:%a' "$release_root/venv")" = 0:0:755 || exit 1; test -d "$release_root/venv/bin" && test ! -L "$release_root/venv/bin" && test "$(stat -c '%u:%g:%a' "$release_root/venv/bin")" = 0:0:755 || exit 1; for command in moodle-autotask-scheduler moodle-autotask-telegram moodle-autotask-worker moodle-autotask-agent; do test -f "$release_root/venv/bin/$command" && test ! -L "$release_root/venv/bin/$command" && test -x "$release_root/venv/bin/$command" || exit 1; done; release_was_present=true; fi
trap restore_release_on_exit EXIT
'@).Replace('__RELEASE_ROOT__', $ReleaseRoot)
}

function New-ActivationGuardCommand {
    return @'
activation_marker=/var/lib/moodle-autotask/health-enabled; activation_marker_present=false; activation_marker_absent=false; activation_marker_backup=""; activation_marker_candidate=""; activation_marker_complete=false; activation_services=(scheduler telegram worker agent)
for service in "${activation_services[@]}"; do eval "activation_${service}_active=false"; eval "activation_${service}_enabled=false"; systemctl is-active --quiet "moodle-autotask-$service.service" && eval "activation_${service}_active=true"; systemctl is-enabled --quiet "moodle-autotask-$service.service" && eval "activation_${service}_enabled=true"; done
activation_timer_active=false; activation_timer_enabled=false; systemctl is-active --quiet moodle-autotask-health.timer && activation_timer_active=true; systemctl is-enabled --quiet moodle-autotask-health.timer && activation_timer_enabled=true
restore_activation() { result=$?; trap - EXIT; systemctl stop moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service || true; for service in "${activation_services[@]}"; do eval "enabled=\$activation_${service}_enabled"; eval "active=\$activation_${service}_active"; if [ "$enabled" = true ]; then systemctl enable "moodle-autotask-$service.service" || true; else systemctl disable "moodle-autotask-$service.service" || true; fi; [ "$active" = true ] && systemctl start "moodle-autotask-$service.service" || true; done; if [ "$activation_marker_present" = true ] && [ "$activation_marker_complete" = true ]; then mv -f "$activation_marker_backup" "$activation_marker" || true; elif [ "$activation_marker_absent" = true ]; then rm -f "$activation_marker" || true; fi; if [ "$activation_timer_enabled" = true ]; then systemctl enable moodle-autotask-health.timer || true; else systemctl disable moodle-autotask-health.timer || true; fi; if [ "$activation_timer_active" = true ]; then systemctl start moodle-autotask-health.timer || true; else systemctl stop moodle-autotask-health.timer || true; fi; rm -f "$activation_marker_candidate" "$activation_marker_backup" || true; exit "$result"; }; trap restore_activation EXIT
if [ -e "$activation_marker" ] || [ -L "$activation_marker" ]; then test -f "$activation_marker" && test ! -L "$activation_marker" && test ! -s "$activation_marker" && test "$(stat -c '%u:%a' "$activation_marker")" = 0:600 || exit 1; activation_marker_present=true; activation_marker_candidate=$(mktemp /var/lib/moodle-autotask/.activation-marker.XXXXXX); cp -p "$activation_marker" "$activation_marker_candidate"; activation_marker_backup="$activation_marker_candidate"; activation_marker_candidate=""; activation_marker_complete=true; else activation_marker_absent=true; fi
'@
}

if ($Action -eq 'Deploy') {
    $schedulerConfigJson = New-SchedulerConfigJson -SelectedCourseShortnames $CourseShortname `
        -UseAllCourses $AllCourses.IsPresent -EventCap $MaxNewEventsPerCycle
    $schedulerConfigBase64 = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($schedulerConfigJson)
    )
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = Join-Path $repoRoot '.runtime\aws-deploy'
$artifactBucket = "moodle-autotask-artifacts-$AccountId-$Region"

function Resolve-AwsCli {
    $command = Get-Command aws -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $perUserPath = Join-Path $env:LOCALAPPDATA 'Programs\Amazon\AWSCLIV2\aws.exe'
    if (Test-Path -LiteralPath $perUserPath -PathType Leaf) {
        return $perUserPath
    }

    throw 'AWS CLI v2 was not found.'
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $Executable @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw "Native command failed: $Executable. $detail"
    }
    return @($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
}

function Invoke-Aws {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    return Invoke-Native -Executable $script:awsCli -Arguments (
        $Arguments + @('--region', $Region, '--profile', $Profile)
    )
}

function Initialize-RuntimeRoot {
    if (Test-Path -LiteralPath $runtimeRoot) {
        $runtimeItem = Get-Item -Force -LiteralPath $runtimeRoot
        if ($runtimeItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'The deployment runtime directory cannot be a reparse point.'
        }
        return
    }

    New-Item -ItemType Directory -Path $runtimeRoot | Out-Null
}

function Resolve-ControllerInstanceId {
    if ($InstanceId) {
        return $InstanceId
    }

    $response = Invoke-Aws -Arguments @(
        'ec2', 'describe-instances',
        '--filters',
        'Name=tag:Project,Values=moodle-autotask',
        'Name=tag:Role,Values=controller',
        'Name=instance-state-name,Values=running',
        '--query', 'Reservations[].Instances[].InstanceId',
        '--output', 'json'
    )
    $ids = @($response | ConvertFrom-Json)
    if ($ids.Count -ne 1 -or $ids[0] -notmatch '^i-[0-9a-f]{8,17}$') {
        throw 'Expected exactly one running Moodle Autotask controller instance.'
    }
    return [string]$ids[0]
}

function Wait-SsmCommand {
    param(
        [Parameter(Mandatory)]
        [string]$CommandId,

        [Parameter(Mandatory)]
        [string]$TargetInstanceId
    )

    $terminalStatuses = @('Success', 'Cancelled', 'TimedOut', 'Failed', 'Cancelling')
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        Start-Sleep -Seconds 2
        try {
            $response = Invoke-Aws -Arguments @(
                'ssm', 'get-command-invocation',
                '--command-id', $CommandId,
                '--instance-id', $TargetInstanceId,
                '--output', 'json'
            )
        }
        catch {
            if ($attempt -lt 5) {
                continue
            }
            throw
        }

        $invocation = $response | ConvertFrom-Json
        if ($terminalStatuses -notcontains $invocation.Status) {
            continue
        }
        if ($invocation.Status -ne 'Success' -or $invocation.ResponseCode -ne 0) {
            throw "SSM command failed with status $($invocation.Status)."
        }
        if ($invocation.StandardOutputContent) {
            $invocation.StandardOutputContent.TrimEnd() | Write-Output
        }
        return
    }

    throw 'SSM command did not finish within 180 seconds.'
}

function Send-ControllerCommand {
    param(
        [Parameter(Mandatory)]
        [string]$TargetInstanceId,

        [Parameter(Mandatory)]
        [string[]]$Commands,

        [Parameter(Mandatory)]
        [string]$Comment
    )

    # AWS-RunShellScript starts its generated script with /bin/sh. Re-exec the
    # complete script under Bash before any command payload can use Bash syntax.
    $bashTransport = 'if [ -z "${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi'
    $normalizedCommands = @(
        foreach ($command in $Commands) {
            if ($command.IndexOf([char]0) -ge 0) {
                throw 'SSM commands must not contain NUL bytes.'
            }
            $command.Replace("`r`n", "`n").Replace("`r", "`n")
        }
    )
    $parameters = @{ commands = @($bashTransport) + $normalizedCommands } | ConvertTo-Json -Compress
    $parametersPath = Join-Path $runtimeRoot "ssm-$([Guid]::NewGuid().ToString('N')).json"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($parametersPath, $parameters, $utf8NoBom)
    try {
        $parametersUri = 'file://' + $parametersPath.Replace('\', '/')
        $commandId = (
            Invoke-Aws -Arguments @(
                'ssm', 'send-command',
                '--instance-ids', $TargetInstanceId,
                '--document-name', 'AWS-RunShellScript',
                '--comment', $Comment,
                '--parameters', $parametersUri,
                '--timeout-seconds', '180',
                '--query', 'Command.CommandId',
                '--output', 'text'
            )
        ).Trim()
    }
    finally {
        Remove-Item -Force -LiteralPath $parametersPath -ErrorAction SilentlyContinue
    }
    if ($commandId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
        throw 'AWS returned an invalid SSM command ID.'
    }
    Wait-SsmCommand -CommandId $commandId -TargetInstanceId $TargetInstanceId
}

$script:awsCli = Resolve-AwsCli
$identityAccountId = (
    Invoke-Aws -Arguments @('sts', 'get-caller-identity', '--query', 'Account', '--output', 'text')
).Trim()
if ($identityAccountId -ne $AccountId) {
    throw 'The authenticated AWS account does not match AccountId.'
}

Initialize-RuntimeRoot
$controllerInstanceId = Resolve-ControllerInstanceId

if ($Action -eq 'Status') {
    Send-ControllerCommand -TargetInstanceId $controllerInstanceId -Comment 'Inspect application deployment' -Commands @(
        'set -eu',
        'printf "scheduler-enabled="; systemctl is-enabled moodle-autotask-scheduler.service 2>/dev/null || true',
        'printf "scheduler-active="; systemctl is-active moodle-autotask-scheduler.service 2>/dev/null || true',
        'printf "telegram-enabled="; systemctl is-enabled moodle-autotask-telegram.service 2>/dev/null || true',
        'printf "telegram-active="; systemctl is-active moodle-autotask-telegram.service 2>/dev/null || true',
        'printf "worker-enabled="; systemctl is-enabled moodle-autotask-worker.service 2>/dev/null || true',
        'printf "worker-active="; systemctl is-active moodle-autotask-worker.service 2>/dev/null || true',
        'printf "agent-enabled="; systemctl is-enabled moodle-autotask-agent.service 2>/dev/null || true',
        'printf "agent-active="; systemctl is-active moodle-autotask-agent.service 2>/dev/null || true',
        'printf "current-release="; readlink /opt/moodle-autotask/current 2>/dev/null || true',
        'printf "codex-version="; /usr/local/bin/moodle-autotask-codex --version 2>/dev/null || echo unavailable',
        'if runuser -u moodle-agent -- env HOME=/var/lib/moodle-agent CODEX_HOME=/var/lib/moodle-agent/.codex /usr/local/bin/moodle-autotask-codex login status >/dev/null 2>&1; then echo codex-auth=authenticated; else echo codex-auth=unauthenticated; fi'
    )
    exit 0
}

if ($Action -eq 'CodexLogin') {
    Send-ControllerCommand -TargetInstanceId $controllerInstanceId -Comment 'Start one-time Codex device login' -Commands @(
        'set -eu',
        'test -x /usr/local/bin/moodle-autotask-codex',
        'test -f /etc/systemd/system/moodle-autotask-codex-login.service',
        'if runuser -u moodle-agent -- env HOME=/var/lib/moodle-agent CODEX_HOME=/var/lib/moodle-agent/.codex /usr/local/bin/moodle-autotask-codex login status >/dev/null 2>&1; then echo codex-auth=authenticated; exit 0; fi',
        'systemctl stop moodle-autotask-codex-login.service 2>/dev/null || true',
        'systemctl reset-failed moodle-autotask-codex-login.service 2>/dev/null || true',
        'systemctl start --no-block moodle-autotask-codex-login.service',
        'sleep 3',
        'invocation_id="$(systemctl show moodle-autotask-codex-login.service --property=InvocationID --value)"',
        'test "$invocation_id" != ""',
        'journalctl "_SYSTEMD_INVOCATION_ID=$invocation_id" --no-pager --output=cat'
    )
    exit 0
}

if ($Action -eq 'CodexSmoke') {
    Send-ControllerCommand -TargetInstanceId $controllerInstanceId -Comment 'Verify isolated Codex authentication' -Commands @(
        'set -eu',
        'test "$(stat -c ''%U:%G:%a'' /var/lib/moodle-agent/.codex/auth.json)" = "moodle-agent:moodle-agent:600"',
        'test "$(stat -c ''%U:%G:%a'' /etc/codex/requirements.toml)" = "root:root:644"',
        '! id -nG moodle-agent | tr '' '' ''\n'' | grep -Fxq moodle-autotask',
        'if runuser -u moodle-agent -- test -r /etc/moodle-autotask/moodle-token.json; then echo secret-boundary-failed; exit 1; fi',
        'install -d -o moodle-agent -g moodle-agent -m 0700 /var/lib/moodle-agent/smoke',
        'runuser -u moodle-agent -- env HOME=/var/lib/moodle-agent CODEX_HOME=/var/lib/moodle-agent/.codex /usr/local/bin/moodle-autotask-codex sandbox --permission-profile moodle-autotask --include-managed-config -C /var/lib/moodle-agent/smoke -- sh -c ''test ! -r /var/lib/moodle-agent/.codex/auth.json && test ! -r /etc/moodle-autotask/moodle-token.json && test -w /var/lib/moodle-agent/smoke''',
        'result="$(timeout 120s runuser -u moodle-agent -- env HOME=/var/lib/moodle-agent CODEX_HOME=/var/lib/moodle-agent/.codex /usr/local/bin/moodle-autotask-codex exec --ephemeral --skip-git-repo-check --color never -C /var/lib/moodle-agent/smoke ''Reply with exactly: CODEX_SMOKE_OK'')"',
        'test "$result" = "CODEX_SMOKE_OK"',
        'echo codex-smoke=ok',
        'echo codex-sandbox=isolated',
        'echo auth-permissions=private',
        'echo application-secrets=unreadable'
    )
    exit 0
}

if ($Action -eq 'Activate') {
    $activationGuardCommand = New-ActivationGuardCommand
    Send-ControllerCommand -TargetInstanceId $controllerInstanceId -Comment 'Activate application services' -Commands @(
        'set -eu',
        $activationGuardCommand,
        'test -x /opt/moodle-autotask/current/venv/bin/moodle-autotask-scheduler',
        'test -x /opt/moodle-autotask/current/venv/bin/moodle-autotask-telegram',
        'test -x /opt/moodle-autotask/current/venv/bin/moodle-autotask-worker',
        'test -x /opt/moodle-autotask/current/venv/bin/moodle-autotask-agent',
        'test -x /usr/local/sbin/moodle-autotask-refresh-config',
        'test -x /usr/local/sbin/moodle-autotask-health-publish',
        'test -x /usr/local/sbin/moodle-autotask-health-prepare',
        '/usr/local/sbin/moodle-autotask-refresh-config',
        'systemctl daemon-reload',
        '/usr/local/sbin/moodle-autotask-health-prepare',
        'for pulse in scheduler telegram worker agent; do path="/run/moodle-autotask-health/$pulse"; test -f "$path" && test ! -L "$path" && test ! -s "$path" && test "$(stat -c %u:%a "$path")" = 0:620; touch -d @0 -- "$path"; done; activation_started=$(date +%s)',
        'if ! systemctl enable moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service; then systemctl disable moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service || true; exit 1; fi',
        'if ! systemctl start moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service; then systemctl stop moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service || true; systemctl disable moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service || true; exit 1; fi',
        'systemctl is-active --quiet moodle-autotask-scheduler.service',
        'systemctl is-active --quiet moodle-autotask-telegram.service',
        'systemctl is-active --quiet moodle-autotask-worker.service',
        'systemctl is-active --quiet moodle-autotask-agent.service',
        'deadline=$(( $(date +%s) + 240 )); while [ "$(date +%s)" -lt "$deadline" ]; do ready=true; for pulse in scheduler telegram worker agent; do test -f "/run/moodle-autotask-health/$pulse" && test ! -L "/run/moodle-autotask-health/$pulse" && test ! -s "/run/moodle-autotask-health/$pulse" && test "$(stat -c %Y "/run/moodle-autotask-health/$pulse")" -ge "$activation_started" || ready=false; systemctl is-enabled --quiet "moodle-autotask-$pulse.service" && test "$(systemctl show "moodle-autotask-$pulse.service" -p ActiveState --value)" = active && test "$(systemctl show "moodle-autotask-$pulse.service" -p SubState --value)" = running || ready=false; done; $ready && break; sleep 5; done; $ready',
        'account=$(getent passwd moodle-autotask); group=$(getent group moodle-autotask); test -n "$account" && test -n "$group" && test "$(printf ''%s\n'' "$account" | wc -l)" = 1 && test "$(printf ''%s\n'' "$group" | wc -l)" = 1; state_uid=$(printf ''%s'' "$account" | awk -F: ''NF==7 && $3 ~ /^[0-9]+$/ { print $3 }''); state_gid=$(printf ''%s'' "$group" | awk -F: ''NF>=3 && $3 ~ /^[0-9]+$/ { print $3 }''); test -n "$state_uid" && test -n "$state_gid" && test -d /var/lib/moodle-autotask && test ! -L /var/lib/moodle-autotask && test "$(stat -c ''%u:%g:%a'' /var/lib/moodle-autotask)" = "$state_uid:$state_gid:750"; activation_marker_candidate=$(mktemp /var/lib/moodle-autotask/.health-enabled.XXXXXX); chmod 0600 "$activation_marker_candidate"; chown root:root "$activation_marker_candidate"; mv -f "$activation_marker_candidate" /var/lib/moodle-autotask/health-enabled; activation_marker_candidate=""; test -f /var/lib/moodle-autotask/health-enabled && test ! -L /var/lib/moodle-autotask/health-enabled && test ! -s /var/lib/moodle-autotask/health-enabled && test "$(stat -c ''%u:%g:%a:%h'' /var/lib/moodle-autotask/health-enabled)" = 0:0:600:1',
        'systemctl enable --now moodle-autotask-health.timer',
        'trap - EXIT; rm -f "$activation_marker_candidate" "$activation_marker_backup"',
        "echo 'activated-environment=$Environment'"
    )
    exit 0
}

if ($Action -eq 'Deactivate') {
    Send-ControllerCommand -TargetInstanceId $controllerInstanceId -Comment 'Deactivate application services' -Commands @(
        'set -eu',
        'systemctl stop moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service',
        'systemctl disable moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service',
        '! systemctl is-active --quiet moodle-autotask-scheduler.service; ! systemctl is-active --quiet moodle-autotask-telegram.service; ! systemctl is-active --quiet moodle-autotask-worker.service; ! systemctl is-active --quiet moodle-autotask-agent.service',
        '! systemctl is-enabled --quiet moodle-autotask-scheduler.service; ! systemctl is-enabled --quiet moodle-autotask-telegram.service; ! systemctl is-enabled --quiet moodle-autotask-worker.service; ! systemctl is-enabled --quiet moodle-autotask-agent.service',
        'if [ -e /var/lib/moodle-autotask/health-enabled ] || [ -L /var/lib/moodle-autotask/health-enabled ]; then test -f /var/lib/moodle-autotask/health-enabled && test ! -L /var/lib/moodle-autotask/health-enabled && test ! -s /var/lib/moodle-autotask/health-enabled; rm -f /var/lib/moodle-autotask/health-enabled; fi',
        'echo services-deactivated'
    )
    exit 0
}

$gitStatus = Invoke-Native -Executable 'git' -Arguments @(
    '-C', $repoRoot, 'status', '--porcelain', '--untracked-files=all'
)
if ($gitStatus) {
    throw 'Deploy requires a clean Git worktree.'
}

$commitSha = (
    Invoke-Native -Executable 'git' -Arguments @('-C', $repoRoot, 'rev-parse', 'HEAD')
).Trim()
if ($commitSha -notmatch '^[0-9a-f]{40}$') {
    throw 'Git returned an invalid commit SHA.'
}

$namePrefix = "moodle-autotask-$Environment"
$labSubnetResponse = Invoke-Aws -Arguments @(
    'ec2', 'describe-subnets', '--filters',
    "Name=tag:Name,Values=$namePrefix-lab", 'Name=state,Values=available',
    '--query', 'Subnets[].SubnetId', '--output', 'json'
)
$labSubnetIds = @($labSubnetResponse | ConvertFrom-Json)
if ($labSubnetIds.Count -ne 1 -or $labSubnetIds[0] -notmatch '^subnet-[0-9a-f]{8,17}$') {
    throw 'Expected exactly one Moodle Autotask lab subnet.'
}
$labSecurityGroupResponse = Invoke-Aws -Arguments @(
    'ec2', 'describe-security-groups', '--filters',
    "Name=tag:Name,Values=$namePrefix-lab", '--query', 'SecurityGroups[].GroupId',
    '--output', 'json'
)
$labSecurityGroupIds = @($labSecurityGroupResponse | ConvertFrom-Json)
if (
    $labSecurityGroupIds.Count -ne 1 -or
    $labSecurityGroupIds[0] -notmatch '^sg-[0-9a-f]{8,17}$'
) {
    throw 'Expected exactly one Moodle Autotask lab security group.'
}
$labRoleArn = (
    Invoke-Aws -Arguments @(
        'iam', 'get-role', '--role-name', "$namePrefix-lab-provisioner",
        '--query', 'Role.Arn', '--output', 'text'
    )
).Trim()
if ($labRoleArn -notmatch '^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$') {
    throw 'AWS returned an invalid lab provisioner role ARN.'
}
$imageImporterRoleArn = (
    Invoke-Aws -Arguments @(
        'iam', 'get-role', '--role-name', "$namePrefix-image-importer",
        '--query', 'Role.Arn', '--output', 'text'
    )
).Trim()
if ($imageImporterRoleArn -notmatch '^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$') {
    throw 'AWS returned an invalid image importer role ARN.'
}
$vmImportRoleName = "$namePrefix-vmimport"
$resolvedVmImportRoleName = (
    Invoke-Aws -Arguments @(
        'iam', 'get-role', '--role-name', $vmImportRoleName,
        '--query', 'Role.RoleName', '--output', 'text'
    )
).Trim()
if ($resolvedVmImportRoleName -ne $vmImportRoleName) {
    throw 'AWS returned an unexpected VM Import service role.'
}
$labPolicyResponse = Invoke-Aws -Arguments @(
    'iam', 'get-role-policy', '--role-name', "$namePrefix-lab-provisioner",
    '--policy-name', "$namePrefix-lab-provisioner", '--query', 'PolicyDocument',
    '--output', 'json'
)
$labPolicy = $labPolicyResponse | ConvertFrom-Json
$approvedImageIds = @()
foreach ($statement in @($labPolicy.Statement)) {
    if ($statement.Sid -ne 'UseApprovedLaunchResources') {
        continue
    }
    foreach ($resource in @($statement.Resource)) {
        if ($resource -match ":ec2:${Region}::image/(ami-[0-9a-f]{8,17})$") {
            $approvedImageIds += $Matches[1]
        }
    }
}
if ($approvedImageIds.Count -ne 1) {
    throw 'Expected exactly one approved Windows AMI in the lab provisioner policy.'
}
$labImageId = [string]$approvedImageIds[0]
$labInstanceProfileName = "$namePrefix-lab"
$profileName = (
    Invoke-Aws -Arguments @(
        'iam', 'get-instance-profile', '--instance-profile-name', $labInstanceProfileName,
        '--query', 'InstanceProfile.InstanceProfileName', '--output', 'text'
    )
).Trim()
if ($profileName -ne $labInstanceProfileName) {
    throw 'AWS returned an unexpected lab instance profile.'
}
$labSubnetId = [string]$labSubnetIds[0]
$labSecurityGroupId = [string]$labSecurityGroupIds[0]

$wheelRoot = Join-Path $runtimeRoot $commitSha
if (-not (Test-Path -LiteralPath $wheelRoot)) {
    New-Item -ItemType Directory -Path $wheelRoot | Out-Null
}

Invoke-Native -Executable 'python' -Arguments @(
    '-m', 'pip', 'wheel', '--disable-pip-version-check', '--no-deps',
    '--wheel-dir', $wheelRoot, $repoRoot
) | Write-Output

$wheels = @(Get-ChildItem -File -LiteralPath $wheelRoot -Filter 'moddle_autotask-*.whl')
if ($wheels.Count -ne 1) {
    throw 'Expected exactly one Moodle Autotask wheel.'
}
$wheel = $wheels[0]
$wheelDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $wheel.FullName).Hash.ToLowerInvariant()
if ($wheelDigest -notmatch '^[0-9a-f]{64}$') {
    throw 'Wheel digest is invalid.'
}

$artifactKey = "controller/releases/$commitSha/$wheelDigest/$($wheel.Name)"
$artifactUri = "s3://$artifactBucket/$artifactKey"
Invoke-Aws -Arguments @('s3', 'cp', $wheel.FullName, $artifactUri, '--only-show-errors') | Out-Null

$releaseRoot = "/opt/moodle-autotask/releases/$wheelDigest"
$remoteWheel = "/tmp/$($wheel.Name)"
$schedulerConfigInstallCommand = New-SchedulerConfigInstallCommand -Base64Config $schedulerConfigBase64
$schedulerConfigGuardCommand = New-SchedulerConfigGuardCommand
$controllerInstallGuardCommand = New-ControllerInstallGuardCommand
$releaseGuardCommand = New-ReleaseGuardCommand -ReleaseRoot $releaseRoot
Send-ControllerCommand -TargetInstanceId $controllerInstanceId -Comment "Deploy $commitSha" -Commands @(
    'set -eu',
    "aws s3 cp '$artifactUri' '$remoteWheel' --only-show-errors",
    "echo '$wheelDigest  $remoteWheel' | sha256sum --check --strict",
    $releaseGuardCommand,
    "if [ `"`$release_was_present`" = false ]; then install -d -o root -g root -m 0755 '$releaseRoot'; fi",
    "if [ `"`$release_was_present`" = false ]; then python3 -m venv '$releaseRoot/venv'; fi",
    "if [ `"`$release_was_present`" = false ]; then '$releaseRoot/venv/bin/pip' install --disable-pip-version-check --no-deps '$remoteWheel'; fi",
    "'$releaseRoot/venv/bin/moodle-autotask-scheduler' --help >/dev/null",
    "'$releaseRoot/venv/bin/moodle-autotask-telegram' --help >/dev/null",
    "'$releaseRoot/venv/bin/moodle-autotask-worker' --help >/dev/null",
    "'$releaseRoot/venv/bin/moodle-autotask-agent' --help >/dev/null",
    $controllerInstallGuardCommand,
    $schedulerConfigGuardCommand,
    'if [ "$scheduler_was_active" = true ]; then systemctl stop moodle-autotask-scheduler.service; fi',
    'if [ "$telegram_was_active" = true ]; then systemctl stop moodle-autotask-telegram.service; fi',
    'if [ "$worker_was_active" = true ]; then systemctl stop moodle-autotask-worker.service; fi',
    'if [ "$agent_was_active" = true ]; then systemctl stop moodle-autotask-agent.service; fi',
    $schedulerConfigInstallCommand,
    "ln -sfn '$releaseRoot' /opt/moodle-autotask/current.next",
    'mv -Tf /opt/moodle-autotask/current.next /opt/moodle-autotask/current',
    "'/opt/moodle-autotask/current/venv/bin/moodle-autotask-controller' install --region '$Region' --environment '$Environment' --provisioner-role-arn '$labRoleArn' --subnet-id '$labSubnetId' --security-group-id '$labSecurityGroupId' --instance-profile-name '$labInstanceProfileName' --image-id '$labImageId' --artifact-bucket '$artifactBucket' --image-importer-role-arn '$imageImporterRoleArn' --vmimport-role-name '$vmImportRoleName' --instance-type '$LabInstanceType' --root-volume-size-gib '$LabRootVolumeSizeGiB'",
    '/usr/local/sbin/moodle-autotask-install-codex',
    'test -x /usr/local/bin/moodle-autotask-codex',
    'systemctl daemon-reload',
    '/usr/local/sbin/moodle-autotask-health-prepare',
    'if [ "$scheduler_was_active" = true ]; then systemctl start moodle-autotask-scheduler.service; fi',
    'if [ "$telegram_was_active" = true ]; then systemctl start moodle-autotask-telegram.service; fi',
    'if [ "$worker_was_active" = true ]; then systemctl start moodle-autotask-worker.service; fi',
    'if [ "$agent_was_active" = true ] || [ "$legacy_three_was_active" = true ]; then systemctl enable --now moodle-autotask-agent.service; fi',
    'if [ "$health_marker_was_present" = true ] || { [ "$health_marker_was_present" = false ] && [ "$scheduler_was_active" = true ] && [ "$telegram_was_active" = true ] && [ "$worker_was_active" = true ] && { [ "$agent_was_active" = true ] || [ "$legacy_three_was_active" = true ]; }; }; then /usr/local/sbin/moodle-autotask-health-prepare; for pulse in scheduler telegram worker agent; do path="/run/moodle-autotask-health/$pulse"; test -f "$path" && test ! -L "$path" && test ! -s "$path"; touch -d @0 -- "$path"; done; activation_started=$(date +%s); deadline=$((activation_started+240)); ready=false; while [ "$(date +%s)" -lt "$deadline" ]; do ready=true; for pulse in scheduler telegram worker agent; do systemctl is-enabled --quiet "moodle-autotask-$pulse.service" && test "$(systemctl show "moodle-autotask-$pulse.service" -p ActiveState --value)" = active && test "$(systemctl show "moodle-autotask-$pulse.service" -p SubState --value)" = running && test ! -L "/run/moodle-autotask-health/$pulse" && test "$(stat -c %Y "/run/moodle-autotask-health/$pulse")" -ge "$activation_started" || ready=false; done; $ready && break; sleep 5; done; $ready; temporary=$(mktemp /var/lib/moodle-autotask/.health-enabled.XXXXXX); chmod 0600 "$temporary"; chown root:root "$temporary"; mv -f "$temporary" /var/lib/moodle-autotask/health-enabled; systemctl enable --now moodle-autotask-health.timer; fi',
    'systemctl enable --now moodle-autotask-health.timer',
    "rm -f '$remoteWheel'",
    'trap - EXIT; cleanup_controller_install; if [ -n "$scheduler_config_candidate" ]; then rm -f "$scheduler_config_candidate"; fi; if [ -n "$scheduler_config_backup" ]; then rm -f "$scheduler_config_backup"; fi; if [ -n "$health_marker_candidate" ]; then rm -f "$health_marker_candidate"; fi; if [ -n "$health_marker_backup" ]; then rm -f "$health_marker_backup"; fi',
    "echo 'deployed-commit=$commitSha'",
    "echo 'deployed-sha256=$wheelDigest'"
)
