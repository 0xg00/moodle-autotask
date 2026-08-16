from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import threading
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import pytest

from moodle_autotask.adapters.aws import agent_cli, agent_spool, central_protocol, lab_protocol
from moodle_autotask.adapters.aws.agent_cli import (
    CodexSpoolRunner,
    _BundlePublicationBusy,
    _collect_artifact_bundle,
    _load_job,
)
from moodle_autotask.adapters.aws.agent_spool import (
    _MAX_CENTRAL_RESULT_BYTES,
    _MAX_RESULT_BYTES,
    AgentSpoolError,
    ExecutionProgress,
    FileAgentBroker,
    _canonical,
    _central_chain_storage_demand,
    _central_dependency_envelope_bytes,
    _job_storage_demand,
    _write_exclusive,
)
from moodle_autotask.adapters.aws.artifacts import PreparedArtifact, PreparedAssignment
from moodle_autotask.adapters.aws.labs import LabTranscript
from moodle_autotask.adapters.aws.retention import PreparedTombstone, plan_retention
from moodle_autotask.adapters.aws.retention_fs import (
    RetentionFilesystem,
    RetentionRoots,
    retention_job_lock,
)
from moodle_autotask.adapters.aws.storage_quota import (
    StorageCapacityError,
    StorageDemand,
    StorageLimit,
    StoragePolicy,
    admit_owner_write,
    measure_tree_no_follow,
)
from moodle_autotask.adapters.moodle.approval_state import ApprovalState, RetentionRecord, WorkClaim
from moodle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationAttachment,
    NotificationDraft,
    NotificationEvent,
    _event_id,
)
from moodle_autotask.domain.models import Digest, ExecutionMode, LabHandle


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


def _event(
    tmp_path: Path, *, lab: bool = False, marker: str = "a", central: bool = False
) -> NotificationEvent:
    attachments = (NotificationAttachment("input.txt", 14, "text/plain", lab),) if not lab else ()
    event = MoodleState(tmp_path / "moodle.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + marker * 64,
            "moodle-assignment-v1:" + marker * 64,
            "ASIX",
            "ASIX-M06",
            "Tarea controlada" if central else "Práctica controlada",
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
        Digest.of_json(event.as_dict()).value,
        "e" * 64,
        ("C:\\ProgramData\\MoodleAutotask\\inputs\\" + "e" * 64 + "\\input.txt",)
        if artifacts
        else (),
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
    return (
        broker._job_id("lab_report", event, broker._lab_context_digest("e" * 64, plan_digest)),
        plan_digest,
        commands,
    )


def _central_plan() -> dict[str, object]:
    return {
        "steps": ["Produce the report."],
        "acceptanceCriteria": [{"id": "report", "text": "A report exists."}],
        "expectedArtifacts": ["report.md"],
    }


def _claimed_central_state(
    tmp_path: Path, event: NotificationEvent
) -> tuple[ApprovalState, WorkClaim]:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    buttons = state.prepare(event, now=1)
    state.resolve(buttons.approve, 1, 1, now=2)
    pending = state.claim_work("worker", 60, now=3)
    assert pending is not None and state.mark_ready(pending, now=3, for_execution=True)
    ready = state.claim_work("worker", 60, now=4)
    assert ready is not None
    return state, ready


def _write_central_planner_result(results: Path, planner: Path) -> dict[str, object]:
    plan = _central_plan()
    value: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": planner.name,
        "role": "central_planner",
        "succeeded": True,
        "summary": "plan ready",
        "reportMarkdown": "# Informe\nPlan verified.",
        "plan": plan,
        "planDigest": hashlib.sha256(_canonical(plan)).hexdigest(),
    }
    value["plannerResultDigest"] = hashlib.sha256(_canonical(value)).hexdigest()
    (results / f"{planner.name}.json").write_bytes(_canonical(value))
    return value


def _write_central_executor_result(
    tmp_path: Path, results: Path, executor: Path
) -> dict[str, object]:
    outputs = tmp_path / "verified-outputs"
    outputs.mkdir()
    (outputs / "report.md").write_bytes(b"verified artifact\n")
    manifest, bundle_digest = _collect_artifact_bundle(outputs, ["report.md"], results / "bundles")
    value: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": executor.name,
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
    value["executorResultDigest"] = hashlib.sha256(_canonical(value)).hexdigest()
    (results / f"{executor.name}.json").write_bytes(_canonical(value))
    return value


def test_noncentral_spool_requires_transfer_and_binds_digest_to_plan_identity(
    tmp_path: Path,
) -> None:
    runner = _S3Runner()
    event = _event(tmp_path, central=True)
    missing = replace(_prepared(event, runner), guest_input_transfer_digest="")
    broker = FileAgentBroker(tmp_path / "jobs", tmp_path / "results", "eu-south-2", runner)
    with pytest.raises(AgentSpoolError, match="transfer digest"):
        broker.step(event, missing, ExecutionMode.HYBRID, LabHandle("lab:test"), _LabExecutor())
    assert not (tmp_path / "jobs").exists()

    first = _prepared(event, runner)
    second = replace(
        first,
        guest_input_transfer_digest="f" * 64,
        guest_input_paths=("C:\\ProgramData\\MoodleAutotask\\inputs\\" + "f" * 64 + "\\input.txt",),
    )
    first_id = broker._job_id("lab_plan", event, broker._guest_transfer_digest(first))
    second_id = broker._job_id("lab_plan", event, broker._guest_transfer_digest(second))
    assert first_id != second_id
    with pytest.raises(AgentSpoolError, match="transfer context"):
        broker._ensure_job(
            "lab_report",
            event,
            second,
            {"planDigest": "a" * 64, "transferDigest": "e" * 64},
        )


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
    event = _event(tmp_path, central=True)
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


def test_executor_budget_failure_keeps_only_planner_prefix_for_state_and_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, results = tmp_path / "jobs", tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, central=True)
    prepared = _prepared(event, runner)
    state, claim = _claimed_central_state(tmp_path, event)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)

    assert (
        broker.step(
            event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor()
        ).status
        == "pending"
    )
    planner = _central_job(jobs, "central_planner")
    planner_result = _write_central_planner_result(results, planner)
    monkeypatch.setattr(agent_spool, "_MAX_RESULT_BYTES", (planner / "job.json").stat().st_size)

    progress = broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor())

    assert progress.status == "failed" and progress.provenance is not None
    assert progress.provenance["terminalRole"] == "central_executor"
    assert progress.provenance["terminalStatus"] == "budget_error"
    assert progress.provenance["jobIds"] == [planner.name]
    assert progress.provenance["resultDigests"] == [planner_result["plannerResultDigest"]]
    assert [_load_job(job)["role"] for job in jobs.iterdir()] == ["central_planner"]
    assert sorted(path.name for path in results.glob("*.json")) == [f"{planner.name}.json"]
    assert not (results / "bundles").exists()
    assert state.complete_execution(
        claim,
        succeeded=False,
        summary=progress.summary,
        report_markdown=progress.report_markdown,
        provenance=progress.provenance,
        now=5,
    )
    records = state.retention_records(10, 1, 1, 2)
    assert [(plan.target_phase, plan.job_ids, plan.bundle_digest) for plan in plan_retention(
        records, now=10, limit=2
    )] == [("scratch", (planner.name,), None)]


def test_reviewer_budget_failure_keeps_verified_executor_bundle_for_state_and_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, results = tmp_path / "jobs", tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, central=True)
    prepared = _prepared(event, runner)
    state, claim = _claimed_central_state(tmp_path, event)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)

    assert (
        broker.step(
            event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor()
        ).status
        == "pending"
    )
    planner = _central_job(jobs, "central_planner")
    planner_result = _write_central_planner_result(results, planner)
    assert (
        broker.step(
            event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor()
        ).status
        == "pending"
    )
    executor = _central_job(jobs, "central_executor")
    executor_result = _write_central_executor_result(tmp_path, results, executor)
    budget = max(
        (planner / "job.json").stat().st_size,
        (results / f"{planner.name}.json").stat().st_size,
        (executor / "job.json").stat().st_size,
        (results / f"{executor.name}.json").stat().st_size,
    )
    monkeypatch.setattr(agent_spool, "_MAX_RESULT_BYTES", budget)

    progress = broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor())

    bundle_digest = executor_result["artifactBundleDigest"]
    assert isinstance(bundle_digest, str)
    assert progress.status == "failed" and progress.provenance is not None
    assert progress.provenance["terminalRole"] == "central_reviewer"
    assert progress.provenance["terminalStatus"] == "budget_error"
    assert progress.provenance["jobIds"] == [planner.name, executor.name]
    assert progress.provenance["resultDigests"] == [
        planner_result["plannerResultDigest"],
        executor_result["executorResultDigest"],
    ]
    assert progress.provenance["artifactManifest"] == executor_result["artifactManifest"]
    assert progress.provenance["artifactBundleDigest"] == bundle_digest
    assert (results / "bundles" / f"{bundle_digest}.zip").is_file()
    assert sorted(cast(str, _load_job(job)["role"]) for job in jobs.iterdir()) == [
        "central_executor",
        "central_planner",
    ]
    assert {path.name for path in results.glob("*.json")} == {
        f"{executor.name}.json",
        f"{planner.name}.json",
    }
    assert state.complete_execution(
        claim,
        succeeded=False,
        summary=progress.summary,
        report_markdown=progress.report_markdown,
        provenance=progress.provenance,
        now=5,
    )
    notification = state.pending_execution_notification()
    assert notification is not None and state.mark_execution_notification_delivered(
        notification, now=6
    )
    records = state.retention_records(10, 1, 1, 2)
    assert [(plan.target_phase, plan.job_ids, plan.bundle_digest) for plan in plan_retention(
        records, now=10, limit=2
    )] == [
        ("scratch", (planner.name, executor.name), None),
        ("evidence", (), bundle_digest),
    ]


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

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", invalid_executor)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    result = json.loads((results / f"{executor_job.name}.json").read_text(encoding="utf-8"))
    assert result["kind"] == "moodle-agent-result-v2"
    assert result["role"] == "central_executor"
    assert result["succeeded"] is False
    assert list((results / "bundles").glob("*.zip")) == []
    progress = broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor())
    assert progress.status == "failed"
    assert progress.provenance is not None
    assert progress.provenance["kind"] == "moodle-central-provenance-v3"
    assert progress.provenance["roles"] == ["central_planner", "central_executor"]
    assert progress.provenance["jobIds"] == [
        next(
            json.loads((job / "job.json").read_text(encoding="utf-8"))["jobId"]
            for job in jobs.iterdir()
            if json.loads((job / "job.json").read_text(encoding="utf-8"))["role"] == role
        )
        for role in ("central_planner", "central_executor")
    ]


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

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", invalid_reviewer)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    result = json.loads((results / f"{reviewer_job.name}.json").read_text(encoding="utf-8"))
    assert result["kind"] == "moodle-agent-result-v2"
    assert result["role"] == "central_reviewer"
    assert result["succeeded"] is False
    progress = broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor())
    assert progress.status == "failed"
    assert progress.provenance is not None
    assert progress.provenance["kind"] == "moodle-central-provenance-v3"
    assert progress.provenance["roles"] == list(central_protocol.CENTRAL_ROLES)
    assert progress.provenance["artifactBundleDigest"] == bundle_digest


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
    assert payload["specificationDigest"] == Digest.of_json(event.as_dict()).value
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

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", oversized_run)
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

    # Production deliberately keeps the controller out of the agent's primary
    # group. Publication must rely on setgid inheritance, not supplementary
    # membership in the shared directory group.
    run_as(worker_uid, worker_gid, [worker_gid], publish_central)
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

    run_as(worker_uid, worker_gid, [worker_gid], publish_lab)
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
    finished = broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)
    assert finished.status == "succeeded"
    assert finished.provenance is not None
    assert finished.provenance["kind"] == "moodle-lab-provenance-v1"
    assert finished.provenance["phases"] == ["lab_plan", "lab_report"]
    assert finished.provenance["jobIds"] == [plan.name, report.name]
    assert finished.provenance["barrierIds"] == [plan.name, report.name]
    assert cast(dict[str, object], finished.provenance["dispatch"])["state"] == (
        "dispatched"
    )


def test_failed_lab_plan_returns_exact_single_phase_provenance(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    executor = _LabExecutor()

    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == (
        "pending"
    )
    plan = _job_for_phase(jobs, "lab_plan")
    _write_result(results, plan, succeeded=False)
    failed = broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)

    assert failed.status == "failed"
    assert failed.provenance is not None
    assert failed.provenance["phases"] == ["lab_plan"]
    assert failed.provenance["jobIds"] == [plan.name]
    assert failed.provenance["barrierIds"] == [plan.name]
    assert failed.provenance["terminalStatus"] == "failed"
    assert failed.provenance["dispatch"] is None
    assert executor.calls == []


@pytest.mark.parametrize("failure", ["dispatch", "report-job"])
def test_lab_controller_capacity_after_plan_returns_retainable_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    executor = _LabExecutor()
    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == (
        "pending"
    )
    plan = _job_for_phase(jobs, "lab_plan")
    _write_result(results, plan, commands=["Write-Output ok"])
    original_ensure = FileAgentBroker._ensure_job

    if failure == "dispatch":
        def refuse_dispatch(
            self: FileAgentBroker,
            current_executor: object,
            current_handle: LabHandle,
            commands: tuple[str, ...],
            plan_digest: str,
            execution_key: str,
        ) -> LabTranscript | None:
            del self, current_executor, current_handle, commands, plan_digest, execution_key
            raise StorageCapacityError("full")

        monkeypatch.setattr(
            FileAgentBroker,
            "_dispatch_or_resume",
            refuse_dispatch,
        )
    else:
        def refuse_report(
            self: FileAgentBroker,
            phase: str,
            current_event: NotificationEvent,
            current_prepared: PreparedAssignment,
            context: dict[str, object] | None,
        ) -> str:
            if phase == "lab_report":
                raise StorageCapacityError("full")
            return original_ensure(self, phase, current_event, current_prepared, context)

        monkeypatch.setattr(FileAgentBroker, "_ensure_job", refuse_report)

    failed = broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)
    assert failed.status == "failed"
    assert failed.provenance is not None
    assert failed.provenance["terminalStatus"] == "capacity_error"
    assert failed.provenance["jobIds"] == [plan.name]
    if failure == "dispatch":
        assert failed.provenance["barrierIds"] == [plan.name]
        assert failed.provenance["dispatch"] is None
    else:
        report_id = cast(list[str], failed.provenance["barrierIds"])[1]
        assert failed.provenance["barrierIds"] == [plan.name, report_id]
        assert cast(dict[str, object], failed.provenance["dispatch"])["state"] == (
            "dispatched"
        )
        assert not (jobs / report_id).exists()


def test_noncentral_broker_jobs_run_through_codex_with_bound_guest_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, results = tmp_path / "jobs", tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    prepared = _prepared(event, runner)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    executor = _LabExecutor()
    prompts: dict[str, str] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        workspace = Path(arguments[arguments.index("-C") + 1])
        job = json.loads((jobs / workspace.name / "job.json").read_text(encoding="utf-8"))
        phase = job["phase"]
        prompts[phase] = arguments[-1]
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": True,
                    "summary": "done",
                    "reportMarkdown": "# Informe" if phase == "lab_report" else "",
                    "powershellCommands": ["Write-Output 'ok'"] if phase == "lab_plan" else [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", fake_run)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)
    handle = LabHandle("lab:test")

    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == "pending"
    assert agent.process_one() == "processed"
    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == "pending"
    assert agent.process_one() == "processed"
    assert (
        broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == "succeeded"
    )

    transfer = {"guestPaths": list(prepared.guest_input_paths), "transferDigest": "e" * 64}
    encoded_transfer = json.dumps(transfer, ensure_ascii=False, separators=(",", ":"))
    assert encoded_transfer in prompts["lab_plan"]
    assert encoded_transfer in prompts["lab_report"]


def test_noncentral_runner_rejects_forged_guest_transfer_and_context(
    tmp_path: Path,
) -> None:
    jobs, results = tmp_path / "jobs", tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path)
    prepared = _prepared(event, runner)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    executor = _LabExecutor()
    handle = LabHandle("lab:test")
    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == "pending"
    plan = _job_for_phase(jobs, "lab_plan")
    original_plan = json.loads((plan / "job.json").read_text(encoding="utf-8"))

    def rejects_plan(mutator: Callable[[dict[str, object]], None]) -> None:
        payload = json.loads(json.dumps(original_plan))
        mutator(payload)
        (plan / "job.json").write_bytes(_canonical(payload))
        with pytest.raises(AgentSpoolError):
            _load_job(plan)

    def remove_paths(payload: dict[str, object]) -> None:
        cast(dict[str, object], payload["guestInputTransfer"]).pop("guestPaths")

    rejects_plan(remove_paths)
    rejects_plan(
        lambda payload: cast(dict[str, object], payload["guestInputTransfer"]).__setitem__(
            "unexpected", True
        )
    )
    rejects_plan(
        lambda payload: cast(dict[str, object], payload["guestInputTransfer"]).__setitem__(
            "transferDigest", "f" * 64
        )
    )
    rejects_plan(
        lambda payload: cast(dict[str, object], payload["guestInputTransfer"]).__setitem__(
            "guestPaths",
            ["C:\\ProgramData\\MoodleAutotask\\inputs\\" + "e" * 64 + "\\..\\forged.txt"],
        )
    )
    (plan / "job.json").write_bytes(_canonical(original_plan))
    _write_result(results, plan, commands=["Write-Output 'ok'"])
    assert broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor).status == "pending"
    report = _job_for_phase(jobs, "lab_report")
    report_payload = json.loads((report / "job.json").read_text(encoding="utf-8"))
    cast(dict[str, object], report_payload["context"])["transferDigest"] = "f" * 64
    (report / "job.json").write_bytes(_canonical(report_payload))
    with pytest.raises(AgentSpoolError):
        _load_job(report)


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
    original_replace = FileAgentBroker._replace_dispatch_record_locked

    def crash_before_persistence(
        self: FileAgentBroker,
        root: Path,
        execution_key: str,
        previous: dict[str, object],
        payload: dict[str, object],
    ) -> None:
        del self, root, execution_key, previous, payload
        raise RuntimeError("simulated controller crash after SendCommand")

    monkeypatch.setattr(
        FileAgentBroker, "_replace_dispatch_record_locked", crash_before_persistence
    )
    first = broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)
    assert first.status == "failed"
    assert first.summary == "Lab dispatch outcome is unknown"
    dispatch = jobs / "dispatches" / f"{report_id}.json"
    assert dispatch.read_bytes() == _canonical(intent)
    assert executor.calls == [(handle, commands, report_id)]
    assert executor.waits == []

    monkeypatch.setattr(FileAgentBroker, "_replace_dispatch_record_locked", original_replace)
    resumed_executor = _LabExecutor()
    resumed = FileAgentBroker(jobs, results, "eu-south-2", runner)
    progress = resumed.step(event, prepared, ExecutionMode.HYBRID, handle, resumed_executor)

    assert progress.status == "failed"
    assert progress.summary == "Lab dispatch outcome is unknown"
    assert dispatch.read_bytes() == _canonical(intent)
    assert resumed_executor.calls == []
    assert resumed_executor.waits == []


def test_lab_dispatch_response_loss_after_send_publishes_unknown_provenance(
    tmp_path: Path,
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")

    @dataclass
    class ResponseLossExecutor(_LabExecutor):
        def dispatch_powershell(
            self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
        ) -> str:
            super().dispatch_powershell(handle, commands, execution_key=execution_key)
            raise RuntimeError("simulated response loss after SendCommand")

    executor = ResponseLossExecutor()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    report_id, plan_digest, commands = _ready_lab_dispatch(
        broker, jobs, results, event, prepared, executor, handle
    )

    progress = broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)

    assert progress.status == "failed" and progress.provenance is not None
    assert progress.provenance["terminalStatus"] == "dispatch_unknown"
    assert progress.provenance["barrierIds"] == [
        _job_for_phase(jobs, "lab_plan").name,
        report_id,
    ]
    dispatch = cast(dict[str, object], progress.provenance["dispatch"])
    assert dispatch["dispatchId"] == report_id and dispatch["state"] == "intent"
    assert executor.calls == [(handle, commands, report_id)]
    stored = broker._read_exact_dispatch(report_id, handle, plan_digest, commands)
    assert stored is not None and stored["state"] == "intent"


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


def test_lab_dispatch_reserves_full_transition_before_irreversible_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    sent = False

    @dataclass
    class SendingExecutor(_LabExecutor):
        def dispatch_powershell(
            self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
        ) -> str:
            nonlocal sent
            sent = True
            return super().dispatch_powershell(
                handle, commands, execution_key=execution_key
            )

    executor = SendingExecutor()
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    report_id, plan_digest, commands = _ready_lab_dispatch(
        broker, jobs, results, event, prepared, executor, handle
    )

    def reject_late_admission(
        root: Path,
        demand: StorageDemand,
        limit: StorageLimit,
        *,
        exclude: frozenset[str] = frozenset(),
        expected_uid: int | None = None,
        expected_gid: int | None = None,
        expected_file_mode: int | None = None,
        expected_directory_mode: int | None = None,
        root_headroom: bool = True,
    ) -> StorageDemand:
        if sent:
            raise StorageCapacityError("concurrent writer consumed capacity")
        return admit_owner_write(
            root,
            demand,
            limit,
            exclude=exclude,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=expected_file_mode,
            expected_directory_mode=expected_directory_mode,
            root_headroom=root_headroom,
        )

    monkeypatch.setattr(agent_spool, "admit_owner_write", reject_late_admission)
    transcript = broker._dispatch_or_resume(
        executor, handle, commands, plan_digest, report_id
    )

    assert transcript is not None and transcript.succeeded
    assert executor.calls == [(handle, commands, report_id)]
    dispatch = broker._read_exact_dispatch(report_id, handle, plan_digest, commands)
    assert dispatch is not None and dispatch["state"] == "dispatched"

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

    with pytest.raises(AgentSpoolError, match="dispatch"):
        broker.step(event, prepared, ExecutionMode.HYBRID, handle, executor)
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

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", fake_run)
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


@pytest.mark.parametrize("failure", ["workspace-quota", "result-quota", "enospc"])
def test_agent_capacity_after_durable_lab_job_publishes_reserved_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    workspaces = tmp_path / "workspaces"
    bundles = results / "bundles"
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)
    assert broker.step(
        event, prepared, ExecutionMode.HYBRID, LabHandle("lab:test"), _LabExecutor()
    ).status == "pending"

    if failure == "workspace-quota":
        monkeypatch.setattr(
            agent_cli,
            "_admit_workspace_materialization",
            lambda *_args: (_ for _ in ()).throw(StorageCapacityError("full")),
        )
    elif failure == "enospc":
        monkeypatch.setattr(
            agent_cli,
            "_materialize_inputs",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")),
        )
    else:
        original_admit = admit_owner_write

        def refuse_normal_result(
            root: Path, demand: StorageDemand, limit: StorageLimit, **kwargs: Any
        ) -> StorageDemand:
            if root == results and limit == agent_cli._NORMAL_RESULTS_LIMIT:
                raise StorageCapacityError("full")
            return original_admit(root, demand, limit, **kwargs)

        monkeypatch.setattr(agent_cli, "admit_owner_write", refuse_normal_result)

        def fake_run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = Path(arguments[arguments.index("--output-last-message") + 1])
            output.write_text(
                json.dumps(
                    {
                        "succeeded": False,
                        "summary": "model failed",
                        "reportMarkdown": "",
                        "powershellCommands": [],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(arguments, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)

    codex = (tmp_path / "codex").resolve()
    codex.write_text("stub", encoding="utf-8")
    spool = CodexSpoolRunner(
        jobs.resolve(),
        results.resolve(),
        workspaces.resolve(),
        codex,
        60,
        bundles.resolve(),
    )
    assert spool.process_one() == "processed"
    job_id = next(entry.name for entry in jobs.iterdir() if entry.name != "dispatches")
    result = json.loads((results / f"{job_id}.json").read_text(encoding="utf-8"))
    assert result == {
        "kind": "moodle-agent-result-v1",
        "jobId": job_id,
        "phase": "lab_plan",
        "succeeded": False,
        "summary": "Agent storage capacity is exhausted",
        "reportMarkdown": "",
        "powershellCommands": [],
    }


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
    workspace.chmod(0o700)
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

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", fake_run)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    assert (workspace / "inputs/0000-input.txt").read_bytes() == runner.body


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics")
def test_workspace_admission_lock_serializes_runners_through_result_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    workspaces = tmp_path / "workspaces"
    for path in (jobs, results, workspaces, results / "bundles"):
        path.mkdir(parents=True, exist_ok=True)
    first_id, second_id = "a" * 64, "b" * 64
    entered_codex = threading.Event()
    release_codex = threading.Event()
    second_started = threading.Event()
    admissions: list[str] = []
    failures: list[BaseException] = []
    original_admit = agent_cli._admit_workspace_materialization

    def job(job_id: str) -> dict[str, object]:
        return {
            "kind": "moodle-agent-job-v1",
            "jobId": job_id,
            "phase": "central",
            "taskKey": "moodle-task-v1:" + "c" * 64,
            "revisionDigest": "moodle-assignment-v1:" + "d" * 64,
            "title": "lock test",
            "intro": "bounded input",
            "courseName": "course",
            "courseShortname": "course",
            "attachments": [],
        }

    def record_admission(root: Path, workspace: Path, payload: dict[str, object]) -> None:
        admissions.append(workspace.name)
        original_admit(root, workspace, payload)

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        workspace = Path(arguments[arguments.index("-C") + 1])
        if workspace.name == first_id:
            entered_codex.set()
            assert release_codex.wait(timeout=5)
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "succeeded": True,
                    "summary": "done",
                    "reportMarkdown": "# verified",
                    "powershellCommands": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0)

    runner = CodexSpoolRunner(jobs, results, workspaces, tmp_path / "codex", 60)
    monkeypatch.setattr(agent_cli, "_admit_workspace_materialization", record_admission)
    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", fake_run)

    def execute(job_id: str) -> None:
        try:
            runner._execute(jobs / job_id, results / f"{job_id}.json", job(job_id))
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    first = threading.Thread(target=execute, args=(first_id,))
    second = threading.Thread(target=execute, args=(second_id,))
    first.start()
    assert entered_codex.wait(timeout=5)

    def start_second() -> None:
        second_started.set()
        execute(second_id)

    second = threading.Thread(target=start_second)
    second.start()
    assert second_started.wait(timeout=5)
    assert admissions == [first_id]
    release_codex.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert admissions == [first_id, second_id]
    assert (results / f"{first_id}.json").is_file()
    assert (results / f"{second_id}.json").is_file()


def test_workspace_admission_lock_order_covers_publication() -> None:
    execute = inspect.getsource(CodexSpoolRunner._execute)

    assert execute.index("with retention_job_lock") < execute.index("with storage_admission_lock")
    assert execute.index("with storage_admission_lock") < execute.index(
        "_admit_workspace_materialization"
    )
    assert execute.index("_admit_workspace_materialization") < execute.rindex("_publish_result")


def test_central_workspace_is_private_mode_no_follow_and_resets_to_0700(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not available")
    root = tmp_path / "workspaces"
    root.mkdir()
    workspace = root / ("a" * 64)

    CodexSpoolRunner._safe_central_workspace(workspace)
    assert stat.S_IMODE(workspace.lstat().st_mode) == 0o700

    workspace.chmod(0o755)
    with pytest.raises(AgentSpoolError, match="central workspace is unsafe"):
        CodexSpoolRunner._safe_central_workspace(workspace)
    with pytest.raises(AgentSpoolError, match="central workspace is unsafe"):
        CodexSpoolRunner._reset_central_workspace(workspace)
    assert workspace.exists()

    workspace.chmod(0o700)
    (workspace / "stale").write_text("discard", encoding="utf-8")
    CodexSpoolRunner._reset_central_workspace(workspace)
    CodexSpoolRunner._safe_central_workspace(workspace)
    assert stat.S_IMODE(workspace.lstat().st_mode) == 0o700
    assert list(workspace.iterdir()) == []

    workspace.rmdir()
    target = tmp_path / "outside"
    target.mkdir()
    try:
        workspace.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(AgentSpoolError, match="central workspace is unsafe"):
        CodexSpoolRunner._safe_central_workspace(workspace)
    with pytest.raises(AgentSpoolError, match="could not reset central workspace"):
        CodexSpoolRunner._reset_central_workspace(workspace)
    assert list(target.iterdir()) == []


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

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", successful_run)
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
    poisoned_output.parent.chmod(0o700)

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

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", successful_run)
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

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", failed_run)
    agent = CodexSpoolRunner(jobs, results, tmp_path / "workspaces", tmp_path / "codex", 60)

    assert agent.process_one() == "processed"
    result = json.loads((results / f"{job.name}.json").read_text(encoding="utf-8"))
    assert result["kind"] == "moodle-agent-result-v2"
    assert result["jobId"] == job.name
    assert result["role"] == "central_planner"
    assert result["succeeded"] is False
    assert result["summary"] == "Codex execution failed"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX retention locks")
def test_concurrent_dispatch_intent_has_one_send_command(tmp_path: Path) -> None:
    engine = _retention_engine(tmp_path)
    jobs = engine.roots.shared_jobs
    results = engine.roots.agent_results
    runner = _S3Runner()
    event = _event(tmp_path, lab=True)
    prepared = _prepared(event, runner)
    handle = LabHandle("lab:test")
    seed = FileAgentBroker(
        jobs,
        results,
        "eu-south-2",
        runner,
        controller_retention_root=engine._shared,
    )
    report_id, _, _ = _ready_lab_dispatch(
        seed, jobs, results, event, prepared, _LabExecutor(), handle
    )
    executor = _LabExecutor()
    outcomes: list[ExecutionProgress] = []

    def run() -> None:
        outcomes.append(
            FileAgentBroker(
                jobs,
                results,
                "eu-south-2",
                runner,
                controller_retention_root=engine._shared,
            ).step(
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
    assert any(outcome.status == "pending" for outcome in outcomes)
    assert all(
        outcome.status == "pending"
        or (outcome.status == "failed" and outcome.summary == "Lab dispatch outcome is unknown")
        for outcome in outcomes
    )
    assert executor.calls == [(handle, ("Write-Output 'ok'",), report_id)]
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


def _without_root_headroom(
    root: Path,
    demand: StorageDemand,
    limit: StorageLimit,
    **kwargs: Any,
) -> StorageDemand:
    return admit_owner_write(root, demand, limit, root_headroom=False, **kwargs)


def _lab_job_template(tmp_path: Path) -> tuple[bytes, tuple[PreparedArtifact, ...]]:
    jobs = tmp_path / "template-jobs"
    runner = _S3Runner()
    event = _event(tmp_path, marker="a")
    prepared = _prepared(event, runner)
    broker = FileAgentBroker(jobs, tmp_path / "template-results", "eu-south-2", runner)
    job_id = broker._ensure_job("lab_plan", event, prepared, None)
    return (jobs / job_id / "job.json").read_bytes(), prepared.artifacts


def test_lab_job_storage_admission_has_exact_boundary_and_rejects_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded, artifacts = _lab_job_template(tmp_path)
    monkeypatch.setattr(agent_spool, "admit_owner_write", _without_root_headroom)

    accepted_jobs = tmp_path / "accepted-jobs"
    accepted_jobs.mkdir()
    accepted_current = measure_tree_no_follow(accepted_jobs)
    demand = _job_storage_demand(accepted_jobs, encoded, artifacts)
    monkeypatch.setattr(
        agent_spool,
        "_STORAGE_POLICY",
        StoragePolicy(
            jobs=StorageLimit(
                accepted_current.allocated_bytes + demand.allocated_bytes,
                accepted_current.nodes + demand.nodes,
            )
        ),
    )
    runner = _S3Runner()
    event = _event(tmp_path, marker="b")
    broker = FileAgentBroker(accepted_jobs, tmp_path / "accepted-results", "eu-south-2", runner)
    job_id = broker._ensure_job("lab_plan", event, _prepared(event, runner), None)
    assert (accepted_jobs / job_id / "job.json").is_file()

    rejected_jobs = tmp_path / "rejected-jobs"
    rejected_jobs.mkdir()
    rejected_current = measure_tree_no_follow(rejected_jobs)
    rejected_demand = _job_storage_demand(rejected_jobs, encoded, artifacts)
    monkeypatch.setattr(
        agent_spool,
        "_STORAGE_POLICY",
        StoragePolicy(
            jobs=StorageLimit(
                rejected_current.allocated_bytes + rejected_demand.allocated_bytes - 1,
                rejected_current.nodes + rejected_demand.nodes,
            )
        ),
    )
    rejected_runner = _S3Runner()
    rejected_event = _event(tmp_path, marker="c")
    rejected = FileAgentBroker(
        rejected_jobs, tmp_path / "rejected-results", "eu-south-2", rejected_runner
    )
    with pytest.raises(StorageCapacityError):
        rejected._ensure_job(
            "lab_plan", rejected_event, _prepared(rejected_event, rejected_runner), None
        )
    assert rejected_runner.calls == []
    assert list(rejected_jobs.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX flock admission")
def test_concurrent_lab_publishers_admit_only_one_boundary_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded, artifacts = _lab_job_template(tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    current = measure_tree_no_follow(jobs)
    demand = _job_storage_demand(jobs, encoded, artifacts)
    monkeypatch.setattr(agent_spool, "admit_owner_write", _without_root_headroom)
    monkeypatch.setattr(
        agent_spool,
        "_STORAGE_POLICY",
        StoragePolicy(
            jobs=StorageLimit(
                current.allocated_bytes + demand.allocated_bytes,
                current.nodes + demand.nodes,
            )
        ),
    )
    outcomes: list[str] = []

    def publish(marker: str) -> None:
        runner = _S3Runner()
        event = _event(tmp_path, marker=marker)
        broker = FileAgentBroker(jobs, tmp_path / f"results-{marker}", "eu-south-2", runner)
        try:
            broker._ensure_job("lab_plan", event, _prepared(event, runner), None)
        except StorageCapacityError:
            outcomes.append("refused")
        else:
            outcomes.append("published")

    first = threading.Thread(target=publish, args=("d",))
    second = threading.Thread(target=publish, args=("e",))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert sorted(outcomes) == ["published", "refused"]
    assert len([entry for entry in jobs.iterdir() if entry.is_dir()]) == 1
    assert not [entry for entry in jobs.iterdir() if entry.name.startswith(".")]


def test_central_chain_projection_reserves_three_results_and_dependency_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def capture(root: Path, sizes: tuple[int, ...], nodes: int) -> StorageDemand:
        observed.update(root=root, sizes=sizes, nodes=nodes)
        return StorageDemand(0, nodes)

    monkeypatch.setattr(agent_spool, "storage_demand_for_files", capture)
    artifact = PreparedArtifact("key", "input", 17, "a" * 64, "bucket", "object")
    encoded = b"planner"
    demand = _central_chain_storage_demand(tmp_path, encoded, (artifact,))
    base = (len(encoded), artifact.size_bytes)
    assert observed == {
        "root": tmp_path,
        "sizes": base * 3
        + (_MAX_CENTRAL_RESULT_BYTES,) * 3
        + (_central_dependency_envelope_bytes(),),
        "nodes": 12,
    }
    assert demand == StorageDemand(0, 12)


def test_central_chain_capacity_refusal_happens_before_planner_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    runner = _S3Runner()
    event = _event(tmp_path, marker="f")
    broker = FileAgentBroker(jobs, results, "eu-south-2", runner)

    def refuse(*_args: object, **_kwargs: object) -> StorageDemand:
        raise StorageCapacityError("full")

    monkeypatch.setattr(agent_spool, "admit_owner_write", refuse)
    with pytest.raises(StorageCapacityError):
        broker.step(event, _prepared(event, runner), ExecutionMode.CENTRAL, None, _LabExecutor())
    assert runner.calls == []
    assert not jobs.exists() or list(jobs.iterdir()) == []


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="requires POSIX retention locks")


def _retention_engine(tmp_path: Path) -> RetentionFilesystem:
    engine = RetentionFilesystem(
        RetentionRoots(
            tmp_path / "controller",
            tmp_path / "jobs",
            tmp_path / "agent",
            tmp_path / "results",
            tmp_path / "workspaces",
            tmp_path / "bundles",
        )
    )
    for root, mode in (
        (engine.roots.controller_private, 0o750),
        (engine.roots.shared_jobs, 0o2750),
        (engine.roots.agent_private, 0o700),
        (engine.roots.agent_results, 0o2750),
        (engine.roots.agent_workspaces, 0o700),
        (engine.roots.agent_bundles, 0o2750),
    ):
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(mode)
    for root in (
        engine.roots.shared_jobs / ".retention",
        engine._committed,
        engine._controller_barriers,
        engine._controller_locks,
        engine.roots.agent_results / ".retention",
        engine._acks,
    ):
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o2750)
    return engine


def _central_job(jobs: Path, role: str) -> Path:
    return next(
        job
        for job in jobs.iterdir()
        if job.is_dir()
        and not job.is_symlink()
        and (job / "job.json").is_file()
        and json.loads((job / "job.json").read_text(encoding="utf-8")).get("role") == role
    )


def _central_chain(
    engine: RetentionFilesystem,
) -> tuple[FileAgentBroker, NotificationEvent, PreparedAssignment, tuple[Path, ...]]:
    runner = _S3Runner()
    event = _event(engine.roots.controller_private)
    prepared = _prepared(event, runner)
    broker = FileAgentBroker(
        engine.roots.shared_jobs, engine.roots.agent_results, "eu-south-2", runner
    )
    executor = _LabExecutor()
    assert broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor).status == "pending"
    planner = _central_job(engine.roots.shared_jobs, "central_planner")
    plan = _central_plan()
    planner_result: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": planner.name,
        "role": "central_planner",
        "succeeded": True,
        "summary": "plan ready",
        "reportMarkdown": "# Informe\nPlan verified.",
        "plan": plan,
        "planDigest": hashlib.sha256(_canonical(plan)).hexdigest(),
    }
    planner_result["plannerResultDigest"] = hashlib.sha256(_canonical(planner_result)).hexdigest()
    (engine.roots.agent_results / f"{planner.name}.json").write_bytes(_canonical(planner_result))
    assert broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor).status == "pending"
    executor_job = _central_job(engine.roots.shared_jobs, "central_executor")
    outputs = engine.roots.controller_private / "outputs"
    outputs.mkdir()
    (outputs / "report.md").write_bytes(b"verified artifact\n")
    manifest, bundle_digest = _collect_artifact_bundle(
        outputs, ["report.md"], engine.roots.agent_results / "bundles"
    )
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
    (engine.roots.agent_results / f"{executor_job.name}.json").write_bytes(
        _canonical(executor_result)
    )
    assert broker.step(event, prepared, ExecutionMode.CENTRAL, None, executor).status == "pending"
    reviewer = _central_job(engine.roots.shared_jobs, "central_reviewer")
    return broker, event, prepared, (planner, executor_job, reviewer)


def _scratch_tombstone(
    event: NotificationEvent,
    job_paths: tuple[Path, ...],
    *,
    results_root: Path | None = None,
) -> PreparedTombstone:
    job_ids = tuple(path.name for path in job_paths)
    result_digests: list[str] = []
    for path in job_paths:
        result_path = (results_root or job_paths[0].parent.parent / "results") / f"{path.name}.json"
        if not result_path.exists():
            result_digests.append(hashlib.sha256(path.name.encode()).hexdigest())
            continue
        result = cast(dict[str, object], json.loads(result_path.read_bytes()))
        result_digests.append(
            cast(
                str,
                result[
                    {
                        "central_planner": "plannerResultDigest",
                        "central_executor": "executorResultDigest",
                        "central_reviewer": "reviewerResultDigest",
                    }[cast(str, result["role"])]
                ],
            )
        )
    record = RetentionRecord(
        _event_id(event.task_key, event.revision_digest),
        event.task_key,
        event.revision_digest,
        ExecutionMode.CENTRAL,
        "cleaned",
        True,
        1,
        None,
        1,
        None,
        job_ids,
        hashlib.sha256(b"bundle").hexdigest(),
        result_digests=tuple(result_digests),
    )
    plans = plan_retention((record,), now=1, limit=1)
    assert len(plans) == 1
    assert plans[0].target_phase == "scratch" and plans[0].job_ids == job_ids
    return plans[0]


@_POSIX_ONLY
def test_controller_publish_cannot_recreate_job_after_scratch_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _retention_engine(tmp_path)
    _broker, event, prepared, jobs = _central_chain(engine)
    scratch = _scratch_tombstone(event, jobs)
    planner = jobs[0]
    planner_result = engine.roots.agent_results / f"{planner.name}.json"
    shutil.rmtree(planner)
    planner_result.unlink()
    broker = FileAgentBroker(
        engine.roots.shared_jobs,
        engine.roots.agent_results,
        "eu-south-2",
        _S3Runner(),
        controller_retention_root=engine._shared,
    )
    requested, release = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    original_lock = retention_job_lock

    @contextmanager
    def gated_lock(root: Path | None, job_id: str) -> Iterator[None]:
        if root == engine._shared and job_id == planner.name:
            requested.set()
            assert release.wait(timeout=5)
        with original_lock(root, job_id):
            yield

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_spool.retention_job_lock", gated_lock)

    def publish() -> None:
        try:
            broker.step(event, prepared, ExecutionMode.CENTRAL, None, _LabExecutor())
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=publish)
    thread.start()
    assert requested.wait(timeout=5)
    engine.commit(scratch, committed_at=1)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], AgentSpoolError)
    assert not planner.exists()
    assert all((engine._controller_barriers / f"{job.name}.json").is_file() for job in jobs)


@_POSIX_ONLY
def test_lab_publish_cannot_recreate_job_after_scratch_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _retention_engine(tmp_path)
    runner = _S3Runner()
    event = _event(tmp_path, lab=True, marker="e")
    prepared = _prepared(event, runner)
    seed = FileAgentBroker(
        engine.roots.shared_jobs, engine.roots.agent_results, "eu-south-2", runner
    )
    executor = _LabExecutor()
    assert (
        seed.step(
            event, prepared, ExecutionMode.HYBRID, LabHandle("lab:test"), executor
        ).status
        == "pending"
    )
    plan = _job_for_phase(engine.roots.shared_jobs, "lab_plan")
    _write_result(engine.roots.agent_results, plan, succeeded=False)
    result = cast(
        dict[str, object],
        json.loads((engine.roots.agent_results / f"{plan.name}.json").read_bytes()),
    )
    record = RetentionRecord(
        event.event_id,
        event.task_key,
        event.revision_digest,
        ExecutionMode.HYBRID,
        "cleaned",
        False,
        1,
        None,
        1,
        None,
        (plan.name,),
        None,
        "lab",
        (plan.name,),
        result_digests=(lab_protocol.canonical_digest(result),),
    )
    scratch = plan_retention((record,), now=1, limit=1)[0]
    shutil.rmtree(plan)
    (engine.roots.agent_results / f"{plan.name}.json").unlink()
    broker = FileAgentBroker(
        engine.roots.shared_jobs,
        engine.roots.agent_results,
        "eu-south-2",
        runner,
        controller_retention_root=engine._shared,
    )
    requested, release = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    original_lock = retention_job_lock

    @contextmanager
    def gated_lock(root: Path | None, job_id: str) -> Iterator[None]:
        if root == engine._shared and job_id == plan.name:
            requested.set()
            assert release.wait(timeout=5)
        with original_lock(root, job_id):
            yield

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_spool.retention_job_lock", gated_lock)

    def publish() -> None:
        try:
            broker.step(
                event,
                prepared,
                ExecutionMode.HYBRID,
                LabHandle("lab:test"),
                _LabExecutor(),
            )
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=publish)
    thread.start()
    assert requested.wait(timeout=5)
    engine.commit(scratch, committed_at=1)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], AgentSpoolError)
    assert not plan.exists()
    assert (engine._controller_barriers / f"{plan.name}.json").is_file()


@_POSIX_ONLY
def test_agent_execution_cannot_publish_result_after_scratch_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _retention_engine(tmp_path)
    _broker, event, _prepared_assignment, jobs = _central_chain(engine)
    fixture_results = tmp_path / "fixture-results"
    fixture_workspaces = tmp_path / "fixture-workspaces"
    fixture_bundles = tmp_path / "fixture-bundles"

    def accepted_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        workspace = Path(arguments[arguments.index("-C") + 1])
        job = _load_job(engine.roots.shared_jobs / workspace.name)
        role = cast(str, job["role"])
        models: dict[str, dict[str, object]] = {
            "central_planner": {
                "succeeded": True,
                "summary": "plan ready",
                "reportMarkdown": "# Informe\nPlan verified.",
                "plan": _central_plan(),
            },
            "central_executor": {
                "succeeded": True,
                "summary": "executed",
                "reportMarkdown": "# Informe\nArtifact verified.",
                "evidence": {"report": "outputs/report.md"},
            },
            "central_reviewer": {
                "succeeded": True,
                "summary": "reviewed",
                "reportMarkdown": "# Informe\nReview verified.",
                "accepted": True,
                "decisions": {"report": "accepted"},
                "findings": [],
            },
        }
        if role == "central_executor":
            outputs = workspace / "outputs"
            outputs.mkdir()
            (outputs / "report.md").write_bytes(b"verified artifact\n")
        output = Path(arguments[arguments.index("--output-last-message") + 1])
        output.write_bytes(_canonical(models[role]))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", accepted_run)
    fixture_runner = CodexSpoolRunner(
        engine.roots.shared_jobs,
        fixture_results,
        fixture_workspaces,
        tmp_path / "fixture-codex",
        60,
        bundles_root=fixture_bundles,
    )
    assert [fixture_runner.process_one() for _ in jobs] == ["processed"] * len(jobs)
    bundles = {bundle.name: bundle.read_bytes() for bundle in fixture_bundles.glob("*.zip")}
    for name, data in bundles.items():
        _write_exclusive(engine.roots.agent_bundles / name, data, 0o640)
    for job in jobs:
        (engine.roots.agent_results / f"{job.name}.json").unlink(missing_ok=True)
        workspace = shutil.copytree(
            fixture_workspaces / job.name,
            engine.roots.agent_workspaces / job.name,
        )
        workspace.chmod(0o700)
        inputs = workspace / "inputs"
        inputs.chmod(0o2750)
        for input_file in inputs.iterdir():
            input_file.chmod(0o640)
    scratch = _scratch_tombstone(event, jobs, results_root=fixture_results)
    engine.commit(scratch, committed_at=1)
    requested, release = threading.Event(), threading.Event()
    outcomes: list[str] = []
    failures: list[BaseException] = []
    invoked: list[object] = []
    original_lock = retention_job_lock

    @contextmanager
    def gated_lock(root: Path | None, job_id: str) -> Iterator[None]:
        if root == engine.roots.agent_private:
            requested.set()
            assert release.wait(timeout=5)
        with original_lock(root, job_id):
            yield

    def should_not_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        invoked.append(object())
        raise AssertionError("Codex must not run after the retention barrier")

    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.retention_job_lock", gated_lock)
    monkeypatch.setattr("moodle_autotask.adapters.aws.agent_cli.subprocess.run", should_not_run)
    runner = CodexSpoolRunner(
        engine.roots.shared_jobs,
        engine.roots.agent_results,
        engine.roots.agent_workspaces,
        tmp_path / "codex",
        60,
        bundles_root=engine.roots.agent_bundles,
        retention_root=engine.roots.agent_private,
    )

    def execute() -> None:
        try:
            outcomes.append(runner.process_one())
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert requested.wait(timeout=5)
    for job in jobs:
        _write_exclusive(
            engine.roots.agent_results / f"{job.name}.json",
            (fixture_results / f"{job.name}.json").read_bytes(),
            0o640,
        )
    engine.agent_consume(scratch.tombstone_id, acknowledged_at=2, now=2)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    assert outcomes == ["retention-refused"]
    assert invoked == []
    assert all(not (engine.roots.agent_workspaces / job.name).exists() for job in jobs)
    assert all(not (engine.roots.agent_results / f"{job.name}.json").exists() for job in jobs)
    assert {name: (engine.roots.agent_bundles / name).read_bytes() for name in bundles} == bundles


@_POSIX_ONLY
def test_fallback_failure_does_not_recreate_result_after_barrier_race(
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
    agent_private = tmp_path / "agent-private"
    agent_private.mkdir()
    agent_private.chmod(0o700)
    fallback_requested = threading.Event()
    barrier_written = threading.Event()
    executions: list[object] = []
    original_lock = retention_job_lock

    def fail_after_execution_lock(
        _directory: Path, _result: Path, payload: dict[str, object]
    ) -> None:
        with original_lock(agent_private, cast(str, payload["jobId"])):
            executions.append(object())
        raise AgentSpoolError("injected execution failure")

    @contextmanager
    def gate_fallback_lock(root: Path | None, job_id: str) -> Iterator[None]:
        assert root == agent_private and job_id == job.name
        fallback_requested.set()
        assert barrier_written.wait(timeout=5)
        with original_lock(root, job_id):
            yield

    agent = CodexSpoolRunner(
        jobs,
        results,
        tmp_path / "workspaces",
        tmp_path / "codex",
        60,
        retention_root=agent_private,
    )
    monkeypatch.setattr(agent, "_execute", fail_after_execution_lock)
    monkeypatch.setattr(
        "moodle_autotask.adapters.aws.agent_cli.retention_job_lock", gate_fallback_lock
    )
    outcomes: list[str] = []

    thread = threading.Thread(target=lambda: outcomes.append(agent.process_one()))
    thread.start()
    assert fallback_requested.wait(timeout=5)
    barriers = agent_private / "retention" / "barriers"
    barriers.mkdir(parents=True)
    (barriers / f"{job.name}.json").write_bytes(b"{}")
    barrier_written.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert outcomes == ["retention-refused"]
    assert len(executions) == 1
    assert not (results / f"{job.name}.json").exists()
