[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('Bootstrap', 'Up', 'Down', 'Status', 'Smoke', 'Reset')]
    [string]$Action = 'Status',
    [Parameter()]
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-MoodleVersions {
    param([Parameter(Mandatory = $true)][string]$Path)
    $importer = Get-Command 'Import-PowerShellDataFile' -ErrorAction SilentlyContinue
    if ($null -ne $importer) {
        return Import-PowerShellDataFile -LiteralPath $Path
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^\s*(?<name>[A-Za-z][A-Za-z0-9_]*)\s*=\s*'(?<value>[^']*)'\s*$") {
            $values[$Matches.name] = $Matches.value
        }
    }
    foreach ($name in @(
        'MoodleDockerRepository', 'MoodleDockerCommit', 'MoodleDockerTablePrefix', 'MoodleDockerConfigTable',
        'MoodleRepository', 'MoodleRelease', 'MoodleTagObject', 'MoodlePeeledCommit'
    )) {
        if (-not $values.ContainsKey($name)) {
            throw "Moodle version data is missing required value: $name"
        }
    }
    return [PSCustomObject]$values
}

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$RuntimeRoot = Join-Path $RepoRoot '.runtime'
$MoodleRoot = Join-Path $RuntimeRoot 'moodle'
$MoodleDockerRoot = Join-Path $RuntimeRoot 'moodle-docker'
$MoodleDataRoot = Join-Path $RuntimeRoot 'moodledata'
$SecretsPath = Join-Path $RuntimeRoot 'moodle-secrets.json'
$TokenPath = Join-Path $RuntimeRoot 'moodle-token.json'
$ImageEvidencePath = Join-Path $RuntimeRoot 'moodle-images.json'
$InstallEvidencePath = Join-Path $RuntimeRoot 'moodle-install.json'
$GitHooksRoot = Join-Path $RuntimeRoot 'moodle-git-hooks'
$VersionsPath = Join-Path $RepoRoot 'infra/moodle/versions.psd1'
$Versions = $null
if ($Action -eq 'Bootstrap') {
    $Versions = Read-MoodleVersions -Path $VersionsPath
}
$ProjectName = 'moddle_autotask_moodle'
$WebPort = '127.0.0.1:8000'
$WebBaseUrl = 'http://127.0.0.1:8000'
$script:GitBashPath = $null

function Fail {
    param([Parameter(Mandatory = $true)][string]$Message)
    throw $Message
}

function Assert-PathNotReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Fail "Refusing reparse-point runtime path: $Path"
        }
    }
}

function Assert-ContainedNonReparsePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $repoFullPath = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $targetFullPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $repoFullPath + [System.IO.Path]::DirectorySeparatorChar
    if ($targetFullPath -ne $repoFullPath -and -not $targetFullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        Fail "Refusing path outside repository: $targetFullPath"
    }
    $current = $targetFullPath
    while ($true) {
        Assert-PathNotReparsePoint -Path $current
        if ($current -eq $repoFullPath) {
            break
        }
        $parent = Split-Path -Parent $current
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $current) {
            Fail "Could not validate repository ancestor chain for: $targetFullPath"
        }
        $current = $parent
    }
}

function Assert-SafeRuntimePaths {
    Assert-ContainedNonReparsePath -Path $RepoRoot
    Assert-ContainedNonReparsePath -Path $RuntimeRoot
    Assert-ContainedNonReparsePath -Path $MoodleRoot
    Assert-ContainedNonReparsePath -Path $MoodleDockerRoot
    Assert-ContainedNonReparsePath -Path $MoodleDataRoot
}

function Assert-SafeWriteTarget {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-SafeRuntimePaths
    Assert-ContainedNonReparsePath -Path $Path
}

function Assert-NoMoodleDockerLocalOverride {
    Assert-SafeRuntimePaths
    $localOverride = Join-Path $MoodleDockerRoot 'local.yml'
    Assert-SafeWriteTarget -Path $localOverride
    if (Test-Path -LiteralPath $localOverride) {
        Fail 'Refusing moodle-docker/local.yml override. Remove it or run Reset -Force.'
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$Description
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "$Description failed with exit code $LASTEXITCODE."
    }
}

function Get-GitBashPath {
    if ($null -ne $script:GitBashPath) {
        return $script:GitBashPath
    }

    $candidates = @(
        (Join-Path ${env:ProgramFiles} 'Git\bin\bash.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Git\bin\bash.exe'),
        'C:\Program Files\Git\bin\bash.exe'
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
    $command = Get-Command 'bash.exe' -ErrorAction SilentlyContinue
    if ($null -ne $command -and $command.Source -match 'Git') {
        $candidates += $command.Source
    }
    $script:GitBashPath = $candidates | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($script:GitBashPath)) {
        Fail 'Git Bash was not found. Install Git for Windows (including Git Bash), then retry.'
    }
    return $script:GitBashPath
}

function ConvertTo-BashLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)
    $quote = [string][char]39
    return $quote + $Value.Replace($quote, ($quote + '"' + $quote + '"' + $quote)) + $quote
}

function ConvertTo-BashPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $bash = Get-GitBashPath
    $output = & $bash -lc ('cygpath -u -- ' + (ConvertTo-BashLiteral -Value $Path))
    if ($LASTEXITCODE -ne 0) {
        Fail 'Git Bash could not convert a Windows path for moodle-docker.'
    }
    return ($output | Select-Object -First 1).Trim()
}

function Assert-DockerDaemon {
    $docker = Get-Command 'docker.exe' -ErrorAction SilentlyContinue
    if ($null -eq $docker) {
        $docker = Get-Command 'docker' -ErrorAction SilentlyContinue
    }
    if ($null -eq $docker) {
        Fail 'Docker CLI was not found. Install Docker Desktop and ensure docker is on PATH.'
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $docker.Source
    $startInfo.Arguments = 'info --format "{{.ServerVersion}}"'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    if (-not $process.WaitForExit(15000)) {
        $process.Kill()
        Fail 'Docker daemon did not respond within 15 seconds. Start Docker Desktop manually, wait for it to be running, then retry; this script will not start it for you.'
    }
    $daemonVersion = $process.StandardOutput.ReadToEnd().Trim()
    $daemonError = $process.StandardError.ReadToEnd().Trim()
    if ($process.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($daemonVersion)) {
        Fail 'Docker daemon is unavailable. Start Docker Desktop manually, wait for it to be running, then retry; this script will not start it for you.'
    }
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Assert-Git {
    $git = Get-Command 'git.exe' -ErrorAction SilentlyContinue
    if ($null -eq $git) {
        $git = Get-Command 'git' -ErrorAction SilentlyContinue
    }
    if ($null -eq $git) {
        Fail 'Git was not found. Install Git for Windows, then retry.'
    }
    return $git.Source
}

function Get-TrustedGitHooksPath {
    Assert-SafeWriteTarget -Path $GitHooksRoot
    if ((Test-Path -LiteralPath $GitHooksRoot) -and -not (Test-Path -LiteralPath $GitHooksRoot -PathType Container)) {
        Fail "Refusing non-directory Git hooks path: $GitHooksRoot"
    }
    Ensure-Directory -Path $GitHooksRoot
    Assert-ContainedNonReparsePath -Path $GitHooksRoot
    if (@(Get-ChildItem -LiteralPath $GitHooksRoot -Force).Count -ne 0) {
        Fail "Refusing nonempty Git hooks path: $GitHooksRoot"
    }
    return $GitHooksRoot
}

function Get-GitConfigEntries {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Scope
    )
    $hooksPath = Get-TrustedGitHooksPath
    $keys = & $GitPath --no-replace-objects -c "core.hooksPath=$hooksPath" -c 'core.fsmonitor=false' -c 'credential.helper=' -C $Repository config --includes $Scope --name-only --list
    if ($LASTEXITCODE -ne 0) {
        Fail "Could not inspect canonical Git $Scope configuration in $Repository."
    }
    $entries = @()
    foreach ($keyValue in @($keys | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $key = ([string]$keyValue).Trim().ToLowerInvariant()
        $values = & $GitPath --no-replace-objects -c "core.hooksPath=$hooksPath" -c 'core.fsmonitor=false' -c 'credential.helper=' -C $Repository config --includes $Scope --get-all $key
        if ($LASTEXITCODE -ne 0) {
            Fail "Could not read canonical Git setting $key in $Repository."
        }
        $entries += [PSCustomObject]@{
            Key = $key
            Values = @($values | ForEach-Object { [string]$_ })
        }
    }
    return $entries
}

function Assert-AllowedGitConfigEntries {
    param(
        [Parameter(Mandatory = $true)][object[]]$Entries,
        [Parameter(Mandatory = $true)][hashtable]$Allowed,
        [Parameter(Mandatory = $true)][string]$Scope,
        [Parameter(Mandatory = $true)][string]$Repository
    )
    foreach ($entry in $Entries) {
        if (-not $Allowed.ContainsKey($entry.Key)) {
            Fail "Refusing unsupported Git $Scope config key $($entry.Key) in $Repository."
        }
        $expectedValues = @($Allowed[$entry.Key])
        $actualValues = @($entry.Values)
        if ($entry.Key -in @('core.filemode', 'core.symlinks', 'core.ignorecase', 'core.precomposeunicode')) {
            if ($actualValues.Count -ne 1 -or $actualValues[0] -notin $expectedValues) {
                Fail "Refusing unexpected value for Git $Scope config key $($entry.Key) in $Repository."
            }
            continue
        }
        if ($actualValues.Count -ne $expectedValues.Count) {
            Fail "Refusing unexpected value count for Git $Scope config key $($entry.Key) in $Repository."
        }
        for ($index = 0; $index -lt $expectedValues.Count; $index++) {
            if ($actualValues[$index] -ne $expectedValues[$index]) {
                Fail "Refusing unexpected value for Git $Scope config key $($entry.Key) in $Repository."
            }
        }
    }
}

function Assert-SafeGitIndexFlags {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$GitPath
    )
    $hooksPath = Get-TrustedGitHooksPath
    $rawEntries = & $GitPath --no-replace-objects -c "core.hooksPath=$hooksPath" -c 'core.fsmonitor=false' -C $Repository ls-files -v -z
    if ($LASTEXITCODE -ne 0) {
        Fail "Could not inspect Git index flags in $Repository."
    }
    $entries = ([string]::Concat(@($rawEntries))) -split [char]0
    foreach ($entry in $entries) {
        if ([string]::IsNullOrEmpty($entry)) {
            continue
        }
        if ($entry.Length -lt 3 -or $entry[1] -ne ' ') {
            Fail "Could not parse Git index flags in $Repository."
        }
        $tag = $entry[0]
        if ([char]::IsLower($tag) -or $tag -eq 'S') {
            Fail "Refusing Git index visibility flag '$tag' in runtime source: $Repository"
        }
    }
}

function Assert-NoGitReplacementObjects {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$GitPath
    )
    $replaceDirectory = Join-Path $Repository '.git/refs/replace'
    Assert-ContainedNonReparsePath -Path $replaceDirectory
    $hooksPath = Get-TrustedGitHooksPath
    $replacementRefs = & $GitPath --no-replace-objects -c "core.hooksPath=$hooksPath" -c 'core.fsmonitor=false' -C $Repository for-each-ref '--format=%(refname)' refs/replace
    if ($LASTEXITCODE -ne 0) {
        Fail "Could not inspect Git replacement objects in $Repository."
    }
    if (@($replacementRefs | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -ne 0) {
        Fail "Refusing Git replacement objects in runtime source: $Repository"
    }
}

function Assert-SafeGitControlState {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$ExpectedOrigin,
        [Parameter(Mandatory = $true)][string]$GitPath
    )
    $gitDirectory = Join-Path $Repository '.git'
    $hooksDirectory = Join-Path $gitDirectory 'hooks'
    $infoDirectory = Join-Path $gitDirectory 'info'
    $attributesPath = Join-Path $infoDirectory 'attributes'
    $excludePath = Join-Path $infoDirectory 'exclude'
    $configPath = Join-Path $gitDirectory 'config'
    $worktreeConfigPath = Join-Path $gitDirectory 'config.worktree'
    foreach ($path in @($gitDirectory, $hooksDirectory, $infoDirectory, $attributesPath, $excludePath, $configPath, $worktreeConfigPath)) {
        Assert-ContainedNonReparsePath -Path $path
    }
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container)) {
        Fail "Runtime source is not a complete Git checkout: $Repository"
    }
    $activeHooks = @(
        'applypatch-msg', 'pre-applypatch', 'post-applypatch', 'pre-commit', 'pre-merge-commit',
        'prepare-commit-msg', 'commit-msg', 'post-commit', 'pre-rebase', 'post-checkout', 'post-merge',
        'pre-push', 'pre-auto-gc', 'post-rewrite', 'pre-receive', 'update', 'proc-receive',
        'post-receive', 'post-update', 'reference-transaction', 'push-to-checkout'
    )
    foreach ($hook in $activeHooks) {
        $hookPath = Join-Path $hooksDirectory $hook
        Assert-ContainedNonReparsePath -Path $hookPath
        if (Test-Path -LiteralPath $hookPath) {
            Fail "Refusing active Git hook in runtime source: $hookPath"
        }
    }
    if (Test-Path -LiteralPath $attributesPath -PathType Leaf) {
        if ((Get-Item -LiteralPath $attributesPath -Force).Length -ne 0) {
            Fail "Refusing nonempty Git attributes control file: $attributesPath"
        }
    } elseif (Test-Path -LiteralPath $attributesPath) {
        Fail "Refusing non-file Git attributes control path: $attributesPath"
    }
    if (Test-Path -LiteralPath $excludePath -PathType Leaf) {
        $excludeEntries = @(Get-Content -LiteralPath $excludePath | Where-Object {
                -not [string]::IsNullOrWhiteSpace($_) -and -not $_.TrimStart().StartsWith('#')
            })
        if ($excludeEntries.Count -ne 0) {
            Fail "Refusing Git info/exclude entries that could hide runtime payloads: $excludePath"
        }
    } elseif (Test-Path -LiteralPath $excludePath) {
        Fail "Refusing non-file Git info/exclude control path: $excludePath"
    }
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        Fail "Runtime source is missing Git config: $Repository"
    }
    Assert-SafeGitIndexFlags -Repository $Repository -GitPath $GitPath
    Assert-NoGitReplacementObjects -Repository $Repository -GitPath $GitPath
    $localEntries = @(Get-GitConfigEntries -Repository $Repository -GitPath $GitPath -Scope '--local')
    $allowedLocal = @{
        'core.repositoryformatversion' = @('0')
        'core.filemode' = @('true', 'false')
        'core.bare' = @('false')
        'core.logallrefupdates' = @('true')
        'core.symlinks' = @('true', 'false')
        'core.ignorecase' = @('true', 'false')
        'core.precomposeunicode' = @('true', 'false')
        'remote.origin.url' = @($ExpectedOrigin)
        'remote.origin.fetch' = @('+refs/heads/*:refs/remotes/origin/*')
        'branch.main.remote' = @('origin')
        'branch.main.merge' = @('refs/heads/main')
    }
    Assert-AllowedGitConfigEntries -Entries $localEntries -Allowed $allowedLocal -Scope 'local' -Repository $Repository

    if (Test-Path -LiteralPath $worktreeConfigPath) {
        if (-not (Test-Path -LiteralPath $worktreeConfigPath -PathType Leaf)) {
            Fail "Refusing non-file Git worktree config: $worktreeConfigPath"
        }
        $worktreeEntries = @(Get-GitConfigEntries -Repository $Repository -GitPath $GitPath -Scope '--worktree')
        Assert-AllowedGitConfigEntries -Entries $worktreeEntries -Allowed @{} -Scope 'worktree' -Repository $Repository
        Fail "Refusing unexpected Git worktree config file: $worktreeConfigPath"
    }
}

function Invoke-GitRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $hooksPath = Get-TrustedGitHooksPath
    Invoke-External -FilePath $GitPath -Arguments (@('--no-replace-objects', '-c', "core.hooksPath=$hooksPath", '-c', 'core.fsmonitor=false') + $Arguments) -Description $Description
}

function Get-GitRevision {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Revision,
        [Parameter(Mandatory = $true)][string]$GitPath
    )
    $hooksPath = Get-TrustedGitHooksPath
    $result = & $GitPath --no-replace-objects -c "core.hooksPath=$hooksPath" -c 'core.fsmonitor=false' -C $Repository rev-parse $Revision
    if ($LASTEXITCODE -ne 0) {
        Fail "Unable to resolve $Revision in $Repository."
    }
    return ($result | Select-Object -First 1).Trim().ToLowerInvariant()
}

function Ensure-GitRepository {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RepositoryUrl,
        [Parameter(Mandatory = $true)][string]$GitPath
    )
    if (-not (Test-Path -LiteralPath (Join-Path $Path '.git') -PathType Container)) {
        if (Test-Path -LiteralPath $Path) {
            Fail "Runtime source path exists but is not a Git repository: $Path. Use Reset -Force after confirming it is disposable."
        }
        Invoke-GitRuntime -GitPath $GitPath -Arguments @('clone', '--no-checkout', $RepositoryUrl, $Path) -Description "Cloning $RepositoryUrl"
        Assert-SafeGitControlState -Repository $Path -ExpectedOrigin $RepositoryUrl -GitPath $GitPath
        return
    }
    Assert-SafeGitControlState -Repository $Path -ExpectedOrigin $RepositoryUrl -GitPath $GitPath
}

function Assert-GitOrigin {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl,
        [Parameter(Mandatory = $true)][string]$GitPath
    )
    $hooksPath = Get-TrustedGitHooksPath
    $origin = & $GitPath --no-replace-objects -c "core.hooksPath=$hooksPath" -c 'core.fsmonitor=false' -C $Repository remote get-url origin
    if ($LASTEXITCODE -ne 0 -or ($origin | Select-Object -First 1).Trim() -ne $ExpectedUrl) {
        Fail "Runtime source origin does not match required official repository: $ExpectedUrl"
    }
}

function Assert-CleanRuntimeSource {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Kind,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter()][switch]$RequireGeneratedMoodleConfig
    )
    $hooksPath = Get-TrustedGitHooksPath
    $status = & $GitPath --no-replace-objects -c "core.hooksPath=$hooksPath" -c 'core.fsmonitor=false' -C $Repository status --porcelain --ignored --untracked-files=all
    if ($LASTEXITCODE -ne 0) {
        Fail "Could not inspect $Kind runtime source integrity."
    }
    $entries = @($status | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($Kind -eq 'moodle-docker') {
        if ($entries.Count -ne 0) {
            Fail 'moodle-docker runtime source is dirty or contains untracked files (including local.yml). Run Reset -Force.'
        }
        return
    }
    $allowed = @('!! config.php')
    $unexpected = @($entries | Where-Object { $_ -notin $allowed })
    if ($unexpected.Count -ne 0) {
        Fail 'Moodle runtime source has tracked changes, unexpected untracked overrides, or ignored payloads. Run Reset -Force.'
    }
    if ($RequireGeneratedMoodleConfig) {
        if ($entries.Count -ne 1 -or $entries[0] -ne '!! config.php') {
            Fail 'Moodle generated config.php is missing or not ignored as expected. Run Bootstrap or Reset -Force.'
        }
        Assert-GeneratedMoodleConfig
    }
}

function Assert-GeneratedMoodleConfig {
    $template = Join-Path $MoodleDockerRoot 'config.docker-template.php'
    $config = Join-Path $MoodleRoot 'config.php'
    Assert-ContainedNonReparsePath -Path $template
    Assert-ContainedNonReparsePath -Path $config
    if (-not (Test-Path -LiteralPath $template -PathType Leaf) -or -not (Test-Path -LiteralPath $config -PathType Leaf)) {
        Fail 'Pinned Moodle Docker template or generated Moodle config.php is missing. Run Bootstrap again.'
    }
    $templateHash = Get-FileSha256 -Path $template
    $configHash = Get-FileSha256 -Path $config
    if ($templateHash -ne $configHash) {
        Fail 'Generated Moodle config.php does not exactly match pinned config.docker-template.php. Run Bootstrap or Reset -Force.'
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return -join ($algorithm.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') })
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Assert-MoodleDockerWrapperTrust {
    param(
        [Parameter(Mandatory = $true)][string]$Wrapper,
        [Parameter()][switch]$RequireMoodleSource
    )
    Assert-ContainedNonReparsePath -Path $MoodleDockerRoot
    Assert-ContainedNonReparsePath -Path (Join-Path $MoodleDockerRoot '.git')
    Assert-ContainedNonReparsePath -Path $Wrapper
    if (-not (Test-Path -LiteralPath (Join-Path $MoodleDockerRoot '.git') -PathType Container)) {
        Fail 'moodle-docker is not a complete Git checkout. Run Bootstrap first.'
    }
    if (-not (Test-Path -LiteralPath $Wrapper -PathType Leaf)) {
        Fail 'The pinned moodle-docker wrapper is missing. Run Bootstrap first.'
    }
    $git = Assert-Git
    Assert-SafeGitControlState -Repository $MoodleDockerRoot -ExpectedOrigin $Versions.MoodleDockerRepository -GitPath $git
    Assert-GitOrigin -Repository $MoodleDockerRoot -ExpectedUrl $Versions.MoodleDockerRepository -GitPath $git
    Assert-CleanRuntimeSource -Repository $MoodleDockerRoot -Kind 'moodle-docker' -GitPath $git

    if ($RequireMoodleSource) {
        Assert-ContainedNonReparsePath -Path $MoodleRoot
        Assert-ContainedNonReparsePath -Path (Join-Path $MoodleRoot '.git')
        if (-not (Test-Path -LiteralPath (Join-Path $MoodleRoot '.git') -PathType Container)) {
            Fail 'Moodle is not a complete Git checkout. Run Bootstrap first.'
        }
        Assert-SafeGitControlState -Repository $MoodleRoot -ExpectedOrigin $Versions.MoodleRepository -GitPath $git
        Assert-GitOrigin -Repository $MoodleRoot -ExpectedUrl $Versions.MoodleRepository -GitPath $git
        Assert-CleanRuntimeSource -Repository $MoodleRoot -Kind 'moodle' -GitPath $git -RequireGeneratedMoodleConfig
        $moodleHead = Get-GitRevision -Repository $MoodleRoot -Revision 'HEAD' -GitPath $git
        if ($moodleHead -ne $Versions.MoodlePeeledCommit) {
            Fail 'Moodle HEAD does not match the required peeled commit.'
        }
    }

    $dockerHead = Get-GitRevision -Repository $MoodleDockerRoot -Revision 'HEAD' -GitPath $git
    if ($dockerHead -ne $Versions.MoodleDockerCommit) {
        Fail 'moodle-docker HEAD does not match the required pin.'
    }
}

function Assert-NormalMoodleDockerExecutionTrust {
    $wrapper = Join-Path $MoodleDockerRoot 'bin/moodle-docker-compose'
    Assert-MoodleDockerWrapperTrust -Wrapper $wrapper -RequireMoodleSource
}

function Initialize-Sources {
    $git = Assert-Git
    Ensure-Directory -Path $RuntimeRoot
    Ensure-GitRepository -Path $MoodleDockerRoot -RepositoryUrl $Versions.MoodleDockerRepository -GitPath $git
    Assert-SafeGitControlState -Repository $MoodleDockerRoot -ExpectedOrigin $Versions.MoodleDockerRepository -GitPath $git
    Assert-GitOrigin -Repository $MoodleDockerRoot -ExpectedUrl $Versions.MoodleDockerRepository -GitPath $git
    Invoke-GitRuntime -GitPath $git -Arguments @('-C', $MoodleDockerRoot, 'fetch', '--force', 'origin', $Versions.MoodleDockerCommit) -Description 'Fetching pinned moodle-docker commit'
    $dockerCommit = Get-GitRevision -Repository $MoodleDockerRoot -Revision $Versions.MoodleDockerCommit -GitPath $git
    if ($dockerCommit -ne $Versions.MoodleDockerCommit) {
        Fail 'The fetched moodle-docker commit does not match the required pin.'
    }
    Assert-SafeGitControlState -Repository $MoodleDockerRoot -ExpectedOrigin $Versions.MoodleDockerRepository -GitPath $git
    Invoke-GitRuntime -GitPath $git -Arguments @('-C', $MoodleDockerRoot, 'checkout', '--detach', $Versions.MoodleDockerCommit) -Description 'Checking out pinned moodle-docker commit'
    Assert-CleanRuntimeSource -Repository $MoodleDockerRoot -Kind 'moodle-docker' -GitPath $git

    Ensure-GitRepository -Path $MoodleRoot -RepositoryUrl $Versions.MoodleRepository -GitPath $git
    Assert-SafeGitControlState -Repository $MoodleRoot -ExpectedOrigin $Versions.MoodleRepository -GitPath $git
    Assert-GitOrigin -Repository $MoodleRoot -ExpectedUrl $Versions.MoodleRepository -GitPath $git
    Invoke-GitRuntime -GitPath $git -Arguments @('-C', $MoodleRoot, 'fetch', '--force', 'origin', "refs/tags/$($Versions.MoodleRelease):refs/tags/$($Versions.MoodleRelease)") -Description 'Fetching pinned Moodle release tag'
    $tagObject = Get-GitRevision -Repository $MoodleRoot -Revision "refs/tags/$($Versions.MoodleRelease)" -GitPath $git
    $peeledCommit = Get-GitRevision -Repository $MoodleRoot -Revision "refs/tags/$($Versions.MoodleRelease)^{}" -GitPath $git
    if ($tagObject -ne $Versions.MoodleTagObject -or $peeledCommit -ne $Versions.MoodlePeeledCommit) {
        Fail 'The fetched Moodle v5.2.1 tag object or peeled commit does not match the required pins.'
    }
    Assert-SafeGitControlState -Repository $MoodleRoot -ExpectedOrigin $Versions.MoodleRepository -GitPath $git
    Invoke-GitRuntime -GitPath $git -Arguments @('-C', $MoodleRoot, 'checkout', '--detach', $Versions.MoodlePeeledCommit) -Description 'Checking out pinned Moodle commit'
    $head = Get-GitRevision -Repository $MoodleRoot -Revision 'HEAD' -GitPath $git
    if ($head -ne $Versions.MoodlePeeledCommit) {
        Fail 'Moodle checkout HEAD does not match the required peeled commit.'
    }
    Assert-CleanRuntimeSource -Repository $MoodleRoot -Kind 'moodle' -GitPath $git
}

function Get-MoodleLayout {
    $installCli = Join-Path $MoodleRoot 'admin/cli/install_database.php'
    $configCli = Join-Path $MoodleRoot 'admin/cli/cfg.php'
    $resetPasswordCli = Join-Path $MoodleRoot 'admin/cli/reset_password.php'
    if ((Test-Path -LiteralPath $installCli -PathType Leaf) -and
        (Test-Path -LiteralPath $configCli -PathType Leaf) -and
        (Test-Path -LiteralPath $resetPasswordCli -PathType Leaf)) {
        return [PSCustomObject]@{
            CoreCliRoot = '/var/www/html'
            CoreConfigPath = '/var/www/html/config.php'
            LocalConfigPath = Join-Path $MoodleRoot 'config.php'
            EndpointCandidates = @($WebBaseUrl, "$WebBaseUrl/public")
        }
    }
    Fail 'Pinned Moodle v5.2.1 layout is incomplete: expected root admin CLI files.'
}

function New-LocalPassword {
    $classes = @(
        'abcdefghijkmnopqrstuvwxyz'.ToCharArray(),
        'ABCDEFGHJKLMNPQRSTUVWXYZ'.ToCharArray(),
        '23456789'.ToCharArray(),
        '!@#%_-'.ToCharArray()
    )
    $alphabet = @($classes | ForEach-Object { $_ })
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $characters = New-Object 'System.Collections.Generic.List[char]'
        foreach ($class in $classes) {
            do {
                $bytes = New-Object byte[] 1
                $rng.GetBytes($bytes)
            } while ($bytes[0] -ge (256 - (256 % $class.Length)))
            $characters.Add($class[$bytes[0] % $class.Length])
        }
        while ($characters.Count -lt 36) {
            do {
                $bytes = New-Object byte[] 1
                $rng.GetBytes($bytes)
            } while ($bytes[0] -ge (256 - (256 % $alphabet.Length)))
            $characters.Add($alphabet[$bytes[0] % $alphabet.Length])
        }
        for ($index = $characters.Count - 1; $index -gt 0; $index--) {
            do {
                $bytes = New-Object byte[] 1
                $rng.GetBytes($bytes)
            } while ($bytes[0] -ge (256 - (256 % ($index + 1))))
            $swap = $bytes[0] % ($index + 1)
            $temporary = $characters[$index]
            $characters[$index] = $characters[$swap]
            $characters[$swap] = $temporary
        }
        return -join $characters
    } finally {
        $rng.Dispose()
    }
}

function Get-LocalSecrets {
    Ensure-Directory -Path $RuntimeRoot
    if (Test-Path -LiteralPath $SecretsPath -PathType Leaf) {
        return Get-Content -LiteralPath $SecretsPath -Raw | ConvertFrom-Json
    }
    $secrets = [PSCustomObject]@{
        adminPassword = New-LocalPassword
        studentPassword = New-LocalPassword
    }
    Assert-SafeWriteTarget -Path $SecretsPath
    $secrets | ConvertTo-Json | Set-Content -LiteralPath $SecretsPath -Encoding UTF8 -NoNewline
    return $secrets
}

function Copy-DockerConfiguration {
    param([Parameter(Mandatory = $true)]$Layout)
    $template = Join-Path $MoodleDockerRoot 'config.docker-template.php'
    if (-not (Test-Path -LiteralPath $template -PathType Leaf)) {
        Fail 'Pinned moodle-docker config.docker-template.php was not found.'
    }
    Assert-SafeWriteTarget -Path $Layout.LocalConfigPath
    Copy-Item -LiteralPath $template -Destination $Layout.LocalConfigPath -Force
}

function Get-MoodleDockerEnvironment {
    param([Parameter()][string]$WwwRoot = $MoodleRoot)
    Assert-ContainedNonReparsePath -Path $WwwRoot
    if (-not (Test-Path -LiteralPath $WwwRoot -PathType Container)) {
        Fail "Refusing missing Moodle Docker WWWROOT: $WwwRoot"
    }
    return @{
        'MOODLE_DOCKER_WWWROOT' = ConvertTo-BashPath -Path $WwwRoot
        'MOODLE_DOCKER_DB' = 'pgsql'
        'COMPOSE_PROJECT_NAME' = $ProjectName
        'MOODLE_DOCKER_WEB_PORT' = $WebPort
    }
}

function Invoke-MoodleDocker {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter()][switch]$SkipMoodleSourceTrust,
        [Parameter()][switch]$UseRuntimeWwwRoot
    )
    if ($UseRuntimeWwwRoot -and -not $SkipMoodleSourceTrust) {
        Fail 'The runtime WWWROOT override is reserved for trusted Reset cleanup only.'
    }
    Assert-NoMoodleDockerLocalOverride
    $wrapper = Join-Path $MoodleDockerRoot 'bin/moodle-docker-compose'
    if ($SkipMoodleSourceTrust) {
        Assert-MoodleDockerWrapperTrust -Wrapper $wrapper
    } else {
        Assert-MoodleDockerWrapperTrust -Wrapper $wrapper -RequireMoodleSource
    }
    $bash = Get-GitBashPath
    $wwwRoot = if ($UseRuntimeWwwRoot) { $RuntimeRoot } else { $MoodleRoot }
    $environment = Get-MoodleDockerEnvironment -WwwRoot $wwwRoot
    $exports = foreach ($entry in $environment.GetEnumerator()) {
        'export ' + $entry.Key + '=' + (ConvertTo-BashLiteral -Value $entry.Value)
    }
    $command = 'set -e; ' + ($exports -join '; ') + '; cd ' + (ConvertTo-BashLiteral -Value (ConvertTo-BashPath -Path $MoodleDockerRoot)) + '; ./bin/moodle-docker-compose'
    foreach ($argument in $Arguments) {
        $command += ' ' + (ConvertTo-BashLiteral -Value $argument)
    }
    $output = & $bash -lc $command
    if ($LASTEXITCODE -ne 0) {
        Fail "moodle-docker-compose command failed with exit code $LASTEXITCODE."
    }
    return $output
}

function Invoke-MoodleDockerWaitForDb {
    Assert-NoMoodleDockerLocalOverride
    $wrapper = Join-Path $MoodleDockerRoot 'bin/moodle-docker-wait-for-db'
    Assert-MoodleDockerWrapperTrust -Wrapper $wrapper -RequireMoodleSource
    $bash = Get-GitBashPath
    $environment = Get-MoodleDockerEnvironment
    $exports = foreach ($entry in $environment.GetEnumerator()) {
        'export ' + $entry.Key + '=' + (ConvertTo-BashLiteral -Value $entry.Value)
    }
    $command = 'set -e; ' + ($exports -join '; ') + '; cd ' + (ConvertTo-BashLiteral -Value (ConvertTo-BashPath -Path $MoodleDockerRoot)) + '; ./bin/moodle-docker-wait-for-db'
    $output = & $bash -lc $command
    if ($LASTEXITCODE -ne 0) {
        Fail "moodle-docker-wait-for-db failed with exit code $LASTEXITCODE."
    }
    return $output
}

function Write-ImageEvidence {
    $images = Invoke-MoodleDocker -Arguments @('images', '--format', 'json')
    Assert-SafeWriteTarget -Path $ImageEvidencePath
    $images | Set-Content -LiteralPath $ImageEvidencePath -Encoding UTF8
}

function Create-Stack {
    Invoke-MoodleDocker -Arguments @('up', '-d') | Write-Output
    Invoke-MoodleDockerWaitForDb | Out-Null
    Write-ImageEvidence
}

function Test-StackContainersExist {
    $containers = Invoke-MoodleDocker -Arguments @('ps', '--all', '--quiet')
    return -not [string]::IsNullOrWhiteSpace(($containers -join "`n"))
}

function Resume-Stack {
    if (-not (Test-StackContainersExist)) {
        Fail 'Local Moodle containers do not exist. Run Bootstrap to create the site before using Up.'
    }
    Invoke-MoodleDocker -Arguments @('start') | Write-Output
    Invoke-MoodleDockerWaitForDb | Out-Null
    Write-ImageEvidence
}

function Invoke-MoodleDatabaseQuery {
    param([Parameter(Mandatory = $true)][string]$Query)
    $result = Invoke-MoodleDocker -Arguments @(
        'exec', '-T', 'db', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'moodle', '-d', 'moodle', '-tAc', $Query
    )
    return ($result -join "`n").Trim()
}

function Get-PinnedMoodleConfigTable {
    $prefix = [string]$Versions.MoodleDockerTablePrefix
    $configTable = [string]$Versions.MoodleDockerConfigTable
    if ($prefix -ne 'm_' -or $configTable -ne "${prefix}config") {
        Fail 'Pinned moodle-docker table prefix is not the expected m_; refusing to construct a database identifier.'
    }
    return $configTable
}

function Test-SiteInstalled {
    $configTable = Get-PinnedMoodleConfigTable
    $tableState = Invoke-MoodleDatabaseQuery -Query "SELECT CASE WHEN to_regclass('public.$configTable') IS NULL THEN 'absent' ELSE 'table-present' END;"
    if ($tableState -eq 'absent') {
        return $false
    }
    if ($tableState -ne 'table-present') {
        Fail 'Could not determine local Moodle installation state from PostgreSQL; refusing to run installation.'
    }
    $prefix = [string]$Versions.MoodleDockerTablePrefix
    $completionState = Invoke-MoodleDatabaseQuery -Query "SELECT CASE WHEN EXISTS (SELECT 1 FROM $configTable WHERE name = 'version') AND EXISTS (SELECT 1 FROM $configTable WHERE name = 'rolesactive' AND value = '1') AND EXISTS (SELECT 1 FROM ${prefix}user WHERE username = 'admin') AND EXISTS (SELECT 1 FROM ${prefix}course WHERE id = 1) AND NOT EXISTS (SELECT 1 FROM $configTable WHERE name IN ('upgraderunning', 'upgrading') AND value NOT IN ('', '0')) THEN 'complete' ELSE 'partial' END;"
    if ($completionState -eq 'complete') {
        return $true
    }
    if ($completionState -eq 'partial') {
        Fail 'Local Moodle database has incomplete installation or upgrade markers; refusing to rerun installation. Run Reset -Force.'
    }
    Fail 'Could not verify local Moodle installation completion; refusing to run installation.'
}

function Write-InstallEvidence {
    Assert-SafeWriteTarget -Path $InstallEvidencePath
    [PSCustomObject]@{
        moodleRelease = $Versions.MoodleRelease
        moodleCommit = $Versions.MoodlePeeledCommit
        verifiedInstalledAt = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $InstallEvidencePath -Encoding UTF8 -NoNewline
}

function Install-Site {
    param([Parameter(Mandatory = $true)]$Layout, [Parameter(Mandatory = $true)]$Secrets)
    if (-not (Test-SiteInstalled)) {
        Invoke-MoodleDocker -Arguments @(
            'exec', '-T', 'webserver', 'php', "$($Layout.CoreCliRoot)/admin/cli/install_database.php",
            '--agree-license', '--lang=en', '--fullname=AutoTask local Moodle', '--shortname=autotask-local',
            '--summary=Development-only Moodle integration test site', '--adminuser=admin',
            "--adminpass=$($Secrets.adminPassword)", '--adminemail=admin@example.test'
        ) | Write-Output
        if (-not (Test-SiteInstalled)) {
            Fail 'Moodle install command completed but PostgreSQL does not report an installed site.'
        }
    }
    Write-InstallEvidence
}

function Set-MoodleConfiguration {
    param([Parameter(Mandatory = $true)]$Layout)
    $settings = @(
        @('enablewebservices', '1')
    )
    foreach ($setting in $settings) {
        Invoke-MoodleDocker -Arguments @('exec', '-T', 'webserver', 'php', "$($Layout.CoreCliRoot)/admin/cli/cfg.php", "--name=$($setting[0])", "--set=$($setting[1])") | Out-Null
    }
}

function Enable-MoodleMobileService {
    param([Parameter(Mandatory = $true)]$Layout)
    $activation = "define('CLI_SCRIPT', true); require '$($Layout.CoreConfigPath)'; \core\session\manager::set_user(get_admin()); require_once(`$CFG->libdir . '/adminlib.php'); `$setting = admin_get_root()->locate('enablemobilewebservice'); if (!(`$setting instanceof admin_setting_enablemobileservice)) { throw new moodle_exception('mobile setting is unavailable'); } `$error = `$setting->write_setting(1); if (`$error !== '') { throw new moodle_exception(`$error); } `$manager = new webservice(); `$service = `$manager->get_external_service_by_shortname(MOODLE_OFFICIAL_MOBILE_SERVICE); `$protocols = (string)get_config('core', 'webserviceprotocols'); `$role = `$DB->get_record('role', array('shortname' => 'user'), '*', MUST_EXIST); `$context = context_system::instance(); `$restallowed = `$DB->record_exists('role_capabilities', array('roleid' => `$role->id, 'contextid' => `$context->id, 'capability' => 'webservice/rest:use', 'permission' => CAP_ALLOW)); if (!`$service || !`$service->enabled || strpos(`$protocols, 'rest') === false || !`$restallowed) { throw new moodle_exception('mobile REST service activation verification failed'); } echo 'mobile-service-ready';"
    $result = Invoke-MoodleDocker -Arguments @('exec', '-T', 'webserver', 'php', '-r', $activation)
    if (($result -join "`n").Trim() -ne 'mobile-service-ready') {
        Fail 'Moodle mobile service activation did not verify successfully.'
    }
}

function Get-FixtureState {
    param([Parameter(Mandatory = $true)]$Layout)
    $probe = "define('CLI_SCRIPT', true); require '$($Layout.CoreConfigPath)'; `$course = `$DB->get_record('course', array('shortname' => 'ASIX-LAB')); `$user = `$DB->get_record('user', array('username' => 'student1')); `$assignment = `$course ? `$DB->record_exists_sql(`"SELECT 1 FROM {assign} a JOIN {course_modules} cm ON cm.instance = a.id AND cm.course = a.course JOIN {modules} m ON m.id = cm.module WHERE a.course = ? AND a.name = ? AND cm.idnumber = ? AND m.name = 'assign'`", array(`$course->id, 'AutoTask assignment', 'autotask-assignment')) : false; `$relatedassignment = `$DB->record_exists_sql(`"SELECT 1 FROM {assign} a JOIN {course_modules} cm ON cm.instance = a.id JOIN {modules} m ON m.id = cm.module WHERE (a.name = ? OR cm.idnumber = ?) AND m.name = 'assign'`", array('AutoTask assignment', 'autotask-assignment')); `$enrolled = (`$course && `$user) ? `$DB->record_exists_sql(`"SELECT 1 FROM {user_enrolments} ue JOIN {enrol} e ON e.id = ue.enrolid JOIN {role_assignments} ra ON ra.userid = ue.userid JOIN {role} r ON r.id = ra.roleid JOIN {context} ctx ON ctx.id = ra.contextid WHERE ue.userid = ? AND e.courseid = ? AND e.enrol = 'manual' AND r.shortname = 'student' AND ctx.contextlevel = ? AND ctx.instanceid = ?`", array(`$user->id, `$course->id, CONTEXT_COURSE, `$course->id)) : false; `$complete = (`$user && `$user->email === 'student1@example.test' && `$course && `$course->fullname === 'ASIX Lab' && `$assignment && `$enrolled); `$any = (`$user || `$course || `$relatedassignment || `$enrolled); echo (`$complete ? 'complete' : (`$any ? 'partial' : 'absent'));"
    $result = Invoke-MoodleDocker -Arguments @('exec', '-T', 'webserver', 'php', '-r', $probe)
    $state = ($result -join "`n").Trim()
    if ($state -notin @('absent', 'complete', 'partial')) {
        Fail 'Could not determine local Moodle fixture state.'
    }
    return $state
}

function Seed-Fixture {
    param([Parameter(Mandatory = $true)]$Layout, [Parameter(Mandatory = $true)]$Secrets)
    $state = Get-FixtureState -Layout $Layout
    if ($state -eq 'partial') {
        Fail 'Local Moodle fixture is partial; refusing to rerun the seed. Run Reset -Force.'
    }
    if ($state -eq 'absent') {
        $seed = "define('CLI_SCRIPT', true); require '$($Layout.CoreConfigPath)'; require_once(`$CFG->dirroot . '/user/lib.php'); require_once(`$CFG->dirroot . '/course/lib.php'); require_once(`$CFG->dirroot . '/course/modlib.php'); global `$DB; `$user = `$DB->get_record('user', array('username' => 'student1')); if (!`$user) { `$newuser = (object)array('username' => 'student1', 'firstname' => 'Student', 'lastname' => 'One', 'email' => 'student1@example.test', 'auth' => 'manual', 'confirmed' => 1, 'mnethostid' => `$CFG->mnet_localhost_id); `$newuser->id = user_create_user(`$newuser, false, false); `$user = `$DB->get_record('user', array('id' => `$newuser->id), '*', MUST_EXIST); } `$course = `$DB->get_record('course', array('shortname' => 'ASIX-LAB')); if (!`$course) { `$course = create_course((object)array('fullname' => 'ASIX Lab', 'shortname' => 'ASIX-LAB', 'category' => 1)); } `$role = `$DB->get_record('role', array('shortname' => 'student'), '*', MUST_EXIST); `$instance = `$DB->get_record('enrol', array('courseid' => `$course->id, 'enrol' => 'manual'), '*', MUST_EXIST); `$manual = enrol_get_plugin('manual'); if (!`$manual) { throw new moodle_exception('manual enrolment plugin unavailable'); } `$manual->enrol_user(`$instance, `$user->id, `$role->id); `$module = `$DB->get_record('modules', array('name' => 'assign'), '*', MUST_EXIST); `$moduleexists = `$DB->record_exists_sql(`"SELECT 1 FROM {assign} a JOIN {course_modules} cm ON cm.instance = a.id AND cm.course = a.course JOIN {modules} m ON m.id = cm.module WHERE a.course = ? AND a.name = ? AND cm.idnumber = ? AND m.name = 'assign' AND cm.module = ?`", array(`$course->id, 'AutoTask assignment', 'autotask-assignment', `$module->id)); if (!`$moduleexists) { `$moduleinfo = (object)array('modulename' => 'assign', 'module' => `$module->id, 'course' => `$course->id, 'section' => 0, 'visible' => 1, 'showdescription' => 0, 'name' => 'AutoTask assignment', 'cmidnumber' => 'autotask-assignment', 'intro' => '', 'introformat' => FORMAT_HTML, 'alwaysshowdescription' => 1, 'submissionattachments' => 0, 'submissiondrafts' => 0, 'requiresubmissionstatement' => 0, 'sendnotifications' => 0, 'sendlatenotifications' => 0, 'sendstudentnotifications' => 0, 'duedate' => 0, 'allowsubmissionsfromdate' => 0, 'grade' => 100, 'completionsubmit' => 0, 'cutoffdate' => 0, 'gradingduedate' => 0, 'teamsubmission' => 0, 'requireallteammemberssubmit' => 0, 'teamsubmissiongroupingid' => 0, 'blindmarking' => 0, 'hidegrader' => 0, 'markingworkflow' => 0, 'markingallocation' => 0, 'preventsubmissionnotingroup' => 0, 'attemptreopenmethod' => 'untilpass', 'maxattempts' => 1, 'markinganonymous' => 0, 'timelimit' => 0, 'gradepenalty' => 0, 'completion' => 0, 'completionexpected' => 0); add_moduleinfo(`$moduleinfo, `$course); } echo 'seeded';"
        $result = Invoke-MoodleDocker -Arguments @('exec', '-T', 'webserver', 'php', '-r', $seed)
        if (($result -join "`n").Trim() -ne 'seeded') {
            Fail 'Moodle inline seed did not complete successfully.'
        }
        $state = Get-FixtureState -Layout $Layout
        if ($state -ne 'complete') {
            Fail 'Moodle seed did not produce the complete expected fixture. Run Reset -Force.'
        }
    }
    if ($state -ne 'complete') {
        Fail 'Could not verify complete Moodle fixture state. Run Reset -Force.'
    }
    Invoke-MoodleDocker -Arguments @('exec', '-T', 'webserver', 'php', "$($Layout.CoreCliRoot)/admin/cli/reset_password.php", '--username=student1', "--password=$($Secrets.studentPassword)") | Out-Null
}

function Invoke-MoodleRest {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Token,
        [Parameter(Mandatory = $true)][string]$Function,
        [Parameter()][hashtable]$Parameters = @{}
    )
    $query = @{ wstoken = $Token; wsfunction = $Function; moodlewsrestformat = 'json' }
    foreach ($entry in $Parameters.GetEnumerator()) {
        $query[$entry.Key] = $entry.Value
    }
    try {
        $response = Invoke-RestMethod -Method Post -Uri "$BaseUrl/webservice/rest/server.php" -Body $query -ContentType 'application/x-www-form-urlencoded'
    } catch {
        Fail "Moodle REST call $Function failed: $($_.Exception.Message)"
    }
    if ($null -ne $response -and ($response.PSObject.Properties.Name -contains 'exception' -or $response.PSObject.Properties.Name -contains 'errorcode' -or $response.PSObject.Properties.Name -contains 'error')) {
        Fail "Moodle REST call $Function returned an error response."
    }
    return $response
}

function Get-MoodleToken {
    param([Parameter(Mandatory = $true)]$Layout, [Parameter(Mandatory = $true)]$Secrets)
    foreach ($baseUrl in $Layout.EndpointCandidates) {
        try {
            $response = Invoke-RestMethod -Method Post -Uri "$baseUrl/login/token.php" -Body @{
                username = 'student1'
                password = $Secrets.studentPassword
                service = 'moodle_mobile_app'
            } -ContentType 'application/x-www-form-urlencoded'
        } catch {
            continue
        }
        if ($null -ne $response -and ($response.PSObject.Properties.Name -contains 'token')) {
            Assert-SafeWriteTarget -Path $TokenPath
            [PSCustomObject]@{ token = $response.token; baseUrl = $baseUrl; obtainedAt = (Get-Date).ToUniversalTime().ToString('o') } |
                ConvertTo-Json | Set-Content -LiteralPath $TokenPath -Encoding UTF8 -NoNewline
            return Get-Content -LiteralPath $TokenPath -Raw | ConvertFrom-Json
        }
        if ($null -ne $response -and ($response.PSObject.Properties.Name -contains 'exception' -or $response.PSObject.Properties.Name -contains 'errorcode' -or $response.PSObject.Properties.Name -contains 'error')) {
            Fail 'Moodle token endpoint returned an error response; verify mobile web services are enabled.'
        }
    }
    Fail 'Could not obtain a Moodle mobile token from either detected local public endpoint.'
}

function Invoke-Smoke {
    if (-not (Test-Path -LiteralPath $TokenPath -PathType Leaf)) {
        Fail 'No local Moodle token exists. Run Bootstrap first.'
    }
    $tokenData = Get-Content -LiteralPath $TokenPath -Raw | ConvertFrom-Json
    if ($null -eq $tokenData -or -not ($tokenData.PSObject.Properties.Name -contains 'baseUrl')) {
        Fail 'Persisted Moodle token is missing its local base URL. Run Bootstrap again.'
    }
    $baseUrl = [string]$tokenData.baseUrl
    $allowedBaseUrls = @($WebBaseUrl, "$WebBaseUrl/public")
    if ($baseUrl -notin $allowedBaseUrls) {
        Fail 'Persisted Moodle token base URL is not an allowed local loopback endpoint. Run Bootstrap again.'
    }
    $siteInfo = Invoke-MoodleRest -BaseUrl $baseUrl -Token $tokenData.token -Function 'core_webservice_get_site_info'
    if ($null -eq $siteInfo.userid) {
        Fail 'Moodle site-info response did not contain userid.'
    }
    $courses = Invoke-MoodleRest -BaseUrl $baseUrl -Token $tokenData.token -Function 'core_enrol_get_users_courses' -Parameters @{ userid = $siteInfo.userid }
    $course = @($courses | Where-Object { $_.shortname -eq 'ASIX-LAB' }) | Select-Object -First 1
    if ($null -eq $course) {
        Fail 'Smoke test could not find enrolled ASIX-LAB course.'
    }
    $contents = Invoke-MoodleRest -BaseUrl $baseUrl -Token $tokenData.token -Function 'core_course_get_contents' -Parameters @{ courseid = $course.id }
    $assignment = @($contents | ForEach-Object { $_.modules } | Where-Object { $_.modname -eq 'assign' -and $_.name -eq 'AutoTask assignment' }) | Select-Object -First 1
    if ($null -eq $assignment) {
        Fail 'Smoke test could not find assign module named AutoTask assignment.'
    }
    Write-Output "Moodle REST smoke passed for ASIX-LAB / AutoTask assignment at $baseUrl."
}

function Assert-ResetTarget {
    param([Parameter(Mandatory = $true)][string]$Path)
    $runtimeFullPath = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $targetFullPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $runtimeFullPath + [System.IO.Path]::DirectorySeparatorChar
    if (-not $targetFullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $targetFullPath) -notlike 'moodle*') {
        Fail "Refusing to remove unsafe reset target: $targetFullPath"
    }
    if (Test-Path -LiteralPath $targetFullPath) {
        $item = Get-Item -LiteralPath $targetFullPath -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Fail "Refusing to remove reparse-point reset target: $targetFullPath"
        }
    }
}

function Remove-MoodleDockerLocalOverrideForReset {
    $localOverride = Join-Path $MoodleDockerRoot 'local.yml'
    Assert-SafeWriteTarget -Path $localOverride
    if (-not (Test-Path -LiteralPath $localOverride)) {
        return
    }
    if (-not (Test-Path -LiteralPath $localOverride -PathType Leaf)) {
        Fail "Refusing non-file moodle-docker/local.yml reset target: $localOverride"
    }
    $item = Get-Item -LiteralPath $localOverride -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Fail "Refusing reparse-point moodle-docker/local.yml reset target: $localOverride"
    }
    Remove-Item -LiteralPath $localOverride -Force
}

function Test-MoodleDockerTrustedForReset {
    try {
        $wrapper = Join-Path $MoodleDockerRoot 'bin/moodle-docker-compose'
        Assert-MoodleDockerWrapperTrust -Wrapper $wrapper
        return $true
    } catch {
        Write-Warning "Skipping Compose down: $($_.Exception.Message) Removing validated runtime data only."
        return $false
    }
}

function Reset-LocalEnvironment {
    if (-not $Force) {
        Fail 'Reset is destructive. Re-run with -Force to remove only this repository .runtime/moodle* data.'
    }
    $script:Versions = Read-MoodleVersions -Path $VersionsPath
    Assert-SafeRuntimePaths
    Remove-MoodleDockerLocalOverrideForReset
    if ((Test-Path -LiteralPath $MoodleDockerRoot -PathType Container) -and (Test-MoodleDockerTrustedForReset)) {
        Assert-DockerDaemon
        Invoke-MoodleDocker -Arguments @('down', '--volumes', '--remove-orphans') -SkipMoodleSourceTrust -UseRuntimeWwwRoot | Write-Output
    }
    $targets = @($MoodleDockerRoot, $MoodleRoot, $MoodleDataRoot, $GitHooksRoot, $SecretsPath, $TokenPath, $ImageEvidencePath, $InstallEvidencePath)
    foreach ($target in $targets) {
        Assert-ResetTarget -Path $target
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
    Write-Output 'Removed validated local Moodle runtime data.'
}

switch ($Action) {
    'Bootstrap' {
        Assert-NoMoodleDockerLocalOverride
        Assert-DockerDaemon
        Initialize-Sources
        $layout = Get-MoodleLayout
        $secrets = Get-LocalSecrets
        Copy-DockerConfiguration -Layout $layout
        Assert-NormalMoodleDockerExecutionTrust
        Create-Stack
        Install-Site -Layout $layout -Secrets $secrets
        Set-MoodleConfiguration -Layout $layout
        Enable-MoodleMobileService -Layout $layout
        Seed-Fixture -Layout $layout -Secrets $secrets
        Get-MoodleToken -Layout $layout -Secrets $secrets | Out-Null
        Invoke-Smoke
    }
    'Up' {
        Assert-NoMoodleDockerLocalOverride
        $script:Versions = Read-MoodleVersions -Path $VersionsPath
        Assert-NormalMoodleDockerExecutionTrust
        Assert-DockerDaemon
        Resume-Stack
    }
    'Down' {
        Assert-NoMoodleDockerLocalOverride
        $script:Versions = Read-MoodleVersions -Path $VersionsPath
        Assert-NormalMoodleDockerExecutionTrust
        Assert-DockerDaemon
        Invoke-MoodleDocker -Arguments @('stop') | Write-Output
    }
    'Status' {
        Assert-NoMoodleDockerLocalOverride
        if (Test-Path -LiteralPath $MoodleDockerRoot -PathType Container) {
            $script:Versions = Read-MoodleVersions -Path $VersionsPath
            Assert-NormalMoodleDockerExecutionTrust
        }
        Assert-DockerDaemon
        if (Test-Path -LiteralPath $MoodleDockerRoot -PathType Container) {
            Invoke-MoodleDocker -Arguments @('ps') | Write-Output
        } else {
            Write-Output 'Local Moodle is not bootstrapped. Run Bootstrap to create it.'
        }
    }
    'Smoke' {
        Assert-NoMoodleDockerLocalOverride
        $script:Versions = Read-MoodleVersions -Path $VersionsPath
        Assert-NormalMoodleDockerExecutionTrust
        Assert-DockerDaemon
        Invoke-Smoke
    }
    'Reset' {
        Reset-LocalEnvironment
    }
}
