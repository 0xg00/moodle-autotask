[CmdletBinding()]
param(
    [ValidateSet('Run', 'Cleanup')]
    [string]$Action = 'Run',

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]{12}$')]
    [string]$AccountId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z]{2}-[a-z]+-[0-9]$')]
    [string]$Region,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
    [string]$Profile,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^i-[0-9a-f]{8,17}$')]
    [string]$ControllerInstanceId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9](?:[a-z0-9-]{6,38}[a-z0-9])$')]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [string]$MoodleTokenFile,

    [ValidateRange(300, 14400)]
    [int]$TimeoutSeconds = 3600,

    [ValidateRange(5, 60)]
    [int]$PollSeconds = 15,

    [string]$EvidencePath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:AWS_CLI_FILE_ENCODING = 'UTF-8'
$env:AWS_CLI_OUTPUT_ENCODING = 'UTF-8'
$env:AWS_PAGER = ''

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$RuntimeRoot = Join-Path $RepoRoot '.runtime'
$MoodleScript = Join-Path $RepoRoot 'scripts/moodle.ps1'
$script:AwsCli = $null
$script:ScopeApplied = $false
$script:ScopeRestoreFailed = $false
$script:Evidence = [ordered]@{
    kind = 'autotask-central-e2e-evidence-v1'
    runId = $RunId
    controllerInstanceId = $ControllerInstanceId
    region = $Region
    startedAt = (Get-Date).ToUniversalTime().ToString('o')
    status = 'running'
    phases = @()
}

function Fail {
    param([Parameter(Mandatory = $true)][string]$Message)
    throw $Message
}

function Get-OptionalPropertyValue {
    param([Parameter()]$Object, [Parameter(Mandatory = $true)][string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Add-Phase {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter()]$Data = $null)
    $entry = [ordered]@{ name = $Name; at = (Get-Date).ToUniversalTime().ToString('o') }
    if ($null -ne $Data) {
        $entry.data = $Data
    }
    $script:Evidence.phases += [PSCustomObject]$entry
}

function Assert-ContainedRuntimePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $runtime = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $target = [System.IO.Path]::GetFullPath($Path)
    if (-not $target.StartsWith($runtime + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        Fail 'Evidence and token files must be inside this repository runtime directory.'
    }
}

function Assert-NonReparsePath {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter()][switch]$AllowMissingLeaf)
    $full = [System.IO.Path]::GetFullPath($Path)
    $current = if ($AllowMissingLeaf -and -not (Test-Path -LiteralPath $full)) { Split-Path -Parent $full } else { $full }
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                Fail 'Refusing a reparse-point evidence or token path.'
            }
        }
        if ($current -eq [System.IO.Path]::GetPathRoot($current)) { break }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Write-Evidence {
    $script:Evidence.completedAt = (Get-Date).ToUniversalTime().ToString('o')
    Assert-ContainedRuntimePath -Path $EvidencePath
    Assert-NonReparsePath -Path $RuntimeRoot
    $parent = Split-Path -Parent $EvidencePath
    Assert-NonReparsePath -Path $parent -AllowMissingLeaf
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Assert-NonReparsePath -Path $parent
    Assert-NonReparsePath -Path $EvidencePath -AllowMissingLeaf
    if (Test-Path -LiteralPath $EvidencePath -PathType Container) { Fail 'Evidence target is not a file.' }
    $json = $script:Evidence | ConvertTo-Json -Depth 12
    $temporary = Join-Path $parent ('.central-e2e-' + $RunId + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $stream = New-Object System.IO.FileStream($temporary, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try {
            $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($json)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally { $stream.Dispose() }
        if (Test-Path -LiteralPath $EvidencePath) {
            Assert-NonReparsePath -Path $EvidencePath
            [System.IO.File]::Replace($temporary, $EvidencePath, $null)
        } else {
            [System.IO.File]::Move($temporary, $EvidencePath)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    }
}

function Get-AwsCli {
    if ($null -ne $script:AwsCli) { return $script:AwsCli }
    $command = Get-Command 'aws.exe' -ErrorAction SilentlyContinue
    if ($null -eq $command) { $command = Get-Command 'aws' -ErrorAction SilentlyContinue }
    if ($null -eq $command) { Fail 'AWS CLI v2 was not found.' }
    $script:AwsCli = $command.Source
    return $script:AwsCli
}

function Invoke-Aws {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $aws = Get-AwsCli
    $output = & $aws @('--profile', $Profile, '--region', $Region, '--no-cli-pager') @Arguments
    if ($LASTEXITCODE -ne 0) { Fail 'AWS CLI command failed.' }
    return @($output)
}

function ConvertFrom-CanonicalJson {
    param([Parameter(Mandatory = $true)][string]$Text, [Parameter(Mandatory = $true)][string]$Context)
    try { return $Text | ConvertFrom-Json } catch { Fail "$Context returned invalid JSON." }
}

function Test-LocalMoodleTokenFile {
    $canonicalToken = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'moodle-token.json'))
    $resolvedToken = [System.IO.Path]::GetFullPath($MoodleTokenFile)
    if (-not [string]::Equals($resolvedToken, $canonicalToken, [System.StringComparison]::OrdinalIgnoreCase)) {
        Fail 'Central E2E requires the canonical local .runtime/moodle-token.json file.'
    }
    Assert-ContainedRuntimePath -Path $resolvedToken
    Assert-NonReparsePath -Path $resolvedToken
    if (-not (Test-Path -LiteralPath $MoodleTokenFile -PathType Leaf)) { Fail 'Local Moodle token file is missing.' }
    try { $token = Get-Content -LiteralPath $MoodleTokenFile -Raw | ConvertFrom-Json } catch { Fail 'Local Moodle token file is invalid.' }
    $tokenValue = Get-OptionalPropertyValue -Object $token -Name 'token'
    $baseUrl = Get-OptionalPropertyValue -Object $token -Name 'baseUrl'
    if ($null -eq $tokenValue -or [string]::IsNullOrWhiteSpace([string]$tokenValue) -or
        $null -eq $baseUrl -or [string]::IsNullOrWhiteSpace([string]$baseUrl)) {
        Fail 'Local Moodle token file is incomplete.'
    }
    try { $uri = [Uri][string]$baseUrl } catch { Fail 'Local Moodle token URL is invalid.' }
    $uriHost = $uri.Host
    $localHost = $uriHost -eq 'localhost' -or $uriHost -eq '127.0.0.1' -or
        $uriHost -match '^10\.' -or $uriHost -match '^192\.168\.' -or
        $uriHost -match '^172\.(1[6-9]|2[0-9]|3[0-1])\.' -or
        $uriHost -match '^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.'
    if ($uri.Scheme -ne 'http' -or -not $localHost) {
        Fail 'Central E2E accepts only the local fictitious Moodle token file.'
    }
    $material = 'moodle-autotask-e2e-credential-v1' + [char]0 + [string]$baseUrl + [char]0 + [string]$tokenValue
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($material)
    $digest = [BitConverter]::ToString(([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    # The token is deliberately validated but never returned or logged.  The digest is
    # sent only to the remote preflight to bind this local fixture to deployed config.
    Add-Phase -Name 'local-token-file-validated' -Data ([ordered]@{ path = '.runtime/moodle-token.json' })
    return $digest
}

function Get-ControllerPreflightScript {
    param([Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$CredentialBinding)
    return @"
set -euo pipefail
for unit in moodle-autotask-scheduler.service moodle-autotask-telegram.service moodle-autotask-worker.service moodle-autotask-agent.service; do systemctl is-enabled --quiet "`$unit"; systemctl is-active --quiet "`$unit"; done
release=`$(readlink -f /opt/moodle-autotask/current); test -d "`$release" && test ! -L "`$release"
python3 - "`$release" '$CredentialBinding' <<'PY' | base64 -w0 | sed 's/^/AUTOTASK_E2E_JSON=/'
import grp,hashlib,json,os,pwd,stat,sys
def unique_pairs(items):
    value={}
    for key,item in items:
        if key in value: raise ValueError('duplicate key')
        value[key]=item
    return value
path='/etc/moodle-autotask/moodle-token.json'; descriptor=-1
try: expected_uid=pwd.getpwnam('moodle-autotask').pw_uid; expected_gid=grp.getgrnam('moodle-autotask').gr_gid
except KeyError: raise SystemExit('missing credential owner')
try:
    before=os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or (before.st_uid,before.st_gid)!=(expected_uid,expected_gid) or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1: raise ValueError('invalid credential inode')
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)); opened=os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or (opened.st_uid,opened.st_gid)!=(expected_uid,expected_gid) or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1 or opened.st_size > 1048576: raise ValueError('invalid credential inode')
    with os.fdopen(descriptor,'rb') as stream: descriptor=-1; raw=stream.read(1048577)
    if len(raw)>1048576: raise ValueError('credential too large')
    after=os.lstat(path)
    if (before.st_dev,before.st_ino,before.st_size)!=(opened.st_dev,opened.st_ino,opened.st_size) or (after.st_dev,after.st_ino,after.st_size)!=(opened.st_dev,opened.st_ino,opened.st_size): raise ValueError('credential changed')
finally:
    if descriptor != -1: os.close(descriptor)
try: credential=json.loads(raw.decode('utf-8'),object_pairs_hook=unique_pairs)
except Exception: raise SystemExit('invalid deployed credential')
if not isinstance(credential,dict) or set(credential)-{'baseUrl','token','obtainedAt'} or not isinstance(credential.get('baseUrl'),str) or not isinstance(credential.get('token'),str) or ('obtainedAt' in credential and not isinstance(credential['obtainedAt'],str)): raise SystemExit('invalid deployed credential')
actual=hashlib.sha256(b'moodle-autotask-e2e-credential-v1\0'+credential['baseUrl'].encode('utf-8')+b'\0'+credential['token'].encode('utf-8')).hexdigest()
if actual != sys.argv[2]: raise SystemExit('credential mismatch')
print(json.dumps({'kind':'autotask-central-e2e-controller-preflight-v1','release':os.path.basename(sys.argv[1]),'servicesActive':True,'credentialBound':True},sort_keys=True,separators=(',',':')))
PY
"@
}

function Invoke-MoodleFixture {
    param([Parameter(Mandatory = $true)][ValidateSet('LiveE2EPrepare', 'LiveE2EInspect', 'LiveE2ECleanup')][string]$FixtureAction)
    if (-not (Test-Path -LiteralPath $MoodleScript -PathType Leaf)) { Fail 'Local Moodle wrapper is missing.' }
    $result = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $MoodleScript -Action $FixtureAction -RunId $RunId
    if ($LASTEXITCODE -ne 0) { Fail "Local Moodle $FixtureAction failed." }
    $line = @($result | Where-Object { [string]$_ -match '^\{' } | Select-Object -Last 1)
    if ($line.Count -ne 1) { Fail "Local Moodle $FixtureAction did not return one JSON record." }
    $evidence = ConvertFrom-CanonicalJson -Text ([string]$line[0]) -Context "Local Moodle $FixtureAction"
    if ($evidence.kind -ne 'autotask-live-e2e-fixture-v1' -or $evidence.runId -ne $RunId) {
        Fail 'Local Moodle fixture returned mismatched evidence.'
    }
    return $evidence
}

function ConvertTo-Base64Utf8 {
    param([Parameter(Mandatory = $true)][string]$Text)
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text))
}

function Invoke-ControllerScript {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][string]$Script)
    $encoded = ConvertTo-Base64Utf8 -Text $Script
    $command = "set -euo pipefail; printf '%s' '$encoded' | base64 --decode | /bin/bash"
    $parameterPath = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($parameterPath, (@{ commands = @($command) } | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
        $commandId = ((Invoke-Aws -Arguments @(
            'ssm', 'send-command', '--instance-ids', $ControllerInstanceId, '--document-name', 'AWS-RunShellScript',
            '--comment', "AutoTask central E2E $Name", '--parameters', ('file://' + $parameterPath.Replace('\', '/')),
            '--timeout-seconds', '120', '--query', 'Command.CommandId', '--output', 'text'
        )) -join "`n").Trim()
    } finally {
        Remove-Item -LiteralPath $parameterPath -Force -ErrorAction SilentlyContinue
    }
    if ($commandId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') { Fail 'AWS returned an invalid SSM command ID.' }
    $deadline = (Get-Date).ToUniversalTime().AddSeconds(180)
    do {
        Start-Sleep -Seconds 2
        $status = ((Invoke-Aws -Arguments @('ssm', 'get-command-invocation', '--command-id', $commandId, '--instance-id', $ControllerInstanceId, '--query', 'Status', '--output', 'text')) -join "`n").Trim()
    } while ($status -in @('Pending', 'InProgress', 'Delayed') -and (Get-Date).ToUniversalTime() -lt $deadline)
    if ($status -ne 'Success') { Fail "Controller $Name command failed." }
    $output = (Invoke-Aws -Arguments @('ssm', 'get-command-invocation', '--command-id', $commandId, '--instance-id', $ControllerInstanceId, '--query', 'StandardOutputContent', '--output', 'text')) -join "`n"
    $matches = [regex]::Matches($output, '(?m)^AUTOTASK_E2E_JSON=([A-Za-z0-9+/=]+)\s*$')
    if ($matches.Count -ne 1) { Fail "Controller $Name did not return one bounded evidence record." }
    try { $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($matches[0].Groups[1].Value)) } catch { Fail "Controller $Name returned malformed evidence encoding." }
    return ConvertFrom-CanonicalJson -Text $json -Context "Controller $Name"
}

function Get-ScopeScript {
    $course = "AUTOTASK-LIVE-E2E-$RunId"
    $desiredBase64 = ConvertTo-Base64Utf8 -Text (([ordered]@{ courseShortnames = @($course); maxNewEventsPerCycle = 1 } | ConvertTo-Json -Compress))
    return @"
set -euo pipefail
run='$RunId'; base=/var/lib/moodle-autotask/e2e; root=`$base/active; retired=`$base/.`$run.retired; config=/etc/moodle-autotask/scheduler.json
desired=`$(printf '%s' '$desiredBase64' | base64 --decode); desired_sha=`$(printf '%s' "`$desired" | sha256sum | awk '{print `$1}')
account=`$(getent passwd moodle-autotask); group=`$(getent group moodle-autotask); test -n "`$account" && test -n "`$group" && test "`$(printf '%s\n' "`$account" | wc -l)" = 1 && test "`$(printf '%s\n' "`$group" | wc -l)" = 1
scheduler_uid=`$(printf '%s' "`$account" | awk -F: 'NF==7 && `$3 ~ /^[0-9]+$/ { print `$3 }'); scheduler_gid=`$(printf '%s' "`$group" | awk -F: 'NF>=3 && `$3 ~ /^[0-9]+$/ { print `$3 }'); test -n "`$scheduler_uid" && test -n "`$scheduler_gid"
assert_config() { test -f "`$config" && test ! -L "`$config" && test "`$(stat -c '%u:%g:%a:%h' "`$config")" = "0:`$scheduler_gid:640:1" || exit 1; }
assert_record() { record=`$1; test -d "`$record" && test ! -L "`$record" && test "`$(stat -c '%u:%g:%a:%h' "`$record")" = 0:0:700:2 || exit 1; mapfile -t children < <(find "`$record" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort); test "`${#children[@]}" = 2 && test "`${children[0]}" = scheduler.json.backup && test "`${children[1]}" = scheduler.state || exit 1; test -f "`$record/scheduler.json.backup" && test ! -L "`$record/scheduler.json.backup" && test "`$(stat -c '%u:%g:%a:%h' "`$record/scheduler.json.backup")" = "0:`$scheduler_gid:640:1" || exit 1; test -f "`$record/scheduler.state" && test ! -L "`$record/scheduler.state" && test "`$(stat -c '%u:%g:%a:%h' "`$record/scheduler.state")" = 0:0:600:1 || exit 1; }
read_state() { record=`$1; assert_record "`$record"; mapfile -t state < "`$record/scheduler.state"; test "`${#state[@]}" = 6 || exit 1; state_run=`${state[0]#run=}; state_desired=`${state[1]#desired=}; state_backup=`${state[2]#backup=}; state_enabled=`${state[3]#enabled=}; state_active=`${state[4]#active=}; state_phase=`${state[5]#phase=}; test "`${state[0]}" = "run=`$state_run" && test "`${state[1]}" = "desired=`$state_desired" && test "`${state[2]}" = "backup=`$state_backup" && test "`${state[3]}" = enabled=true && test "`${state[4]}" = active=true && { test "`$state_phase" = prepared || test "`$state_phase" = applied; } && [[ "`$state_desired" =~ ^[0-9a-f]{64}$ ]] && [[ "`$state_backup" =~ ^[0-9a-f]{64}$ ]] && test "`$state_backup" = "`$(sha256sum "`$record/scheduler.json.backup" | awk '{print `$1}')" || exit 1; }
assert_retired() { record=`$1; expected_run=`$2; read_state "`$record"; test "`$state_run" = "`$expected_run"; }
assert_current() { record=`$1; read_state "`$record"; test "`$state_run" = "`$run"; test "`$state_desired" = "`$desired_sha"; assert_config; current_sha=`$(sha256sum "`$config" | awk '{print `$1}'); if [ "`$current_sha" = "`$state_backup" ]; then state_phase=prepared; elif [ "`$current_sha" = "`$desired_sha" ]; then state_phase=applied; else exit 1; fi; systemctl is-enabled --quiet moodle-autotask-scheduler.service; systemctl is-active --quiet moodle-autotask-scheduler.service; backup_sha=`$state_backup; }
write_state() { phase=`$1; temporary=`$(mktemp "`$root/.scheduler.state.XXXXXX"); printf 'run=%s\ndesired=%s\nbackup=%s\nenabled=true\nactive=true\nphase=%s\n' "`$run" "`$desired_sha" "`$backup_sha" "`$phase" > "`$temporary"; chown root:root "`$temporary"; chmod 0600 "`$temporary"; mv -f "`$temporary" "`$root/scheduler.state"; }
test -d /var/lib/moodle-autotask && test ! -L /var/lib/moodle-autotask && test "`$(stat -c '%u:%g:%a' /var/lib/moodle-autotask)" = "`$scheduler_uid:`$scheduler_gid:750"
if [ ! -e "`$base" ] && [ ! -L "`$base" ]; then install -d -o root -g root -m 0700 "`$base"; fi
test -d "`$base" && test ! -L "`$base" && test "`$(stat -c '%u:%g:%a' "`$base")" = 0:0:700
assert_config
own_pending=''; shopt -s nullglob dotglob; children=("`$base"/*); shopt -u nullglob dotglob
for child in "`${children[@]}"; do name=`$(basename "`$child"); case "`$name" in active) assert_record "`$child" ;; ."`$run".retired) assert_retired "`$child" "`$run"; exit 1 ;; .*.retired) retired_run=`${name#.}; retired_run=`${retired_run%.retired}; [[ "`$retired_run" =~ ^[a-z0-9][a-z0-9-]{6,38}[a-z0-9]$ ]] || exit 1; assert_retired "`$child" "`$retired_run" ;; ."`$run".pending.*) test -z "`$own_pending" || exit 1; assert_retired "`$child" "`$run"; own_pending=`$child ;; .*.pending.*) exit 1 ;; *) exit 1 ;; esac; done
if [ -e "`$root" ] || [ -L "`$root" ]; then
  assert_current "`$root"; phase=`$state_phase
else
  systemctl is-enabled --quiet moodle-autotask-scheduler.service; systemctl is-active --quiet moodle-autotask-scheduler.service
  if [ -n "`$own_pending" ]; then candidate=`$own_pending; read_state "`$candidate"; test "`$state_desired" = "`$desired_sha"; backup_sha=`$state_backup; root=`$base/active; mv -T "`$candidate" "`$root"; assert_current "`$root"; phase=`$state_phase
  else
    candidate=`$(mktemp -d "`$base/.`$run.pending.XXXXXX"); test -d "`$candidate" && test ! -L "`$candidate" && test "`$(stat -c '%u:%g:%a:%h' "`$candidate")" = 0:0:700:2
    cp -p "`$config" "`$candidate/scheduler.json.backup"; backup_sha=`$(sha256sum "`$candidate/scheduler.json.backup" | awk '{print `$1}'); root=`$candidate; write_state prepared; assert_record "`$candidate"
    root=`$base/active; mv -T "`$candidate" "`$root"; assert_current "`$root"; phase=`$state_phase
  fi
fi
if [ "`$phase" = prepared ]; then candidate=`$(mktemp /etc/moodle-autotask/.scheduler.e2e.XXXXXX); printf '%s' "`$desired" > "`$candidate"; chown root:moodle-autotask "`$candidate"; chmod 0640 "`$candidate"; mv -f "`$candidate" "`$config"; write_state applied; fi
systemctl restart moodle-autotask-scheduler.service; systemctl is-active --quiet moodle-autotask-scheduler.service
python3 - <<'PY' | base64 -w0 | sed 's/^/AUTOTASK_E2E_JSON=/'
import json
print(json.dumps({'kind':'autotask-central-e2e-scope-v1','courseShortname':'$course','maxNewEventsPerCycle':1,'resumable':True},sort_keys=True,separators=(',',':')))
PY
"@
}

function Get-RestoreScopeScript {
    $course = "AUTOTASK-LIVE-E2E-$RunId"
    $desiredBase64 = ConvertTo-Base64Utf8 -Text (([ordered]@{ courseShortnames = @($course); maxNewEventsPerCycle = 1 } | ConvertTo-Json -Compress))
    return @"
set -euo pipefail
run='$RunId'; base=/var/lib/moodle-autotask/e2e; root=`$base/active; retired=`$base/.`$run.retired; config=/etc/moodle-autotask/scheduler.json
desired=`$(printf '%s' '$desiredBase64' | base64 --decode); desired_sha=`$(printf '%s' "`$desired" | sha256sum | awk '{print `$1}')
account=`$(getent passwd moodle-autotask); group=`$(getent group moodle-autotask); test -n "`$account" && test -n "`$group" && test "`$(printf '%s\n' "`$account" | wc -l)" = 1 && test "`$(printf '%s\n' "`$group" | wc -l)" = 1
scheduler_uid=`$(printf '%s' "`$account" | awk -F: 'NF==7 && `$3 ~ /^[0-9]+$/ { print `$3 }'); scheduler_gid=`$(printf '%s' "`$group" | awk -F: 'NF>=3 && `$3 ~ /^[0-9]+$/ { print `$3 }'); test -n "`$scheduler_uid" && test -n "`$scheduler_gid"
assert_config() { test -f "`$config" && test ! -L "`$config" && test "`$(stat -c '%u:%g:%a:%h' "`$config")" = "0:`$scheduler_gid:640:1" || exit 1; }
assert_record() { record=`$1; test -d "`$record" && test ! -L "`$record" && test "`$(stat -c '%u:%g:%a:%h' "`$record")" = 0:0:700:2 || exit 1; mapfile -t children < <(find "`$record" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort); test "`${#children[@]}" = 2 && test "`${children[0]}" = scheduler.json.backup && test "`${children[1]}" = scheduler.state || exit 1; test -f "`$record/scheduler.json.backup" && test ! -L "`$record/scheduler.json.backup" && test "`$(stat -c '%u:%g:%a:%h' "`$record/scheduler.json.backup")" = "0:`$scheduler_gid:640:1" || exit 1; test -f "`$record/scheduler.state" && test ! -L "`$record/scheduler.state" && test "`$(stat -c '%u:%g:%a:%h' "`$record/scheduler.state")" = 0:0:600:1 || exit 1; }
read_state() { record=`$1; assert_record "`$record"; mapfile -t state < "`$record/scheduler.state"; test "`${#state[@]}" = 6 || exit 1; state_run=`${state[0]#run=}; state_desired=`${state[1]#desired=}; state_backup=`${state[2]#backup=}; state_enabled=`${state[3]#enabled=}; state_active=`${state[4]#active=}; state_phase=`${state[5]#phase=}; test "`${state[0]}" = "run=`$state_run" && test "`${state[1]}" = "desired=`$state_desired" && test "`${state[2]}" = "backup=`$state_backup" && test "`${state[3]}" = enabled=true && test "`${state[4]}" = active=true && { test "`$state_phase" = prepared || test "`$state_phase" = applied; } && [[ "`$state_desired" =~ ^[0-9a-f]{64}$ ]] && [[ "`$state_backup" =~ ^[0-9a-f]{64}$ ]] && test "`$state_backup" = "`$(sha256sum "`$record/scheduler.json.backup" | awk '{print `$1}')" || exit 1; }
assert_retired() { record=`$1; expected_run=`$2; read_state "`$record"; test "`$state_run" = "`$expected_run"; }
assert_current() { read_state "`$root"; test "`$state_run" = "`$run"; test "`$state_desired" = "`$desired_sha"; assert_config; current_sha=`$(sha256sum "`$config" | awk '{print `$1}'); test "`$current_sha" = "`$desired_sha" -o "`$current_sha" = "`$state_backup"; }
test -d /var/lib/moodle-autotask && test ! -L /var/lib/moodle-autotask && test "`$(stat -c '%u:%g:%a' /var/lib/moodle-autotask)" = "`$scheduler_uid:`$scheduler_gid:750"
assert_config
if [ ! -e "`$base" ] && [ ! -L "`$base" ]; then restored=noop; else
  test -d "`$base" && test ! -L "`$base" && test "`$(stat -c '%u:%g:%a' "`$base")" = 0:0:700
  own_pending=''; shopt -s nullglob dotglob; children=("`$base"/*); shopt -u nullglob dotglob
  for child in "`${children[@]}"; do name=`$(basename "`$child"); case "`$name" in active) assert_record "`$child" ;; .*.retired) retired_run=`${name#.}; retired_run=`${retired_run%.retired}; [[ "`$retired_run" =~ ^[a-z0-9][a-z0-9-]{6,38}[a-z0-9]$ ]] || exit 1; assert_retired "`$child" "`$retired_run" ;; ."`$run".pending.*) test -z "`$own_pending" || exit 1; assert_retired "`$child" "`$run"; own_pending=`$child ;; .*.pending.*) exit 1 ;; *) exit 1 ;; esac; done
  if [ -e "`$retired" ] || [ -L "`$retired" ]; then
    test ! -e "`$root" && test ! -L "`$root"; read_state "`$retired"; test "`$state_run" = "`$run" && test "`$state_desired" = "`$desired_sha" && test "`$(sha256sum "`$config" | awk '{print `$1}')" = "`$state_backup"; systemctl is-enabled --quiet moodle-autotask-scheduler.service; systemctl is-active --quiet moodle-autotask-scheduler.service
    restored=retired
  elif [ ! -e "`$root" ] && [ ! -L "`$root" ] && [ -n "`$own_pending" ]; then
    read_state "`$own_pending"; test "`$state_run" = "`$run"; test "`$state_desired" = "`$desired_sha"; test "`$(sha256sum "`$config" | awk '{print `$1}')" = "`$state_backup"; systemctl is-enabled --quiet moodle-autotask-scheduler.service; systemctl is-active --quiet moodle-autotask-scheduler.service; mv -T "`$own_pending" "`$retired"; restored=retired
  elif [ ! -e "`$root" ] && [ ! -L "`$root" ]; then restored=noop; else
  assert_current; current=`$(sha256sum "`$config" | awk '{print `$1}'); systemctl stop moodle-autotask-scheduler.service; ! systemctl is-active --quiet moodle-autotask-scheduler.service; if [ "`$current" != "`$state_backup" ]; then candidate=`$(mktemp /etc/moodle-autotask/.scheduler.restore.XXXXXX); cp -p "`$root/scheduler.json.backup" "`$candidate"; mv -f "`$candidate" "`$config"; fi
  if ! systemctl enable moodle-autotask-scheduler.service; then exit 1; fi; if ! systemctl start moodle-autotask-scheduler.service; then exit 1; fi; systemctl is-active --quiet moodle-autotask-scheduler.service
  mv -T "`$root" "`$retired"; restored=retired
  fi
fi
python3 - <<'PY' | base64 -w0 | sed 's/^/AUTOTASK_E2E_JSON=/'
import json
print(json.dumps({'kind':'autotask-central-e2e-scope-restore-v1','restored':True},sort_keys=True,separators=(',',':')))
PY
"@
}

function Get-ControllerReadScript {
    $course = "AUTOTASK-LIVE-E2E-$RunId"
    return @"
set -euo pipefail
python3 - '$course' <<'PY' | base64 -w0 | sed 's/^/AUTOTASK_E2E_JSON=/'
import grp,hashlib,io,json,os,pwd,re,sqlite3,stat,sys,unicodedata,zipfile
course=sys.argv[1]; digest=re.compile(r'^[0-9a-f]{64}$'); event_id=re.compile(r'^moodle-notification-event-v1:[0-9a-f]{64}$'); task_key=re.compile(r'^moodle-task-v1:[0-9a-f]{64}$'); revision_digest=re.compile(r'^moodle-assignment-v1:[0-9a-f]{64}$')
try: expected_uid=pwd.getpwnam('moodle-autotask').pw_uid; expected_gid=grp.getgrnam('moodle-autotask').gr_gid
except KeyError: raise SystemExit('missing config owner')
def unique_pairs(items):
    value={}
    for key,item in items:
        if key in value: raise ValueError('duplicate key')
        value[key]=item
    return value
def read_json(path):
    descriptor=-1
    try:
        before=os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or (before.st_uid,before.st_gid)!=(expected_uid,expected_gid) or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1: raise ValueError('invalid config inode')
        descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)); opened=os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_uid,opened.st_gid)!=(expected_uid,expected_gid) or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1 or opened.st_size > 1048576: raise ValueError('invalid config inode')
        with os.fdopen(descriptor,'rb') as stream: descriptor=-1; raw=stream.read(1048577)
        if len(raw)>1048576: raise ValueError('config too large')
        after=os.lstat(path)
        if (before.st_dev,before.st_ino,before.st_size)!=(opened.st_dev,opened.st_ino,opened.st_size) or (after.st_dev,after.st_ino,after.st_size)!=(opened.st_dev,opened.st_ino,opened.st_size): raise ValueError('config changed')
    finally:
        if descriptor != -1: os.close(descriptor)
    return json.loads(raw.decode('utf-8'),object_pairs_hook=unique_pairs)
try: telegram=read_json('/etc/moodle-autotask/telegram.json')
except Exception: raise SystemExit('invalid telegram config')
if not isinstance(telegram,dict) or set(telegram)!={'botToken','chatId','allowedUserId'} or not isinstance(telegram['botToken'],str) or not re.fullmatch(r'[1-9][0-9]{5,15}:[A-Za-z0-9_-]{30,100}',telegram['botToken']) or any(not isinstance(telegram[key],int) or isinstance(telegram[key],bool) or not 1 <= telegram[key] < 2**63 for key in ('chatId','allowedUserId')): raise SystemExit('invalid telegram config')
db='/var/lib/moodle-autotask/approval.sqlite3'
con=sqlite3.connect('file:'+db+'?mode=ro',uri=True)
rows=[]
for row in con.execute('SELECT r.event_id,r.task_key,r.revision_digest,r.decision,r.delivery_state,r.decided_by,r.decided_at,r.chat_id,r.message_id,w.selected_mode,w.status,w.provision_key,r.payload,o.payload,s.status,s.manifest_digest,s.payload,s.receipt_reference,s.receipt_payload,s.decided_by,s.decided_at,so.delivered_at FROM requests r LEFT JOIN work_items w ON w.event_id=r.event_id LEFT JOIN execution_outbox o ON o.event_id=r.event_id LEFT JOIN submissions s ON s.event_id=r.event_id LEFT JOIN submission_outbox so ON so.event_id=r.event_id'):
    try: payload=json.loads(row[12])
    except Exception: raise SystemExit('invalid request')
    if payload.get('course_shortname') == course: rows.append((row,payload))
if len(rows) == 0:
    result={'kind':'autotask-central-e2e-controller-v1','state':'absent','courseShortname':course}
elif len(rows) != 1:
    raise SystemExit('ambiguous run event')
else:
    (event,task,revision,decision,delivery,request_decider,request_decided_at,request_chat,request_message,mode,work,provision,request,execution,substatus,manifestdigest,submission,receipt,receipt_payload,submission_decider,submission_decided_at,submission_delivered_at),request_payload=rows[0]; payload=request_payload
    if not (isinstance(event,str) and event_id.fullmatch(event) and isinstance(task,str) and task_key.fullmatch(task) and isinstance(revision,str) and revision_digest.fullmatch(revision)) or decision not in ('pending','approved','ignored') or not isinstance(payload,dict) or payload.get('event_id') != event or payload.get('task_key') != task or payload.get('revision_digest') != revision or payload.get('course_shortname') != course or not isinstance(payload.get('assignment_id'),int) or isinstance(payload['assignment_id'],bool) or payload['assignment_id'] <= 0:
        raise SystemExit('invalid event identity')
    positive=lambda value:isinstance(value,int) and not isinstance(value,bool) and value > 0
    if decision == 'approved' and (delivery != 'notified' or request_decider != telegram['allowedUserId'] or not positive(request_decided_at) or request_chat != telegram['chatId'] or not positive(request_message)):
        raise SystemExit('invalid request approval')
    result={'kind':'autotask-central-e2e-controller-v1','state':'pending','courseShortname':course,'eventId':event,'taskKey':task,'revisionDigest':revision,'decision':decision}
    if mode is not None:
        if mode not in ('central','hybrid','in_guest') or work not in ('pending','lab_pending','ready','failed','cleaned') or not isinstance(provision,str) or not digest.fullmatch(provision): raise SystemExit('invalid work')
        result.update({'selectedMode':mode,'workStatus':work,'provisionKey':provision})
    if execution is not None:
        value=json.loads(execution); provenance=value.get('provenance'); report=value.get('reportMarkdown')
        if not isinstance(provenance,dict) or not isinstance(report,str): raise SystemExit('invalid execution')
        required={'kind','roles','jobIds','selectedMode','specificationDigest','preparedInputManifestDigest','plannerJobId','executorJobId','reviewerJobId','planDigest','plannerResultDigest','executorResultDigest','artifactManifestDigest','artifactBundleDigest','reviewerResultDigest','reviewerAccepted','bundleLocator','artifactManifest'}
        if set(provenance)!=required or provenance['kind']!='moodle-central-provenance-v2' or provenance['roles']!=['central_planner','central_executor','central_reviewer'] or provenance['selectedMode']!='central' or provenance['reviewerAccepted'] is not True: raise SystemExit('invalid provenance')
        jobs=provenance['jobIds']; fields=required-{'kind','roles','jobIds','selectedMode','reviewerAccepted','bundleLocator','artifactManifest'}
        if not isinstance(jobs,list) or len(jobs)!=3 or len(set(jobs))!=3 or jobs != [provenance['plannerJobId'],provenance['executorJobId'],provenance['reviewerJobId']] or not all(isinstance(x,str) and digest.fullmatch(x) for x in jobs) or not all(isinstance(provenance[x],str) and digest.fullmatch(provenance[x]) for x in fields): raise SystemExit('invalid provenance digests')
        manifest=provenance['artifactManifest']; bundle=provenance['artifactBundleDigest']
        if not isinstance(manifest,dict) or set(manifest)!={'kind','files','totals'} or manifest.get('kind')!='artifact-manifest-v1' or provenance['bundleLocator']!='bundles/'+bundle+'.zip' or hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()!=provenance['artifactManifestDigest']: raise SystemExit('invalid manifest')
        files=manifest['files']; totals=manifest['totals']
        if not isinstance(files,list) or not 1<=len(files)<=64 or not isinstance(totals,dict) or set(totals)!={'files','bytes'} or any(not isinstance(totals[x],int) or isinstance(totals[x],bool) or totals[x]<0 for x in totals): raise SystemExit('invalid manifest')
        def safe_path(value):
            try: encoded=value.encode('utf-8')
            except Exception: return False
            parts=value.split('/')
            return isinstance(value,str) and 0<len(encoded)<=240 and 1<=len(parts)<=8 and not value.startswith('/') and '\\' not in value and ':' not in value and not any(not part or part in ('.','..') or any(ord(char)<32 or ord(char)==127 for char in part) for part in parts)
        if any(not isinstance(item,dict) or set(item)!={'path','size','sha256'} or not safe_path(item.get('path')) or not isinstance(item['size'],int) or isinstance(item['size'],bool) or item['size']<0 or not isinstance(item['sha256'],str) or not digest.fullmatch(item['sha256']) for item in files): raise SystemExit('invalid manifest')
        expected=[item['path'] for item in files]
        normalized=[unicodedata.normalize('NFC',value).casefold() for value in expected]
        if expected != sorted(expected,key=lambda value:value.encode('utf-8')) or len(set(normalized)) != len(normalized) or totals != {'files':len(files),'bytes':sum(item['size'] for item in files)} or totals['bytes']>1900000: raise SystemExit('invalid manifest')
        path='/var/spool/moodle-autotask/results/bundles/'+bundle+'.zip'; descriptor=-1
        try:
            descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)); metadata=os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 512*1024*1024: raise SystemExit('invalid bundle inode')
            with os.fdopen(descriptor,'rb') as stream: descriptor=-1; raw=stream.read(512*1024*1024+1)
            path_metadata=os.lstat(path)
            if (path_metadata.st_dev,path_metadata.st_ino,path_metadata.st_size) != (metadata.st_dev,metadata.st_ino,metadata.st_size): raise SystemExit('invalid bundle identity')
        finally:
            if descriptor != -1: os.close(descriptor)
        if len(raw)>512*1024*1024 or hashlib.sha256(raw).hexdigest()!=bundle: raise SystemExit('invalid bundle')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.comment != b'': raise SystemExit('zip metadata mismatch')
            names=archive.namelist()
            if names != expected: raise SystemExit('zip manifest mismatch')
            for item,info in zip(files,archive.infolist()):
                if info.is_dir() or info.compress_type != zipfile.ZIP_STORED or info.date_time != (1980,1,1,0,0,0) or info.external_attr != (0o100640 << 16) or info.extra or info.comment: raise SystemExit('zip metadata mismatch')
                data=archive.read(info)
                if len(data)!=item['size'] or hashlib.sha256(data).hexdigest()!=item['sha256']: raise SystemExit('zip content mismatch')
        report_digest=hashlib.sha256(report.encode('utf-8')).hexdigest()
        result.update({'state':'executed','provenance':provenance,'reportDigest':report_digest,'bundleVerified':True})
    if substatus is not None:
        if substatus not in ('awaiting_approval','approved','declined','uploading','saving','finalizing','submitted','failed'): raise SystemExit('invalid submission status')
        if execution is None: raise SystemExit('submission without execution')
        payload=json.loads(submission)
        required={'artifacts','assignmentId','submissionDrafts','requireSubmissionStatement','submissionStatement','submissionStatementFormat','submissionStatementDigest','submissionStatementPlain','manifestDigest','reportDigest','reportMarkdown','revisionDigest','taskKey'}
        if set(payload)!=required or payload.get('taskKey') != task or payload.get('revisionDigest') != revision or payload.get('manifestDigest') != manifestdigest or not isinstance(payload.get('assignmentId'),int) or payload['assignmentId'] <= 0 or payload.get('submissionDrafts') is not False or payload.get('requireSubmissionStatement') is not False or payload.get('submissionStatement') != '' or not isinstance(payload.get('submissionStatementFormat'),int) or payload.get('submissionStatementDigest') is not None or payload.get('submissionStatementPlain') is not None or not isinstance(payload.get('reportDigest'),str) or not digest.fullmatch(payload['reportDigest']) or not isinstance(payload.get('reportMarkdown'),str): raise SystemExit('invalid submission')
        artifacts=payload['artifacts']; expected_name='autotask-'+revision.removeprefix('moodle-assignment-v1:')[:16]+'.md'
        if not isinstance(artifacts,list) or len(artifacts)!=1: raise SystemExit('invalid submission artifact')
        artifact=artifacts[0]
        if not isinstance(artifact,dict) or set(artifact)!={'filename','sizeBytes','sha256'} or artifact.get('filename') != expected_name or not isinstance(artifact.get('sizeBytes'),int) or artifact['sizeBytes'] != len(payload['reportMarkdown'].encode('utf-8')) or artifact.get('sha256') != payload['reportDigest'] or not digest.fullmatch(artifact['sha256']): raise SystemExit('invalid submission artifact')
        canonical=dict(payload); canonical.pop('manifestDigest'); computed=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()
        if computed != manifestdigest or computed != payload['manifestDigest'] or payload['assignmentId'] != request_payload['assignment_id'] or hashlib.sha256(payload['reportMarkdown'].encode('utf-8')).hexdigest() != payload['reportDigest'] or payload['reportDigest'] != report_digest: raise SystemExit('invalid submission binding')
        result['submission']={'status':substatus,'manifestDigest':manifestdigest,'reportDigest':payload['reportDigest'],'assignmentId':payload['assignmentId'],'filename':artifact['filename'],'sizeBytes':artifact['sizeBytes'],'sha256':artifact['sha256']}
        if substatus == 'submitted':
            try: receipt_value=json.loads(receipt_payload)
            except Exception: raise SystemExit('invalid submission receipt')
            if not isinstance(receipt,str) or not re.fullmatch(r'moodle-submission:[1-9][0-9]*',receipt) or not isinstance(receipt_value,dict) or set(receipt_value)!={'approvedAt','approvedBy','manifestDigest','reference','submittedAt'} or not all(isinstance(receipt_value[x],int) and not isinstance(receipt_value[x],bool) and receipt_value[x] >= 0 for x in ('approvedAt','approvedBy','submittedAt')) or receipt_value['manifestDigest'] != manifestdigest or receipt_value['reference'] != receipt or submission_decider != telegram['allowedUserId'] or not positive(submission_decided_at) or not positive(submission_delivered_at) or receipt_value['approvedBy'] != submission_decider or receipt_value['approvedAt'] != submission_decided_at: raise SystemExit('invalid submission receipt')
            result['submission']['submissionId']=int(receipt.split(':')[1])
        elif receipt is not None or receipt_payload is not None: raise SystemExit('invalid submission receipt')
print(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False))
PY
"@
}

function Restore-SchedulerScope {
    $restored = Invoke-ControllerScript -Name 'restore-scheduler-scope' -Script (Get-RestoreScopeScript)
    if ($restored.kind -ne 'autotask-central-e2e-scope-restore-v1' -or $restored.restored -ne $true) {
        Fail 'Scheduler scope restore evidence is invalid.'
    }
    $script:ScopeApplied = $false
    Add-Phase -Name 'scheduler-restored' -Data ([ordered]@{ restored = $true })
}

function Assert-CentralExecutionEvidence {
    param([Parameter(Mandatory = $true)]$Evidence)
    if ($Evidence.decision -ne 'approved' -or $Evidence.selectedMode -ne 'central' -or $Evidence.workStatus -notin @('ready', 'cleaned') -or $Evidence.state -ne 'executed' -or $Evidence.bundleVerified -ne $true) {
        Fail 'Controller did not produce the required approved central execution.'
    }
    $provenance = $Evidence.provenance
    if ($null -eq $provenance -or @($provenance.roles).Count -ne 3 -or @($provenance.jobIds).Count -ne 3 -or @($provenance.jobIds | Select-Object -Unique).Count -ne 3 -or $provenance.reviewerAccepted -ne $true) {
        Fail 'Controller central role provenance is invalid.'
    }
    if ($Evidence.reportDigest -notmatch '^[0-9a-f]{64}$' -or $provenance.artifactBundleDigest -notmatch '^[0-9a-f]{64}$') { Fail 'Controller digest evidence is invalid.' }
}

function Assert-ZeroLabEvidence {
    param([Parameter(Mandatory = $true)][string]$ProvisionKey)
    if ($ProvisionKey -notmatch '^[0-9a-f]{64}$') { Fail 'Controller provision key is invalid.' }
    $queries = @(
        @('ec2', 'describe-instances', '--filters', "Name=tag:ProvisionKey,Values=$ProvisionKey", '--query', 'Reservations[].Instances[].InstanceId', '--output', 'json'),
        @('ec2', 'describe-images', '--owners', 'self', '--filters', "Name=tag:ProvisionKey,Values=$ProvisionKey", '--query', 'Images[].ImageId', '--output', 'json'),
        @('ec2', 'describe-snapshots', '--owner-ids', 'self', '--filters', "Name=tag:ProvisionKey,Values=$ProvisionKey", '--query', 'Snapshots[].SnapshotId', '--output', 'json'),
        @('ec2', 'describe-import-image-tasks', '--query', "ImportImageTasks[?Tags[?Key=='ProvisionKey' && Value=='$ProvisionKey']].ImportTaskId", '--output', 'json')
    )
    $labels = @('instances', 'amis', 'snapshots', 'importTasks')
    $zero = [ordered]@{}
    for ($index = 0; $index -lt $queries.Count; $index++) {
        $value = ConvertFrom-CanonicalJson -Text ((Invoke-Aws -Arguments $queries[$index]) -join "`n") -Context "AWS $($labels[$index]) query"
        if (@($value).Count -ne 0) { Fail "Central E2E created $($labels[$index]) for this run." }
        $zero[$labels[$index]] = 0
    }
    Add-Phase -Name 'zero-lab-compute-verified' -Data $zero
}

function Wait-ControllerState {
    param([Parameter(Mandatory = $true)][ValidateSet('StartApproval', 'Execution', 'Submission')][string]$Gate, [Parameter(Mandatory = $true)][datetime]$Deadline)
    $announced = $false
    while ((Get-Date).ToUniversalTime() -lt $Deadline) {
        $state = Invoke-ControllerScript -Name 'read-canonical-state' -Script (Get-ControllerReadScript)
        if ($state.state -eq 'absent') { Start-Sleep -Seconds $PollSeconds; continue }
        if ($state.decision -eq 'ignored') { Fail 'The human declined the start approval.' }
        if ($Gate -eq 'StartApproval') {
            $selectedMode = Get-OptionalPropertyValue -Object $state -Name 'selectedMode'
            if ($state.decision -eq 'approved' -and $selectedMode -eq 'central') { return $state }
            if (-not $announced) { Write-Output 'Awaiting the real Telegram “Hacer tarea” approval for this exact run.'; $announced = $true }
        } elseif ($Gate -eq 'Execution') {
            if ($state.state -eq 'executed') { return $state }
            if (-not $announced) { Write-Output 'Start approval received; awaiting planner, executor, and reviewer completion.'; $announced = $true }
        } else {
            $submission = Get-OptionalPropertyValue -Object $state -Name 'submission'
            $submissionStatus = Get-OptionalPropertyValue -Object $submission -Name 'status'
            if ($submissionStatus -eq 'submitted') { return $state }
            if ($submissionStatus -eq 'declined') { Fail 'The human declined the submission approval.' }
            if (-not $announced) { Write-Output 'Awaiting the separate real Telegram “Entregar” approval; this harness never submits.'; $announced = $true }
        }
        Start-Sleep -Seconds $PollSeconds
    }
    Fail "Timed out while waiting for $Gate."
}

if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $RuntimeRoot ("central-e2e\$RunId.evidence.json")
}

try {
    $credentialBinding = Test-LocalMoodleTokenFile
    $identity = ((Invoke-Aws -Arguments @('sts', 'get-caller-identity', '--query', 'Account', '--output', 'text')) -join "`n").Trim()
    if ($identity -ne $AccountId) { Fail 'The authenticated AWS account does not match AccountId.' }
    if ($Action -eq 'Cleanup') {
        # Repair the exact durable record before application-health preflight:
        # the scheduler may be stopped precisely because the prior run failed.
        Restore-SchedulerScope
        $controllerPreflight = Invoke-ControllerScript -Name 'preflight' -Script (Get-ControllerPreflightScript -CredentialBinding $credentialBinding)
        if ($controllerPreflight.kind -ne 'autotask-central-e2e-controller-preflight-v1' -or $controllerPreflight.servicesActive -ne $true -or $controllerPreflight.credentialBound -ne $true) {
            Fail 'Controller is not healthy after scheduler scope recovery.'
        }
        Add-Phase -Name 'controller-preflight' -Data ([ordered]@{ servicesActive = $true; credentialBound = $true })
        $cleanup = Invoke-MoodleFixture -FixtureAction 'LiveE2ECleanup'
        if ($cleanup.state -ne 'absent') { Fail 'Live E2E cleanup did not prove absence.' }
        Add-Phase -Name 'fixture-cleaned' -Data ([ordered]@{ state = $cleanup.state })
        $script:Evidence.status = 'success'
        return
    }
    $gitStatus = (& git -C $RepoRoot status --porcelain --untracked-files=all) -join "`n"
    if ($LASTEXITCODE -ne 0 -or -not [string]::IsNullOrWhiteSpace($gitStatus)) { Fail 'Central E2E requires a clean Git worktree.' }
    $commit = ((& git -C $RepoRoot rev-parse HEAD) -join "`n").Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { Fail 'Could not determine the deployed commit candidate.' }
    Add-Phase -Name 'repository-preflight' -Data ([ordered]@{ commit = $commit })
    $controllerPreflight = Invoke-ControllerScript -Name 'preflight' -Script (Get-ControllerPreflightScript -CredentialBinding $credentialBinding)
    if ($controllerPreflight.kind -ne 'autotask-central-e2e-controller-preflight-v1' -or $controllerPreflight.servicesActive -ne $true -or $controllerPreflight.credentialBound -ne $true -or $controllerPreflight.release -notmatch '^[0-9a-f]{64}$') {
        Fail 'Controller deployment preflight is not healthy or canonical.'
    }
    Add-Phase -Name 'controller-preflight' -Data ([ordered]@{ servicesActive = $true; credentialBound = $true; release = $controllerPreflight.release })
    $fixture = Invoke-MoodleFixture -FixtureAction 'LiveE2EPrepare'
    if ($fixture.state -ne 'ready' -or $null -eq $fixture.assignmentId -or $null -ne $fixture.submission) { Fail 'Disposable Moodle fixture is not pristine.' }
    Add-Phase -Name 'fixture-prepared' -Data ([ordered]@{ state = $fixture.state; courseId = $fixture.courseId; assignmentId = $fixture.assignmentId; assignmentCmid = $fixture.assignmentCmid })
    # Mark recovery required before SSM dispatch: a committed remote swap whose
    # response is lost is still restored by finally or a later Cleanup rerun.
    $script:ScopeApplied = $true
    $scope = Invoke-ControllerScript -Name 'scope-scheduler' -Script (Get-ScopeScript)
    if ($scope.kind -ne 'autotask-central-e2e-scope-v1' -or $scope.maxNewEventsPerCycle -ne 1 -or $scope.resumable -ne $true) { Fail 'Could not scope the controller scheduler.' }
    Add-Phase -Name 'scheduler-scoped' -Data ([ordered]@{ maxNewEventsPerCycle = 1; resumable = $true })
    $deadline = (Get-Date).ToUniversalTime().AddSeconds($TimeoutSeconds)
    $approved = Wait-ControllerState -Gate 'StartApproval' -Deadline $deadline
    Add-Phase -Name 'start-approved' -Data ([ordered]@{ eventId = $approved.eventId; taskKey = $approved.taskKey; revisionDigest = $approved.revisionDigest })
    $executed = Wait-ControllerState -Gate 'Execution' -Deadline $deadline
    Assert-CentralExecutionEvidence -Evidence $executed
    Assert-ZeroLabEvidence -ProvisionKey $executed.provisionKey
    Add-Phase -Name 'central-execution-verified' -Data ([ordered]@{ eventId = $executed.eventId; taskKey = $executed.taskKey; revisionDigest = $executed.revisionDigest; reportDigest = $executed.reportDigest; artifactBundleDigest = $executed.provenance.artifactBundleDigest; jobIds = @($executed.provenance.jobIds) })
    $submitted = Wait-ControllerState -Gate 'Submission' -Deadline $deadline
    Assert-CentralExecutionEvidence -Evidence $submitted
    if ($submitted.submission.reportDigest -ne $submitted.reportDigest -or $submitted.submission.manifestDigest -notmatch '^[0-9a-f]{64}$') { Fail 'Submission manifest is not bound to the reviewed report.' }
    $receipt = Invoke-MoodleFixture -FixtureAction 'LiveE2EInspect'
    if ($receipt.state -ne 'ready' -or $receipt.assignmentId -ne $fixture.assignmentId -or $receipt.submission.status -ne 'submitted' -or $receipt.submission.filename -ne $submitted.submission.filename -or $receipt.submission.sizeBytes -ne $submitted.submission.sizeBytes -or $receipt.submission.sha256 -ne $submitted.submission.sha256 -or $receipt.submission.submissionId -ne $submitted.submission.submissionId) { Fail 'Moodle submitted-file receipt does not match the approved manifest.' }
    Add-Phase -Name 'moodle-submission-verified' -Data ([ordered]@{ manifestDigest = $submitted.submission.manifestDigest; reportDigest = $submitted.submission.reportDigest; filename = $receipt.submission.filename; sizeBytes = $receipt.submission.sizeBytes; submissionId = $receipt.submission.submissionId })
    Assert-ZeroLabEvidence -ProvisionKey $submitted.provisionKey
    Restore-SchedulerScope
    $cleanup = Invoke-MoodleFixture -FixtureAction 'LiveE2ECleanup'
    if ($cleanup.state -ne 'absent') { Fail 'Successful run cleanup did not prove fixture absence.' }
    Add-Phase -Name 'fixture-cleaned' -Data ([ordered]@{ state = $cleanup.state })
    $script:Evidence.status = 'success'
}
catch {
    $script:Evidence.status = 'failed'
    $script:Evidence.failure = 'central-e2e-failed'
    throw
}
finally {
    if ($script:ScopeApplied) {
        try {
            Restore-SchedulerScope
        } catch {
            $script:Evidence.status = 'failed'
            $script:Evidence.restoreFailure = 'scheduler-restore-failed'
            if (-not $script:Evidence.Contains('failure')) { $script:Evidence.failure = 'scheduler-restore-failed' }
            $script:ScopeRestoreFailed = $true
        }
    }
    Write-Evidence
    if ($script:ScopeRestoreFailed) { throw 'Scheduler scope restoration failed.' }
}
