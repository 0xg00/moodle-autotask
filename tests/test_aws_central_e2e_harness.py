import base64
import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "aws-central-e2e.ps1"
MOODLE = ROOT / "scripts" / "moodle.ps1"
FIXTURE = ROOT / "infra" / "moodle" / "fixture.php"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_harness_requires_explicit_scoped_live_inputs_and_bounded_timeout() -> None:
    script = read(HARNESS)
    for parameter in (
        "$AccountId",
        "$Region",
        "$Profile",
        "$ControllerInstanceId",
        "$RunId",
        "$MoodleTokenFile",
    ):
        assert parameter in script
    assert "[ValidateRange(300, 14400)]" in script
    assert "maxNewEventsPerCycle = 1" in script
    assert "courseShortnames = @($course)" in script
    assert "scheduler.json.backup" in script
    assert "Get-RestoreScopeScript" in script


def test_run_id_contract_is_exact_and_shared_with_disposable_fixture() -> None:
    expected = r"^[a-z0-9](?:[a-z0-9-]{6,38}[a-z0-9])$"
    assert expected in read(HARNESS)
    assert "AUTOTASK_LIVE_E2E_PREFIX" in read(FIXTURE)
    assert "AUTOTASK_LIVE_E2E_ASSIGNMENT_PREFIX" in read(FIXTURE)
    pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]{6,38}[a-z0-9])$")
    assert pattern.fullmatch("run-0001")
    for malformed in ("", "short", "RUN-0001", "run_0001", "run-0001-", "a" * 41):
        assert pattern.fullmatch(malformed) is None


def test_no_automatic_telegram_decision_or_moodle_submission_is_possible() -> None:
    script = read(HARNESS)
    assert "Awaiting the real Telegram" in script
    assert "this harness never submits" in script
    assert "callback" not in script.lower()
    assert "mod_assign_save_submission" not in script
    assert "mod_assign_submit_for_grading" not in script
    assert "LiveE2EPrepare" in script
    assert "LiveE2EInspect" in script
    assert "LiveE2ECleanup" in script


def test_all_persisted_evidence_phases_exclude_credentials() -> None:
    persisted = [line for line in read(HARNESS).splitlines() if "Add-Phase -Name" in line]
    names = []
    for line in persisted:
        match = re.search(r"'([^']+)'", line)
        assert match is not None
        names.append(match.group(1))
    assert names == [
        "local-token-file-validated",
        "scheduler-restored",
        "zero-lab-compute-verified",
        "controller-preflight",
        "fixture-cleaned",
        "repository-preflight",
        "controller-preflight",
        "fixture-prepared",
        "scheduler-scoped",
        "start-approved",
        "central-execution-verified",
        "moodle-submission-verified",
        "fixture-cleaned",
    ]
    for line, name in zip(persisted, names, strict=True):
        assert "token" not in line.lower() or name == "local-token-file-validated"


def test_run_preflight_evidence_persists_the_validated_release_digest() -> None:
    script = read(HARNESS)
    start = script.index("    $gitStatus =")
    end = script.index("    $fixture = Invoke-MoodleFixture", start)
    run_preflight = script[start:end]
    validation = "$controllerPreflight.release -notmatch '^[0-9a-f]{64}$'"
    persisted = (
        "Add-Phase -Name 'controller-preflight' -Data "
        "([ordered]@{ servicesActive = $true; credentialBound = $true; "
        "release = $controllerPreflight.release })"
    )
    assert validation in run_preflight
    assert persisted in run_preflight
    assert run_preflight.index(validation) < run_preflight.index(persisted)


def test_harness_fails_closed_on_ambiguity_timeout_and_tamper() -> None:
    script = read(HARNESS)
    fixture = read(FIXTURE)
    assert "ambiguous run event" in script
    assert 'Fail "Timed out while waiting for $Gate."' in script
    for message in (
        "live e2e course collision",
        "live e2e course metadata is tampered",
        "live e2e assignment is tampered",
        "live e2e course module set is tampered",
        "live e2e enrolment is tampered",
    ):
        assert message in fixture
    assert "delete_course($row['course'], false)" in fixture
    assert "fixture_state() !== 'complete-v4'" in fixture
    assert "live_e2e_module_footprint_valid" in fixture
    assert "get_string('namenews', 'forum')" in fixture
    assert "cm.idnumber AS cmidnumber" in fixture
    assert "$assignment->idnumber" not in fixture
    assert "($forum['type'] ?? null) === 'news'" in fixture


def test_pinned_moodle_sources_define_the_real_forum_and_assignment_contract() -> None:
    forum_lib = ROOT / ".runtime" / "moodle" / "public" / "mod" / "forum" / "lib.php"
    assign_schema = (
        ROOT / ".runtime" / "moodle" / "public" / "mod" / "assign" / "db" / "install.xml"
    )
    if not forum_lib.is_file() or not assign_schema.is_file():
        pytest.skip("Pinned Moodle checkout is unavailable")
    assert 'get_string("namenews", "forum")' in read(forum_lib)
    assert re.search(r'<FIELD NAME="idnumber"', read(assign_schema)) is None


def test_central_provenance_bundle_and_zero_lab_assertions_are_canonical() -> None:
    script = read(HARNESS)
    for value in (
        "central_planner",
        "central_executor",
        "central_reviewer",
        "reviewerAccepted",
        "artifactManifestDigest",
        "artifactBundleDigest",
        "zip manifest mismatch",
        "zip content mismatch",
        "describe-instances",
        "describe-import-image-tasks",
        "describe-images",
        "describe-snapshots",
    ):
        assert value in script
    assert "AUTOTASK_E2E_JSON=" in script
    assert "StandardOutputContent" in script
    assert "raw SSM" not in script


def test_failure_preserves_fixture_and_finally_restores_controller_scope() -> None:
    script = read(HARNESS)
    assert "$script:ScopeApplied = $true" in script
    assert "finally {" in script
    assert "restore-scheduler-scope" in script
    assert "LiveE2ECleanup" in script
    cleanup_call = "$cleanup = Invoke-MoodleFixture -FixtureAction 'LiveE2ECleanup'"
    cleanup_after_success = script.index(cleanup_call)
    completion = script.index("$script:Evidence.status = 'success'", cleanup_after_success)
    assert cleanup_after_success < completion
    assert "Write-Evidence" in script


def test_moodle_wrapper_exposes_only_guarded_disposable_operations() -> None:
    script = read(MOODLE)
    for action in ("LiveE2EPrepare", "LiveE2EInspect", "LiveE2ECleanup"):
        assert f"'{action}'" in script
    assert "Live E2E fixture actions require -RunId." in script
    assert "Invoke-MoodleDocker" in script
    assert "moodle-autotask-fixture.php" in script


def test_submission_gate_handles_absent_then_awaiting_then_submitted_under_strict_mode(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    script = read(HARNESS)
    helper_start = script.index("function Get-OptionalPropertyValue")
    helper_end = script.index("function Add-Phase")
    start = script.index("function Wait-ControllerState")
    end = script.index("if ([string]::IsNullOrWhiteSpace($EvidencePath))")
    function = script[start:end]
    driver = tmp_path / "submission-gate.ps1"
    driver.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        "function Fail { param([string]$Message) throw $Message }\n"
        "function Start-Sleep { param([int]$Seconds) }\n"
        "function Get-ControllerReadScript { return '' }\n"
        "$PollSeconds = 5\n"
        "$script:states = @(\n"
        "  [PSCustomObject]@{ state = 'executed'; decision = 'approved' },\n"
        "  [PSCustomObject]@{ state = 'executed'; decision = 'approved';\n"
        "    submission = [PSCustomObject]@{ status = 'awaiting_approval' } },\n"
        "  [PSCustomObject]@{ state = 'executed'; decision = 'approved';\n"
        "    submission = [PSCustomObject]@{ status = 'submitted' } }\n)\n"
        "function Invoke-ControllerScript {\n"
        "  param([string]$Name, [string]$Script)\n"
        "  $next = $script:states[0]\n"
        "  $script:states = @($script:states | Select-Object -Skip 1)\n"
        "  return $next\n}\n"
        + script[helper_start:helper_end]
        + function
        + "\n$deadline = (Get-Date).ToUniversalTime().AddSeconds(10)\n"
        + "$result = @(Wait-ControllerState -Gate Submission -Deadline $deadline)[-1]\n"
        + "if ($result.submission.status -ne 'submitted') { exit 7 }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_fixture_pure_contract_runs_in_pinned_moodle_php_container(tmp_path: Path) -> None:
    docker = shutil.which("docker") or shutil.which("docker.exe")
    if docker is None:
        pytest.skip("Docker is unavailable")
    container = "moddle_autotask_moodle-webserver-1"
    unique = uuid.uuid4().hex
    fixture_path = f"/tmp/autotask-e2e-{unique}.php"
    driver_path = f"/tmp/autotask-e2e-{unique}-driver.php"
    driver = tmp_path / "driver.php"
    driver.write_text(
        "<?php\n"
        "define('AUTOTASK_FIXTURE_LIBRARY', true); define('FORMAT_HTML', '1');\n"
        "function get_string($name, $component) { if ($name !== 'namenews' || $component !== 'forum') exit(1); return 'Announcements'; }\n"  # noqa: E501
        f"require '{fixture_path}';\n"
        "$spec = live_e2e_spec('run-0001');\n"
        "$assign = ['cmidnumber'=>$spec['assignment_idnumber'],'name'=>$spec['assignment_name'],"
        "'intro'=>$spec['assignment_intro'],'introformat'=>1,'submissionattachments'=>1,"
        "'submissiondrafts'=>0,'requiresubmissionstatement'=>0];\n"
        "$forum = ['name'=>'Announcements','type'=>'news','cmidnumber'=>''];\n"
        "if (!live_e2e_module_footprint_valid($assign,$forum,1,1,2,$spec)) exit(2);\n"
        "if (live_e2e_module_footprint_valid($assign,$forum,1,2,3,$spec)) exit(3);\n"
        "$student = (object)['username'=>'student1'];\n"
        "if (!live_e2e_enrolment_valid([7=>$student],['student'],7)) exit(4);\n"
        "if (live_e2e_enrolment_valid([7=>$student],['editingteacher'],7)) exit(5);\n"
        "foreach (['BAD','short','run_0001','run-0001-'] as $bad) { try { live_e2e_run_id($bad); exit(6); } catch (InvalidArgumentException $e) {} }\n"  # noqa: E501
        "echo 'fixture-pure-contract-ok';\n",
        encoding="utf-8",
    )
    try:
        copied = subprocess.run(
            [docker, "cp", str(FIXTURE), f"{container}:{fixture_path}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if copied.returncode != 0:
            pytest.skip("Pinned Moodle PHP container is unavailable")
        copied_driver = subprocess.run(
            [docker, "cp", str(driver), f"{container}:{driver_path}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert copied_driver.returncode == 0, copied_driver.stderr
        result = subprocess.run(
            [docker, "exec", container, "php", driver_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "fixture-pure-contract-ok"
    finally:
        subprocess.run(
            [docker, "exec", container, "rm", "-f", fixture_path, driver_path],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def _generate_scope_scripts(tmp_path: Path, run_id: str) -> tuple[Path, Path]:
    """Extract and execute the production PowerShell generators, never a copy."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise RuntimeError("PowerShell is required for scope transaction tests")
    source = read(HARNESS)
    helper_start = source.index("function ConvertTo-Base64Utf8")
    helper_end = source.index("function Invoke-ControllerScript")
    start = source.index("function Get-ScopeScript")
    end = source.index("function Get-ControllerReadScript")
    scope = tmp_path / f"scope-{run_id}.sh"
    restore = tmp_path / f"restore-{run_id}.sh"
    driver = tmp_path / f"generate-scope-{run_id}.ps1"
    driver.write_text(
        "Set-StrictMode -Version Latest\n"
        f"$RunId = '{run_id}'\n"
        + source[helper_start:helper_end]
        + source[start:end]
        + f"\n[IO.File]::WriteAllText('{scope}', (Get-ScopeScript), (New-Object Text.UTF8Encoding($false)))\n"  # noqa: E501
        + f"[IO.File]::WriteAllText('{restore}', (Get-RestoreScopeScript), (New-Object Text.UTF8Encoding($false)))\n",  # noqa: E501
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert scope.is_file(), (  # noqa: E501
        result.stdout + result.stderr + "\n" + driver.read_text(encoding="utf-8")
    )
    assert restore.is_file(), result.stdout + result.stderr
    return scope, restore


def _generate_controller_read_script(tmp_path: Path, run_id: str) -> Path:
    """Generate the production reader through PowerShell, without copying its logic."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise RuntimeError("PowerShell is required for controller reader tests")
    source = read(HARNESS)
    start = source.index("function Get-ControllerReadScript")
    end = source.index("function Restore-SchedulerScope")
    reader = tmp_path / "controller-read.sh"
    driver = tmp_path / "generate-controller-read.ps1"
    driver.write_text(
        "Set-StrictMode -Version Latest\n"
        f"$RunId = '{run_id}'\n"
        + source[start:end]
        + f"\n[IO.File]::WriteAllText('{reader}', (Get-ControllerReadScript), "
        "(New-Object Text.UTF8Encoding($false)))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert reader.is_file(), result.stdout + result.stderr
    return reader


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    )


def _namespaced_identity(prefix: str, marker: str) -> str:
    return prefix + _sha256_bytes(marker.encode("ascii"))


def _write_controller_reader_fixture(root: Path, tamper: str | None) -> dict[str, object]:
    """Create producer-shaped data; the generated reader remains the only verifier."""
    event = _namespaced_identity("moodle-notification-event-v1:", "event")
    task = _namespaced_identity("moodle-task-v1:", "task")
    revision = _namespaced_identity("moodle-assignment-v1:", "revision")
    course = "AUTOTASK-LIVE-E2E-run-0001"
    assignment_id = 123
    report = "# Completed\n"
    report_digest = _sha256_bytes(report.encode("utf-8"))
    contents = [("alpha.txt", b"alpha\n"), ("zeta.txt", b"zeta\n")]
    files: list[dict[str, object]] = [
        {"path": path, "size": len(content), "sha256": _sha256_bytes(content)}
        for path, content in contents
    ]
    manifest: dict[str, object] = {
        "kind": "artifact-manifest-v1",
        "files": files,
        "totals": {"files": len(files), "bytes": sum(len(value) for _, value in contents)},
    }
    if tamper == "artifact-totals":
        manifest["totals"] = {"files": 1, "bytes": 1}
    elif tamper == "artifact-order":
        manifest["files"] = list(reversed(files))
    elif tamper == "artifact-item-digest":
        files[0]["sha256"] = "0" * 64
    elif tamper == "artifact-casefold-collision":
        files[0]["path"] = "Alpha.txt"
        files[1]["path"] = "alpha.txt"

    archive_contents = list(contents)
    if tamper == "zip-content":
        archive_contents[0] = ("alpha.txt", b"Xlpha\n")
    compression = zipfile.ZIP_DEFLATED if tamper == "zip-compression" else zipfile.ZIP_STORED
    timestamp = (1980, 1, 2, 0, 0, 0) if tamper == "zip-date" else (1980, 1, 1, 0, 0, 0)
    bundle_stream = io.BytesIO()
    with zipfile.ZipFile(bundle_stream, "w", compression=compression) as archive:
        if tamper == "zip-comment":
            archive.comment = b"unexpected"
        for path, content in archive_contents:
            info = zipfile.ZipInfo(path, date_time=timestamp)
            info.compress_type = compression
            info.external_attr = (
                (0o100600 << 16)
                if tamper == "zip-external-attr"
                else (0o100640 << 16)
            )
            if tamper == "zip-extra":
                info.extra = b"\x01\x00\x00\x00"
            archive.writestr(info, content)
    bundle = bundle_stream.getvalue()
    bundle_digest = _sha256_bytes(bundle)
    if tamper == "bundle-digest":
        bundle_digest = "f" * 64
    (root / "bundles").mkdir(exist_ok=True)
    (root / "bundles" / f"{bundle_digest}.zip").write_bytes(bundle)

    artifact_manifest_digest = _canonical_digest(manifest)
    digests = {
        name: _sha256_bytes(name.encode("ascii"))
        for name in (
            "specification",
            "prepared",
            "planner",
            "executor",
            "reviewer",
            "plan",
            "planner-result",
            "executor-result",
            "reviewer-result",
        )
    }
    jobs = [
        _sha256_bytes(b"planner-job"),
        _sha256_bytes(b"executor-job"),
        _sha256_bytes(b"reviewer-job"),
    ]
    provenance: dict[str, object] = {
        "kind": "moodle-central-provenance-v2",
        "roles": ["central_planner", "central_executor", "central_reviewer"],
        "jobIds": jobs,
        "selectedMode": "central",
        "specificationDigest": digests["specification"],
        "preparedInputManifestDigest": digests["prepared"],
        "plannerJobId": jobs[0],
        "executorJobId": jobs[1],
        "reviewerJobId": jobs[2],
        "planDigest": digests["plan"],
        "plannerResultDigest": digests["planner-result"],
        "executorResultDigest": digests["executor-result"],
        "artifactManifestDigest": artifact_manifest_digest,
        "artifactBundleDigest": bundle_digest,
        "reviewerResultDigest": digests["reviewer-result"],
        "reviewerAccepted": True,
        "bundleLocator": f"bundles/{bundle_digest}.zip",
        "artifactManifest": manifest,
    }
    execution = {"provenance": provenance, "reportMarkdown": report}
    request_payload: dict[str, object] = {
        "kind": "moodle-notification-event-v1",
        "event_id": event,
        "status": "NEW",
        "task_key": task,
        "revision_digest": revision,
        "course_name": "E2E",
        "course_shortname": course,
        "assignment_title": "Acceptance",
        "allows_submissions_from": None,
        "due_date": None,
        "cutoff_date": None,
        "grading_due_date": None,
        "time_modified": 1,
        "attachments": [],
        "assignment_id": assignment_id,
        "submission_drafts": False,
        "requires_submission_statement": False,
        "submission_statement": "",
        "submission_statement_format": 0,
        "team_submission": False,
        "no_submissions": False,
        "file_submission_enabled": True,
        "file_submission_max_files": 1,
        "file_submission_max_bytes": 2097152,
        "file_submission_filetypes": ".md",
    }
    artifact: dict[str, object] = {
        "filename": f"autotask-{revision.removeprefix('moodle-assignment-v1:')[:16]}.md",
        "sizeBytes": len(report.encode("utf-8")),
        "sha256": report_digest,
    }
    submission_payload: dict[str, object] = {
        "artifacts": [artifact],
        "assignmentId": assignment_id,
        "submissionDrafts": False,
        "requireSubmissionStatement": False,
        "submissionStatement": "",
        "submissionStatementFormat": 0,
        "submissionStatementDigest": None,
        "submissionStatementPlain": None,
        "reportDigest": report_digest,
        "reportMarkdown": report,
        "revisionDigest": revision,
        "taskKey": task,
    }
    if tamper == "submission-report-content":
        # Same byte length; producer-shaped manifest and receipt are recomputed,
        # while the real execution/file digest remains unchanged.
        submission_payload["reportMarkdown"] = "! Completed\n"
    manifest_digest = _canonical_digest(submission_payload)
    submission_payload["manifestDigest"] = manifest_digest
    if tamper == "submission-manifest-digest":
        manifest_digest = "0" * 64
    elif tamper == "submission-filename":
        artifact["filename"] = "wrong.md"
    elif tamper == "submission-size":
        artifact["sizeBytes"] = 0
    elif tamper == "submission-report-digest":
        artifact["sha256"] = "0" * 64
    receipt = "invalid-receipt" if tamper == "receipt" else "moodle-submission:42"
    receipt_payload: dict[str, object] = {
        "approvedAt": 1,
        "approvedBy": 42,
        "manifestDigest": manifest_digest,
        "reference": receipt,
        "submittedAt": 3,
    }
    if tamper == "receipt-payload":
        receipt_payload["reference"] = "moodle-submission:999"
    elif tamper == "receipt-approval":
        receipt_payload["approvedBy"] = 999

    request_delivery = "prepared" if tamper == "request-prepared" else "notified"
    request_decider = 999 if tamper == "request-wrong-decider" else 42
    submission_decider = 999 if tamper == "submission-wrong-decider" else 42
    submission_delivered_at = None if tamper == "submission-undelivered" else 4
    (root / "telegram.json").write_text(
        json.dumps(
            {
                "botToken": "123456:abcdefghijklmnopqrstuvwxyzABCDE",
                "chatId": 77,
                "allowedUserId": 42,
            }
        ),
        encoding="utf-8",
    )
    (root / "telegram.json").chmod(0o600)
    if tamper == "telegram-owner":
        (root / "telegram-owner-invalid").touch()

    connection = sqlite3.connect(root / "approval.sqlite3")
    try:
        connection.executescript(
            "CREATE TABLE requests (event_id TEXT, task_key TEXT, revision_digest TEXT, "
            "decision TEXT, delivery_state TEXT, decided_by INTEGER, decided_at INTEGER, "
            "chat_id INTEGER, message_id INTEGER, payload TEXT);"
            "CREATE TABLE work_items (event_id TEXT, selected_mode TEXT, status TEXT, "
            "provision_key TEXT);"
            "CREATE TABLE execution_outbox (event_id TEXT, payload TEXT);"
            "CREATE TABLE submissions (event_id TEXT, status TEXT, manifest_digest TEXT, "
            "payload TEXT, receipt_reference TEXT, receipt_payload TEXT, decided_by INTEGER, "
            "decided_at INTEGER);"
            "CREATE TABLE submission_outbox (event_id TEXT, delivered_at INTEGER);"
        )
        connection.execute(
            "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event,
                task,
                revision,
                "approved",
                request_delivery,
                request_decider,
                1,
                77,
                2,
                json.dumps(request_payload),
            ),
        )
        connection.execute(
            "INSERT INTO work_items VALUES (?, ?, ?, ?)",
            (event, "central", "ready", _sha256_bytes(b"provision")),
        )
        connection.execute(
            "INSERT INTO execution_outbox VALUES (?, ?)", (event, json.dumps(execution)),
        )
        connection.execute(
            "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event,
                "submitted",
                manifest_digest,
                json.dumps(submission_payload),
                receipt,
                json.dumps(receipt_payload),
                submission_decider,
                1,
            ),
        )
        connection.execute(
            "INSERT INTO submission_outbox VALUES (?, ?)", (event, submission_delivered_at)
        )
        connection.commit()
    finally:
        connection.close()
    return {"event": event, "task": task, "revision": revision, "course": course}


def _run_generated_controller_reader(root: Path, reader: Path) -> subprocess.CompletedProcess[str]:
    generated = reader.read_text(encoding="utf-8")
    generated = generated.replace(
        "db='/var/lib/moodle-autotask/approval.sqlite3'", "db='/work/approval.sqlite3'"
    ).replace(
        "path='/var/spool/moodle-autotask/results/bundles/'+bundle+'.zip'",
        "path='/work/bundles/'+bundle+'.zip'",
    ).replace(
        "read_json('/etc/moodle-autotask/telegram.json')", "read_json('/work/telegram.json')"
    )
    assert "/var/lib/moodle-autotask/approval.sqlite3" not in generated
    assert "/var/spool/moodle-autotask/results/bundles/" not in generated
    assert "/etc/moodle-autotask/telegram.json" not in generated
    runnable = root / "controller-read-runnable.sh"
    runnable.write_text(generated, encoding="utf-8")
    docker = shutil.which("docker") or shutil.which("docker.exe")
    assert docker is not None
    owner_setup = (
        "getent group moodle-autotask >/dev/null || groupadd moodle-autotask; "
        "id -u moodle-autotask >/dev/null 2>&1 || "
        "useradd -g moodle-autotask -M moodle-autotask; "
        "if test -f /work/telegram-owner-invalid; then "
        "chown root:root /work/telegram.json; else "
        "chown moodle-autotask:moodle-autotask /work/telegram.json; fi; "
        "chmod 0600 /work/telegram.json; "
    )
    return subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-v",
            f"{root}:/work",
            "python:3.12-bookworm",
            "/bin/bash",
            "-c",
            owner_setup
            +
            "sed -i 's/\\r$//' /work/controller-read-runnable.sh; "
            "exec /bin/bash /work/controller-read-runnable.sh",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_generated_controller_reader_executes_happy_path_and_tamper_matrix(
    tmp_path: Path,
) -> None:
    docker = shutil.which("docker") or shutil.which("docker.exe")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if docker is None or powershell is None:
        pytest.skip("Docker and PowerShell are required for controller reader tests")
    reader = _generate_controller_read_script(tmp_path, "run-0001")
    happy = tmp_path / "happy"
    happy.mkdir()
    expected = _write_controller_reader_fixture(happy, None)
    result = _run_generated_controller_reader(happy, reader)
    assert result.returncode == 0, result.stderr + result.stdout
    encoded = result.stdout.strip().removeprefix("AUTOTASK_E2E_JSON=")
    evidence = json.loads(base64.b64decode(encoded))
    assert evidence["state"] == "executed"
    assert evidence["bundleVerified"] is True
    assert evidence["eventId"] == expected["event"]
    assert evidence["taskKey"] == expected["task"]
    assert evidence["revisionDigest"] == expected["revision"]
    assert evidence["submission"]["filename"] == (
        "autotask-" + str(expected["revision"]).split(":", 1)[1][:16] + ".md"
    )
    for tamper in (
        "submission-manifest-digest",
        "submission-filename",
        "submission-size",
        "submission-report-digest",
        "submission-report-content",
        "receipt",
        "receipt-payload",
        "receipt-approval",
        "request-prepared",
        "request-wrong-decider",
        "submission-undelivered",
        "submission-wrong-decider",
        "telegram-owner",
        "artifact-totals",
        "artifact-order",
        "artifact-item-digest",
        "artifact-casefold-collision",
        "zip-compression",
        "zip-date",
        "zip-external-attr",
        "zip-extra",
        "zip-comment",
        "zip-content",
        "bundle-digest",
    ):
        case_root = tmp_path / tamper
        case_root.mkdir()
        _write_controller_reader_fixture(case_root, tamper)
        rejected = _run_generated_controller_reader(case_root, reader)
        assert rejected.returncode != 0, tamper + ": " + rejected.stdout + rejected.stderr


def test_scope_transaction_is_executable_durable_and_fail_closed_in_linux_docker(
    tmp_path: Path,
) -> None:
    """Run the exact generated Bash against an ephemeral Linux controller filesystem."""
    docker = shutil.which("docker") or shutil.which("docker.exe")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if docker is None or powershell is None:
        pytest.skip("Docker and PowerShell are required for scope transaction test")
    _generate_scope_scripts(tmp_path, "run-0001")
    _generate_scope_scripts(tmp_path, "run-0002")
    shutil.copyfile(tmp_path / "scope-run-0001.sh", tmp_path / "scope.sh")
    shutil.copyfile(tmp_path / "restore-run-0001.sh", tmp_path / "restore.sh")
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "systemctl").write_text(
        "#!/bin/bash\nset -eu\nstate=/work/service-state\n"
        "test -f $state || printf 'enabled=true\\nactive=true\\n' >$state\n"
        "enabled=$(sed -n 's/^enabled=//p' $state); active=$(sed -n 's/^active=//p' $state)\n"
        "case \"$1\" in\n"
        "is-enabled) test \"$enabled\" = true;; is-active) test \"$active\" = true;;\n"
        "restart) test \"${FAIL_RESTART:-0}\" = 0 || exit 91; active=true;;\n"
        "stop) test \"${FAIL_STOP:-0}\" = 0 || exit 94; active=false;; start) active=true;; enable) test ! -f /work/fail-start || exit 93; enabled=true;; disable) enabled=false;; *) exit 92;; esac\n"  # noqa: E501
        "printf 'enabled=%s\\nactive=%s\\n' $enabled $active >$state\n",
        encoding="utf-8",
    )
    (fake / "cp").write_text(
        "#!/bin/bash\nif test \"${FAIL_CP:-0}\" != 0; then exit 81; fi\nexec /bin/cp \"$@\"\n",
        encoding="utf-8",
    )
    (fake / "mv").write_text(
        "#!/bin/bash\nfor value in \"$@\"; do\n"
        "  if test \"${FAIL_SWAP:-0}\" != 0 && test \"$value\" = /etc/moodle-autotask/scheduler.json; then exit 82; fi\n"  # noqa: E501
        "done\nexec /bin/mv \"$@\"\n",
        encoding="utf-8",
    )
    (fake / "chown").write_text("#!/bin/bash\nexec /bin/chown \"$@\"\n", encoding="utf-8")
    # The product scripts only use Python to emit a bounded JSON line.  The fake
    # consumes the here-doc so this test exercises transaction code, not Python.
    (fake / "python3").write_text("#!/bin/bash\ncat >/dev/null\nprintf '{}'\n", encoding="utf-8")
    for command in fake.iterdir():
        command.chmod(0o755)
    scenario = tmp_path / "scenario.sh"
    scenario.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        "export PATH=/work/fake:$PATH\n"
        "getent group moodle-autotask >/dev/null || groupadd moodle-autotask\n"
        "id -u moodle-autotask >/dev/null 2>&1 || useradd -g moodle-autotask -M moodle-autotask\n"
        "mkdir -p /etc/moodle-autotask\n"
        "install -d -o moodle-autotask -g moodle-autotask -m 0750 /var/lib/moodle-autotask\n"
        "printf 'original-scheduler-bytes\\n' >/etc/moodle-autotask/scheduler.json\n"
        "chmod 0640 /etc/moodle-autotask/scheduler.json\n"
        "chown root:moodle-autotask /etc/moodle-autotask/scheduler.json\n"
        "run=/var/lib/moodle-autotask/e2e/active\n"
        "retired=/var/lib/moodle-autotask/e2e/.run-0001.retired\n"
        "original=$(cat /etc/moodle-autotask/scheduler.json)\n"
        # Pristine apply, process rerun, and idempotent restore preserve bytes.
        "/bin/bash /work/scope.sh >/dev/null\n"
        "test -f $run/scheduler.json.backup && test -f $run/scheduler.state\n"
        "/bin/bash /work/scope-run-0002.sh >/dev/null 2>&1 && exit 8 || true\n"
        "grep -qx 'run=run-0001' $run/scheduler.state\n"
        "test \"$(cat /etc/moodle-autotask/scheduler.json)\" != \"$original\"\n"
        "/bin/bash /work/scope.sh >/dev/null\n"
        "FAIL_STOP=1 /bin/bash /work/restore.sh >/dev/null 2>&1 && exit 18 || true\n"
        "test -f $run/scheduler.json.backup && test -f $run/scheduler.state\n"
        "test \"$(cat /etc/moodle-autotask/scheduler.json)\" != \"$original\"\n"
        "/bin/bash /work/restore.sh >/dev/null\n"
        "test \"$(cat /etc/moodle-autotask/scheduler.json)\" = \"$original\"\n"
        "test ! -e $run && grep -qx 'enabled=true' /work/service-state && grep -qx 'active=true' /work/service-state\n"  # noqa: E501
        "/bin/bash /work/restore.sh >/dev/null\n"
        "test -d $retired && test -f $retired/scheduler.json.backup && test -f $retired/scheduler.state\n"  # noqa: E501
        "/bin/bash /work/scope.sh >/dev/null 2>&1 && exit 9 || true\n"
        "rm -rf $retired\n"
        # A lost SSM response leaves a committed record; a later Cleanup restore repairs it.
        "/bin/bash /work/scope.sh >/dev/null\n"
        "test -e $run\n"
        "/bin/bash /work/restore.sh >/dev/null\n"
        "test ! -e $run\n"
        "rm -rf $retired\n"
        # The production contract deliberately refuses a scheduler that was not
        # enabled and active before the transaction.
        "printf 'enabled=false\\nactive=false\\n' >/work/service-state\n"
        "/bin/bash /work/scope.sh >/dev/null 2>&1 && exit 10 || true\n"
        "test ! -e $run\n"
        "printf 'enabled=true\\nactive=true\\n' >/work/service-state\n"
        # Faults before backup, pre-swap, and post-swap/restart retain forensic evidence.
        "FAIL_CP=1 /bin/bash /work/scope.sh >/dev/null 2>&1 && exit 11 || true\n"
        "test ! -e $run\n"
        "pending=$(find /var/lib/moodle-autotask/e2e -maxdepth 1 -name '.run-0001.pending.*')\n"
        "test $(printf '%s\\n' \"$pending\" | wc -l) = 1 && test -d \"$pending\"\n"
        "rm -rf \"$pending\"\n"
        "FAIL_SWAP=1 /bin/bash /work/scope.sh >/dev/null 2>&1 && exit 12 || true\n"
        "test -f $run/scheduler.json.backup && test -f $run/scheduler.state\n"
        "/bin/bash /work/restore.sh >/dev/null\n"
        "rm -rf $retired\n"
        "FAIL_RESTART=1 /bin/bash /work/scope.sh >/dev/null 2>&1 && exit 13 || true\n"
        "test -f $run/scheduler.json.backup && test -f $run/scheduler.state\n"
        "/bin/bash /work/scope.sh >/dev/null\n"
        "touch /work/fail-start; /bin/bash /work/restore.sh >/dev/null 2>&1 && exit 15 || true; rm -f /work/fail-start\n"  # noqa: E501
        "test -f $run/scheduler.json.backup && test -f $run/scheduler.state\n"
        "/bin/bash /work/restore.sh >/dev/null\n"
        "rm -rf $retired\n"
        # Backup digest and config/link tampering fail closed and retain the
        # exact run record.  The test resets its ephemeral controller only after
        # proving preservation, just as an operator would need explicit repair.
        "/bin/bash /work/scope.sh >/dev/null\n"
        "printf 'tampered-backup\\n' >$run/scheduler.json.backup\n"
        "/bin/bash /work/restore.sh >/dev/null 2>&1 && exit 16 || true\n"
        "test -f $run/scheduler.json.backup && test -f $run/scheduler.state\n"
        "rm -rf $run\n"
        "printf 'original-scheduler-bytes\\n' >/etc/moodle-autotask/scheduler.json\n"
        "/bin/bash /work/scope.sh >/dev/null\n"
        "mv /etc/moodle-autotask/scheduler.json /etc/moodle-autotask/scheduler.real\n"
        "ln -s /etc/moodle-autotask/scheduler.real /etc/moodle-autotask/scheduler.json\n"
        "/bin/bash /work/restore.sh >/dev/null 2>&1 && exit 17 || true\n"
        "test -L /etc/moodle-autotask/scheduler.json && test -f $run/scheduler.state\n"
        "rm -f /etc/moodle-autotask/scheduler.json; mv /etc/moodle-autotask/scheduler.real /etc/moodle-autotask/scheduler.json; rm -rf $run\n"  # noqa: E501
        # A tampered state is never removed or restored through.
        "/bin/bash /work/scope.sh >/dev/null\n"
        "printf 'tampered\\n' >$run/scheduler.state\n"
        "/bin/bash /work/restore.sh >/dev/null 2>&1 && exit 14 || true\n"
        "test -f $run/scheduler.state && test -f $run/scheduler.json.backup\n"
        "test -e $run\n",
        encoding="utf-8",
    )
    scenario.chmod(0o755)
    image = "moodlehq/moodle-php-apache:8.3"
    result = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--user",
            "0",
            "--entrypoint",
            "/bin/bash",
            "-v",
            f"{tmp_path}:/work",
            image,
            "-c",
            "sed -i 's/\\r$//' /work/scope.sh /work/restore.sh /work/scenario.sh /work/fake/*; exec /bin/bash /work/scenario.sh",  # noqa: E501
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_evidence_writer_rejects_reparse_paths_and_cleans_failed_atomic_temp(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    source = read(HARNESS)
    start = source.index("function Assert-ContainedRuntimePath")
    end = source.index("function Get-AwsCli")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "central-e2e" / "run-0001.evidence.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    driver = tmp_path / "evidence.ps1"
    driver.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        "function Fail { param([string]$Message) throw $Message }\n"
        f"$RuntimeRoot = '{runtime}'\n$RunId = 'run-0001'\n$EvidencePath = '{target}'\n"
        + f"$outside = '{outside}'\n"
        "$script:Evidence = [ordered]@{ kind='test'; runId=$RunId; phases=@() }\n"
        + source[start:end]
        + "\nWrite-Evidence\n"
        + "if (-not (Test-Path -LiteralPath $EvidencePath -PathType Leaf)) { exit 2 }\n"
        + "New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot 'broken') | Out-Null\n"
        + "$EvidencePath = Join-Path $RuntimeRoot 'broken'\n"
        + "try { Write-Evidence; exit 3 } catch {}\n"
        + "if (Get-ChildItem -LiteralPath $RuntimeRoot -Recurse -Force -Filter '.central-e2e-run-0001.*.tmp') { exit 4 }\n"  # noqa: E501
        + f"$link = Join-Path $RuntimeRoot 'link'; New-Item -ItemType SymbolicLink -Path $link -Target '{outside}' | Out-Null\n"  # noqa: E501
        + "$EvidencePath = Join-Path $link 'missing\\evidence.json'\n"
        + "try { Write-Evidence; exit 5 } catch {}\n"
        + "if (Test-Path -LiteralPath (Join-Path $outside 'missing')) { exit 6 }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
