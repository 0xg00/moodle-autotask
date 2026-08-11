from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from moddle_autotask.adapters.aws import agent_cli
from moddle_autotask.adapters.aws.agent_cli import (
    CodexSpoolRunner,
    _BundlePublicationBusy,
    _collect_artifact_bundle,
    _load_job,
)
from moddle_autotask.adapters.aws.agent_spool import (
    _MAX_RESULT_BYTES,
    AgentSpoolError,
    FileAgentBroker,
    _canonical,
    _write_exclusive,
)
from moddle_autotask.adapters.aws.artifacts import PreparedArtifact, PreparedAssignment
from moddle_autotask.adapters.aws.labs import LabTranscript
from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationAttachment,
    NotificationDraft,
    NotificationEvent,
)
from moddle_autotask.domain.models import ExecutionMode, LabHandle


@dataclass
class _S3Runner:
    body: bytes = b"verified input"
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run_json(
        self,
        arguments: tuple[str, ...],
        *,
        extra_environment: Mapping[str, str] | None = None,
    ) -> object:
        assert extra_environment is None
        self.calls.append(arguments)
        assert arguments[:2] == ("s3api", "get-object")
        Path(arguments[-1]).write_bytes(self.body)
        return {"Body": "not-returned"}


@dataclass
class _LabExecutor:
    calls: list[tuple[LabHandle, tuple[str, ...], str]] = field(default_factory=list)
    waits: list[tuple[LabHandle, str, str]] = field(default_factory=list)
    wait_failures_remaining: int = 0

    def dispatch_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> str:
        self.calls.append((handle, commands, execution_key))
        return "12345678-1234-1234-1234-123456789abc"

    def wait_powershell(
        self, handle: LabHandle, command_id: str, *, execution_key: str
    ) -> LabTranscript:
        self.waits.append((handle, command_id, execution_key))
        if self.wait_failures_remaining:
            self.wait_failures_remaining -= 1
            raise RuntimeError("simulated controller crash during wait")
        return LabTranscript(True, "command output")

    def run_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> LabTranscript:
        return self.wait_powershell(
            handle,
            self.dispatch_powershell(handle, commands, execution_key=execution_key),
            execution_key=execution_key,
        )


def _event(tmp_path: Path, *, lab: bool = False, marker: str = "a") -> NotificationEvent:
    attachments = (NotificationAttachment("input.txt", 14, "text/plain", lab),) if not lab else ()
    event = MoodleState(tmp_path / "moodle.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + marker * 64,
            "moodle-assignment-v1:" + marker * 64,
            "ASIX",
            "ASIX-M06",
            "Práctica controlada",
            0,
            100,
            0,
            0,
            1,
            attachments,
        ),
        now=1,
    )
    assert event is not None
    return event


def _prepared(event: NotificationEvent, runner: _S3Runner) -> PreparedAssignment:
    artifacts: tuple[PreparedArtifact, ...] = ()
    if event.attachments:
        artifacts = (
            PreparedArtifact(
                "moodle-attachment-v1:" + "c" * 64,
                "input.txt",
                len(runner.body),
                hashlib.sha256(runner.body).hexdigest(),
                "private-bucket",
                "assignments/input.txt",
            ),
        )
    return PreparedAssignment(
        event.task_key,
        event.revision_digest,
        artifacts,
        "ASIX",
        "ASIX-M06",
        "Práctica controlada",
        "Genera un informe verificable.",
        "d" * 64,
    )


def _write_result(
    results: Path,
    job: Path,
    *,
    succeeded: bool = True,
    commands: list[str] | None = None,
) -> None:
    payload = json.loads((job / "job.json").read_text(encoding="utf-8"))
    result = {
        "kind": "moodle-agent-result-v1",
        "jobId": payload["jobId"],
        "phase": payload["phase"],
        "succeeded": succeeded,
        "summary": "done" if succeeded else "failed",
        "reportMarkdown": "# Informe\nEvidencia.",
        "powershellCommands": [] if commands is None else commands,
    }
    (results / f"{payload['jobId']}.json").write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def _job_for_phase(jobs: Path, phase: str) -> Path:
    return next(
        job
        for job in jobs.iterdir()
        if job.is_dir()
        and not job.is_symlink()
        and (job / "job.json").is_file()
        and json.loads((job / "job.json").read_text(encoding="utf-8"))["phase"] == phase
    )


def _ready_lab_dispatch(
    broker: FileAgentBroker,
    jobs: Path,
    results: Path,
    event: NotificationEvent,
    prepared: PreparedAssignment,
    executor: _LabExecutor,
    handle: LabHandle,
) -> tuple[str, str, tuple[str, ...]]:
    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == "pending"
    plan = _job_for_phase(jobs, "lab_plan")
    commands = ("Write-Output 'ok'",)
    _write_result(results, plan, commands=list(commands))
    plan_result = json.loads((results / f"{plan.name}.json").read_text(encoding="utf-8"))
    plan_digest = hashlib.sha256(_canonical(plan_result)).hexdigest()
    return broker._job_id("lab_report", event, plan_digest), plan_digest, commands


def _central_plan() -> dict[str, object]:
    return {
        "steps": ["Produce the report."],
        "acceptanceCriteria": [{"id": "report", "text": "A report exists."}],
        "expectedArtifacts": ["report.md"],
    }


def _seed_executor_job(
    tmp_path: Path,
) -> tuple[
    FileAgentBroker,
    Path,
    Path,
    _S3Runner,
    NotificationEvent,
    PreparedAssignment,
    Path,
    dict[str, object],
]:
    jobs, results = tmp_path / "jobs", tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    prepared = _prepared(event, runner)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    assert (
        broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor()).status
        == "pending"
    )
    planner_job = next(jobs.iterdir())
    plan = _central_plan()
    planner: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": planner_job.name,
        "role": "central_planner",
        "succeeded": True,
        "summary": "plan ready",
        "reportMarkdown": "# Informe\nPlan verified.",
        "plan": plan,
        "planDigest": hashlib.sha256(_canonical(plan)).hexdigest(),
    }
    planner["plannerResultDigest"] = hashlib.sha256(_canonical(planner)).hexdigest()
    (results / f"{planner_job.name}.json").write_bytes(_canonical(planner))
    assert (
        broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor()).status
        == "pending"
    )
    executor_job = next(
        job
        for job in jobs.iterdir()
        if json.loads((job / "job.json").read_text(encoding="utf-8"))["role"] == "central_executor"
    )
    return broker, jobs, results, runner, event, prepared, executor_job, plan


def test_runner_publishes_terminal_failure_for_invalid_executor_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker, jobs, results, _runner, event, prepared, executor_job, _plan = _seed_executor_job(
        tmp_path
    )

    def invalid_executor(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        workspace = Path(arguments[arguments.index("-C") + 1])
        outputs = workspace / "outputs"
        outputs.mkdir()
        (outputs / "report.md").write_bytes(b"verified\n")
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": True,
                    "summary": "executed",
                    "reportMarkdown": "# Informe\nArtifact verified.",
                    "evidence": {"wrong": "outputs/report.md"},
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moddle_autotask.adapters.aws.agent_cli.subprocess.run", invalid_executor)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    result = json.loads((results / f"{executor_job.name}.json").read_text(encoding="utf-8"))
    assert result["kind"] == "moodle-agent-result-v2"
    assert result["role"] == "central_executor"
    assert result["succeeded"] is False
    assert list((results / "bundles").glob("*.zip")) == []
    assert (
        broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor()).status
        == "failed"
    )


def test_runner_publishes_terminal_failure_for_invalid_reviewer_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker, jobs, results, _runner, event, prepared, executor_job, plan = _seed_executor_job(
        tmp_path
    )
    outputs = tmp_path / "verified-outputs"
    outputs.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    manifest, bundle_digest = _collect_artifact_bundle(outputs, ["report.md"], results / "bundles")
    executor_result: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": executor_job.name,
        "role": "central_executor",
        "succeeded": True,
        "summary": "executed",
        "reportMarkdown": "# Informe\nArtifact verified.",
        "evidence": {"report": "outputs/report.md"},
        "artifactManifest": manifest,
        "artifactManifestDigest": hashlib.sha256(_canonical(manifest)).hexdigest(),
        "artifactBundleDigest": bundle_digest,
        "bundleLocator": f"bundles/{bundle_digest}.zip",
    }
    executor_result["executorResultDigest"] = hashlib.sha256(
        _canonical(executor_result)
    ).hexdigest()
    (results / f"{executor_job.name}.json").write_bytes(_canonical(executor_result))
    assert (
        broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor()).status
        == "pending"
    )
    reviewer_job = next(
        job
        for job in jobs.iterdir()
        if json.loads((job / "job.json").read_text(encoding="utf-8"))["role"] == "central_reviewer"
    )

    def invalid_reviewer(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": True,
                    "summary": "reviewed",
                    "reportMarkdown": "# Informe\nReview verified.",
                    "accepted": True,
                    "decisions": {"wrong": "accepted"},
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moddle_autotask.adapters.aws.agent_cli.subprocess.run", invalid_reviewer)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    result = json.loads((results / f"{reviewer_job.name}.json").read_text(encoding="utf-8"))
    assert result["kind"] == "moodle-agent-result-v2"
    assert result["role"] == "central_reviewer"
    assert result["succeeded"] is False
    assert (
        broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor()).status
        == "failed"
    )


def test_central_planner_job_is_exact_idempotent_and_digest_bound(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    event = _event(tmp_path)
    prepared = _prepared(event, runner)
    executor = _LabExecutor()

    first = broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor)
    second = broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor)

    assert first.status == second.status == "pending"
    assert len(tuple(jobs.iterdir())) == 1
    assert len(runner.calls) == 1
    job = next(jobs.iterdir())
    assert (job / "inputs/0000-input.txt").read_bytes() == runner.body
    payload = json.loads((job / "job.json").read_text(encoding="utf-8"))
    assert payload["kind"] == "moodle-agent-job-v2"
    assert payload["role"] == "central_planner"
    assert payload["selectedMode"] == "central"
    assert payload["specificationDigest"] == "d" * 64
    assert payload["preparedInputs"][0]["attachmentKey"].startswith("moodle-attachment-v1:")
    assert executor.calls == []


def test_central_oversized_planner_job_becomes_terminal_v2_failure(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    prepared = _prepared(event, runner)
    oversized = PreparedAssignment(
        prepared.task_key,
        prepared.revision_digest,
        prepared.artifacts,
        prepared.course_name,
        prepared.course_shortname,
        prepared.title,
        "x" * (_MAX_RESULT_BYTES + 1),
        prepared.specification_digest,
    )
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)

    progress = broker.step(event, oversized, ExecutionMode.CENTRAL, None, _LabExecutor())

    assert progress.status == "failed"
    assert not jobs.exists() or list(jobs.iterdir()) == []
    assert list(results.glob("*.json")) == []


def test_runner_converts_oversized_central_model_result_to_durable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    progress = broker.step(
        event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor()
    )
    assert progress.status == "pending"
    job = next(jobs.iterdir())

    def oversized_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": False,
                    "summary": "x",
                    "reportMarkdown": "x" * (_MAX_RESULT_BYTES // 4),
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moddle_autotask.adapters.aws.agent_cli.subprocess.run", oversized_run)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    result = json.loads((results / f"{job.name}.json").read_text(encoding="utf-8"))
    assert result["kind"] == "moodle-agent-result-v2"
    assert result["succeeded"] is False
    assert result["summary"] == "Agent workspace is unsafe"
    assert len(_canonical(result)) < _MAX_RESULT_BYTES


@pytest.mark.parametrize("role", ["central_executor", "central_reviewer"])
def test_oversized_downstream_central_job_becomes_terminal_v2_failure(
    tmp_path: Path, role: str
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    prepared = _prepared(event, runner)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    plan: dict[str, object] = {"oversized": "x" * _MAX_RESULT_BYTES}
    with pytest.raises(AgentSpoolError, match="size budget"):
        broker._ensure_central_job(
            role,
            event,
            prepared,
            {},
            plan=plan,
            executor_result={"oversized": "x" * _MAX_RESULT_BYTES}
            if role == "central_reviewer"
            else None,
        )
    assert not jobs.exists() or list(jobs.iterdir()) == []
    assert not results.exists() or list(results.iterdir()) == []


def test_central_three_role_chain_binds_only_digest_dependencies(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    event = _event(tmp_path)
    prepared = _prepared(event, runner)
    executor = _LabExecutor()
    plan = {
        "steps": ["Produce the report."],
        "acceptanceCriteria": [{"id": "report", "text": "A report exists."}],
        "expectedArtifacts": ["report.md"],
    }

    assert broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor).status == "pending"
    planner_job = next(jobs.iterdir())
    planner: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": planner_job.name,
        "role": "central_planner",
        "succeeded": True,
        "summary": "plan ready",
        "reportMarkdown": "# Informe\nPlan verified.",
        "plan": plan,
        "planDigest": hashlib.sha256(_canonical(plan)).hexdigest(),
    }
    planner["plannerResultDigest"] = hashlib.sha256(_canonical(planner)).hexdigest()
    (results / f"{planner_job.name}.json").write_bytes(_canonical(planner))

    assert broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor).status == "pending"
    executor_job = next(
        job
        for job in jobs.iterdir()
        if json.loads((job / "job.json").read_text(encoding="utf-8"))["role"] == "central_executor"
    )
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    artifact = b"verified artifact\n"
    (outputs / "report.md").write_bytes(artifact)
    manifest, bundle_digest = _collect_artifact_bundle(outputs, ["report.md"], results / "bundles")
    executor_result: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": executor_job.name,
        "role": "central_executor",
        "succeeded": True,
        "summary": "executed",
        "reportMarkdown": "# Informe\nArtifact verified.",
        "evidence": {"report": "outputs/report.md"},
        "artifactManifest": manifest,
        "artifactManifestDigest": hashlib.sha256(_canonical(manifest)).hexdigest(),
        "artifactBundleDigest": bundle_digest,
        "bundleLocator": f"bundles/{bundle_digest}.zip",
    }
    executor_result["executorResultDigest"] = hashlib.sha256(
        _canonical(executor_result)
    ).hexdigest()
    (results / f"{executor_job.name}.json").write_bytes(_canonical(executor_result))

    assert broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor).status == "pending"
    reviewer_job = next(
        job
        for job in jobs.iterdir()
        if json.loads((job / "job.json").read_text(encoding="utf-8"))["role"] == "central_reviewer"
    )
    reviewer_payload = _load_job(reviewer_job)
    assert set(cast(dict[str, object], reviewer_payload["dependencies"])) == {
        "plannerJobId",
        "planDigest",
        "plannerResultDigest",
        "executorJobId",
        "executorResultDigest",
        "artifactManifestDigest",
        "artifactBundleDigest",
    }
    dependencies = cast(dict[str, str], reviewer_payload["dependencies"])
    reviewer: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": reviewer_job.name,
        "role": "central_reviewer",
        "succeeded": True,
        "summary": "reviewed",
        "reportMarkdown": "# Informe\nReview verified.",
        "accepted": True,
        "decisions": {"report": "accepted"},
        "findings": [],
        "dependencyDigests": {
            key: value for key, value in dependencies.items() if key.endswith("Digest")
        },
    }
    reviewer["reviewerResultDigest"] = hashlib.sha256(_canonical(reviewer)).hexdigest()
    (results / f"{reviewer_job.name}.json").write_bytes(_canonical(reviewer))

    progress = broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor)
    assert progress.status == "succeeded"
    assert progress.provenance is not None
    assert progress.provenance["artifactManifest"] == manifest


def test_runner_retries_bundle_lock_contention_without_publishing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, results = tmp_path / "jobs", tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    def busy(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise _BundlePublicationBusy("bundle publication is busy")

    monkeypatch.setattr(agent, "_execute", busy)
    assert agent.process_one() == "idle"
    assert list(results.glob("*.json")) == []


def test_published_job_layout_overrides_a_restrictive_umask(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX group mode bits are not meaningful on Windows")
    jobs = tmp_path / "jobs"
    jobs.mkdir(mode=0o2750)
    os.chmod(jobs, 0o2750)
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    event = _event(tmp_path)
    previous_umask = os.umask(0o077)
    try:
        broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    finally:
        os.umask(previous_umask)

    job = next(jobs.iterdir())
    assert stat.S_IMODE(job.stat().st_mode) == 0o2750
    assert stat.S_IMODE((job / "inputs").stat().st_mode) == 0o2750
    assert stat.S_IMODE((job / "job.json").stat().st_mode) == 0o640
    assert stat.S_IMODE((job / "inputs/0000-input.txt").stat().st_mode) == 0o640


def test_lab_job_layout_retains_setgid_shared_group(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX group mode bits are not meaningful on Windows")
    jobs = tmp_path / "jobs"
    jobs.mkdir(mode=0o2750)
    os.chmod(jobs, 0o2750)
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)

    assert (
        broker.step(
            event,
            _prepared(event, runner),
            ExecutionMode.HYBRID,
            LabHandle("lab:test"),
            _LabExecutor(),
        ).status
        == "pending"
    )
    job = next(path for path in jobs.iterdir() if path.is_dir())
    assert stat.S_IMODE(job.stat().st_mode) == 0o2750
    assert stat.S_IMODE((job / "inputs").stat().st_mode) == 0o2750
    assert stat.S_IMODE((job / "job.json").stat().st_mode) == 0o640


def test_setgid_jobs_are_readable_by_distinct_agent_identity(tmp_path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("requires a POSIX root test process")
    posix_os = cast(Any, os)
    # pytest may create private ancestor directories; make only this test's
    # path traversable by the two synthetic service identities.
    os.chmod(tmp_path.parent.parent, 0o755)
    os.chmod(tmp_path.parent, 0o755)
    os.chmod(tmp_path, 0o755)
    worker_uid, worker_gid, shared_gid, agent_uid, agent_gid = 41001, 41002, 41003, 41004, 41005
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    posix_os.chown(jobs, worker_uid, shared_gid)
    os.chmod(jobs, 0o2750)

    def run_as(uid: int, gid: int, groups: list[int], action: object) -> None:
        read_end, write_end = posix_os.pipe()
        pid = posix_os.fork()
        if pid == 0:
            posix_os.close(read_end)
            try:
                posix_os.setgroups(groups)
                posix_os.setgid(gid)
                posix_os.setuid(uid)
                cast(Callable[[], None], action)()
            except BaseException as error:
                del error
                posix_os.write(write_end, traceback.format_exc().encode("utf-8", "replace"))
                os._exit(1)
            os._exit(0)
        posix_os.close(write_end)
        _child, status = posix_os.waitpid(pid, 0)
        trace = posix_os.read(read_end, 4096).decode("utf-8", "replace")
        posix_os.close(read_end)
        assert posix_os.WIFEXITED(status) and posix_os.WEXITSTATUS(status) == 0, trace

    runner = _S3Runner()
    central_event = _event(tmp_path)
    central_prepared = _prepared(central_event, runner)
    central_results = tmp_path / "central-results"
    central_results.mkdir()
    posix_os.chown(central_results, worker_uid, shared_gid)
    os.chmod(central_results, 0o2750)

    def publish_central() -> None:
        progress = FileAgentBroker(jobs, central_results, "eu-south-2", runner).step(
            central_event, central_prepared, ExecutionMode.CENTRAL, None, _LabExecutor()
        )
        if progress.status != "pending":
            raise RuntimeError("central job was not published")

    run_as(worker_uid, worker_gid, [worker_gid, shared_gid], publish_central)
    lab_event = _event(tmp_path, lab=True, marker="b")
    lab_prepared = _prepared(lab_event, runner)
    lab_results = tmp_path / "lab-results"
    lab_results.mkdir()
    posix_os.chown(lab_results, worker_uid, shared_gid)
    os.chmod(lab_results, 0o2750)

    def publish_lab() -> None:
        progress = FileAgentBroker(jobs, lab_results, "eu-south-2", runner).step(
            lab_event, lab_prepared, ExecutionMode.HYBRID, LabHandle("lab:test"), _LabExecutor()
        )
        if progress.status != "pending":
            raise RuntimeError("lab job was not published")

    run_as(worker_uid, worker_gid, [worker_gid, shared_gid], publish_lab)
    published = [path for path in jobs.iterdir() if path.is_dir()]
    assert len(published) == 2
    for job in published:
        inputs = job / "inputs"
        assert job.stat().st_gid == inputs.stat().st_gid == shared_gid
        assert stat.S_IMODE(job.stat().st_mode) == stat.S_IMODE(inputs.stat().st_mode) == 0o2750
        assert stat.S_IMODE((job / "job.json").stat().st_mode) == 0o640

    def read_as_agent() -> None:
        for job in published:
            (job / "job.json").read_bytes()
            for item in (job / "inputs").iterdir():
                item.read_bytes()

    run_as(agent_uid, agent_gid, [shared_gid], read_as_agent)


def test_lab_plan_executes_once_then_waits_for_final_report(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    executor = _LabExecutor()
    handle = LabHandle("lab:test")

    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == (
        "pending"
    )
    plan = _job_for_phase(jobs, "lab_plan")
    _write_result(results, plan, commands=["Write-Output 'ok'"])
    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == (
        "pending"
    )
    report = _job_for_phase(jobs, "lab_report")
    assert len(executor.calls) == 1
    dispatches = list((jobs / "dispatches").glob("*.json"))
    assert (jobs / "dispatches").is_dir()
    assert plan != jobs / "dispatches" and report != jobs / "dispatches"
    assert len(dispatches) == 1
    dispatch = json.loads(dispatches[0].read_text(encoding="utf-8"))
    assert dispatch["state"] == "dispatched"
    assert dispatch["executionKey"] == report.name
    assert dispatch["commandId"] == "12345678-1234-1234-1234-123456789abc"
    assert len(executor.waits) == 1
    assert "command output" in (report / "job.json").read_text(encoding="utf-8")
    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == (
        "pending"
    )
    assert len(executor.calls) == 1
    _write_result(results, report)
    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == (
        "succeeded"
    )


def test_lab_dispatch_crash_before_command_id_persistence_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    executor = _LabExecutor()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    report_id, plan_digest, commands = _ready_lab_dispatch(
        broker, jobs, results, event, prepared, executor, handle
    )
    intent = broker._dispatch_payload(
        report_id,
        handle,
        plan_digest,
        hashlib.sha256(_canonical(list(commands))).hexdigest(),
        None,
    )
    original_replace = FileAgentBroker._replace_dispatch_record

    def crash_before_persistence(
        self: FileAgentBroker,
        execution_key: str,
        previous: dict[str, object],
        payload: dict[str, object],
    ) -> None:
        del self, execution_key, previous, payload
        raise RuntimeError("simulated controller crash after SendCommand")

    monkeypatch.setattr(FileAgentBroker, "_replace_dispatch_record", crash_before_persistence)
    with pytest.raises(RuntimeError, match="after SendCommand"):
        broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)
    dispatch = jobs / "dispatches" / f"{report_id}.json"
    assert dispatch.read_bytes() == _canonical(intent)
    assert executor.calls == [(handle, commands, report_id)]
    assert executor.waits == []

    monkeypatch.setattr(FileAgentBroker, "_replace_dispatch_record", original_replace)
    resumed_executor = _LabExecutor()
    resumed = FileAgentBroker(jobs, results, "eu-south-2", runner)
    progress = resumed.step(event, prepared, ExecutionMode.HYBRID, handle, resumed_executor)

    assert progress.status == "failed"
    assert progress.summary == "Lab dispatch state is unsafe"
    assert dispatch.read_bytes() == _canonical(intent)
    assert resumed_executor.calls == []
    assert resumed_executor.waits == []


def test_lab_dispatch_wait_crash_resumes_stored_command_once(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    executor = _LabExecutor(wait_failures_remaining=1)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    report_id, plan_digest, commands = _ready_lab_dispatch(
        broker, jobs, results, event, prepared, executor, handle
    )
    command_id = "12345678-1234-1234-1234-123456789abc"
    dispatched = broker._dispatch_payload(
        report_id,
        handle,
        plan_digest,
        hashlib.sha256(_canonical(list(commands))).hexdigest(),
        command_id,
    )

    with pytest.raises(RuntimeError, match="crash during wait"):
        broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)
    dispatch = jobs / "dispatches" / f"{report_id}.json"
    assert dispatch.read_bytes() == _canonical(dispatched)
    assert executor.calls == [(handle, commands, report_id)]
    assert executor.waits == [(handle, command_id, report_id)]

    resumed_executor = _LabExecutor()
    resumed = FileAgentBroker(jobs, results, "eu-south-2", runner)
    assert resumed.step(event, prepared, ExecutionMode.HYBRID, handle, resumed_executor).status == (
        "pending"
    )

    report = _job_for_phase(jobs, "lab_report")
    assert report.name == report_id
    assert dispatch.read_bytes() == _canonical(dispatched)
    assert resumed_executor.calls == []
    assert resumed_executor.waits == [(handle, command_id, report_id)]


def test_lab_report_publication_crash_rewaits_stored_command_without_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    executor = _LabExecutor()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    report_id, plan_digest, commands = _ready_lab_dispatch(
        broker, jobs, results, event, prepared, executor, handle
    )
    command_id = "12345678-1234-1234-1234-123456789abc"
    dispatched = broker._dispatch_payload(
        report_id,
        handle,
        plan_digest,
        hashlib.sha256(_canonical(list(commands))).hexdigest(),
        command_id,
    )
    original_ensure = FileAgentBroker._ensure_job
    failed = False

    def crash_before_report_publication(
        self: FileAgentBroker,
        phase: str,
        current_event: NotificationEvent,
        current_prepared: PreparedAssignment,
        context: dict[str, object] | None,
    ) -> str:
        nonlocal failed
        if phase == "lab_report" and not failed:
            failed = True
            raise RuntimeError("simulated controller crash before lab report publication")
        return original_ensure(self, phase, current_event, current_prepared, context)

    monkeypatch.setattr(FileAgentBroker, "_ensure_job", crash_before_report_publication)
    with pytest.raises(RuntimeError, match="before lab report publication"):
        broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)
    dispatch = jobs / "dispatches" / f"{report_id}.json"
    assert dispatch.read_bytes() == _canonical(dispatched)
    assert executor.calls == [(handle, commands, report_id)]
    assert executor.waits == [(handle, command_id, report_id)]
    assert not (jobs / report_id).exists()

    monkeypatch.setattr(FileAgentBroker, "_ensure_job", original_ensure)
    resumed_executor = _LabExecutor()
    resumed = FileAgentBroker(jobs, results, "eu-south-2", runner)
    assert resumed.step(event, prepared, ExecutionMode.HYBRID, handle, resumed_executor).status == (
        "pending"
    )

    report = _job_for_phase(jobs, "lab_report")
    assert report.name == report_id
    assert "command output" in (report / "job.json").read_text(encoding="utf-8")
    assert dispatch.read_bytes() == _canonical(dispatched)
    assert resumed_executor.calls == []
    assert resumed_executor.waits == [(handle, command_id, report_id)]


@pytest.mark.parametrize(
    "poison",
    [
        "malformed",
        "foreign-plan",
        "foreign-handle",
        "foreign-digest",
        "foreign-schema",
        "invalid-command-id",
        "directory",
        "symlink",
    ],
)
def test_lab_dispatch_poison_fails_closed_without_replacing_record(
    tmp_path: Path, poison: str
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    executor = _LabExecutor()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    report_id, plan_digest, commands = _ready_lab_dispatch(
        broker, jobs, results, event, prepared, executor, handle
    )
    intent = broker._dispatch_payload(
        report_id,
        handle,
        plan_digest,
        hashlib.sha256(_canonical(list(commands))).hexdigest(),
        None,
    )
    dispatch = broker._dispatch_root() / f"{report_id}.json"
    expected_bytes: bytes | None
    if poison == "malformed":
        expected_bytes = b"{"
        dispatch.write_bytes(expected_bytes)
    elif poison == "directory":
        expected_bytes = None
        dispatch.mkdir()
    elif poison == "symlink":
        target = tmp_path / "dispatch-target.json"
        expected_bytes = b'{"preserve":"target"}'
        target.write_bytes(expected_bytes)
        try:
            dispatch.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is unavailable")
    else:
        record = dict(intent)
        if poison == "foreign-plan":
            record["planDigest"] = "f" * 64
        elif poison == "foreign-handle":
            record["labHandle"] = "lab:foreign"
        elif poison == "foreign-digest":
            record["commandsDigest"] = "f" * 64
        elif poison == "invalid-command-id":
            record["state"] = "dispatched"
            record["commandId"] = "-" * 36
        else:
            record["kind"] = "moodle-lab-dispatch-v0"
        expected_bytes = _canonical(record)
        dispatch.write_bytes(expected_bytes)

    progress = broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)

    assert progress.status == "failed"
    assert progress.summary == "Lab dispatch state is unsafe"
    assert executor.calls == []
    assert executor.waits == []
    if poison == "directory":
        assert dispatch.is_dir()
    elif poison == "symlink":
        assert dispatch.is_symlink()
        assert target.read_bytes() == expected_bytes
    else:
        assert dispatch.read_bytes() == expected_bytes


def test_codex_runner_uses_structured_output_and_scrubs_aws_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    observed: dict[str, object] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["arguments"] = arguments
        observed["environment"] = kwargs["env"]
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": True,
                    "summary": "done",
                    "reportMarkdown": "# Informe",
                    "powershellCommands": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moddle_autotask.adapters.aws.agent_cli.subprocess.run", fake_run)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross")
    agent = CodexSpoolRunner(
        jobs,
        results,
        tmp_path / "workspaces",
        tmp_path / "codex",
        60,
    )

    assert agent.process_one() == "processed"
    arguments = observed["arguments"]
    environment = observed["environment"]
    assert isinstance(arguments, list)
    assert isinstance(environment, dict)
    assert "--output-schema" in arguments
    assert "--ephemeral" in arguments
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert agent.process_one() == "idle"


def test_codex_runner_recovers_stale_workspace_inputs_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    job = next(jobs.iterdir())
    workspace = tmp_path / "workspaces" / job.name
    stale_inputs = workspace / "inputs"
    stale_inputs.mkdir(parents=True)
    (stale_inputs / "0000-input.txt").write_bytes(b"partial")

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        current_workspace = Path(arguments[arguments.index("-C") + 1])
        assert (current_workspace / "inputs/0000-input.txt").read_bytes() == runner.body
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": True,
                    "summary": "done",
                    "reportMarkdown": "# Informe",
                    "powershellCommands": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moddle_autotask.adapters.aws.agent_cli.subprocess.run", fake_run)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    assert (workspace / "inputs/0000-input.txt").read_bytes() == runner.body


def test_codex_runner_rejects_forged_job_and_malformed_result(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    job = next(jobs.iterdir())
    payload = json.loads((job / "job.json").read_text(encoding="utf-8"))
    payload["taskKey"] = "moodle-task-v1:" + "f" * 64
    (job / "job.json").write_text(json.dumps(payload), encoding="utf-8")
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "idle"

    payload["taskKey"] = event.task_key
    (job / "job.json").write_text(json.dumps(payload), encoding="utf-8")
    results.mkdir(exist_ok=True)
    (results / f"{job.name}.json").write_text("{}", encoding="utf-8")

    assert agent.process_one() == "idle"


def test_codex_runner_reports_workspace_poison_and_processes_later_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    schema_event = _event(tmp_path, marker="d")
    inputs_event = _event(tmp_path, marker="e")
    valid_event = _event(tmp_path, marker="f")
    for event in (schema_event, inputs_event, valid_event):
        broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    job_by_task = {
        json.loads((job / "job.json").read_text(encoding="utf-8"))["taskKey"]: job
        for job in jobs.iterdir()
    }
    workspaces = tmp_path / "workspaces"
    schema_job = job_by_task[schema_event.task_key]
    inputs_job = job_by_task[inputs_event.task_key]
    schema_workspace = workspaces / schema_job.name
    schema_workspace.mkdir(parents=True)
    schema_target = tmp_path / "schema-target"
    schema_target.write_text("preserve schema", encoding="utf-8")
    schema = schema_workspace / "result-schema.json"
    try:
        schema.symlink_to(schema_target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    inputs_target = tmp_path / "inputs-target"
    inputs_target.mkdir()
    inputs = workspaces / inputs_job.name / "inputs"
    inputs.parent.mkdir(parents=True)
    inputs.symlink_to(inputs_target, target_is_directory=True)

    def successful_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": True,
                    "summary": "done",
                    "reportMarkdown": "# Informe",
                    "powershellCommands": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moddle_autotask.adapters.aws.agent_cli.subprocess.run", successful_run)
    agent = CodexSpoolRunner(jobs, results, workspaces, tmp_path / "codex", 60)

    assert [agent.process_one() for _ in range(3)] == ["processed"] * 3
    for job in (schema_job, inputs_job):
        result = json.loads((results / f"{job.name}.json").read_text(encoding="utf-8"))
        assert result["jobId"] == job.name
        assert result["role"] == "central_planner"
        assert result["succeeded"] is False
        assert result["summary"] == "Agent workspace is unsafe"
    assert schema_target.read_text(encoding="utf-8") == "preserve schema"
    assert list(inputs_target.iterdir()) == []
    valid_job = job_by_task[valid_event.task_key]
    valid_result = json.loads((results / f"{valid_job.name}.json").read_text(encoding="utf-8"))
    assert not valid_result["succeeded"]


def test_codex_runner_reports_last_message_directory_and_processes_later_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    poisoned_event = _event(tmp_path, marker="a")
    valid_event = _event(tmp_path, marker="b")
    for event in (poisoned_event, valid_event):
        broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    job_by_task = {
        json.loads((job / "job.json").read_text(encoding="utf-8"))["taskKey"]: job
        for job in jobs.iterdir()
    }
    poisoned_job = job_by_task[poisoned_event.task_key]
    poisoned_output = tmp_path / "workspaces" / poisoned_job.name / "last-message.json"
    poisoned_output.mkdir(parents=True)

    def successful_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": True,
                    "summary": "done",
                    "reportMarkdown": "# Informe",
                    "powershellCommands": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moddle_autotask.adapters.aws.agent_cli.subprocess.run", successful_run)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)
    assert [agent.process_one() for _ in range(2)] == ["processed", "processed"]
    poisoned_result = json.loads(
        (results / f"{poisoned_job.name}.json").read_text(encoding="utf-8")
    )
    assert poisoned_result["succeeded"] is False
    assert poisoned_output.is_file()
    valid_job = job_by_task[valid_event.task_key]
    valid_result = json.loads((results / f"{valid_job.name}.json").read_text(encoding="utf-8"))
    assert not valid_result["succeeded"]


def test_broker_rejects_forged_result_keys_and_phase_semantics(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    event = _event(tmp_path)
    broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    job = next(jobs.iterdir())
    payload = json.loads((job / "job.json").read_text(encoding="utf-8"))
    path = results / f"{job.name}.json"
    result = {
        "kind": "moodle-agent-result-v1",
        "jobId": payload["jobId"],
        "phase": "central",
        "succeeded": True,
        "summary": "done",
        "reportMarkdown": "# Informe",
        "powershellCommands": [],
    }
    result["forged"] = True
    results.mkdir(exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(AgentSpoolError, match="shape"):
        broker._result(job.name, "central")
    result.pop("forged")
    result["powershellCommands"] = ["Write-Output bad"]
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(AgentSpoolError, match="non-plan"):
        broker._result(job.name, "central")


def test_result_publication_is_atomic_and_never_replaces_unsafe_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    path = results / ("a" * 64 + ".json")
    seen: list[Path] = []
    real_write = _write_exclusive

    def observing_write(target: Path, data: bytes, mode: int) -> None:
        assert not path.exists()
        seen.append(target)
        real_write(target, data, mode)

    monkeypatch.setattr(agent_cli, "_write_exclusive", observing_write)
    data = b'{"complete":true}'
    agent_cli._publish_result(path, data)
    assert seen and seen[0].parent == results and seen[0].suffix == ".tmp"
    assert json.loads(path.read_text(encoding="utf-8")) == {"complete": True}
    assert stat.S_ISREG(path.lstat().st_mode)
    collision = results / ("b" * 64 + ".json")
    collision.mkdir()
    with pytest.raises(AgentSpoolError, match="unsafe"):
        agent_cli._publish_result(collision, data)
    assert collision.is_dir()


def test_codex_runner_publishes_failure_and_skips_corrupt_preceding_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    job = next(jobs.iterdir())
    corrupt = jobs / ("0" * 64)
    corrupt.mkdir()
    (corrupt / "job.json").write_text("{}", encoding="utf-8")

    def failed_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(arguments, 1)

    monkeypatch.setattr("moddle_autotask.adapters.aws.agent_cli.subprocess.run", failed_run)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    result = json.loads((results / f"{job.name}.json").read_text(encoding="utf-8"))
    assert result["kind"] == "moodle-agent-result-v2"
    assert result["jobId"] == job.name
    assert result["role"] == "central_planner"
    assert result["succeeded"] is False
    assert result["summary"] == "Codex execution failed"


def test_concurrent_dispatch_intent_has_one_send_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    seed = FileAgentBroker(jobs, results, "eu-south-2", runner)
    report_id, _, _ = _ready_lab_dispatch(
        seed, jobs, results, event, prepared, _LabExecutor(), handle
    )
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    initial_reads = 0
    original_read = FileAgentBroker._read_dispatch_record

    def synchronized_initial_reads(
        self: FileAgentBroker, execution_key: str, intent: dict[str, object]
    ) -> dict[str, object] | None:
        nonlocal initial_reads
        with lock:
            initial_reads += 1
            synchronize = initial_reads <= 2
        if synchronize:
            barrier.wait(timeout=5)
            return None
        return original_read(self, execution_key, intent)

    monkeypatch.setattr(FileAgentBroker, "_read_dispatch_record", synchronized_initial_reads)
    executor = _LabExecutor()
    outcomes: list[object] = []

    def run() -> None:
        outcomes.append(
            FileAgentBroker(jobs, results, "eu-south-2", runner).step(
                event, prepared, ExecutionMode.HYBRID, handle, executor
            )
        )

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2
    assert executor.calls == [(handle, ("Write-Output 'ok'",), report_id)]
    assert len(executor.waits) >= 1
    dispatch = json.loads((jobs / "dispatches" / f"{report_id}.json").read_text("utf-8"))
    assert dispatch["commandId"] == "12345678-1234-1234-1234-123456789abc"


@pytest.mark.parametrize("collision_errno", [errno.EEXIST, errno.ENOTEMPTY])
@pytest.mark.parametrize("tampered", [False, True])
def test_concurrent_job_publish_converges_only_on_exact_existing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision_errno: int,
    tampered: bool,
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    original_rename = Path.rename

    def competing_publish(temporary: Path, target: Path) -> Path:
        assert temporary.parent == jobs
        assert temporary.name.startswith(".")
        assert not target.exists()
        shutil.copytree(temporary, target)
        if tampered:
            (target / "job.json").write_bytes(b'{"tampered":true}')
        raise OSError(collision_errno, os.strerror(collision_errno))

    monkeypatch.setattr(Path, "rename", competing_publish)

    if tampered:
        with pytest.raises(AgentSpoolError, match="concurrent agent job conflicts"):
            broker.step(
                event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor()
            )
    else:
        progress = broker.step(
            event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor()
        )
        assert progress.status == "pending"
        assert len(runner.calls) == 1

    assert len([path for path in jobs.iterdir() if path.name.startswith(".")]) == 0
    if tampered:
        job = next(path for path in jobs.iterdir() if not path.name.startswith("."))
        assert (job / "job.json").read_bytes() == b'{"tampered":true}'
    monkeypatch.setattr(Path, "rename", original_rename)
