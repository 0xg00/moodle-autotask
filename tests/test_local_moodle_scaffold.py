import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "moodle.ps1"
VERSIONS = ROOT / "infra" / "moodle" / "versions.psd1"
DOCS = ROOT / "docs" / "local-moodle.md"
FIXTURE = ROOT / "infra" / "moodle" / "fixture.php"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_runtime_data_is_ignored() -> None:
    assert ".runtime/" in read(ROOT / ".gitignore")


def test_source_pins_are_exact() -> None:
    versions = read(VERSIONS)
    assert "https://github.com/moodlehq/moodle-docker.git" in versions
    assert "f4c2324d32fb74d7753264381f0a9b418b6034b2" in versions
    assert "MoodleDockerTablePrefix = 'm_'" in versions
    assert "MoodleDockerConfigTable = 'm_config'" in versions
    assert "https://github.com/moodle/moodle.git" in versions
    assert "v5.2.1" in versions
    assert "cbc847cd037906036e7047630eee03d5f87d3ff8" in versions
    assert "63e16b757ca8fee05b672a27c23ee27cc8f9fabb" in versions


def test_script_has_required_actions_and_strict_mode() -> None:
    script = read(SCRIPT)
    for action in (
        "Bootstrap",
        "Up",
        "Down",
        "Status",
        "Smoke",
        "AdvanceFixture",
        "Reset",
    ):
        assert f"'{action}'" in script
    assert "Set-StrictMode -Version Latest" in script
    assert "$ErrorActionPreference = 'Stop'" in script


def test_script_uses_pinned_sources_and_validates_tag_and_commit() -> None:
    script = read(SCRIPT)
    assert "fetch', '--force', 'origin', $Versions.MoodleDockerCommit" in script
    assert 'refs/tags/$($Versions.MoodleRelease)^{}' in script
    assert "$Versions.MoodleTagObject" in script
    assert "$Versions.MoodlePeeledCommit" in script
    assert "checkout', '--detach'" in script


def test_script_limits_runtime_and_reset_targets() -> None:
    script = read(SCRIPT)
    assert "$RuntimeRoot = Join-Path $RepoRoot '.runtime'" in script
    assert "Assert-ResetTarget" in script
    assert "-notlike 'moodle*'" in script
    assert "Reset is destructive" in script
    assert "if (-not $Force)" in script


def test_script_uses_official_wrapper_with_validated_private_postgresql_environment() -> None:
    script = read(SCRIPT)
    assert "./bin/moodle-docker-compose" in script
    assert "MOODLE_DOCKER_DB' = 'pgsql'" in script
    assert "COMPOSE_PROJECT_NAME' = $ProjectName" in script
    assert "$ProjectName = 'moddle_autotask_moodle'" in script
    assert "MOODLE_AUTOTASK_BIND_IP" in script
    assert "return '127.0.0.1'" in script
    assert '$WebPort = "${BindIp}:8000"' in script
    assert '$WebBaseUrl = "http://${BindIp}:8000"' in script
    assert "MOODLE_DOCKER_WEB_HOST' = $BindIp" in script
    assert "Git Bash was not found" in script


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is required",
)
def test_bind_ip_validation_allows_only_local_private_or_tailscale_addresses(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    script = read(SCRIPT)
    validation = script[
        script.index("function Get-MoodleBindIp") : script.index("$BindIp = Get-MoodleBindIp")
    ]
    driver = tmp_path / "bind-ip-validation.ps1"
    driver.write_text(
        "function Fail { param([string]$Message) throw $Message }\n"
        + validation
        + "\nfunction Get-NetIPAddress { param($AddressFamily, $ErrorAction) $script:assigned }\n"
        + "function Invoke-BindValidation {\n"
        + "    param([string]$Candidate, [object[]]$Assigned)\n"
        + "    $env:MOODLE_AUTOTASK_BIND_IP = $Candidate\n"
        + "    $script:assigned = @($Assigned)\n"
        + "    try { [PSCustomObject]@{ candidate = $Candidate; value = Get-MoodleBindIp; "
        + "error = $null } }\n"
        + "    catch { [PSCustomObject]@{ candidate = $Candidate; value = $null; "
        + "error = $_.Exception.Message } }\n"
        + "}\n"
        + "$ethernet = [PSCustomObject]@{ IPAddress = '10.0.0.20'; InterfaceAlias = 'Ethernet' }\n"
        + "$rfc172 = [PSCustomObject]@{ IPAddress = '172.16.0.20'; InterfaceAlias = 'Ethernet' }\n"
        + "$rfc192 = [PSCustomObject]@{ IPAddress = '192.168.1.20'; InterfaceAlias = 'Ethernet' }\n"
        + "$tailscale = [PSCustomObject]@{ IPAddress = '100.64.0.20'; "
        + "InterfaceAlias = 'TAILSCALE' }\n"
        + "$wrongTailscale = [PSCustomObject]@{ IPAddress = '100.64.0.20'; "
        + "InterfaceAlias = 'Ethernet' }\n"
        + "$results = @(\n"
        + "    Invoke-BindValidation '' @();\n"
        + "    Invoke-BindValidation '10.0.0.20' @($ethernet);\n"
        + "    Invoke-BindValidation '172.16.0.20' @($rfc172);\n"
        + "    Invoke-BindValidation '192.168.1.20' @($rfc192);\n"
        + "    Invoke-BindValidation '100.64.0.20' @($tailscale);\n"
        + "    Invoke-BindValidation '8.8.8.8' @();\n"
        + "    Invoke-BindValidation '0.0.0.0' @();\n"
        + "    Invoke-BindValidation '010.0.0.20' @($ethernet);\n"
        + "    Invoke-BindValidation '10.0.0.21' @($ethernet);\n"
        + "    Invoke-BindValidation '100.64.0.20' @($wrongTailscale)\n"
        + ")\n$results | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    results = json.loads(result.stdout)
    assert [entry["value"] for entry in results[:5]] == [
        "127.0.0.1",
        "10.0.0.20",
        "172.16.0.20",
        "192.168.1.20",
        "100.64.0.20",
    ]
    assert "RFC1918 private IPv4" in results[5]["error"]
    assert "RFC1918 private IPv4" in results[6]["error"]
    assert "canonical dotted-quad" in results[7]["error"]
    assert "not currently assigned" in results[8]["error"]
    assert "Tailscale interface" in results[9]["error"]


@pytest.mark.skipif(
    shutil.which("powershell") is None
    or not Path(r"C:\Program Files\Git\bin\bash.exe").is_file(),
    reason="Windows PowerShell and Git Bash are required",
)
def test_moodle_docker_preserves_sql_and_container_paths_through_windows_powershell(
    tmp_path: Path,
) -> None:
    docker_root = tmp_path / "móódle-漢字"
    moodle_root = tmp_path / "moodle"
    wrapper = docker_root / "bin" / "moodle-docker-compose"
    host_compose = docker_root / "base.yml"
    wrapper.parent.mkdir(parents=True)
    moodle_root.mkdir()
    host_compose.write_text("services: {}\n", encoding="utf-8")
    wrapper.write_text(
        "#!/usr/bin/env bash\nexec docker.exe \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    prefix = read(SCRIPT).split("\nswitch ($Action) {", maxsplit=1)[0]
    driver = tmp_path / "preserve-sql-quotes.ps1"
    query = (
        "SELECT CASE WHEN to_regclass('public.m_config') IS NULL "
        "THEN 'absent' ELSE 'table-present' END;"
    )
    container_cli = "/var/www/html/áé漢字/admin/cli/install_database.php"
    unicode_value = "--label=áé漢字"
    driver.write_text(
        prefix
        + f"\n$RepoRoot = '{tmp_path.as_posix()}'\n"
        + f"$RuntimeRoot = '{(tmp_path / 'runtime').as_posix()}'\n"
        + f"$MoodleRoot = '{moodle_root.as_posix()}'\n"
        + f"$MoodleDockerRoot = '{docker_root.as_posix()}'\n"
        + "function Assert-NoMoodleDockerLocalOverride {}\n"
        + (
            "function Assert-MoodleDockerWrapperTrust { param($Wrapper, "
            "[switch]$RequireMoodleSource) }\n"
        )
        + "Add-Type -TypeDefinition @'\n"
        + "using System;\n"
        + "using System.Text;\n"
        + "public static class FakeDocker {\n"
        + "    public static void Main(string[] arguments) {\n"
        + (
            "        Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes("
            "\"MOODLE_DOCKER_WEB_HOST=\" + Environment.GetEnvironmentVariable("
            "\"MOODLE_DOCKER_WEB_HOST\"))));\n"
        )
        + (
            "        Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes("
            "\"MOODLE_DOCKER_WEB_PORT=\" + Environment.GetEnvironmentVariable("
            "\"MOODLE_DOCKER_WEB_PORT\"))));\n"
        )
        + "        foreach (string argument in arguments) {\n"
        + (
            "            Console.WriteLine(Convert.ToBase64String("
            "Encoding.UTF8.GetBytes(argument)));\n"
        )
        + "        }\n"
        + "    }\n"
        + "}\n"
        + "'@ -OutputAssembly '"
        + f"{(tmp_path / 'docker.exe').as_posix()}"
        + "' -OutputType ConsoleApplication\n"
        + f"$env:PATH = '{tmp_path.as_posix()};' + $env:PATH\n"
        + f"$query = \"{query}\"\n"
        + f"$containerCli = '{container_cli}'\n"
        + f"$unicodeValue = '{unicode_value}'\n"
        + f"$hostCompose = ConvertTo-BashPath -Path '{host_compose.as_posix()}'\n"
        + "$initialOutputEncoding = $global:OutputEncoding\n"
        + (
            "$output = Invoke-MoodleDocker -Arguments @('exec', '-T', 'webserver', "
            "'php', $containerCli, $unicodeValue, '-f', $hostCompose, '-tAc', "
            "$query)\n"
        )
        + (
            "if (-not [object]::ReferenceEquals($global:OutputEncoding, "
            "$initialOutputEncoding)) { throw 'Invoke-MoodleDocker changed global "
            "OutputEncoding.' }\n"
        )
        + "$output | ConvertTo-Json -Compress\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(driver),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    captured = [base64.b64decode(value).decode("utf-8") for value in json.loads(result.stdout)]
    assert captured[:2] == [
        "MOODLE_DOCKER_WEB_HOST=127.0.0.1",
        "MOODLE_DOCKER_WEB_PORT=127.0.0.1:8000",
    ]
    arguments = captured[2:]
    assert arguments[:6] == [
        "exec",
        "-T",
        "webserver",
        "php",
        container_cli,
        unicode_value,
    ]
    assert arguments[6] == "-f"
    assert Path(arguments[7]).resolve() == host_compose.resolve()
    assert arguments[8:] == [
        "-tAc",
        query,
    ]


def test_script_keeps_credentials_and_token_in_runtime() -> None:
    script = read(SCRIPT)
    assert "moodle-secrets.json" in script
    assert "moodle-token.json" in script
    assert "New-LocalPassword" in script
    assert "moodle_mobile_app" in script
    assert "Write-Output $($Secrets" not in script
    assert "Write-Output $($token" not in script


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is not available",
)
def test_new_local_password_meets_moodle_default_policy_without_printing_passwords(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    prefix = read(SCRIPT).split("\nswitch ($Action) {", maxsplit=1)[0]
    driver = tmp_path / "password-policy.ps1"
    driver.write_text(
        prefix
        + "\n1..64 | ForEach-Object {\n"
        + "    $password = New-LocalPassword\n"
        + "    Write-Output (\"{0}|{1}|{2}|{3}|{4}\" -f $password.Length, "
        + "($password -cmatch '[a-z]'), ($password -cmatch '[A-Z]'), "
        + "($password -match '[0-9]'), ($password -match '[^A-Za-z0-9]'))\n"
        + "}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["36|True|True|True|True"] * 64


def test_script_uses_root_cli_and_inline_core_api_seed() -> None:
    script = read(SCRIPT)
    assert "admin/cli/install_database.php" in script
    assert "CoreCliRoot = '/var/www/html'" in script
    assert "CoreConfigPath = '/var/www/html/config.php'" in script
    assert "runtestscenario.php" not in script
    assert "autoload.php" not in script
    assert "user_create_user" in script
    assert "create_course" in script
    assert "enrol_get_plugin('manual')" in script
    assert "add_moduleinfo" in script
    assert "EndpointCandidates" in script
    assert "Get-FixtureState" in script
    assert "reset_password.php" in script


def test_inline_assign_seed_supplies_pinned_add_instance_contract() -> None:
    script = read(SCRIPT)
    seed = script.split("$seed = ", maxsplit=1)[1].split(
        "$result = Invoke-MoodleDocker", maxsplit=1
    )[0]
    expected_defaults = {
        "alwaysshowdescription": "1",
        "submissionattachments": "0",
        "submissiondrafts": "0",
        "requiresubmissionstatement": "0",
        "sendnotifications": "0",
        "sendlatenotifications": "0",
        "sendstudentnotifications": "0",
        "duedate": "0",
        "allowsubmissionsfromdate": "0",
        "grade": "100",
        "completionsubmit": "0",
        "cutoffdate": "0",
        "gradingduedate": "0",
        "teamsubmission": "0",
        "requireallteammemberssubmit": "0",
        "teamsubmissiongroupingid": "0",
        "blindmarking": "0",
        "hidegrader": "0",
        "markingworkflow": "0",
        "markingallocation": "0",
        "preventsubmissionnotingroup": "0",
        "attemptreopenmethod": "'untilpass'",
        "maxattempts": "1",
        "markinganonymous": "0",
        "timelimit": "0",
        "gradepenalty": "0",
    }
    for name, value in expected_defaults.items():
        assert f"'{name}' => {value}" in seed
    assert "'intro' => ''" in seed
    assert "'introformat' => FORMAT_HTML" in seed
    assert "introeditor" not in seed
    assert "'timemodified' =>" not in seed


def test_script_probes_database_before_using_or_rewriting_install_evidence() -> None:
    script = read(SCRIPT)
    assert "function Test-SiteInstalled" in script
    assert "MoodleDockerTablePrefix" in script
    assert "MoodleDockerConfigTable" in script
    assert "m_" in script
    assert "m_config" in read(VERSIONS)
    assert "mdl_config" not in script
    assert "to_regclass('public.$configTable')" in script
    assert "ON_ERROR_STOP=1" in script
    assert "rolesactive' AND value = '1'" in script
    assert "registrationpending" not in script
    assert "username = 'admin'" in script
    assert "course WHERE id = 1" in script
    assert "upgraderunning" in script
    assert "incomplete installation or upgrade markers" in script
    assert "function Write-InstallEvidence" in script
    assert "if (-not (Test-SiteInstalled))" in script


def test_install_database_uses_only_pinned_supported_options() -> None:
    script = read(SCRIPT)
    install = script.split("function Install-Site", maxsplit=1)[1].split(
        "function Set-MoodleConfiguration", maxsplit=1
    )[0]
    assert "--non-interactive" not in install
    expected_options = {
        "--agree-license",
        "--lang=en",
        "--fullname=AutoTask local Moodle",
        "--shortname=autotask-local",
        "--summary=Development-only Moodle integration test site",
        "--adminuser=admin",
        "--adminpass=$($Secrets.adminPassword)",
        "--adminemail=admin@example.test",
    }
    for option in expected_options:
        assert option in install
    actual_option_names = set(re.findall(r"[\"'](--[a-z-]+)(?:=|[\"'])", install))
    assert actual_option_names == {
        "--agree-license",
        "--lang",
        "--fullname",
        "--shortname",
        "--summary",
        "--adminuser",
        "--adminpass",
        "--adminemail",
    }


def test_fixture_assignment_probe_is_scoped_to_the_asix_course() -> None:
    script = read(SCRIPT)
    assert "FROM {assign} a JOIN {course_modules} cm" in script
    assert "JOIN {modules} m ON m.id = cm.module" in script
    assert "cm.idnumber = ?" in script
    assert "m.name = 'assign'" in script
    assert "'cmidnumber' => 'autotask-assignment'" in script
    assert "{assign}.idnumber" not in script
    assert "'idnumber' => 'autotask-assignment'" not in script
    assert "e.enrol = 'manual'" in script
    assert "r.shortname = 'student'" in script
    assert "`$user->email === 'student1@example.test'" in script
    assert "`$course->fullname === 'ASIX Lab'" in script


def test_fixture_state_is_exact_and_partial_fails_closed() -> None:
    script = read(SCRIPT)
    assert "function Get-FixtureState" in script
    assert "@('absent', 'complete', 'legacy', 'lost', 'partial')" in script
    assert "$state -eq 'partial'" in script
    assert "$state -eq 'absent'" in script
    assert "$state -ne 'complete'" in script
    assert "-match 'present'" not in script


def test_fixture_attachment_probe_distinguishes_safe_legacy_from_partial_bytes() -> None:
    script = read(SCRIPT)
    probe = script.split("$attachmentProbe = ", maxsplit=1)[1].split(
        "$attachmentResult", maxsplit=1
    )[0]
    assert "beec33f762521fcc5976c5dd799348d888014d988dd335e91c7e195ed811f11c" in probe
    assert "(int)`$file->get_filesize() === 76" in probe
    assert "`$file->get_contenthash() === `$contenthash" in probe
    assert "hash('sha256', `$content) === `$hash" in probe
    assert "`$row->intro === '' || `$row->intro === `$intro" in probe
    assert "? 'legacy' : 'partial'" in probe
    assert "`$row->intro === `$intro && `$exactmetadata" in probe
    assert "(`$content === '' || `$content === false)" in probe
    assert "echo 'lost'" in probe
    assert "if ($attachmentState -notin @('complete', 'legacy', 'lost', 'partial'))" in script
    assert "$state = $attachmentState" in script


def test_fixture_attachment_migration_refuses_existing_or_nonlegacy_fixture() -> None:
    script = read(SCRIPT)
    upgrade = script.split("function Ensure-FixtureAttachment", maxsplit=1)[1].split(
        "function Invoke-MoodleRest", maxsplit=1
    )[0]
    assert "`$repairable = (int)`$existing->get_filesize() === 76" in upgrade
    assert "`$existing->get_contenthash() === `$contenthash" in upgrade
    assert "(`$content === '' || `$content === false)" in upgrade
    assert "if (!`$repairable)" in upgrade
    assert "fixture attachment already exists" in upgrade
    assert upgrade.index("if (!`$repairable)") < upgrade.index("`$existing->delete()")
    assert "if (`$row->intro !== '' && `$row->intro !== `$intro)" in upgrade
    assert "fixture intro is not migratable" in upgrade
    assert "create_file_from_string" in upgrade
    assert "if ($state -in @('absent', 'legacy', 'lost'))" in script


def test_rich_fixture_models_the_public_asix_catalog_with_fictitious_data() -> None:
    fixture = read(FIXTURE)
    course_block = fixture.split("function fixture_courses(): array", maxsplit=1)[1].split(
        "function fixture_assignments(): array", maxsplit=1
    )[0]
    assignment_block = fixture.split(
        "function fixture_assignments(): array", maxsplit=1
    )[1].split("function fixture_footprint_exists(): bool", maxsplit=1)[0]
    courses = re.findall(r"'(ASIX[12]-[A-Z0-9-]+)'\s*=>", course_block)
    assignment_ids = re.findall(
        r"'idnumber'\s*=>\s*'(autotask-rich-[a-z0-9-]+)'", assignment_block
    )
    assert courses == [
        "ASIX1-0369-ISO",
        "ASIX1-0371-FM",
        "ASIX1-0372-GBD",
        "ASIX1-0373-LMSGI",
        "ASIX1-0376-IAW",
        "ASIX1-0377-ASGBD",
        "ASIX2-0370-PAX",
        "ASIX2-0374-ASO",
        "ASIX2-0375-SXI",
        "ASIX2-0378-SAD",
        "ASIX2-0379-PROJ",
    ]
    assert len(assignment_ids) == len(set(assignment_ids)) == 11
    for filename in (
        "asix-router-lab.ova",
        "practica-iso-ova.pdf",
        "inventari.sql",
        "servidors.xml",
        "servidors.xsd",
        "compose.yml",
        "site.yml",
        "baseline.ps1",
        "plantilla-projecte.md",
    ):
        assert filename in assignment_block
    assert "metadata-only and is not bootable" in assignment_block
    assert "Dades de prova exclusivament fictícies" in assignment_block


def test_rich_fixture_is_versioned_idempotent_and_fails_closed_on_partial_state() -> None:
    fixture = read(FIXTURE)
    assert "AUTOTASK_FIXTURE_CONFIG" in fixture
    assert "AUTOTASK_FIXTURE_ANCHOR_CONFIG" in fixture
    assert "return fixture_footprint_exists() ? 'partial' : 'absent'" in fixture
    assert "return 'complete-v' . $version" in fixture
    assert "function infer_fixture_anchor(bool $advanced): ?int" in fixture
    assert "preg_match('/^[1-9][0-9]*$/', (string)$stored)" in fixture
    assert "set_config(AUTOTASK_FIXTURE_ANCHOR_CONFIG, (string)$now)" in fixture
    ensure = fixture.split("} elseif ($action === 'ensure')", maxsplit=1)[1].split(
        "} elseif ($action === 'seed')", maxsplit=1
    )[0]
    assert "$state === 'partial'" in ensure
    assert "throw new RuntimeException('rich fixture is partial')" in ensure
    assert "$state === 'absent'" in ensure
    assert "seed_fixture()" in ensure
    assert "rich fixture anchor migration failed" in ensure
    assert "echo fixture_state()" in ensure


def test_rich_fixture_verifies_category_course_and_deadline_contracts() -> None:
    fixture = read(FIXTURE)
    verify = fixture.split("function verify_fixture", maxsplit=1)[1].split(
        "function fixture_state", maxsplit=1
    )[0]
    for category in ("AUTOTASK-CF", "AUTOTASK-INFO", "AUTOTASK-ASIX"):
        assert category in verify
    assert "$info->parent !== (int)$root->id" in verify
    assert "$asix->parent !== (int)$info->id" in verify
    assert "$course->category !== (int)$asix->id" in verify
    assert "Curs fictici i determinista per a proves locals de Moodle Autotask." in verify
    assert "$reservedmodules !== count(fixture_assignments())" in verify
    assert "$expecteddue = $spec['dueoffset'] === 0 ? 0 : $anchor + $spec['dueoffset']" in verify
    assert "$expecteddue += 86400" in verify
    assert "$expectedallow = $spec['allowoffset'] === 0" in verify
    assert "(int)$row->duedate !== $expecteddue" in verify
    assert "(int)$row->allowsubmissionsfromdate !== $expectedallow" in verify


def test_rich_fixture_advance_changes_the_same_assignment_revision_once() -> None:
    fixture = read(FIXTURE)
    advance = fixture.split("function advance_fixture(): void", maxsplit=1)[1].split(
        "$action = $argv[1]", maxsplit=1
    )[0]
    assert "fixture_state() !== 'complete-v1'" in advance
    assert "$idnumber = 'autotask-rich-iso-ova'" in advance
    assert "$row->duedate += 86400" in advance
    assert "'filename' => 'revision-2.txt'" in advance
    assert "set_config(AUTOTASK_FIXTURE_CONFIG, '2')" in advance
    assert "verify_fixture(true, $anchor)" in advance


def test_fixture_tool_is_copied_hash_verified_executed_and_removed() -> None:
    script = read(SCRIPT)
    tool = script.split("function Invoke-RichFixtureTool", maxsplit=1)[1].split(
        "function Invoke-MoodleDockerWaitForDb", maxsplit=1
    )[0]
    assert "ValidateSet('state', 'ensure', 'seed', 'advance')" in tool
    assert "Assert-ContainedNonReparsePath -Path $FixtureToolPath" in tool
    assert "Get-FileHash -LiteralPath $FixtureToolPath -Algorithm SHA256" in tool
    assert "@('cp', (ConvertTo-BashPath -Path $FixtureToolPath)" in tool
    assert "'sha256sum', $containerPath" in tool
    assert tool.index("$actualHash -ne $expectedHash") < tool.index(
        "'php', $containerPath, $FixtureAction"
    )
    assert "unlink('$containerPath')" in tool
    assert "MSYS2_ARG_CONV_EXCL' = '/var/www/html;/tmp/moodle-autotask-fixture.php'" in script
    assert "Invoke-RichFixtureTool -FixtureAction 'ensure'" in script


def test_smoke_requires_exact_rich_course_assignment_and_ova_metadata_matrix() -> None:
    script = read(SCRIPT)
    smoke = script.split("function Invoke-Smoke", maxsplit=1)[1].split(
        "function Assert-ResetTarget", maxsplit=1
    )[0]
    assert "$expectedRichCourses = @(" in smoke
    assert "$richAssignments.Count -ne 11" in smoke
    assert "mod_assign_get_assignments" in smoke
    assert "Pràctica ISO 1 - Desplegament d'una OVA" in smoke
    assert "$ovaFiles -notcontains 'asix-router-lab.ova'" in smoke
    assert "12 assignments across the base and ASIX fixtures" in smoke


def test_advance_fixture_action_is_explicit_one_way_and_runs_smoke() -> None:
    script = read(SCRIPT)
    action = script.split("    'AdvanceFixture' {", maxsplit=1)[1].split(
        "    'Reset' {", maxsplit=1
    )[0]
    assert "Invoke-RichFixtureTool -FixtureAction 'advance'" in action
    assert "rich-fixture-advanced" in action
    assert "Invoke-Smoke" in action


def test_cfg_writes_web_services_only_and_mobile_uses_setting_semantics() -> None:
    script = read(SCRIPT)
    configuration = script.split("function Set-MoodleConfiguration", maxsplit=1)[1].split(
        "function Enable-MoodleMobileService", maxsplit=1
    )[0]
    assert "enablewebservices" in configuration
    assert "enablemobilewebservice" not in configuration
    assert "@('debug'" not in configuration
    assert "@('debugdisplay'" not in configuration
    activation = script.split("function Enable-MoodleMobileService", maxsplit=1)[1].split(
        "function Get-FixtureState", maxsplit=1
    )[0]
    assert (
        "new admin_setting_enablemobileservice('enablemobilewebservice', '', '', 0)"
        in activation
    )
    assert "admin_get_root" not in activation
    assert ".locate(" not in activation
    assert "write_setting(1)" in activation
    assert "\\core\\session\\manager::set_user(get_admin());" in activation
    assert activation.index("\\core\\session\\manager::set_user(get_admin());") < activation.index(
        "new admin_setting_enablemobileservice"
    )
    assert "`$USER = get_admin();" not in activation
    assert "MOODLE_OFFICIAL_MOBILE_SERVICE" in activation
    assert "webserviceprotocols" in activation
    assert "webservice/rest:use" in activation
    assert "mobile REST service activation verification failed" in activation
    assert "mobile-service-ready" in activation


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is required",
)
def test_set_moodle_configuration_invokes_cfg_once_with_exact_arguments(
    tmp_path: Path,
) -> None:
    script = read(SCRIPT)
    configuration = script[
        script.index("function Set-MoodleConfiguration") : script.index(
            "function Enable-MoodleMobileService"
        )
    ]
    driver = tmp_path / "set-moodle-configuration.ps1"
    driver.write_text(
        configuration
        + "\n$script:invocations = @()\n"
        + (
            "function Invoke-MoodleDocker { param([string[]]$Arguments) "
            "$script:invocations += ,@($Arguments) }\n"
        )
        + "$layout = [PSCustomObject]@{ CoreCliRoot = '/var/www/html' }\n"
        + "Set-MoodleConfiguration -Layout $layout\n"
        + "if ($script:invocations.Count -ne 1) { exit 1 }\n"
        + "$script:invocations[0] | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "exec",
        "-T",
        "webserver",
        "php",
        "/var/www/html/admin/cli/cfg.php",
        "--name=enablewebservices",
        "--set=1",
    ]


def test_down_and_up_preserve_existing_containers() -> None:
    script = read(SCRIPT)
    down_case = script.split("    'Down' {", maxsplit=1)[1].split("    'Status' {", maxsplit=1)[0]
    up_case = script.split("    'Up' {", maxsplit=1)[1].split("    'Down' {", maxsplit=1)[0]
    assert "Invoke-MoodleDocker -Arguments @('stop')" in down_case
    assert "down" not in down_case
    assert "Resume-Stack" in up_case
    assert "Invoke-MoodleDocker -Arguments @('up', '-d')" in script
    assert "Invoke-MoodleDocker -Arguments @('start')" not in script
    assert "Set-MoodleContainerRestartPolicy" in script
    assert "Test-StackContainersExist" in script
    assert "Run Bootstrap to create the site" in script
    assert "down', '--volumes', '--remove-orphans'" in script


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is required",
)
def test_restart_policy_rejects_empty_or_malformed_compose_container_ids(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    script = read(SCRIPT)
    policy = script[
        script.index("function Set-MoodleContainerRestartPolicy") : script.index(
            "function Resume-Stack"
        )
    ]
    driver = tmp_path / "restart-policy-validation.ps1"
    driver.write_text(
        "function Fail { param([string]$Message) throw $Message }\n"
        + policy
        + "\nfunction Invoke-MoodleDocker { param([string[]]$Arguments) $script:containerIds }\n"
        + "$errors = @()\n"
        + "$script:containerIds = @()\n"
        + "try { Set-MoodleContainerRestartPolicy } catch { $errors += $_.Exception.Message }\n"
        + "$script:containerIds = @('not-a-container-id')\n"
        + "try { Set-MoodleContainerRestartPolicy } catch { $errors += $_.Exception.Message }\n"
        + "$errors | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "The Moodle Compose project did not report any container IDs for restart-policy "
        "configuration.",
        "The Moodle Compose project returned a malformed Docker container ID.",
    ]
    assert "@('update', '--restart', 'unless-stopped', $containerId)" in policy
    assert "@('inspect', '--format', '{{.HostConfig.RestartPolicy.Name}}', $containerId)" in policy
    assert "$policy -ne 'unless-stopped'" in policy


def test_source_integrity_rejects_dirty_runtime_sources() -> None:
    script = read(SCRIPT)
    assert "function Assert-GitOrigin" in script
    assert "remote get-url origin" in script
    assert "function Assert-CleanRuntimeSource" in script
    assert "$unexpected = @($entries | Where-Object { $_ -ne '!! local.yml' })" in script
    assert "dirty or contains untracked files or an untrusted override" in script
    assert "unexpected untracked overrides" in script
    assert "--ignored --untracked-files=all" in script
    assert "$allowed = @('!! config.php')" in script
    assert "Assert-GeneratedMoodleConfig" in script
    assert "function Get-FileSha256" in script
    assert "[System.Security.Cryptography.SHA256]::Create()" in script
    assert "Assert-CleanRuntimeSource -Repository $MoodleDockerRoot" in script
    assert "Assert-CleanRuntimeSource -Repository $MoodleRoot" in script


def test_reset_trusts_only_clean_pinned_moodle_docker_before_compose() -> None:
    script = read(SCRIPT)
    assert "$script:Versions = Read-MoodleVersions -Path $VersionsPath" in script
    trusted = script.split("function Test-MoodleDockerTrustedForReset", maxsplit=1)[1].split(
        "function Reset-LocalEnvironment", maxsplit=1
    )[0]
    assert "Assert-MoodleDockerWrapperTrust -Wrapper $wrapper" in trusted
    assert "core.fsmonitor=false" in script
    reset = script.split("function Reset-LocalEnvironment", maxsplit=1)[1]
    assert "Test-MoodleDockerTrustedForReset" in reset
    assert reset.index("Test-MoodleDockerTrustedForReset") < reset.index("Assert-DockerDaemon")


def test_normal_wrapper_execution_has_centralized_pinned_source_trust() -> None:
    script = read(SCRIPT)
    trust = script.split("function Assert-MoodleDockerWrapperTrust", maxsplit=1)[1].split(
        "function Assert-NormalMoodleDockerExecutionTrust", maxsplit=1
    )[0]
    for path in ("$MoodleDockerRoot", "$MoodleRoot", "(Join-Path $MoodleDockerRoot '.git')"):
        assert f"Assert-ContainedNonReparsePath -Path {path}" in trust
    assert "Assert-GitOrigin -Repository $MoodleDockerRoot" in trust
    assert "Assert-CleanRuntimeSource -Repository $MoodleDockerRoot" in trust
    assert "Assert-GitOrigin -Repository $MoodleRoot" in trust
    assert "Assert-CleanRuntimeSource -Repository $MoodleRoot" in trust
    assert "-RequireGeneratedMoodleConfig" in trust
    assert "$Versions.MoodleDockerCommit" in trust
    assert "$Versions.MoodlePeeledCommit" in trust
    invoke = script.split("function Invoke-MoodleDocker", maxsplit=1)[1].split(
        "function Invoke-MoodleDockerWaitForDb", maxsplit=1
    )[0]
    wait = script.split("function Invoke-MoodleDockerWaitForDb", maxsplit=1)[1].split(
        "function Write-ImageEvidence", maxsplit=1
    )[0]
    assert "Assert-MoodleDockerWrapperTrust -Wrapper $wrapper -RequireMoodleSource" in invoke
    assert "Assert-MoodleDockerWrapperTrust -Wrapper $wrapper -RequireMoodleSource" in wait
    for action in ("Up", "Down", "Smoke"):
        action_case = script.split(f"    '{action}' {{", maxsplit=1)[1].split(
            "    '", maxsplit=1
        )[0]
        assert "$script:Versions = Read-MoodleVersions -Path $VersionsPath" in action_case
        assert action_case.index("Assert-NormalMoodleDockerExecutionTrust") < action_case.index(
            "Assert-DockerDaemon"
        )


def test_runtime_reparse_and_local_override_guards_run_before_compose() -> None:
    script = read(SCRIPT)
    assert "function Assert-SafeRuntimePaths" in script
    assert "function Assert-ContainedNonReparsePath" in script
    assert "Refusing reparse-point runtime path" in script
    assert "function Assert-NoMoodleDockerLocalOverride" in script
    assert "moodle-docker/local.yml override" in script
    invoke = script.split("function Invoke-MoodleDocker", maxsplit=1)[1].split(
        "function Invoke-MoodleDockerWaitForDb", maxsplit=1
    )[0]
    assert "Assert-NoMoodleDockerLocalOverride" in invoke
    assert "Assert-ContainedNonReparsePath -Path $Wrapper" in script
    reset = script.split("function Reset-LocalEnvironment", maxsplit=1)[1]
    assert "Assert-SafeRuntimePaths" in reset
    assert "Remove-MoodleDockerLocalOverrideForReset" in reset
    assert reset.index("Remove-MoodleDockerLocalOverrideForReset") < reset.index(
        "Assert-DockerDaemon"
    )
    reset_override = script.split(
        "function Remove-MoodleDockerLocalOverrideForReset", maxsplit=1
    )[1].split(
        "function Reset-LocalEnvironment", maxsplit=1
    )[0]
    assert "Assert-SafeWriteTarget -Path $localOverride" in reset_override
    assert "Refusing non-file moodle-docker/local.yml reset target" in reset_override
    assert "Refusing reparse-point moodle-docker/local.yml reset target" in reset_override
    assert "Remove-Item -LiteralPath $localOverride -Force" in reset_override
    for target in (
        "$SecretsPath",
        "$TokenPath",
        "$ImageEvidencePath",
        "$InstallEvidencePath",
        "$Layout.LocalConfigPath",
        "$localOverride",
    ):
        assert f"Assert-SafeWriteTarget -Path {target}" in script


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is required",
)
def test_local_override_accepts_only_the_exact_persistent_moodledata_mount(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    script = read(SCRIPT)
    functions = script.split(
        "function Assert-NoMoodleDockerLocalOverride", maxsplit=1
    )[1].split("function Invoke-External", maxsplit=1)[0]
    docker_root = tmp_path / "moodle-docker"
    docker_root.mkdir()
    driver = tmp_path / "local-override.ps1"
    driver.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        + f"$MoodleDockerRoot = '{docker_root.as_posix()}'\n"
        + "function Fail { param([string]$Message) throw $Message }\n"
        + "function Assert-SafeRuntimePaths {}\n"
        + "function Assert-SafeWriteTarget { param([string]$Path) }\n"
        + "function Assert-NoMoodleDockerLocalOverride"
        + functions
        + "\nWrite-TrustedMoodleDockerLocalOverride\n"
        + "$path = Join-Path $MoodleDockerRoot 'local.yml'\n"
        + "$first = [System.IO.File]::ReadAllText($path)\n"
        + "Write-TrustedMoodleDockerLocalOverride\n"
        + "$second = [System.IO.File]::ReadAllText($path)\n"
        + "[System.IO.File]::WriteAllText($path, 'services: {}')\n"
        + "$tamperError = $null\n"
        + "try { Assert-NoMoodleDockerLocalOverride } "
        + "catch { $tamperError = $_.Exception.Message }\n"
        + "Remove-Item -LiteralPath $path -Force\n"
        + "New-Item -ItemType Directory -Path $path | Out-Null\n"
        + "$directoryError = $null\n"
        + "try { Assert-NoMoodleDockerLocalOverride } "
        + "catch { $directoryError = $_.Exception.Message }\n"
        + "[PSCustomObject]@{ first = $first; second = $second; tamper = $tamperError; "
        + "directory = $directoryError } | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    expected = (
        "services:\n"
        "  webserver:\n"
        "    volumes:\n"
        "      - moodledata:/var/www/moodledata\n"
        "volumes:\n"
        "  moodledata:\n"
    )
    assert payload["first"] == payload["second"] == expected
    assert "untrusted moodle-docker/local.yml override" in payload["tamper"]
    assert "non-file moodle-docker/local.yml" in payload["directory"]


def test_compose_override_notice_is_filtered_only_after_exact_override_validation() -> None:
    script = read(SCRIPT)
    invoke = script.split("function Invoke-MoodleDocker", maxsplit=1)[1].split(
        "function Invoke-RichFixtureTool", maxsplit=1
    )[0]
    assert invoke.index("Assert-NoMoodleDockerLocalOverride") < invoke.index(
        "$localOverrideNotice"
    )
    assert "Including local options from " in invoke
    assert "Where-Object { [string]$_ -cne $localOverrideNotice }" in invoke


def test_git_control_state_and_reset_wwwroot_are_constrained() -> None:
    script = read(SCRIPT)
    controls = script.split("function Assert-SafeGitControlState", maxsplit=1)[1].split(
        "function Invoke-GitRuntime", maxsplit=1
    )[0]
    for control in (
        "Get-GitConfigEntries",
        "Assert-SafeGitIndexFlags",
        "Assert-NoGitReplacementObjects",
        "--no-replace-objects",
        "--includes $Scope --name-only --list",
        "--worktree",
        "remote.origin.url",
        "remote.origin.fetch",
        "branch.main.remote",
        "branch.main.merge",
        "info/exclude",
    ):
        assert control in script
    assert "Get-Content -LiteralPath $configPath" not in controls
    assert "filter|include|includeif|submodule" not in controls
    initialise = script.split("function Initialize-Sources", maxsplit=1)[1].split(
        "function Get-MoodleLayout", maxsplit=1
    )[0]
    assert initialise.count("Assert-SafeGitControlState") >= 4
    assert "Invoke-GitRuntime" in initialise
    environment = script.split("function Get-MoodleDockerEnvironment", maxsplit=1)[1].split(
        "function Invoke-MoodleDocker", maxsplit=1
    )[0]
    assert "[string]$WwwRoot = $MoodleRoot" in environment
    assert "Refusing missing Moodle Docker WWWROOT" in environment
    reset = script.split("function Reset-LocalEnvironment", maxsplit=1)[1]
    assert "-SkipMoodleSourceTrust -UseRuntimeWwwRoot" in reset
    assert "$GitHooksRoot" in reset


@pytest.mark.skipif(
    shutil.which("powershell") is None
    and shutil.which("pwsh") is None
    or shutil.which("git") is None,
    reason="PowerShell and Git are required",
)
def test_trusted_reset_wrapper_uses_runtime_wwwroot_without_moodle_checkout(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    docker_root = runtime / "moodle-docker"
    wrapper = docker_root / "bin" / "moodle-docker-compose"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=docker_root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "bin/moodle-docker-compose"], cwd=docker_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "baseline",
        ],
        cwd=docker_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/moodlehq/moodle-docker.git"],
        cwd=docker_root,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=docker_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    prefix = read(SCRIPT).split("\nswitch ($Action) {", maxsplit=1)[0]
    driver = tmp_path / "reset-wwwroot.ps1"
    temp_root = tmp_path.as_posix()
    runtime_path = runtime.as_posix()
    docker_path = docker_root.as_posix()
    wrapper_path = wrapper.as_posix()
    driver.write_text(
        prefix
        + f"\n$RepoRoot = '{temp_root}'\n"
        + f"$RuntimeRoot = '{runtime_path}'\n"
        + f"$MoodleRoot = '{(runtime / 'moodle').as_posix()}'\n"
        + f"$MoodleDockerRoot = '{docker_path}'\n"
        + f"$MoodleDataRoot = '{(runtime / 'moodledata').as_posix()}'\n"
        + f"$SecretsPath = '{(runtime / 'moodle-secrets.json').as_posix()}'\n"
        + f"$TokenPath = '{(runtime / 'moodle-token.json').as_posix()}'\n"
        + f"$ImageEvidencePath = '{(runtime / 'moodle-images.json').as_posix()}'\n"
        + f"$InstallEvidencePath = '{(runtime / 'moodle-install.json').as_posix()}'\n"
        + f"$GitHooksRoot = '{(runtime / 'moodle-git-hooks').as_posix()}'\n"
        + "$Versions = [PSCustomObject]@{\n"
        + "    MoodleDockerRepository = 'https://github.com/moodlehq/moodle-docker.git'\n"
        + f"    MoodleDockerCommit = '{head}'\n"
        + "    MoodleRepository = 'https://github.com/moodle/moodle.git'\n"
        + "    MoodlePeeledCommit = 'unused'\n"
        + "}\n"
        + "function ConvertTo-BashPath { param([string]$Path) return 'validated-runtime-root' }\n"
        + f"Assert-MoodleDockerWrapperTrust -Wrapper '{wrapper_path}'\n"
        + "$environment = Get-MoodleDockerEnvironment -WwwRoot $RuntimeRoot\n"
        + "if ($environment['MOODLE_DOCKER_WWWROOT'] -ne 'validated-runtime-root') { exit 1 }\n"
        + "Write-Output 'reset-wwwroot-ok'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reset-wwwroot-ok"


@pytest.mark.skipif(
    shutil.which("powershell") is None
    and shutil.which("pwsh") is None
    or shutil.which("git") is None,
    reason="PowerShell and Git are required",
)
def test_real_no_checkout_clone_allows_only_main_tracking_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    bare_remote = tmp_path / "remote.git"
    source.mkdir()
    (source / "README").write_text("baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "add", "README"], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "baseline",
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare_remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    runtime = tmp_path / "runtime"
    checkout = runtime / "moodle-docker"
    prefix = read(SCRIPT).split("\nswitch ($Action) {", maxsplit=1)[0]
    driver = tmp_path / "clone-control-state.ps1"
    driver.write_text(
        prefix
        + f"\n$RepoRoot = '{tmp_path.as_posix()}'\n"
        + f"$RuntimeRoot = '{runtime.as_posix()}'\n"
        + f"$MoodleRoot = '{(runtime / 'moodle').as_posix()}'\n"
        + f"$MoodleDockerRoot = '{checkout.as_posix()}'\n"
        + f"$MoodleDataRoot = '{(runtime / 'moodledata').as_posix()}'\n"
        + f"$GitHooksRoot = '{(runtime / 'moodle-git-hooks').as_posix()}'\n"
        + "$git = Assert-Git\n"
        + f"$checkoutPath = '{checkout.as_posix()}'\n"
        + f"$originPath = '{bare_remote.as_posix()}'\n"
        + "$cloneArguments = @{\n"
        + "    Path = $checkoutPath\n"
        + "    RepositoryUrl = $originPath\n"
        + "    GitPath = $git\n}\n"
        + "Ensure-GitRepository @cloneArguments\n"
        + "$remote = & $git -C $checkoutPath config --local --get branch.main.remote\n"
        + "$merge = & $git -C $checkoutPath config --local --get branch.main.merge\n"
        + "if ($remote -ne 'origin' -or $merge -ne 'refs/heads/main') { exit 1 }\n"
        + "$flexibleSettings = @(\n"
        + "    @('core.filemode', 'true'), @('core.filemode', 'false'),\n"
        + "    @('core.symlinks', 'true'), @('core.symlinks', 'false'),\n"
        + "    @('core.ignorecase', 'true'), @('core.ignorecase', 'false'),\n"
        + "    @('core.precomposeunicode', 'true'), @('core.precomposeunicode', 'false')\n"
        + ")\n"
        + "foreach ($setting in $flexibleSettings) {\n"
        + "    & $git --no-replace-objects -C $checkoutPath"
        + " config --local $setting[0] $setting[1]\n"
        + "    if ($LASTEXITCODE -ne 0) { exit 1 }\n"
        + "    Assert-SafeGitControlState -Repository $checkoutPath"
        + " -ExpectedOrigin $originPath -GitPath $git\n"
        + "}\n"
        + "Write-Output 'clone-control-state-ok'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("clone-control-state-ok")


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is not available",
)
def test_reset_without_force_is_rejected_before_docker_access() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-Action",
            "Reset",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Reset is destructive" in output
    assert "Docker daemon" not in output


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is not available",
)
def test_local_yml_is_rejected_before_docker_access() -> None:
    runtime = ROOT / ".runtime"
    runtime_preexisting = runtime.exists()
    if runtime_preexisting and any(runtime.iterdir()):
        pytest.skip("existing runtime data must not be changed by this test")
    local_yml = runtime / "moodle-docker" / "local.yml"
    local_yml.parent.mkdir(parents=True)
    local_yml.write_text("services: {}\n", encoding="utf-8")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Status",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    finally:
        if runtime_preexisting and local_yml.parent.exists():
            shutil.rmtree(local_yml.parent)
        elif runtime.exists():
            shutil.rmtree(runtime)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "moodle-docker/local.yml override" in output
    assert "Docker daemon" not in output


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is not available",
)
def test_forced_reset_removes_regular_local_yml_before_docker_access() -> None:
    runtime = ROOT / ".runtime"
    runtime_preexisting = runtime.exists()
    if runtime_preexisting and any(runtime.iterdir()):
        pytest.skip("existing runtime data must not be changed by this test")
    local_yml = runtime / "moodle-docker" / "local.yml"
    local_yml.parent.mkdir(parents=True)
    local_yml.write_text("services: {}\n", encoding="utf-8")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Reset",
                "-Force",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=25,
        )
        removed_before_compose = not local_yml.exists()
    finally:
        if runtime_preexisting and local_yml.parent.exists():
            shutil.rmtree(local_yml.parent)
        elif runtime.exists():
            shutil.rmtree(runtime)
    assert result.returncode == 0
    assert removed_before_compose


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is not available",
)
def test_forced_reset_removes_partial_moodle_docker_without_wrapper_execution(
    tmp_path: Path,
) -> None:
    runtime = ROOT / ".runtime"
    runtime_preexisting = runtime.exists()
    if runtime_preexisting and any(runtime.iterdir()):
        pytest.skip("existing runtime data must not be changed by this test")
    wrapper = runtime / "moodle-docker" / "bin" / "moodle-docker-compose"
    marker = tmp_path / "wrapper-ran"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(f"#!/usr/bin/env sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Reset",
                "-Force",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        removed = not wrapper.parents[1].exists()
    finally:
        if runtime_preexisting and wrapper.parents[1].exists():
            shutil.rmtree(wrapper.parents[1])
        elif runtime.exists():
            shutil.rmtree(runtime)
    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert removed
    assert not marker.exists()
    assert "Skipping Compose down" in output
    assert "Docker daemon" not in output


@pytest.mark.skipif(
    shutil.which("powershell") is None
    and shutil.which("pwsh") is None
    or shutil.which("git") is None,
    reason="PowerShell and Git are required",
)
def test_forced_reset_skips_modified_wrapper_and_removes_runtime(tmp_path: Path) -> None:
    runtime = ROOT / ".runtime"
    runtime_preexisting = runtime.exists()
    if runtime_preexisting and any(runtime.iterdir()):
        pytest.skip("existing runtime data must not be changed by this test")
    docker_root = runtime / "moodle-docker"
    wrapper = docker_root / "bin" / "moodle-docker-compose"
    marker = tmp_path / "wrapper-ran"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=docker_root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "bin/moodle-docker-compose"], cwd=docker_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "baseline",
        ],
        cwd=docker_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/moodlehq/moodle-docker.git"],
        cwd=docker_root,
        check=True,
    )
    wrapper.write_text(f"#!/usr/bin/env sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Reset",
                "-Force",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        removed = not docker_root.exists()
    finally:
        if runtime_preexisting and docker_root.exists():
            shutil.rmtree(docker_root)
        elif runtime.exists():
            shutil.rmtree(runtime)
    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert removed
    assert not marker.exists()
    assert "dirty or contains untracked files" in output
    assert "Docker daemon" not in output


@pytest.mark.skipif(
    shutil.which("powershell") is None
    and shutil.which("pwsh") is None
    or shutil.which("git") is None,
    reason="PowerShell and Git are required",
)
def test_status_rejects_dirty_runtime_sources_before_docker(tmp_path: Path) -> None:
    runtime = ROOT / ".runtime"
    runtime_preexisting = runtime.exists()
    if runtime_preexisting and any(runtime.iterdir()):
        pytest.skip("existing runtime data must not be changed by this test")
    docker_root = runtime / "moodle-docker"
    moodle_root = runtime / "moodle"
    wrapper = docker_root / "bin" / "moodle-docker-compose"
    marker = tmp_path / "wrapper-ran"
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None

    def initialise_repository(root: Path, tracked_file: Path, contents: str, origin: str) -> None:
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(contents, encoding="utf-8")
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "add", tracked_file.relative_to(root).as_posix()], cwd=root, check=True
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.test",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "baseline",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=root, check=True)

    def status() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Status",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

    try:
        initialise_repository(
            docker_root,
            wrapper,
            "#!/usr/bin/env sh\nexit 0\n",
            "https://github.com/moodlehq/moodle-docker.git",
        )
        wrapper.write_text(f"#!/usr/bin/env sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
        subprocess.run(
            ["git", "update-index", "--assume-unchanged", "bin/moodle-docker-compose"],
            cwd=docker_root,
            check=True,
        )
        assumed_wrapper = status()
        assumed_output = assumed_wrapper.stdout + assumed_wrapper.stderr
        assert assumed_wrapper.returncode != 0
        assert "Git index visibility flag" in assumed_output
        assert "Docker daemon" not in assumed_output
        assert not marker.exists()
        subprocess.run(
            ["git", "update-index", "--no-assume-unchanged", "bin/moodle-docker-compose"],
            cwd=docker_root,
            check=True,
        )
        subprocess.run(
            ["git", "update-index", "--skip-worktree", "bin/moodle-docker-compose"],
            cwd=docker_root,
            check=True,
        )
        skipped_wrapper = status()
        skipped_output = skipped_wrapper.stdout + skipped_wrapper.stderr
        assert skipped_wrapper.returncode != 0
        assert "Git index visibility flag" in skipped_output
        assert "Docker daemon" not in skipped_output
        assert not marker.exists()
        subprocess.run(
            ["git", "update-index", "--no-skip-worktree", "bin/moodle-docker-compose"],
            cwd=docker_root,
            check=True,
        )
        dirty_wrapper = status()
        wrapper_output = dirty_wrapper.stdout + dirty_wrapper.stderr
        assert dirty_wrapper.returncode != 0
        assert "dirty or contains untracked files" in wrapper_output
        assert "Docker daemon" not in wrapper_output
        assert not marker.exists()

        wrapper.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        initialise_repository(
            moodle_root,
            moodle_root / "README",
            "baseline\n",
            "https://github.com/moodle/moodle.git",
        )
        (moodle_root / "README").write_text("modified\n", encoding="utf-8")
        dirty_moodle = status()
        moodle_output = dirty_moodle.stdout + dirty_moodle.stderr
        assert dirty_moodle.returncode != 0
        assert "Moodle runtime source has tracked changes" in moodle_output
        assert "Docker daemon" not in moodle_output
        assert not marker.exists()
    finally:
        subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Reset",
                "-Force",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        if runtime_preexisting and runtime.exists() and any(runtime.iterdir()):
            pytest.fail("test did not restore the pre-existing empty runtime directory")
        if not runtime_preexisting and runtime.exists():
            runtime.rmdir()


@pytest.mark.skipif(
    shutil.which("powershell") is None
    and shutil.which("pwsh") is None
    or shutil.which("git") is None,
    reason="PowerShell and Git are required",
)
def test_status_rejects_ignored_payloads_and_active_hooks_before_docker(tmp_path: Path) -> None:
    runtime = ROOT / ".runtime"
    runtime_preexisting = runtime.exists()
    if runtime_preexisting and any(runtime.iterdir()):
        pytest.skip("existing runtime data must not be changed by this test")
    docker_root = runtime / "moodle-docker"
    moodle_root = runtime / "moodle"
    wrapper = docker_root / "bin" / "moodle-docker-compose"
    template = docker_root / "config.docker-template.php"
    config = moodle_root / "config.php"
    marker = tmp_path / "hook-ran"
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None

    def initialise_repository(root: Path, origin: str) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.test",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "baseline",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=root, check=True)

    def status() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Status",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

    try:
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        template.write_text("<?php // pinned template\n", encoding="utf-8")
        initialise_repository(docker_root, "https://github.com/moodlehq/moodle-docker.git")
        moodle_root.mkdir(parents=True)
        (moodle_root / ".gitignore").write_text("config.php\npayload/\n", encoding="utf-8")
        (moodle_root / "README").write_text("baseline\n", encoding="utf-8")
        config.write_text("<?php // pinned template\n", encoding="utf-8")
        initialise_repository(moodle_root, "https://github.com/moodle/moodle.git")
        config.write_text("<?php // pinned template\n", encoding="utf-8")

        hook = docker_root / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/usr/bin/env sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
        active_hook = status()
        hook_output = active_hook.stdout + active_hook.stderr
        assert active_hook.returncode != 0
        assert "Refusing active Git hook" in hook_output
        assert "Docker daemon" not in hook_output
        assert not marker.exists()
        hook.unlink()

        exclude = moodle_root / ".git" / "info" / "exclude"
        original_exclude = exclude.read_text(encoding="utf-8")
        exclude.write_text(original_exclude + "payload/\n", encoding="utf-8")
        excluded_payload = status()
        exclude_output = excluded_payload.stdout + excluded_payload.stderr
        assert excluded_payload.returncode != 0
        assert "Git info/exclude entries" in exclude_output
        assert "Docker daemon" not in exclude_output
        exclude.write_text(original_exclude, encoding="utf-8")

        config.write_text("<?php // modified config\n", encoding="utf-8")
        modified_config = status()
        config_output = modified_config.stdout + modified_config.stderr
        assert modified_config.returncode != 0
        assert "does not exactly match pinned" in config_output
        assert "config.docker-template.php" in config_output
        assert "Docker daemon" not in config_output

        config.write_text("<?php // pinned template\n", encoding="utf-8")
        payload = moodle_root / "payload" / "ignored.txt"
        payload.parent.mkdir()
        payload.write_text("ignored payload\n", encoding="utf-8")
        ignored_payload = status()
        payload_output = ignored_payload.stdout + ignored_payload.stderr
        assert ignored_payload.returncode != 0
        assert "ignored payloads" in payload_output
        assert "Docker daemon" not in payload_output
    finally:
        subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Reset",
                "-Force",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        if runtime_preexisting and runtime.exists() and any(runtime.iterdir()):
            pytest.fail("test did not restore the pre-existing empty runtime directory")
        if not runtime_preexisting and runtime.exists():
            runtime.rmdir()


@pytest.mark.skipif(
    shutil.which("powershell") is None
    and shutil.which("pwsh") is None
    or shutil.which("git") is None,
    reason="PowerShell and Git are required",
)
def test_status_canonically_rejects_unsafe_git_config_before_docker(
    tmp_path: Path,
) -> None:
    runtime = ROOT / ".runtime"
    runtime_preexisting = runtime.exists()
    if runtime_preexisting and any(runtime.iterdir()):
        pytest.skip("existing runtime data must not be changed by this test")
    docker_root = runtime / "moodle-docker"
    wrapper = docker_root / "bin" / "moodle-docker-compose"
    marker = tmp_path / "unsafe-git-config-ran"
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None

    def status() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Status",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

    try:
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(f"#!/usr/bin/env sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=docker_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "bin/moodle-docker-compose"], cwd=docker_root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.test",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "baseline",
            ],
            cwd=docker_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/moodlehq/moodle-docker.git"],
            cwd=docker_root,
            check=True,
        )
        config_path = docker_root / ".git" / "config"
        baseline_config = config_path.read_text(encoding="utf-8")

        config_path.write_text(
            baseline_config + "\n[core]\n\tbare = true\n", encoding="utf-8"
        )
        bare_result = status()
        bare_output = bare_result.stdout + bare_result.stderr
        assert bare_result.returncode != 0
        assert "core.bare" in bare_output
        assert not marker.exists()

        config_path.write_text(
            baseline_config + '\n[branch "evil"]\n\tremote = origin\n', encoding="utf-8"
        )
        branch_result = status()
        branch_output = branch_result.stdout + branch_result.stderr
        assert branch_result.returncode != 0
        assert "branch.evil.remote" in branch_output
        assert not marker.exists()

        config_path.write_text(
            baseline_config
            + f'\n[filter "evil"] ; comment\n\tclean = touch "{marker.as_posix()}"\n',
            encoding="utf-8",
        )
        filter_result = status()
        filter_output = filter_result.stdout + filter_result.stderr
        assert filter_result.returncode != 0
        assert "filter.evil.clean" in filter_output
        assert not marker.exists()

        config_path.write_text(
            baseline_config + f'\n[core] # comment\n\thooksPath = "{tmp_path.as_posix()}"\n',
            encoding="utf-8",
        )
        core_result = status()
        core_output = core_result.stdout + core_result.stderr
        assert core_result.returncode != 0
        assert "core.hookspath" in core_output
        assert not marker.exists()

        worktree_config = docker_root / ".git" / "config.worktree"
        worktree_config.write_text(
            f'[core]\n\thooksPath = "{tmp_path.as_posix()}"\n', encoding="utf-8"
        )
        config_path.write_text(
            baseline_config + "\n[extensions]\n\tworktreeConfig = true\n", encoding="utf-8"
        )
        worktree_result = status()
        worktree_output = worktree_result.stdout + worktree_result.stderr
        assert worktree_result.returncode != 0
        assert "extensions.worktreeconfig" in worktree_output
        assert not marker.exists()

        config_path.write_text(baseline_config, encoding="utf-8")
        worktree_config.unlink()
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.test",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "replacement target",
            ],
            cwd=docker_root,
            check=True,
            capture_output=True,
            text=True,
        )
        replaced = subprocess.run(
            ["git", "rev-parse", "HEAD^"],
            cwd=docker_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        replacement_target = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=docker_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "replace", replaced, replacement_target], cwd=docker_root, check=True
        )
        replacement_result = status()
        replacement_output = replacement_result.stdout + replacement_result.stderr
        assert replacement_result.returncode != 0
        assert "Git replacement objects" in replacement_output
        assert not marker.exists()
    finally:
        subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Action",
                "Reset",
                "-Force",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        if runtime_preexisting and runtime.exists() and any(runtime.iterdir()):
            pytest.fail("test did not restore the pre-existing empty runtime directory")
        if not runtime_preexisting and runtime.exists():
            runtime.rmdir()


@pytest.mark.skipif(
    shutil.which("powershell") is None and shutil.which("pwsh") is None,
    reason="PowerShell is not available",
)
def test_smoke_rejects_non_loopback_persisted_token_before_http(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    token_path = tmp_path / "moodle-token.json"
    marker = tmp_path / "http-was-called"
    token_path.write_text(
        '{"token":"ignored","baseUrl":"https://example.invalid"}', encoding="utf-8"
    )
    prefix = read(SCRIPT).split("\nswitch ($Action) {", maxsplit=1)[0]
    driver = tmp_path / "smoke-token-origin.ps1"
    driver.write_text(
        prefix
        + f"\n$TokenPath = '{token_path.as_posix()}'\n"
        + "function Invoke-MoodleRest {"
        + f" New-Item -ItemType File -Path '{marker.as_posix()}' -Force | Out-Null"
        + "; return [PSCustomObject]@{} }\n"
        + "try { Invoke-Smoke; exit 1 } catch { Write-Output $_.Exception.Message; exit 0 }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "not an allowed configured local endpoint" in result.stdout
    assert not marker.exists()


def test_script_smokes_real_rest_discovery_contract() -> None:
    script = read(SCRIPT)
    for function in (
        "core_webservice_get_site_info",
        "core_enrol_get_users_courses",
        "core_course_get_contents",
    ):
        assert function in script
    assert "Moodle REST call $Function returned an error response" in script
    assert "ASIX-LAB" in script
    assert "AutoTask assignment" in script


def test_documentation_explains_development_only_image_evidence_and_pins() -> None:
    docs = read(DOCS)
    assert "development-only" in docs
    assert "127.0.0.1:8000" in docs
    assert "not claimed to be bit-for-bit" in docs


def test_documentation_explains_safe_private_bind_revert_firewall_and_persistence() -> None:
    docs = read(DOCS)
    readme = read(ROOT / "README.md")
    for text in (
        "MOODLE_AUTOTASK_BIND_IP",
        "Remove-Item Env:MOODLE_AUTOTASK_BIND_IP",
        "Run `Bootstrap` or `Up`",
        "unless-stopped",
        "Docker Desktop must itself be configured to start automatically",
        "-RemoteAddress '100.64.0.0/10'",
        "-LocalPort 8000",
        "-InterfaceAlias Tailscale",
        "<TAILSCALE_LOCAL_IPV4>",
    ):
        assert text in docs
    assert "validated private/Tailscale opt-in" in readme
    assert "MOODLE_AUTOTASK_BIND_IP = '<TAILSCALE_LOCAL_IPV4>'" in docs
    assert ".runtime/moodle-images.json" in docs
    assert "https://github.com/moodlehq/moodle-docker" in docs
