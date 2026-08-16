"""Linux-only integration coverage for the explicit Phase B retention engine."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import textwrap
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from moodle_autotask.adapters.aws import central_protocol, lab_protocol, retention_fs
from moodle_autotask.adapters.aws.retention import (
    AgentRetentionAck,
    CommittedTombstone,
    PreparedTombstone,
    decode_committed,
    plan_retention,
)
from moodle_autotask.adapters.aws.retention_fs import (
    RetentionCapacityError,
    RetentionFilesystem,
    RetentionFilesystemError,
    RetentionOwnership,
    RetentionRoots,
    RetentionStoragePolicy,
    retention_job_lock,
)
from moodle_autotask.adapters.aws.retention_runtime import ControllerRetentionCoordinator
from moodle_autotask.adapters.aws.storage_quota import StorageLimit, measure_tree_no_follow
from moodle_autotask.adapters.moodle.approval_state import (
    ApprovalState,
    RetentionRecord,
)
from moodle_autotask.adapters.moodle.state import MoodleState, NotificationDraft, _event_id
from moodle_autotask.domain.models import ExecutionMode

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="requires POSIX dirfd/no-follow traversal")


def _posix_identity() -> tuple[int, int]:
    getuid = cast(Callable[[], int] | None, getattr(os, "getuid", None))
    getgid = cast(Callable[[], int] | None, getattr(os, "getgid", None))
    if getuid is None or getgid is None:
        raise RuntimeError("POSIX identity APIs are unavailable")
    return getuid(), getgid()


def _prepared() -> PreparedTombstone:
    return _prepared_phases()[0]


def _central_corpus(
    task: str, revision: str, *, specification_digest: str = "f" * 64
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    event = _event_id(task, revision)
    input_data = b"input\n"
    input_record = {
        "attachmentKey": "moodle-attachment-v1:" + "a" * 64,
        "filename": "input.txt",
        "sizeBytes": len(input_data),
        "sha256": hashlib.sha256(input_data).hexdigest(),
        "path": "inputs/0000-input.txt",
    }
    plan = {
        "steps": ["Produce report."],
        "acceptanceCriteria": [{"id": "report", "text": "Report exists."}],
        "expectedArtifacts": ["report.md"],
    }
    manifest_data = b"verified artifact\n"
    manifest = {
        "kind": "artifact-manifest-v1",
        "files": [
            {
                "path": "report.md",
                "size": len(manifest_data),
                "sha256": hashlib.sha256(manifest_data).hexdigest(),
            }
        ],
        "totals": {"files": 1, "bytes": len(manifest_data)},
    }
    prepared_manifest = [input_record]

    def job(role: str, dependencies: dict[str, str], **extra: object) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": central_protocol.CENTRAL_JOB_KIND,
            "role": role,
            "eventId": event,
            "taskKey": task,
            "revisionDigest": revision,
            "selectedMode": "central",
            "specificationDigest": specification_digest,
            "preparedInputManifestDigest": central_protocol.canonical_digest(prepared_manifest),
            "assignmentSnapshot": {
                "courseName": "ASIX",
                "courseShortname": "ASIX-M06",
                "title": "Controlled task",
                "intro": "Produce evidence.",
            },
            "preparedInputs": [input_record],
            "dependencies": dependencies,
            **extra,
        }
        value["jobId"] = central_protocol.canonical_digest(value)
        return value

    planner = job("central_planner", {})
    planner_result: dict[str, object] = {
        "kind": central_protocol.CENTRAL_RESULT_KIND,
        "jobId": planner["jobId"],
        "role": "central_planner",
        "succeeded": True,
        "summary": "planned",
        "reportMarkdown": "# Informe\nPlan.",
        "plan": plan,
        "planDigest": central_protocol.canonical_digest(plan),
    }
    planner_result["plannerResultDigest"] = central_protocol.canonical_digest(planner_result)
    executor = job(
        "central_executor",
        {
            "plannerJobId": cast(str, planner["jobId"]),
            "planDigest": central_protocol.canonical_digest(plan),
            "plannerResultDigest": cast(str, planner_result["plannerResultDigest"]),
        },
        plan=plan,
    )
    bundle_digest = hashlib.sha256(b"bundle").hexdigest()
    executor_result: dict[str, object] = {
        "kind": central_protocol.CENTRAL_RESULT_KIND,
        "jobId": executor["jobId"],
        "role": "central_executor",
        "succeeded": True,
        "summary": "executed",
        "reportMarkdown": "# Informe\nExecution.",
        "evidence": {"report": "outputs/report.md"},
        "artifactManifest": manifest,
        "artifactManifestDigest": central_protocol.canonical_digest(manifest),
        "artifactBundleDigest": bundle_digest,
        "bundleLocator": f"bundles/{bundle_digest}.zip",
    }
    executor_result["executorResultDigest"] = central_protocol.canonical_digest(executor_result)
    reviewer = job(
        "central_reviewer",
        {
            "plannerJobId": cast(str, planner["jobId"]),
            "planDigest": central_protocol.canonical_digest(plan),
            "plannerResultDigest": cast(str, planner_result["plannerResultDigest"]),
            "executorJobId": cast(str, executor["jobId"]),
            "executorResultDigest": cast(str, executor_result["executorResultDigest"]),
            "artifactManifestDigest": cast(str, executor_result["artifactManifestDigest"]),
            "artifactBundleDigest": bundle_digest,
        },
        plan=plan,
        executorResult=executor_result,
    )
    reviewer_result: dict[str, object] = {
        "kind": central_protocol.CENTRAL_RESULT_KIND,
        "jobId": reviewer["jobId"],
        "role": "central_reviewer",
        "succeeded": True,
        "summary": "reviewed",
        "reportMarkdown": "# Informe\nReview.",
        "accepted": True,
        "decisions": {"report": "accepted"},
        "findings": [],
        "dependencyDigests": {
            key: value
            for key, value in cast(dict[str, str], reviewer["dependencies"]).items()
            if key.endswith("Digest")
        },
    }
    reviewer_result["reviewerResultDigest"] = central_protocol.canonical_digest(reviewer_result)
    models = [
        {
            "succeeded": True,
            "summary": "planned",
            "reportMarkdown": "# Informe\nPlan.",
            "plan": plan,
        },
        {
            "succeeded": True,
            "summary": "executed",
            "reportMarkdown": "# Informe\nExecution.",
            "evidence": {"report": "outputs/report.md"},
        },
        {
            "succeeded": True,
            "summary": "reviewed",
            "reportMarkdown": "# Informe\nReview.",
            "accepted": True,
            "decisions": {"report": "accepted"},
            "findings": [],
        },
    ]
    return [planner, executor, reviewer], [planner_result, executor_result, reviewer_result], models


def _prepared_phases() -> tuple[PreparedTombstone, PreparedTombstone]:
    task = "moodle-task-v1:" + "a" * 64
    revision = "moodle-assignment-v1:" + "b" * 64
    jobs, results, _models = _central_corpus(task, revision)
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.CENTRAL,
        "cleaned",
        True,
        1,
        1,
        1,
        1,
        cast(tuple[str, str, str], tuple(cast(str, job["jobId"]) for job in jobs)),
        hashlib.sha256(b"bundle").hexdigest(),
        result_digests=tuple(
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
            for result in results
        ),
    )
    plans = plan_retention((record,), now=1, limit=2)
    assert [plan.target_phase for plan in plans] == ["scratch", "evidence"]
    return cast(tuple[PreparedTombstone, PreparedTombstone], plans)


def _engine(tmp_path: Path) -> RetentionFilesystem:
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


def _quota_policy(
    *,
    controller_hard: StorageLimit | None = None,
    controller_soft: StorageLimit | None = None,
) -> RetentionStoragePolicy:
    open_limit = StorageLimit(1 << 30, 100_000)
    return RetentionStoragePolicy(
        controller_private_hard=controller_hard or open_limit,
        controller_private_soft=controller_soft or open_limit,
        shared_hard=open_limit,
        shared_soft=open_limit,
        agent_private_hard=open_limit,
        agent_private_soft=open_limit,
        acknowledgements_hard=open_limit,
        acknowledgements_soft=open_limit,
    )


def _terminalize_corpus(
    jobs: list[dict[str, object]],
    results: list[dict[str, object]],
    models: list[dict[str, object]],
    terminal: str | None,
) -> None:
    if terminal == "failed":
        role = cast(str, jobs[-1]["role"])
        digest_key = {
            "central_planner": "plannerResultDigest",
            "central_executor": "executorResultDigest",
            "central_reviewer": "reviewerResultDigest",
        }[role]
        failure: dict[str, object] = {
            "kind": central_protocol.CENTRAL_RESULT_KIND,
            "jobId": jobs[-1]["jobId"],
            "role": role,
            "succeeded": False,
            "summary": "failed",
            "reportMarkdown": "",
        }
        failure[digest_key] = central_protocol.canonical_digest(failure)
        results[-1] = failure
        models[-1] = {
            "succeeded": False,
            "summary": "failed",
            "reportMarkdown": "",
        }
    elif terminal == "rejected":
        assert jobs[-1]["role"] == "central_reviewer"
        results[-1]["accepted"] = False
        results[-1]["decisions"] = {"report": "rejected"}
        _rehash_result(results[-1])
        models[-1]["accepted"] = False
        models[-1]["decisions"] = {"report": "rejected"}


def _seed_targets(
    engine: RetentionFilesystem,
    prepared: PreparedTombstone,
    *,
    terminal: str | None = None,
    specification_digest: str = "f" * 64,
) -> None:
    if prepared.target_phase == "evidence":
        assert prepared.bundle_digest is not None
        engine.roots.agent_bundles.mkdir(parents=True, exist_ok=True)
        bundle = engine.roots.agent_bundles / f"{prepared.bundle_digest}.zip"
        bundle.write_bytes(b"bundle")
        bundle.chmod(0o640)
        return
    jobs, results, models = _central_corpus(
        prepared.task_key,
        prepared.revision_digest,
        specification_digest=specification_digest,
    )
    jobs = jobs[: len(prepared.job_ids)]
    results = results[: len(prepared.job_ids)]
    models = models[: len(prepared.job_ids)]
    assert tuple(job["jobId"] for job in jobs) == prepared.job_ids
    _terminalize_corpus(jobs, results, models, terminal)
    if len(results) >= 2 and results[1].get("succeeded") is True:
        bundle_digest = cast(str, results[1]["artifactBundleDigest"])
        bundle = engine.roots.agent_bundles / f"{bundle_digest}.zip"
        bundle.write_bytes(b"bundle")
        bundle.chmod(0o640)
    for central_job, result, model in zip(jobs, results, models, strict=True):
        job_id = cast(str, central_job["jobId"])
        job_directory = engine.roots.shared_jobs / job_id
        job_directory.mkdir(parents=True)
        job_directory.chmod(0o2750)
        (job_directory / "job.json").write_bytes(central_protocol.canonical_json(central_job))
        (job_directory / "job.json").chmod(0o640)
        job_inputs = job_directory / "inputs"
        job_inputs.mkdir()
        job_inputs.chmod(0o2750)
        (job_inputs / "0000-input.txt").write_bytes(b"input\n")
        (job_inputs / "0000-input.txt").chmod(0o640)
        workspace = engine.roots.agent_workspaces / job_id
        workspace.mkdir(parents=True)
        workspace.chmod(0o700)
        contract = central_protocol.central_workspace_contract(central_job)
        (workspace / "inputs").mkdir()
        (workspace / "inputs" / "0000-input.txt").write_bytes(b"input\n")
        (workspace / "result-schema.json").write_bytes(
            cast(str, contract["resultSchemaJson"]).encode("utf-8")
        )
        (workspace / "last-message.json").write_bytes(central_protocol.canonical_json(model))
        if central_job["role"] == "central_executor":
            outputs = workspace / "outputs"
            outputs.mkdir()
            (outputs / "report.md").write_bytes(b"verified artifact\n")
        engine.roots.agent_results.mkdir(parents=True, exist_ok=True)
        (engine.roots.agent_results / f"{job_id}.json").write_bytes(
            central_protocol.canonical_json(result)
        )
        (engine.roots.agent_results / f"{job_id}.json").chmod(0o640)


def _lab_corpus(
    task: str, revision: str
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
]:
    transfer = "d" * 64
    attachment = {
        "filename": "input.txt",
        "sizeBytes": 6,
        "sha256": hashlib.sha256(b"input\n").hexdigest(),
        "path": "inputs/0000-input.txt",
    }
    common: dict[str, object] = {
        "kind": lab_protocol.LAB_JOB_KIND,
        "taskKey": task,
        "revisionDigest": revision,
        "courseName": "ASIX",
        "courseShortname": "ASIX-M06",
        "title": "Controlled lab",
        "intro": "Run the plan.",
        "attachments": [attachment],
        "guestInputTransfer": {
            "transferDigest": transfer,
            "guestPaths": [
                f"C:\\ProgramData\\MoodleAutotask\\inputs\\{transfer}\\input.txt"
            ],
        },
    }
    plan: dict[str, object] = {
        **common,
        "phase": "lab_plan",
        "context": None,
    }
    plan["jobId"] = lab_protocol.canonical_digest(
        {
            "contextDigest": transfer,
            "phase": "lab_plan",
            "revisionDigest": revision,
            "taskKey": task,
        }
    )
    plan_result: dict[str, object] = {
        "kind": lab_protocol.LAB_RESULT_KIND,
        "jobId": plan["jobId"],
        "phase": "lab_plan",
        "succeeded": True,
        "summary": "planned",
        "reportMarkdown": "# Plan",
        "powershellCommands": ["Write-Output ok"],
    }
    plan_digest = lab_protocol.canonical_digest(plan_result)
    context = {
        "planDigest": plan_digest,
        "labSucceeded": True,
        "transcript": "ok",
        "transferDigest": transfer,
    }
    report_context_digest = lab_protocol.canonical_digest(
        {"planDigest": plan_digest, "transferDigest": transfer}
    )
    report: dict[str, object] = {
        **common,
        "phase": "lab_report",
        "context": context,
    }
    report["jobId"] = lab_protocol.canonical_digest(
        {
            "contextDigest": report_context_digest,
            "phase": "lab_report",
            "revisionDigest": revision,
            "taskKey": task,
        }
    )
    report_result: dict[str, object] = {
        "kind": lab_protocol.LAB_RESULT_KIND,
        "jobId": report["jobId"],
        "phase": "lab_report",
        "succeeded": True,
        "summary": "reported",
        "reportMarkdown": "# Report",
        "powershellCommands": [],
    }
    models = [
        {key: value for key, value in plan_result.items() if key not in {"kind", "jobId", "phase"}},
        {
            key: value
            for key, value in report_result.items()
            if key not in {"kind", "jobId", "phase"}
        },
    ]
    dispatch = {
        "kind": lab_protocol.LAB_DISPATCH_KIND,
        "executionKey": report["jobId"],
        "labHandle": "lab:test",
        "planDigest": plan_digest,
        "commandsDigest": lab_protocol.canonical_digest(["Write-Output ok"]),
        "state": "dispatched",
        "commandId": "12345678-1234-1234-1234-123456789abc",
    }
    lab_protocol.validate_chain(
        [plan, report],
        [plan_result, report_result],
        expected_job_ids=(cast(str, plan["jobId"]), cast(str, report["jobId"])),
    )
    return [plan, report], [plan_result, report_result], models, dispatch


def _seed_lab_targets(
    engine: RetentionFilesystem,
    jobs: list[dict[str, object]],
    results: list[dict[str, object]],
    models: list[dict[str, object]],
    dispatch: dict[str, object],
) -> None:
    for job, result, model in zip(jobs, results, models, strict=True):
        job_id = cast(str, job["jobId"])
        directory = engine.roots.shared_jobs / job_id
        directory.mkdir(parents=True)
        directory.chmod(0o2750)
        (directory / "job.json").write_bytes(lab_protocol.canonical_json(job))
        (directory / "job.json").chmod(0o640)
        inputs = directory / "inputs"
        inputs.mkdir()
        inputs.chmod(0o2750)
        (inputs / "0000-input.txt").write_bytes(b"input\n")
        (inputs / "0000-input.txt").chmod(0o640)
        workspace = engine.roots.agent_workspaces / job_id
        workspace.mkdir()
        workspace.chmod(0o700)
        (workspace / "inputs").mkdir()
        (workspace / "inputs" / "0000-input.txt").write_bytes(b"input\n")
        (workspace / "result-schema.json").write_bytes(lab_protocol.model_schema_json())
        (workspace / "last-message.json").write_bytes(lab_protocol.canonical_json(model))
        result_path = engine.roots.agent_results / f"{job_id}.json"
        result_path.write_bytes(lab_protocol.canonical_json(result))
        result_path.chmod(0o640)
    dispatches = engine.roots.shared_jobs / "dispatches"
    dispatches.mkdir()
    dispatches.chmod(0o2750)
    dispatch_path = dispatches / f"{dispatch['executionKey']}.json"
    dispatch_path.write_bytes(lab_protocol.canonical_json(dispatch))
    dispatch_path.chmod(0o640)


def _complete_terminal_state(
    tmp_path: Path,
    state: ApprovalState,
    marker: str,
    *,
    now: int,
    delivered_at: int | None = None,
) -> str:
    task = "moodle-task-v1:" + marker * 64
    revision = "moodle-assignment-v1:" + marker * 64
    event = MoodleState(tmp_path / f"moodle-{marker}.sqlite3").enqueue(
        NotificationDraft(
            task,
            revision,
            "Course",
            "C",
            "Assignment",
            0,
            1,
            0,
            0,
            1,
            (),
            1,
        ),
        now=now,
    )
    assert event is not None
    buttons = state.prepare(event, now=now)
    state.resolve(buttons.approve, 1, 1, now=now + 1)
    pending = state.claim_work("worker", 60, now=now + 2)
    assert pending is not None and state.mark_ready(pending, now=now + 2, for_execution=True)
    ready = state.claim_work("worker", 60, now=now + 3)
    assert ready is not None
    jobs, results, _models = _central_corpus(
        task,
        revision,
        specification_digest=ready.item.specification_digest.value,
    )
    results[-1]["accepted"] = False
    results[-1]["decisions"] = {"report": "rejected"}
    _rehash_result(results[-1])
    assert state.complete_execution(
        ready,
        succeeded=False,
        summary="rejected",
        report_markdown="",
        provenance=central_protocol.terminal_provenance(jobs, results),
        now=now + 3,
    )
    if delivered_at is not None:
        notification = state.pending_execution_notification()
        assert notification is not None
        assert state.mark_execution_notification_delivered(notification, now=delivered_at)
    return ready.item.specification_digest.value


def _complete_receipt(
    engine: RetentionFilesystem,
    prepared: PreparedTombstone,
    *,
    now: int,
    specification_digest: str = "f" * 64,
) -> None:
    _seed_targets(
        engine,
        prepared,
        terminal="rejected",
        specification_digest=specification_digest,
    )
    engine.commit(prepared, committed_at=now)
    engine.agent_consume(prepared.tombstone_id, acknowledged_at=now, now=now)
    engine.controller_consume_ack(prepared.tombstone_id)


@_POSIX_ONLY
def test_lab_two_owner_retention_removes_exact_jobs_results_workspaces_and_dispatch(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    task = "moodle-task-v1:" + "6" * 64
    revision = "moodle-assignment-v1:" + "7" * 64
    jobs, results, models, dispatch = _lab_corpus(task, revision)
    job_ids = tuple(cast(str, job["jobId"]) for job in jobs)
    dispatch_id = cast(str, dispatch["executionKey"])
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.HYBRID,
        "cleaned",
        True,
        1,
        2,
        2,
        3,
        job_ids,
        None,
        "lab",
        job_ids,
        dispatch_id,
        lab_protocol.canonical_digest(dispatch),
        tuple(lab_protocol.canonical_digest(result) for result in results),
    )
    prepared = plan_retention((record,), now=3, limit=1)[0]
    assert prepared.execution_family == "lab"
    assert prepared.as_json() == decode_committed(
        CommittedTombstone(prepared, 3).as_json()
    ).prepared.as_json()
    _seed_lab_targets(engine, jobs, results, models, dispatch)
    engine.commit(prepared, committed_at=3)

    engine.agent_consume(prepared.tombstone_id, acknowledged_at=4, now=4)
    assert all(not (engine.roots.agent_workspaces / job_id).exists() for job_id in job_ids)
    assert all(not (engine.roots.agent_results / f"{job_id}.json").exists() for job_id in job_ids)
    assert all((engine.roots.shared_jobs / job_id).exists() for job_id in job_ids)
    assert (engine.roots.shared_jobs / "dispatches" / f"{dispatch_id}.json").exists()

    engine.controller_consume_ack(prepared.tombstone_id)
    assert all(not (engine.roots.shared_jobs / job_id).exists() for job_id in job_ids)
    assert not (engine.roots.shared_jobs / "dispatches" / f"{dispatch_id}.json").exists()
    assert engine.is_completed(prepared)
    assert engine.is_completed(prepared)


@_POSIX_ONLY
@pytest.mark.parametrize(
    "poison",
    [
        "dispatch",
        "dispatch-missing",
        "job-context",
        "result",
        "result-substitution",
        "workspace",
        "input",
    ],
)
def test_lab_retention_hostile_preflight_publishes_nothing(
    tmp_path: Path, poison: str
) -> None:
    engine = _engine(tmp_path)
    task = "moodle-task-v1:" + "8" * 64
    revision = "moodle-assignment-v1:" + "9" * 64
    jobs, results, models, dispatch = _lab_corpus(task, revision)
    job_ids = tuple(cast(str, job["jobId"]) for job in jobs)
    dispatch_id = cast(str, dispatch["executionKey"])
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.HYBRID,
        "cleaned",
        True,
        1,
        2,
        2,
        3,
        job_ids,
        None,
        "lab",
        job_ids,
        dispatch_id,
        lab_protocol.canonical_digest(dispatch),
        tuple(lab_protocol.canonical_digest(result) for result in results),
    )
    prepared = plan_retention((record,), now=3, limit=1)[0]
    _seed_lab_targets(engine, jobs, results, models, dispatch)
    engine.commit(prepared, committed_at=3)
    dispatch_path = engine.roots.shared_jobs / "dispatches" / f"{dispatch_id}.json"
    if poison == "dispatch":
        value = _read_canonical(dispatch_path)
        value["commandsDigest"] = "0" * 64
        _write_canonical(dispatch_path, value)
    elif poison == "dispatch-missing":
        dispatch_path.unlink()
    elif poison == "job-context":
        path = engine.roots.shared_jobs / job_ids[1] / "job.json"
        value = _read_canonical(path)
        cast(dict[str, object], value["context"])["planDigest"] = "0" * 64
        _write_canonical(path, value)
    elif poison == "result":
        path = engine.roots.agent_results / f"{job_ids[0]}.json"
        value = _read_canonical(path)
        value["summary"] = "tampered"
        _write_canonical(path, value)
    elif poison == "result-substitution":
        path = engine.roots.agent_results / f"{job_ids[1]}.json"
        value = _read_canonical(path)
        value["summary"] = "different but valid"
        _write_canonical(path, value)
        model = engine.roots.agent_workspaces / job_ids[1] / "last-message.json"
        model_value = _read_canonical(model)
        model_value["summary"] = "different but valid"
        _write_canonical(model, model_value)
    elif poison == "workspace":
        (engine.roots.agent_workspaces / job_ids[0] / "extra").write_bytes(b"x")
    else:
        (engine.roots.agent_workspaces / job_ids[0] / "inputs/0000-input.txt").write_bytes(
            b"wrong\n"
        )

    with pytest.raises(RetentionFilesystemError):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=4, now=4)
    assert not (engine._intents / f"{prepared.tombstone_id}.json").exists()
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
    assert not any(engine._agent_barriers.iterdir())
    assert all(
        (engine.roots.agent_workspaces / job_id).exists() for job_id in prepared.job_ids
    )
    assert all(
        (engine.roots.agent_results / f"{job_id}.json").exists()
        for job_id in prepared.job_ids
    )


@_POSIX_ONLY
def test_lab_dispatch_unknown_with_absent_report_job_reclaims_exact_barrier_set(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    task = "moodle-task-v1:" + "a" * 64
    revision = "moodle-assignment-v1:" + "c" * 64
    jobs, results, models, dispatch = _lab_corpus(task, revision)
    plan_id = cast(str, jobs[0]["jobId"])
    report_id = cast(str, jobs[1]["jobId"])
    dispatch["state"] = "intent"
    dispatch.pop("commandId")
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.HYBRID,
        "cleaned",
        False,
        1,
        None,
        2,
        None,
        (plan_id,),
        None,
        "lab",
        (plan_id, report_id),
        report_id,
        lab_protocol.canonical_digest(dispatch),
        (lab_protocol.canonical_digest(results[0]),),
    )
    prepared = plan_retention((record,), now=2, limit=1)[0]
    _seed_lab_targets(engine, jobs[:1], results[:1], models[:1], dispatch)
    engine.commit(prepared, committed_at=2)

    engine.agent_consume(prepared.tombstone_id, acknowledged_at=3, now=3)
    assert not (engine.roots.agent_workspaces / plan_id).exists()
    assert not (engine.roots.agent_results / f"{plan_id}.json").exists()
    assert not (engine.roots.agent_workspaces / report_id).exists()
    assert {path.stem for path in engine._agent_barriers.iterdir()} == {
        plan_id,
        report_id,
    }
    engine.controller_consume_ack(prepared.tombstone_id)
    assert not (engine.roots.shared_jobs / plan_id).exists()
    assert not (engine.roots.shared_jobs / "dispatches" / f"{report_id}.json").exists()
    assert engine.is_completed(prepared)


@_POSIX_ONLY
def test_lab_controller_dispatch_delete_response_loss_replays_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    task = "moodle-task-v1:" + "d" * 64
    revision = "moodle-assignment-v1:" + "e" * 64
    jobs, results, models, dispatch = _lab_corpus(task, revision)
    job_ids = tuple(cast(str, job["jobId"]) for job in jobs)
    dispatch_id = cast(str, dispatch["executionKey"])
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.HYBRID,
        "cleaned",
        True,
        1,
        2,
        2,
        3,
        job_ids,
        None,
        "lab",
        job_ids,
        dispatch_id,
        lab_protocol.canonical_digest(dispatch),
        tuple(lab_protocol.canonical_digest(result) for result in results),
    )
    prepared = plan_retention((record,), now=3, limit=1)[0]
    _seed_lab_targets(engine, jobs, results, models, dispatch)
    engine.commit(prepared, committed_at=3)
    ack = engine.agent_consume(prepared.tombstone_id, acknowledged_at=4, now=4)

    def crash(phase: str) -> None:
        if phase == "delete-dispatch":
            raise RetentionFilesystemError("lost dispatch delete response")

    monkeypatch.setattr(retention_fs, "_controller_delete_fault", crash)
    with pytest.raises(RetentionFilesystemError, match="lost dispatch"):
        engine.controller_consume_ack(prepared.tombstone_id)
    assert not (engine.roots.shared_jobs / "dispatches" / f"{dispatch_id}.json").exists()
    assert (engine._deleting / f"{prepared.tombstone_id}.json").exists()
    monkeypatch.setattr(retention_fs, "_controller_delete_fault", lambda _phase: None)
    assert engine.controller_consume_ack(prepared.tombstone_id) == ack
    assert engine.is_completed(prepared)


def _terminal_prepared(
    prefix: int, *, bundle: bool = False, terminal: str | None = None
) -> PreparedTombstone:
    task = "moodle-task-v1:" + "a" * 64
    revision = "moodle-assignment-v1:" + "b" * 64
    jobs, results, _models = _central_corpus(task, revision)
    jobs = jobs[:prefix]
    results = results[:prefix]
    models = _models[:prefix]
    _terminalize_corpus(jobs, results, models, terminal)
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.CENTRAL,
        "cleaned",
        False,
        1,
        1,
        1,
        1,
        tuple(cast(str, job["jobId"]) for job in jobs),
        cast(str, results[1]["artifactBundleDigest"]) if bundle else None,
        result_digests=tuple(
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
            for result in results
        ),
    )
    return plan_retention((record,), now=1, limit=1)[0]


def _scan_prepared(index: int) -> PreparedTombstone:
    task = f"moodle-task-v1:{index:064x}"
    revision = f"moodle-assignment-v1:{index + 100:064x}"
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.CENTRAL,
        "cleaned",
        False,
        1,
        None,
        1,
        None,
        (f"{index + 200:064x}",),
        None,
        result_digests=(f"{index + 300:064x}",),
    )
    return plan_retention((record,), now=1, limit=1)[0]


def _publish_scanned_committed(engine: RetentionFilesystem, index: int) -> CommittedTombstone:
    committed = CommittedTombstone(_scan_prepared(index), 1)
    retention_fs._publish_immutable(
        engine._committed / f"{committed.prepared.tombstone_id}.json",
        committed.as_json(),
        engine._shared_controller_file,
    )
    return committed


def _read_canonical(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_bytes()))


def _write_canonical(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(central_protocol.canonical_json(value))


def _rehash_job(job: dict[str, object]) -> None:
    job["jobId"] = central_protocol.canonical_digest(
        {key: value for key, value in job.items() if key != "jobId"}
    )


def _rehash_result(result: dict[str, object]) -> None:
    field = {
        "central_planner": "plannerResultDigest",
        "central_executor": "executorResultDigest",
        "central_reviewer": "reviewerResultDigest",
    }[cast(str, result["role"])]
    result[field] = central_protocol.canonical_digest(
        {key: value for key, value in result.items() if key != field}
    )


def _assert_scratch_unpublished(engine: RetentionFilesystem, prepared: PreparedTombstone) -> None:
    assert not (engine._intents / f"{prepared.tombstone_id}.json").exists()
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
    assert not any(engine._agent_barriers.glob("*.json"))
    assert all((engine.roots.shared_jobs / job_id).exists() for job_id in prepared.job_ids)
    assert all(
        (engine.roots.agent_workspaces / job_id).exists() for job_id in prepared.job_ids
    )
    assert all(
        (engine.roots.agent_results / f"{job_id}.json").exists()
        for job_id in prepared.job_ids
    )


@_POSIX_ONLY
def test_bounded_metadata_scan_accepts_exact_logical_limit_in_order(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    records = [_publish_scanned_committed(engine, index) for index in (3, 1)]

    scanned = retention_fs._scan_metadata_directory(
        engine._committed, engine._shared_controller_file, decode_committed, 2
    )

    committed = cast(tuple[CommittedTombstone, ...], scanned)
    assert [item.prepared.tombstone_id for item in committed] == sorted(
        item.prepared.tombstone_id for item in records
    )


@_POSIX_ONLY
def test_bounded_metadata_scan_rejects_plus_one_logical_record(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    for index in range(3):
        _publish_scanned_committed(engine, index)

    with pytest.raises(RetentionFilesystemError, match="exceeds its limit"):
        retention_fs._scan_metadata_directory(
            engine._committed, engine._shared_controller_file, decode_committed, 2
        )


@_POSIX_ONLY
def test_bounded_metadata_scan_accepts_stage_final_pairs_at_physical_limit(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    records = [_publish_scanned_committed(engine, index) for index in range(2)]
    for committed in records:
        final = engine._committed / f"{committed.prepared.tombstone_id}.json"
        stage = final.with_name(
            f".retention-stage-{final.name}-{hashlib.sha256(committed.as_json()).hexdigest()}"
        )
        os.link(final, stage)

    scanned = retention_fs._scan_metadata_directory(
        engine._committed, engine._shared_controller_file, decode_committed, 2
    )
    assert len(scanned) == 2


@_POSIX_ONLY
def test_bounded_metadata_scan_stops_before_decoding_physical_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    records = []
    for index in range(2):
        records.append(_publish_scanned_committed(engine, index))
    for committed in records:
        final = engine._committed / f"{committed.prepared.tombstone_id}.json"
        os.link(
            final,
            final.with_name(
                f".retention-stage-{final.name}-{hashlib.sha256(committed.as_json()).hexdigest()}"
            ),
        )
    (engine._committed / ("z" * 64)).write_bytes(b"poison")
    reads = 0
    original = retention_fs._decode_scanned_metadata

    def counted(raw: bytes, decoder: object) -> object:
        nonlocal reads
        reads += 1
        return original(raw, decoder)  # type: ignore[arg-type]

    monkeypatch.setattr(retention_fs, "_decode_scanned_metadata", counted)
    with pytest.raises(RetentionFilesystemError, match="exceeds its limit"):
        retention_fs._scan_metadata_directory(
            engine._committed, engine._shared_controller_file, decode_committed, 2
        )
    assert reads == 0


@_POSIX_ONLY
def test_bounded_metadata_scan_rejects_conflicting_stage_and_final_for_one_id(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    committed = _publish_scanned_committed(engine, 1)
    final = engine._committed / f"{committed.prepared.tombstone_id}.json"
    stage = final.with_name(
        f".retention-stage-{final.name}-{hashlib.sha256(committed.as_json()).hexdigest()}"
    )
    stage.write_bytes(committed.as_json())
    stage.chmod(0o640)

    with pytest.raises(RetentionFilesystemError, match="staging"):
        retention_fs._scan_metadata_directory(
            engine._committed, engine._shared_controller_file, decode_committed, 2
        )


@_POSIX_ONLY
def test_runtime_reconciles_completed_prefix_in_one_batch_then_commits_later_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    engine = _engine(tmp_path)
    specifications: dict[str, str] = {}
    for now, marker in enumerate(("a", "b", "c", "d"), start=1):
        specification = _complete_terminal_state(tmp_path, state, marker, now=now)
        event_id = _event_id(
            "moodle-task-v1:" + marker * 64,
            "moodle-assignment-v1:" + marker * 64,
        )
        specifications[event_id] = specification
    old_plans = plan_retention(state.retention_records(100, 1, 1, 3), now=100, limit=3)
    assert len(old_plans) == 3
    for prepared in old_plans:
        _complete_receipt(
            engine,
            prepared,
            now=100,
            specification_digest=specifications[prepared.event_id],
        )
    calls: list[tuple[PreparedTombstone, ...]] = []
    original = ApprovalState.record_retention_completions

    def record(
        current: ApprovalState, completed: tuple[PreparedTombstone, ...], *, completed_at: int
    ) -> None:
        calls.append(completed)
        original(current, completed, completed_at=completed_at)

    monkeypatch.setattr(ApprovalState, "record_retention_completions", record)
    coordinator = ControllerRetentionCoordinator(
        state, engine, scratch_ttl=1, evidence_ttl=1, candidate_limit=3, clock=lambda: 100
    )

    assert coordinator.cycle() == "reconciled"
    assert calls == [tuple(old_plans)]
    later = plan_retention(state.retention_records(100, 1, 1, 3), now=100, limit=1)[0]
    assert not engine.is_completed(later)

    restarted = ApprovalState(state.path)
    restarted_engine = RetentionFilesystem(engine.roots)
    restarted_coordinator = ControllerRetentionCoordinator(
        restarted,
        restarted_engine,
        scratch_ttl=1,
        evidence_ttl=1,
        candidate_limit=3,
        clock=lambda: 100,
    )
    assert restarted_coordinator.cycle() == "reconciliation-advanced"
    assert restarted_coordinator.cycle() == "reconciliation-wrapped"
    assert restarted_coordinator.cycle() == "committed"
    assert restarted_engine.actionable_committed(limit=1) == (later.tombstone_id,)


@_POSIX_ONLY
def test_runtime_defers_bundle_until_due_then_reconciles_controller_receipt(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    engine = _engine(tmp_path)
    specification = _complete_terminal_state(
        tmp_path, state, "e", now=1, delivered_at=4
    )
    scratch = plan_retention(state.retention_records(5, 1, 10, 1), now=5, limit=1)[0]
    assert scratch.target_phase == "scratch"
    _complete_receipt(
        engine, scratch, now=5, specification_digest=specification
    )
    coordinator = ControllerRetentionCoordinator(
        state, engine, scratch_ttl=1, evidence_ttl=10, candidate_limit=2, clock=lambda: 5
    )

    assert coordinator.cycle() == "reconciled"
    assert state.retention_records(13, 1, 10, 1) == ()
    evidence = plan_retention(
        state.retention_records(14, 1, 10, 1),
        now=14,
        limit=1,
        completed=engine.is_completed,
    )[0]
    assert evidence.target_phase == "evidence"
    _seed_targets(engine, evidence)
    assert coordinator.cycle(now=14) == "reconciliation-advanced"
    assert coordinator.cycle(now=14) == "reconciliation-wrapped"
    assert coordinator.cycle(now=14) == "committed"
    engine.agent_consume(evidence.tombstone_id, acknowledged_at=14, now=14)
    assert coordinator.cycle(now=14) == "reconciliation-advanced"
    assert coordinator.cycle(now=14) == "reconciliation-wrapped"
    assert coordinator.cycle(now=14) == "ack-consumed"
    assert engine.is_completed(evidence)
    assert coordinator.cycle(now=14) == "reconciliation-advanced"
    assert coordinator.cycle(now=14) == "reconciliation-wrapped"
    assert coordinator.cycle(now=14) == "reconciled"
    assert state.retention_records(14, 1, 10, 1) == ()


@_POSIX_ONLY
def test_runtime_refuses_conflicting_sqlite_completion_receipt_before_later_commit(
    tmp_path: Path,
) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    engine = _engine(tmp_path)
    first_specification = _complete_terminal_state(tmp_path, state, "a", now=1)
    _complete_terminal_state(tmp_path, state, "b", now=2)
    plans = plan_retention(state.retention_records(100, 1, 1, 2), now=100, limit=2)
    stale, later = plans
    _complete_receipt(
        engine,
        stale,
        now=100,
        specification_digest=first_specification,
    )
    state.record_retention_completions((replace(stale, tombstone_id="f" * 64),), completed_at=100)
    coordinator = ControllerRetentionCoordinator(
        state, engine, scratch_ttl=1, evidence_ttl=1, candidate_limit=2, clock=lambda: 100
    )

    with pytest.raises(RetentionFilesystemError, match="completion receipt conflicts"):
        coordinator.cycle()

    assert not (engine._committed / f"{later.tombstone_id}.json").exists()


@_POSIX_ONLY
@pytest.mark.parametrize(
    "phase", ["create", "write", "fsync", "link", "post-link", "dir-fsync", "unlink"]
)
def test_immutable_publication_fault_replay_is_no_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    uid, gid = _posix_identity()
    parent = tmp_path / "metadata"
    parent.mkdir(mode=0o700)
    target = parent / ("a" * 64 + ".json")
    raw = b'{"canonical":true}'
    node = retention_fs._ProtocolNode(uid, gid, 0o600, False)

    def crash(actual: str) -> None:
        if actual == phase:
            raise RetentionFilesystemError(f"injected {phase}")

    monkeypatch.setattr(retention_fs, "_publication_fault", crash)
    with pytest.raises(RetentionFilesystemError, match=f"injected {phase}"):
        retention_fs._publish_immutable(target, raw, node)
    monkeypatch.setattr(retention_fs, "_publication_fault", lambda _phase: None)
    retention_fs._publish_immutable(target, raw, node)
    assert target.read_bytes() == raw
    assert not list(parent.glob(".retention-stage-*"))


@_POSIX_ONLY
def test_immutable_publication_rejects_conflicts_and_unknown_staging(tmp_path: Path) -> None:
    uid, gid = _posix_identity()
    parent = tmp_path / "metadata"
    parent.mkdir(mode=0o700)
    target = parent / ("a" * 64 + ".json")
    node = retention_fs._ProtocolNode(uid, gid, 0o600, False)
    target.write_bytes(b"other")
    target.chmod(0o600)
    with pytest.raises(RetentionFilesystemError, match="conflicts"):
        retention_fs._publish_immutable(target, b"expected", node)
    target.unlink()
    poison = parent / ".retention-stage-unknown"
    poison.write_bytes(b"poison")
    poison.chmod(0o600)
    with pytest.raises(RetentionFilesystemError, match="staging"):
        retention_fs._publish_immutable(target, b"expected", node)


@_POSIX_ONLY
def test_new_chain_soft_node_closure_publishes_nothing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.storage_policy = _quota_policy(controller_soft=StorageLimit(1 << 30, 4))
    prepared = _prepared()

    with pytest.raises(RetentionCapacityError, match="admission is closed"):
        engine.prepare(prepared)

    assert not (engine._prepared / f"{prepared.tombstone_id}.json").exists()


@_POSIX_ONLY
def test_hard_node_plus_one_refuses_before_prepared_publication(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    # controller/retention plus its three canonical children are four nodes;
    # crash-safe publication peaks at two additional names (stage and final).
    engine.storage_policy = _quota_policy(
        controller_hard=StorageLimit(1 << 30, 5),
        controller_soft=StorageLimit(1 << 30, 100),
    )
    prepared = _prepared()

    with pytest.raises(RetentionCapacityError, match="capacity is closed"):
        engine.prepare(prepared)

    assert not (engine._prepared / f"{prepared.tombstone_id}.json").exists()


@_POSIX_ONLY
def test_hard_byte_boundary_admits_exactly_and_refuses_one_byte_less(tmp_path: Path) -> None:
    prepared = _prepared()

    def setup(path: Path) -> tuple[RetentionFilesystem, int]:
        engine = _engine(path)
        retention_fs._ensure_dirs(
            engine.roots.controller_private,
            engine._controller_anchor_dir,
            (engine._prepared, engine._controller_private_dir),
            (engine._completed, engine._controller_private_dir),
            (engine._deleting, engine._controller_private_dir),
        )
        root = engine.roots.controller_private / "retention"
        publication = engine._prepared / f"{prepared.tombstone_id}.json"
        current = measure_tree_no_follow(root, expected_uid=engine.ownership.controller_uid)
        demand = retention_fs._publication_peak_demand(
            root, publication, prepared.as_json(), engine._controller_private_file
        )
        return engine, current.allocated_bytes + demand.allocated_bytes

    admitted, exact = setup(tmp_path / "exact")
    admitted.storage_policy = _quota_policy(
        controller_hard=StorageLimit(exact, 100_000),
        controller_soft=StorageLimit(1 << 30, 100_000),
    )
    admitted.prepare(prepared)
    assert (admitted._prepared / f"{prepared.tombstone_id}.json").exists()

    refused, exact = setup(tmp_path / "plus-one")
    refused.storage_policy = _quota_policy(
        controller_hard=StorageLimit(exact - 1, 100_000),
        controller_soft=StorageLimit(1 << 30, 100_000),
    )
    with pytest.raises(RetentionCapacityError, match="capacity is closed"):
        refused.prepare(prepared)
    assert not (refused._prepared / f"{prepared.tombstone_id}.json").exists()


@_POSIX_ONLY
def test_chain_admission_serializes_one_soft_capacity_slot(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    retention_fs._ensure_dirs(
        engine.roots.controller_private,
        engine._controller_anchor_dir,
        (engine._prepared, engine._controller_private_dir),
        (engine._completed, engine._controller_private_dir),
        (engine._deleting, engine._controller_private_dir),
    )
    engine.storage_policy = _quota_policy(
        controller_soft=StorageLimit(1 << 30, 5),
    )
    first, second = _prepared_phases()
    start = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def prepare(value: PreparedTombstone) -> None:
        start.wait()
        try:
            engine.prepare(value)
        except RetentionCapacityError:
            outcomes.append(("closed", value.tombstone_id))
        else:
            outcomes.append(("prepared", value.tombstone_id))

    threads = [threading.Thread(target=prepare, args=(value,)) for value in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    assert sorted(state for state, _identifier in outcomes) == ["closed", "prepared"]
    published = {identifier for state, identifier in outcomes if state == "prepared"}
    assert {
        path.stem for path in engine._prepared.glob("*.json")
    } == published


@_POSIX_ONLY
def test_terminal_acknowledgements_do_not_starve_one_active_ack(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    engine.commit(prepared, committed_at=1)
    for value in range(1_025):
        tombstone_id = f"{value:064x}"
        raw = AgentRetentionAck(tombstone_id, 1, 2).as_json()
        for directory, node in (
            (engine._acks, engine._shared_agent_file),
            (engine._completed, engine._controller_private_file),
        ):
            path = directory / f"{tombstone_id}.json"
            path.write_bytes(raw)
            path.chmod(node.mode)

    active = AgentRetentionAck(prepared.tombstone_id, 1, 2)
    retention_fs._publish_immutable(
        engine._acks / f"{prepared.tombstone_id}.json",
        active.as_json(),
        engine._shared_agent_file,
    )

    assert engine.actionable_acks(limit=1, scan_limit=1) == (prepared.tombstone_id,)


@_POSIX_ONLY
@pytest.mark.parametrize("discovery", ["agent", "controller"])
def test_discovery_finalizes_selected_linked_committed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, discovery: str
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    committed = CommittedTombstone(prepared, 1)
    target = engine._committed / f"{prepared.tombstone_id}.json"

    def crash(phase: str) -> None:
        if phase == "post-link":
            raise RetentionFilesystemError("injected linked committed stage")

    monkeypatch.setattr(retention_fs, "_publication_fault", crash)
    with pytest.raises(RetentionFilesystemError, match="linked committed"):
        retention_fs._publish_immutable(target, committed.as_json(), engine._shared_controller_file)
    monkeypatch.setattr(retention_fs, "_publication_fault", lambda _phase: None)
    if discovery == "agent":
        assert engine.actionable_committed(limit=1, scan_limit=1) == (prepared.tombstone_id,)
    else:
        retention_fs._publish_immutable(
            engine._acks / f"{prepared.tombstone_id}.json",
            AgentRetentionAck(prepared.tombstone_id, 1, 2).as_json(),
            engine._shared_agent_file,
        )
        assert engine.actionable_acks(limit=1, scan_limit=1) == (prepared.tombstone_id,)
    assert target.read_bytes() == committed.as_json()
    assert not list(engine._committed.glob(".retention-stage-*"))


@_POSIX_ONLY
def test_protocol_reclaims_owner_split_targets_and_keeps_terminal_receipt(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    committed = engine.commit(prepared, committed_at=1)

    ack = engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)

    assert ack.committed_at == committed.committed_at
    assert all(not (engine.roots.agent_workspaces / job).exists() for job in prepared.job_ids)
    assert all(
        not (engine.roots.agent_results / f"{job}.json").exists() for job in prepared.job_ids
    )
    engine.controller_consume_ack(prepared.tombstone_id)
    assert all(not (engine.roots.shared_jobs / job).exists() for job in prepared.job_ids)
    receipt = (
        engine.roots.controller_private
        / "retention"
        / "completed"
        / f"{prepared.tombstone_id}.json"
    )
    assert receipt.exists()
    assert engine.is_completed(prepared)
    engine.prepare(prepared)
    replay = engine.commit(prepared, committed_at=1)
    assert replay.prepared == prepared
    assert not (engine._prepared / f"{prepared.tombstone_id}.json").exists()
    assert not (engine._committed / f"{prepared.tombstone_id}.json").exists()


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("prefix", "terminal", "bundle"),
    [
        (1, "failed", False),
        (2, "failed", False),
        (3, "failed", False),
        (3, "rejected", True),
    ],
    ids=("planner-failed", "executor-failed", "reviewer-failed", "reviewer-rejected"),
)
def test_terminal_v3_prefixes_reclaim_exact_targets_and_preserve_neighbors(
    tmp_path: Path, prefix: int, terminal: str, bundle: bool
) -> None:
    engine = _engine(tmp_path)
    prepared = _terminal_prepared(prefix, bundle=bundle, terminal=terminal)
    _seed_targets(engine, prepared, terminal=terminal)
    neighbor = "f" * 64
    (engine.roots.shared_jobs / neighbor).mkdir()
    (engine.roots.agent_workspaces / neighbor).mkdir()
    (engine.roots.agent_results / f"{neighbor}.json").write_bytes(b"{}")

    engine.prepare(prepared)
    engine.commit(prepared, committed_at=1)
    ack = engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert ack.tombstone_id == prepared.tombstone_id
    assert all(not (engine.roots.agent_workspaces / job).exists() for job in prepared.job_ids)
    assert all(
        not (engine.roots.agent_results / f"{job}.json").exists() for job in prepared.job_ids
    )
    engine.controller_consume_ack(prepared.tombstone_id)

    assert all(not (engine.roots.shared_jobs / job).exists() for job in prepared.job_ids)
    assert (engine.roots.shared_jobs / neighbor).is_dir()
    assert (engine.roots.agent_workspaces / neighbor).is_dir()
    assert (engine.roots.agent_results / f"{neighbor}.json").is_file()
    if bundle:
        bundle_digest = hashlib.sha256(b"bundle").hexdigest()
        assert (engine.roots.agent_bundles / f"{bundle_digest}.zip").exists()
    assert engine.is_completed(prepared)


@_POSIX_ONLY
def test_legacy_prefixed_plan_with_wrapper_failure_remains_reclaimable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = "moodle-task-v1:" + "a" * 64
    revision = "moodle-assignment-v1:" + "b" * 64
    jobs, results, models = _central_corpus(task, revision)
    plan = cast(dict[str, object], results[0]["plan"])
    plan["expectedArtifacts"] = ["outputs/report.md"]
    results[0]["planDigest"] = central_protocol.canonical_digest(plan)
    _rehash_result(results[0])
    dependencies = cast(dict[str, str], jobs[1]["dependencies"])
    dependencies["planDigest"] = cast(str, results[0]["planDigest"])
    dependencies["plannerResultDigest"] = cast(str, results[0]["plannerResultDigest"])
    _rehash_job(jobs[1])
    failure: dict[str, object] = {
        "kind": central_protocol.CENTRAL_RESULT_KIND,
        "jobId": jobs[1]["jobId"],
        "role": "central_executor",
        "succeeded": False,
        "summary": "Agent workspace is unsafe",
        "reportMarkdown": "",
    }
    failure["executorResultDigest"] = central_protocol.canonical_digest(failure)
    jobs, results, models = jobs[:2], [results[0], failure], models[:2]
    job_ids = tuple(cast(str, job["jobId"]) for job in jobs)
    result_digests = tuple(
        cast(str, result[field])
        for result, field in zip(
            results,
            ("plannerResultDigest", "executorResultDigest"),
            strict=True,
        )
    )
    record = RetentionRecord(
        _event_id(task, revision),
        task,
        revision,
        ExecutionMode.CENTRAL,
        "cleaned",
        False,
        1,
        1,
        1,
        1,
        job_ids,
        None,
        result_digests=result_digests,
    )
    prepared = plan_retention((record,), now=1, limit=1)[0]
    monkeypatch.setitem(
        _seed_targets.__globals__,
        "_central_corpus",
        lambda _task, _revision, *, specification_digest="f" * 64: (
            jobs,
            results,
            models,
        ),
    )
    engine = _engine(tmp_path)
    _seed_targets(engine, prepared)

    engine.commit(prepared, committed_at=1)
    engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    engine.controller_consume_ack(prepared.tombstone_id)

    assert engine.is_completed(prepared)
    assert all(not (engine.roots.shared_jobs / job_id).exists() for job_id in job_ids)
    assert all(not (engine.roots.agent_workspaces / job_id).exists() for job_id in job_ids)


@_POSIX_ONLY
@pytest.mark.parametrize(("prefix", "bundle"), [(1, False), (2, True)])
def test_budget_prefixes_reclaim_exact_scratch_and_preserve_executor_bundle(
    tmp_path: Path, prefix: int, bundle: bool
) -> None:
    engine = _engine(tmp_path)
    scratch = _terminal_prepared(prefix, bundle=bundle)
    _seed_targets(engine, scratch)
    engine.commit(scratch, committed_at=1)
    engine.agent_consume(scratch.tombstone_id, acknowledged_at=2, now=2)
    engine.controller_consume_ack(scratch.tombstone_id)
    assert engine.is_completed(scratch)
    if not bundle:
        return
    evidence = PreparedTombstone(
        hashlib.sha256(
            central_protocol.canonical_json(
                {
                    "eventId": scratch.event_id,
                    "taskKey": scratch.task_key,
                    "revisionDigest": scratch.revision_digest,
                    "targetPhase": "evidence",
                    "eligibleAt": 1,
                    "jobIds": [],
                    "bundleDigest": hashlib.sha256(b"bundle").hexdigest(),
                    "resultDigests": list(scratch.result_digests),
                }
            )
        ).hexdigest(),
        scratch.event_id,
        scratch.task_key,
        scratch.revision_digest,
        "evidence",
        1,
        (),
        hashlib.sha256(b"bundle").hexdigest(),
        result_digests=scratch.result_digests,
    )
    engine.commit(evidence, committed_at=2)
    engine.agent_consume(evidence.tombstone_id, acknowledged_at=2, now=2)
    engine.controller_consume_ack(evidence.tombstone_id)
    assert engine.is_completed(evidence)


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("phase", "deleted"),
    [
        ("after-marker", 0),
        ("delete-1", 1),
        ("delete-2", 2),
        ("delete-3", 3),
        ("after-receipt", 3),
    ],
)
def test_terminal_v3_ack_response_loss_and_controller_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, deleted: int
) -> None:
    engine = _engine(tmp_path)
    prepared = _terminal_prepared(3, terminal="failed")
    _seed_targets(engine, prepared, terminal="failed")
    engine.commit(prepared, committed_at=1)
    first_ack = engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    # The first acknowledgement may have been durably delivered while its response was lost.
    assert engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2) == first_ack

    def crash(actual: str) -> None:
        if actual == phase:
            raise RetentionFilesystemError(f"injected {phase}")

    monkeypatch.setattr(retention_fs, "_controller_delete_fault", crash)
    with pytest.raises(RetentionFilesystemError, match=f"injected {phase}"):
        engine.controller_consume_ack(prepared.tombstone_id)
    assert sum(
        not (engine.roots.shared_jobs / job_id).exists() for job_id in prepared.job_ids
    ) == deleted
    assert (engine._deleting / f"{prepared.tombstone_id}.json").exists()
    monkeypatch.setattr(retention_fs, "_controller_delete_fault", lambda _phase: None)
    assert engine.controller_consume_ack(prepared.tombstone_id) == first_ack
    assert engine.is_completed(prepared)
    assert not (engine._deleting / f"{prepared.tombstone_id}.json").exists()


@_POSIX_ONLY
@pytest.mark.parametrize(
    "poison",
    [
        "cross-identity",
        "missing-job",
        "extra-job",
        "reordered-job",
        "terminal-digest",
        "reviewer-decision",
        "bundle-content",
        "committed-conflict",
    ],
)
def test_terminal_v3_hostile_preflight_publishes_nothing(
    tmp_path: Path, poison: str
) -> None:
    engine = _engine(tmp_path)
    prepared = _terminal_prepared(3, bundle=True)
    _seed_targets(engine, prepared, terminal="rejected")
    engine.commit(prepared, committed_at=1)
    first, second, third = prepared.job_ids
    if poison == "cross-identity":
        path = engine.roots.shared_jobs / first / "job.json"
        value = _read_canonical(path)
        value["eventId"] = "moodle-notification-event-v1:" + "f" * 64
        _write_canonical(path, value)
    elif poison == "missing-job":
        shutil.rmtree(engine.roots.shared_jobs / first)
    elif poison == "extra-job":
        path = engine._committed / f"{prepared.tombstone_id}.json"
        value = _read_canonical(path)
        value["jobIds"] = [*prepared.job_ids, "e" * 64]
        _write_canonical(path, value)
    elif poison == "reordered-job":
        first_path = engine.roots.shared_jobs / first / "job.json"
        first_path.write_bytes((engine.roots.shared_jobs / second / "job.json").read_bytes())
    elif poison == "terminal-digest":
        path = engine.roots.agent_results / f"{third}.json"
        value = _read_canonical(path)
        value["reviewerResultDigest"] = "0" * 64
        _write_canonical(path, value)
    elif poison == "reviewer-decision":
        path = engine.roots.agent_results / f"{third}.json"
        value = _read_canonical(path)
        value["decisions"] = {"report": "accepted"}
        _write_canonical(path, value)
    elif poison == "bundle-content":
        bundle = engine.roots.agent_bundles / f"{hashlib.sha256(b'bundle').hexdigest()}.zip"
        bundle.write_bytes(b"tampered")
    else:
        path = engine._committed / f"{prepared.tombstone_id}.json"
        path.write_bytes(b"{}")
        path.chmod(0o640)

    with pytest.raises(RetentionFilesystemError):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert not (engine._intents / f"{prepared.tombstone_id}.json").exists()
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
    assert not any(engine._agent_barriers.glob("*.json"))
    assert all(
        (engine.roots.agent_workspaces / job_id).exists() for job_id in prepared.job_ids
    )
    assert all(
        (engine.roots.agent_results / f"{job_id}.json").exists()
        for job_id in prepared.job_ids
    )
    expected_jobs = len(prepared.job_ids) - (poison == "missing-job")
    surviving_jobs = sum(
        (engine.roots.shared_jobs / job_id).exists() for job_id in prepared.job_ids
    )
    assert surviving_jobs == expected_jobs


@_POSIX_ONLY
def test_scratch_then_evidence_complete_without_cross_phase_mutation(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    scratch, evidence = _prepared_phases()
    _seed_targets(engine, scratch)
    _seed_targets(engine, evidence)
    bundle = engine.roots.agent_bundles / f"{evidence.bundle_digest}.zip"

    engine.commit(scratch, committed_at=2)
    engine.agent_consume(scratch.tombstone_id, acknowledged_at=2, now=2)
    engine.controller_consume_ack(scratch.tombstone_id)
    controller_barriers = {path.name for path in engine._controller_barriers.iterdir()}
    agent_barriers = {path.name for path in engine._agent_barriers.iterdir()}
    assert bundle.exists()
    assert not any(
        path.name == f"{evidence.tombstone_id}.json" for path in engine._committed.iterdir()
    )
    untouched_job = engine.roots.shared_jobs / ("f" * 64)
    untouched_workspace = engine.roots.agent_workspaces / untouched_job.name
    untouched_result = engine.roots.agent_results / f"{untouched_job.name}.json"
    untouched_job.mkdir()
    untouched_workspace.mkdir()
    untouched_result.write_text("{}", encoding="utf-8")

    engine.commit(evidence, committed_at=2)
    engine.agent_consume(evidence.tombstone_id, acknowledged_at=2, now=2)
    engine.controller_consume_ack(evidence.tombstone_id)

    assert not bundle.exists()
    assert untouched_job.is_dir() and untouched_workspace.is_dir() and untouched_result.is_file()
    assert {path.name for path in engine._controller_barriers.iterdir()} == controller_barriers
    assert {path.name for path in engine._agent_barriers.iterdir()} == agent_barriers
    assert engine.is_completed(scratch) and engine.is_completed(evidence)


@_POSIX_ONLY
@pytest.mark.parametrize("poison", ["digest", "symlink", "mode", "owner", "gid"])
def test_evidence_rejects_unsafe_bundle_before_ack(tmp_path: Path, poison: str) -> None:
    ownership = RetentionOwnership(target_file_mode=0o600)
    installed = _engine(tmp_path)
    engine = RetentionFilesystem(installed.roots, ownership)
    evidence = _prepared_phases()[1]
    bundle = engine.roots.agent_bundles / f"{evidence.bundle_digest}.zip"
    bundle.write_bytes(b"bundle")
    bundle.chmod(0o640)
    if poison == "digest":
        bundle.write_bytes(b"wrong")
    elif poison == "symlink":
        bundle.unlink()
        outside = tmp_path / "outside.zip"
        outside.write_bytes(b"bundle")
        bundle.symlink_to(outside)
    elif poison == "mode":
        bundle.chmod(0o644)
    elif poison in {"owner", "gid"}:
        metadata = bundle.stat()
        chown = cast(Callable[[Path, int, int], None] | None, getattr(os, "chown", None))
        if chown is None:
            pytest.skip("changing ownership is unavailable")
        try:
            chown(
                bundle,
                metadata.st_uid + 1 if poison == "owner" else metadata.st_uid,
                metadata.st_gid if poison == "owner" else metadata.st_gid + 1,
            )
        except PermissionError:
            pytest.skip("changing ownership requires root")
    engine.commit(evidence, committed_at=2)

    with pytest.raises(RetentionFilesystemError):
        engine.agent_consume(evidence.tombstone_id, acknowledged_at=2, now=2)
    assert not (engine._acks / f"{evidence.tombstone_id}.json").exists()
    assert bundle.exists() or bundle.is_symlink()


@_POSIX_ONLY
def test_evidence_missing_bundle_is_only_a_retry_after_exact_intent(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    evidence = _prepared_phases()[1]
    engine.commit(evidence, committed_at=2)

    with pytest.raises(RetentionFilesystemError, match="bundle is missing"):
        engine.agent_consume(evidence.tombstone_id, acknowledged_at=2, now=2)
    assert not (engine._intents / f"{evidence.tombstone_id}.json").exists()
    assert not (engine._acks / f"{evidence.tombstone_id}.json").exists()

    bundle = engine.roots.agent_bundles / f"{evidence.bundle_digest}.zip"
    bundle.write_bytes(b"bundle")
    bundle.chmod(0o640)
    original = retention_fs._unlink_regular

    def fail_after_bundle_delete(path: Path) -> None:
        original(path)
        if path == bundle:
            raise RetentionFilesystemError("injected evidence cleanup failure")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(retention_fs, "_unlink_regular", fail_after_bundle_delete)
        with pytest.raises(RetentionFilesystemError, match="injected evidence cleanup"):
            engine.agent_consume(evidence.tombstone_id, acknowledged_at=2, now=2)
    assert (engine._intents / f"{evidence.tombstone_id}.json").exists()
    assert not bundle.exists()
    assert engine.agent_consume(evidence.tombstone_id, acknowledged_at=2, now=2).tombstone_id == (
        evidence.tombstone_id
    )


@_POSIX_ONLY
def test_retention_job_lock_matches_engine_shared_and_private_trees(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    with retention_job_lock(engine._shared, prepared.job_ids[0]):
        pass
    engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    with retention_job_lock(engine.roots.agent_private, prepared.job_ids[0]):
        pass
    (engine._controller_locks / f"{prepared.job_ids[0]}.lock").chmod(0o600)
    with pytest.raises(RetentionFilesystemError, match="protocol state"):
        with retention_job_lock(engine._shared, prepared.job_ids[0]):
            pass


@_POSIX_ONLY
def test_agent_retry_converges_after_intent_and_partial_target_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    original = engine._remove_workspace
    calls = 0

    def fail_after_first_intent(path: Path) -> None:
        nonlocal calls
        calls += 1
        original(path)
        if calls == 1:
            raise RetentionFilesystemError("injected workspace failure")

    monkeypatch.setattr(engine, "_remove_workspace", fail_after_first_intent)
    with pytest.raises(RetentionFilesystemError, match="injected"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert (engine._intents / f"{prepared.tombstone_id}.json").exists()
    assert not (engine.roots.agent_workspaces / prepared.job_ids[0]).exists()
    monkeypatch.setattr(engine, "_remove_workspace", original)
    ack = engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert ack.tombstone_id == prepared.tombstone_id
    assert all(
        not (engine.roots.agent_results / f"{job}.json").exists() for job in prepared.job_ids
    )


@_POSIX_ONLY
def test_ack_publication_failure_does_not_expose_ack_and_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared_phases()[1]
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    original = retention_fs._publish_immutable

    def fail_ack(path: Path, raw: bytes, node: object) -> None:
        if path.parent == engine._acks:
            raise RetentionFilesystemError("injected ack publication failure")
        original(path, raw, node)  # type: ignore[arg-type]

    monkeypatch.setattr(retention_fs, "_publish_immutable", fail_ack)
    with pytest.raises(RetentionFilesystemError, match="injected ack"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
    monkeypatch.setattr(retention_fs, "_publish_immutable", original)
    assert (
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2).tombstone_id
        == prepared.tombstone_id
    )


@_POSIX_ONLY
def test_controller_retry_recovers_receipt_before_metadata_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    original = retention_fs._unlink_regular
    prepared_path = engine._prepared / f"{prepared.tombstone_id}.json"

    def fail_prepared_unlink(path: Path) -> None:
        if path == prepared_path:
            raise RetentionFilesystemError("injected cleanup failure")
        original(path)

    monkeypatch.setattr(retention_fs, "_unlink_regular", fail_prepared_unlink)
    with pytest.raises(RetentionFilesystemError, match="injected cleanup"):
        engine.controller_consume_ack(prepared.tombstone_id)
    assert (engine._completed / f"{prepared.tombstone_id}.json").exists()
    assert prepared_path.exists()
    monkeypatch.setattr(retention_fs, "_unlink_regular", original)
    assert (
        engine.controller_consume_ack(prepared.tombstone_id).tombstone_id == prepared.tombstone_id
    )
    assert not prepared_path.exists()


@_POSIX_ONLY
@pytest.mark.parametrize(
    "phase,deleted",
    [
        ("before-marker", 0),
        ("after-marker", 0),
        ("delete-1", 1),
        ("delete-2", 2),
        ("delete-3", 3),
        ("after-receipt", 3),
    ],
)
def test_controller_delete_marker_fault_replay_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, deleted: int
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    neighboring = engine.roots.shared_jobs / ("f" * 64)
    neighboring.mkdir()
    marker = engine._deleting / f"{prepared.tombstone_id}.json"

    def crash(actual: str) -> None:
        if actual == phase:
            raise RetentionFilesystemError(f"injected {phase}")

    monkeypatch.setattr(retention_fs, "_controller_delete_fault", crash)
    with pytest.raises(RetentionFilesystemError, match=f"injected {phase}"):
        engine.controller_consume_ack(prepared.tombstone_id)
    assert sum(
        not (engine.roots.shared_jobs / job_id).exists() for job_id in prepared.job_ids
    ) == deleted
    assert marker.exists() is (phase != "before-marker")
    assert not neighboring.is_symlink() and neighboring.exists()
    receipt = engine._completed / f"{prepared.tombstone_id}.json"
    assert receipt.exists() is (phase == "after-receipt")
    monkeypatch.setattr(retention_fs, "_controller_delete_fault", lambda _phase: None)
    assert (
        engine.controller_consume_ack(prepared.tombstone_id).tombstone_id
        == prepared.tombstone_id
    )
    assert engine.is_completed(prepared)
    assert not marker.exists()
    assert (engine._acks / f"{prepared.tombstone_id}.json").exists()
    assert not neighboring.is_symlink() and neighboring.exists()


@_POSIX_ONLY
@pytest.mark.parametrize("poison", ["missing", "reordered", "cross-identity"])
def test_controller_first_delete_requires_exact_validated_job_chain(
    tmp_path: Path, poison: str
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    first, second, _third = prepared.job_ids
    first_path = engine.roots.shared_jobs / first
    second_path = engine.roots.shared_jobs / second
    if poison == "missing":
        shutil.rmtree(first_path)
    elif poison == "reordered":
        temporary = engine.roots.shared_jobs / "swap"
        first_path.rename(temporary)
        second_path.rename(first_path)
        temporary.rename(second_path)
    else:
        (first_path / "job.json").write_bytes((second_path / "job.json").read_bytes())
    with pytest.raises(RetentionFilesystemError):
        engine.controller_consume_ack(prepared.tombstone_id)
    assert not (engine._deleting / f"{prepared.tombstone_id}.json").exists()
    assert not (engine._completed / f"{prepared.tombstone_id}.json").exists()
    assert (engine.roots.shared_jobs / prepared.job_ids[-1]).exists()


@_POSIX_ONLY
def test_controller_marker_tamper_blocks_partial_delete_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)

    def crash(actual: str) -> None:
        if actual == "after-marker":
            raise RetentionFilesystemError("injected marker crash")

    monkeypatch.setattr(retention_fs, "_controller_delete_fault", crash)
    with pytest.raises(RetentionFilesystemError, match="marker crash"):
        engine.controller_consume_ack(prepared.tombstone_id)
    marker = engine._deleting / f"{prepared.tombstone_id}.json"
    marker.write_bytes(b"{}")
    marker.chmod(0o600)
    monkeypatch.setattr(retention_fs, "_controller_delete_fault", lambda _phase: None)
    with pytest.raises(RetentionFilesystemError, match="marker"):
        engine.controller_consume_ack(prepared.tombstone_id)
    assert all((engine.roots.shared_jobs / job_id).exists() for job_id in prepared.job_ids)
    assert not (engine._completed / f"{prepared.tombstone_id}.json").exists()


@_POSIX_ONLY
def test_conflicting_duplicate_and_workspace_symlink_fail_before_deletion(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    first = prepared.job_ids[0]
    workspace = engine.roots.agent_workspaces / first
    shutil.rmtree(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    (engine.roots.agent_workspaces / first).symlink_to(outside, target_is_directory=True)
    with pytest.raises(RetentionFilesystemError):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert (engine.roots.agent_results / f"{prepared.job_ids[1]}.json").exists()


@_POSIX_ONLY
@pytest.mark.parametrize(
    "poison",
    [
        "job-extra",
        "input",
        "schema",
        "model",
        "model-binding",
        "result",
        "result-substitution",
        "workspace-extra",
        "symlink",
        "hardlink",
        "fifo",
        "output",
        "bundle-missing",
        "bundle-tamper",
    ],
)
def test_scratch_provenance_rejects_before_agent_publication(
    tmp_path: Path, poison: str
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    planner, executor, _reviewer = prepared.job_ids
    planner_job = engine.roots.shared_jobs / planner
    planner_workspace = engine.roots.agent_workspaces / planner
    executor_workspace = engine.roots.agent_workspaces / executor
    if poison == "job-extra":
        (planner_job / "extra").write_text("x", encoding="utf-8")
    elif poison == "input":
        (planner_workspace / "inputs" / "0000-input.txt").write_bytes(b"wrong\n")
    elif poison == "schema":
        (planner_workspace / "result-schema.json").write_bytes(b"{}")
    elif poison == "model":
        (executor_workspace / "last-message.json").write_bytes(
            central_protocol.canonical_json(
                {
                    "succeeded": True,
                    "summary": "executed",
                    "reportMarkdown": "# Informe\nExecution.",
                    "evidence": {"wrong": "outputs/report.md"},
                }
            )
        )
    elif poison == "model-binding":
        (executor_workspace / "last-message.json").write_bytes(
            central_protocol.canonical_json(
                {
                    "succeeded": True,
                    "summary": "executed differently",
                    "reportMarkdown": "# Informe\nExecution.",
                    "evidence": {"report": "outputs/report.md"},
                }
            )
        )
    elif poison == "result":
        (engine.roots.agent_results / f"{planner}.json").write_bytes(b"{}")
    elif poison == "result-substitution":
        reviewer = prepared.job_ids[2]
        result_path = engine.roots.agent_results / f"{reviewer}.json"
        result = _read_canonical(result_path)
        result["summary"] = "different but valid"
        _rehash_result(result)
        _write_canonical(result_path, result)
        model_path = (
            engine.roots.agent_workspaces / reviewer / "last-message.json"
        )
        model = _read_canonical(model_path)
        model["summary"] = "different but valid"
        _write_canonical(model_path, model)
    elif poison == "workspace-extra":
        (planner_workspace / "extra").write_text("x", encoding="utf-8")
    elif poison == "symlink":
        target = planner_workspace / "last-message.json"
        target.unlink()
        target.symlink_to(tmp_path / "outside")
    elif poison == "hardlink":
        os.link(planner_workspace / "last-message.json", planner_workspace / "duplicate")
    elif poison == "fifo":
        os.mkfifo(planner_workspace / "fifo")  # type: ignore[attr-defined, unused-ignore]
    elif poison == "bundle-missing":
        digest = hashlib.sha256(b"bundle").hexdigest()
        (engine.roots.agent_bundles / f"{digest}.zip").unlink()
    elif poison == "bundle-tamper":
        digest = hashlib.sha256(b"bundle").hexdigest()
        (engine.roots.agent_bundles / f"{digest}.zip").write_bytes(b"tampered")
    else:
        (executor_workspace / "outputs" / "report.md").write_bytes(b"wrong\n")

    with pytest.raises(RetentionFilesystemError, match="provenance"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert not (engine._intents / f"{prepared.tombstone_id}.json").exists()
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
    assert not any(engine._agent_barriers.glob("*.json"))
    assert (engine.roots.agent_workspaces / planner).exists()
    assert (engine.roots.agent_results / f"{planner}.json").exists()


@_POSIX_ONLY
@pytest.mark.parametrize(
    "tamper",
    [
        "role",
        "order",
        "event",
        "task",
        "revision",
        "prepared-manifest",
        "prepared-input",
        "plan-digest",
        "reviewer-planner-dependency",
        "embedded-executor-result",
        "embedded-executor-manifest",
        "reviewer-accepted",
        "reviewer-decision",
        "reviewer-dependency-digests",
        "casefold-input-collision",
    ],
)
def test_scratch_provenance_semantic_tampering_fails_before_agent_publication(
    tmp_path: Path, tamper: str
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    planner_id, executor_id, reviewer_id = prepared.job_ids
    job_paths = [engine.roots.shared_jobs / job_id / "job.json" for job_id in prepared.job_ids]
    jobs = [_read_canonical(path) for path in job_paths]
    result_paths = [engine.roots.agent_results / f"{job_id}.json" for job_id in prepared.job_ids]
    results = [_read_canonical(path) for path in result_paths]
    planner, executor, reviewer = jobs
    _planner_result, _executor_result, reviewer_result = results

    if tamper == "role":
        planner["role"] = "central_executor"
        _rehash_job(planner)
        _write_canonical(job_paths[0], planner)
    elif tamper == "order":
        _write_canonical(job_paths[0], executor)
        _write_canonical(job_paths[1], planner)
    elif tamper in {"event", "task", "revision"}:
        field, value = {
            "event": ("eventId", "moodle-notification-event-v1:" + "0" * 64),
            "task": ("taskKey", "moodle-task-v1:" + "0" * 64),
            "revision": ("revisionDigest", "moodle-assignment-v1:" + "0" * 64),
        }[tamper]
        planner[field] = value
        _rehash_job(planner)
        _write_canonical(job_paths[0], planner)
    elif tamper == "prepared-manifest":
        planner["preparedInputManifestDigest"] = "0" * 64
        _rehash_job(planner)
        _write_canonical(job_paths[0], planner)
    elif tamper == "prepared-input":
        (engine.roots.shared_jobs / planner_id / "inputs" / "0000-input.txt").write_bytes(
            b"tampered\n"
        )
    elif tamper == "plan-digest":
        dependencies = cast(dict[str, object], executor["dependencies"])
        dependencies["planDigest"] = "0" * 64
        _rehash_job(executor)
        _write_canonical(job_paths[1], executor)
    elif tamper == "reviewer-planner-dependency":
        dependencies = cast(dict[str, object], reviewer["dependencies"])
        dependencies["plannerJobId"] = "0" * 64
        _rehash_job(reviewer)
        _write_canonical(job_paths[2], reviewer)
    elif tamper in {"embedded-executor-result", "embedded-executor-manifest"}:
        embedded = cast(dict[str, object], reviewer["executorResult"])
        if tamper == "embedded-executor-result":
            embedded["summary"] = "forged"
        else:
            manifest = cast(dict[str, object], embedded["artifactManifest"])
            manifest["totals"] = {"files": 1, "bytes": 0}
            embedded["artifactManifestDigest"] = central_protocol.canonical_digest(manifest)
        _rehash_result(embedded)
        dependencies = cast(dict[str, object], reviewer["dependencies"])
        dependencies["executorResultDigest"] = embedded["executorResultDigest"]
        dependencies["artifactManifestDigest"] = embedded["artifactManifestDigest"]
        _rehash_job(reviewer)
        _write_canonical(job_paths[2], reviewer)
    elif tamper == "reviewer-accepted":
        reviewer_result["accepted"] = False
        _rehash_result(reviewer_result)
        _write_canonical(result_paths[2], reviewer_result)
    elif tamper == "reviewer-decision":
        reviewer_result["decisions"] = {"report": "rejected"}
        _rehash_result(reviewer_result)
        _write_canonical(result_paths[2], reviewer_result)
    elif tamper == "reviewer-dependency-digests":
        reviewer_result["dependencyDigests"] = {}
        _rehash_result(reviewer_result)
        _write_canonical(result_paths[2], reviewer_result)
    else:
        (engine.roots.shared_jobs / planner_id / "inputs" / "0000-INPUT.TXT").write_bytes(
            b"input\n"
        )

    with pytest.raises(RetentionFilesystemError, match="provenance"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    _assert_scratch_unpublished(engine, prepared)
    assert planner_id != executor_id != reviewer_id


@_POSIX_ONLY
def test_scratch_retry_allows_all_workspaces_missing_after_exact_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    original = engine._remove_workspace
    calls = 0

    def fail_after_workspace_cleanup(path: Path) -> None:
        nonlocal calls
        original(path)
        calls += 1
        if calls == len(prepared.job_ids):
            raise RetentionFilesystemError("injected workspace cleanup failure")

    monkeypatch.setattr(engine, "_remove_workspace", fail_after_workspace_cleanup)
    with pytest.raises(RetentionFilesystemError, match="injected workspace cleanup"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert (engine._intents / f"{prepared.tombstone_id}.json").exists()
    assert all(
        not (engine.roots.agent_workspaces / job_id).exists() for job_id in prepared.job_ids
    )
    monkeypatch.setattr(engine, "_remove_workspace", original)
    assert (
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2).tombstone_id
        == prepared.tombstone_id
    )


@_POSIX_ONLY
def test_scratch_retry_allows_results_missing_after_workspace_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    original = retention_fs._unlink_regular
    calls = 0

    def fail_after_first_result(path: Path) -> None:
        nonlocal calls
        original(path)
        if path.parent == engine.roots.agent_results:
            calls += 1
            if calls == 1:
                raise RetentionFilesystemError("injected result cleanup failure")

    monkeypatch.setattr(retention_fs, "_unlink_regular", fail_after_first_result)
    with pytest.raises(RetentionFilesystemError, match="injected result cleanup"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert (engine._intents / f"{prepared.tombstone_id}.json").exists()
    assert all(
        not (engine.roots.agent_workspaces / job_id).exists() for job_id in prepared.job_ids
    )
    assert not (engine.roots.agent_results / f"{prepared.job_ids[0]}.json").exists()
    monkeypatch.setattr(retention_fs, "_unlink_regular", original)
    assert (
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2).tombstone_id
        == prepared.tombstone_id
    )


@_POSIX_ONLY
def test_scratch_retry_recovers_deterministic_trash_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    original = retention_fs._fsync_dir
    interrupted = False

    def fail_after_rename(path: Path) -> None:
        nonlocal interrupted
        if path == engine.roots.agent_workspaces and not interrupted:
            interrupted = True
            raise RetentionFilesystemError("injected post-rename failure")
        original(path)

    monkeypatch.setattr(retention_fs, "_fsync_dir", fail_after_rename)
    with pytest.raises(RetentionFilesystemError, match="post-rename"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    workspace = engine.roots.agent_workspaces / prepared.job_ids[0]
    trash = engine._trash / f"{prepared.job_ids[0]}.trash"
    assert not workspace.exists() and trash.exists()
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
    monkeypatch.setattr(retention_fs, "_fsync_dir", original)
    assert engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2).tombstone_id == (
        prepared.tombstone_id
    )
    assert not trash.exists()


@_POSIX_ONLY
def test_scratch_retry_finishes_partially_removed_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    original = retention_fs._remove_tree_fd
    interrupted = False

    def fail_after_nested_remove(parent: int, name: str) -> None:
        nonlocal interrupted
        original(parent, name)
        if name == "inputs" and not interrupted:
            interrupted = True
            raise RetentionFilesystemError("injected nested removal failure")

    monkeypatch.setattr(retention_fs, "_remove_tree_fd", fail_after_nested_remove)
    with pytest.raises(RetentionFilesystemError, match="nested removal"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    trash = engine._trash / f"{prepared.job_ids[0]}.trash"
    assert trash.exists()
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
    monkeypatch.setattr(retention_fs, "_remove_tree_fd", original)
    assert engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2).tombstone_id == (
        prepared.tombstone_id
    )


@_POSIX_ONLY
@pytest.mark.parametrize("poison", ["both", "foreign", "extra", "symlink", "hardlink", "fifo"])
def test_scratch_trash_tampering_fails_before_ack(tmp_path: Path, poison: str) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    committed = engine.commit(prepared, committed_at=1)
    for directory in (
        engine._agent,
        engine._intents,
        engine._agent_barriers,
        engine._agent_locks,
        engine._trash,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    workspace = engine.roots.agent_workspaces / prepared.job_ids[0]
    trash = engine._trash / f"{prepared.job_ids[0]}.trash"
    if poison == "both":
        trash.mkdir(mode=0o700)
    elif poison == "foreign":
        (engine._trash / ("f" * 64 + ".trash")).mkdir(mode=0o700)
    else:
        workspace.rename(trash)
        if poison == "extra":
            (trash / "extra").write_bytes(b"x")
        elif poison == "symlink":
            (trash / "last-message.json").unlink()
            (trash / "last-message.json").symlink_to(tmp_path / "outside")
        elif poison == "hardlink":
            os.link(trash / "last-message.json", trash / "duplicate")
        else:
            os.mkfifo(trash / "fifo")  # type: ignore[attr-defined, unused-ignore]
        # Simulate an exact durable intent after the rename; its absence must never recover trash.
        retention_fs._publish_immutable(
            engine._intents / f"{prepared.tombstone_id}.json",
            committed.as_json(),
            engine._agent_private_file,
        )
    with pytest.raises(RetentionFilesystemError):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()


@_POSIX_ONLY
def test_scratch_missing_target_without_intent_is_not_crash_recovery(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    shutil.rmtree(engine.roots.agent_workspaces / prepared.job_ids[0])

    with pytest.raises(RetentionFilesystemError, match="workspace is missing"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert not (engine._intents / f"{prepared.tombstone_id}.json").exists()
    assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
    assert not any(engine._agent_barriers.glob("*.json"))
    assert all(
        (engine.roots.agent_workspaces / job_id).exists() for job_id in prepared.job_ids[1:]
    )


@_POSIX_ONLY
def test_protocol_identity_rejects_wrong_owner_group_mode_symlink_and_hardlink(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    engine.prepare(prepared)
    metadata = engine._prepared / f"{prepared.tombstone_id}.json"

    metadata.chmod(0o644)
    with pytest.raises(RetentionFilesystemError, match="protocol state"):
        engine.prepare(prepared)
    metadata.chmod(0o600)

    hardlink = metadata.with_name("duplicate.json")
    os.link(metadata, hardlink)
    with pytest.raises(RetentionFilesystemError, match="protocol state"):
        engine.prepare(prepared)
    hardlink.unlink()

    engine.commit(prepared, committed_at=1)

    outside = tmp_path / "outside"
    outside.mkdir()
    agent_root = engine.roots.agent_private
    agent_root.rmdir()
    agent_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RetentionFilesystemError, match="protocol state"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)


@_POSIX_ONLY
def test_protocol_identity_rejects_wrong_policy_and_tampered_lock(tmp_path: Path) -> None:
    uid, gid = _posix_identity()
    roots = RetentionRoots(
        tmp_path / "controller",
        tmp_path / "jobs",
        tmp_path / "agent",
        tmp_path / "results",
        tmp_path / "workspaces",
        tmp_path / "bundles",
    )
    wrong_identity = RetentionFilesystem(
        roots,
        RetentionOwnership(
            controller_uid=uid + 1,
            agent_uid=uid,
            controller_gid=gid + 1,
            agent_gid=gid + 1,
        ),
    )
    roots.controller_private.mkdir()
    with pytest.raises(RetentionFilesystemError, match="protocol state"):
        wrong_identity.prepare(_prepared())

    engine = _engine(tmp_path / "valid")
    prepared = _prepared()
    engine.commit(prepared, committed_at=1)
    lock = engine._controller_locks / f"{prepared.job_ids[0]}.lock"
    lock.chmod(0o600)
    with pytest.raises(RetentionFilesystemError, match="protocol state"):
        engine.commit(prepared, committed_at=1)


@_POSIX_ONLY
@pytest.mark.parametrize("anchor", ["controller", "agent"])
def test_private_anchor_modes_are_distinct_and_exact(tmp_path: Path, anchor: str) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    if anchor == "controller":
        engine.roots.controller_private.chmod(0o700)
        with pytest.raises(RetentionFilesystemError, match="protocol state"):
            engine.prepare(prepared)
        return
    _seed_targets(engine, prepared)
    engine.commit(prepared, committed_at=1)
    engine.roots.agent_private.chmod(0o750)
    with pytest.raises(RetentionFilesystemError, match="protocol state"):
        engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
    assert not (engine._intents / f"{prepared.tombstone_id}.json").exists()


@_POSIX_ONLY
@pytest.mark.parametrize(
    "target,poison",
    [
        ("results-root", "mode"),
        ("bundles-root", "mode"),
        ("bundles-root", "symlink"),
        ("results-lock", "mode"),
        ("bundles-lock", "gid"),
        ("bundles-lock", "symlink"),
        ("bundles-lock", "hardlink"),
        ("bundles-lock", "fifo"),
    ],
)
def test_publication_locks_reject_root_and_lock_tampering(
    tmp_path: Path, target: str, poison: str
) -> None:
    engine = _engine(tmp_path)
    ownership = engine.ownership
    if target.endswith("root"):
        root = (
            engine.roots.agent_results
            if target == "results-root"
            else engine.roots.agent_bundles
        )
        if poison == "mode":
            root.chmod(0o750)
        else:
            outside = tmp_path / "outside"
            outside.mkdir()
            root.rmdir()
            root.symlink_to(outside, target_is_directory=True)
    else:
        with retention_fs._publication_locks(
            engine.roots.agent_results, engine.roots.agent_bundles, ownership
        ):
            pass
        lock = (
            engine.roots.agent_results / ".results.publish.lock"
            if target == "results-lock"
            else engine.roots.agent_bundles / ".publish.lock"
        )
        if poison == "mode":
            lock.chmod(0o600)
        elif poison == "gid":
            metadata = lock.stat()
            chown = cast(Callable[[Path, int, int], None] | None, getattr(os, "chown", None))
            if chown is None:
                pytest.skip("changing ownership is unavailable")
            try:
                chown(lock, metadata.st_uid, metadata.st_gid + 1)
            except PermissionError:
                pytest.skip("changing ownership requires root")
        elif poison == "symlink":
            lock.unlink()
            lock.symlink_to(tmp_path / "outside.lock")
        elif poison == "hardlink":
            os.link(lock, tmp_path / "duplicate.lock")
        else:
            lock.unlink()
            os.mkfifo(lock)  # type: ignore[attr-defined, unused-ignore]
    with pytest.raises(RetentionFilesystemError):
        with retention_fs._publication_locks(
            engine.roots.agent_results, engine.roots.agent_bundles, ownership
        ):
            pass


@_POSIX_ONLY
def test_publication_locks_serialize_existing_agent_publishers(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    entered = threading.Event()
    acquired = threading.Event()

    def contender() -> None:
        entered.set()
        with retention_fs._publication_locks(
            engine.roots.agent_results, engine.roots.agent_bundles, engine.ownership
        ):
            acquired.set()

    with retention_fs._publication_locks(
        engine.roots.agent_results, engine.roots.agent_bundles, engine.ownership
    ):
        thread = threading.Thread(target=contender)
        thread.start()
        assert entered.wait(1)
        assert not acquired.wait(0.1)
    thread.join(1)
    assert acquired.is_set()


@_POSIX_ONLY
@pytest.mark.parametrize(
    "path_key,poison",
    [
        (key, poison)
        for key in ("committed", "barriers", "locks", "acks")
        for poison in ("missing", "mode")
    ],
)
def test_shared_layout_tamper_fails_before_metadata(
    tmp_path: Path, path_key: str, poison: str
) -> None:
    engine = _engine(tmp_path)
    prepared = _prepared()
    paths = {
        "committed": engine._committed,
        "barriers": engine._controller_barriers,
        "locks": engine._controller_locks,
        "acks": engine._acks,
    }
    path = paths[path_key]
    if poison == "missing":
        path.rmdir()
    else:
        path.chmod(0o700)
    if path_key == "acks":
        engine.commit(prepared, committed_at=1)
        _seed_targets(engine, prepared)
        with pytest.raises(RetentionFilesystemError):
            engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
        assert not (engine._intents / f"{prepared.tombstone_id}.json").exists()
        assert not (engine._acks / f"{prepared.tombstone_id}.json").exists()
        return
    with pytest.raises(RetentionFilesystemError):
        engine.commit(prepared, committed_at=1)
    assert not (engine._prepared / f"{prepared.tombstone_id}.json").exists()
    assert not (engine._committed / f"{prepared.tombstone_id}.json").exists()


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
def test_linux_docker_two_uid_shared_group_harness() -> None:
    """Exercise each shared subtree under the reciprocal two-UID Linux DAC layout."""
    task = "moodle-task-v1:" + "a" * 64
    revision = "moodle-assignment-v1:" + "b" * 64
    jobs, results, models = _central_corpus(task, revision)
    fixture = json.dumps({"jobs": jobs, "results": results, "models": models})
    program = textwrap.dedent(
        """\
        import hashlib
        import json
        import os
        import sys
        from pathlib import Path

        from moodle_autotask.adapters.aws.retention import plan_retention
        from moodle_autotask.adapters.aws import central_protocol
        from moodle_autotask.adapters.aws.retention_fs import (
            RetentionFilesystem,
            RetentionOwnership,
            RetentionRoots,
        )
        from moodle_autotask.adapters.moodle.approval_state import RetentionRecord
        from moodle_autotask.adapters.moodle.state import _event_id
        from moodle_autotask.domain.models import ExecutionMode

        task = "moodle-task-v1:" + "a" * 64
        revision = "moodle-assignment-v1:" + "b" * 64
        fixture = json.loads(os.environ["RETENTION_FIXTURE"])
        jobs = fixture["jobs"]
        results = fixture["results"]
        models = fixture["models"]
        record = RetentionRecord(
            _event_id(task, revision),
            task,
            revision,
            ExecutionMode.CENTRAL,
            "cleaned",
            True,
            1,
            1,
            1,
            1,
            tuple(job["jobId"] for job in jobs),
            hashlib.sha256(b"bundle").hexdigest(),
            result_digests=tuple(
                result[
                    {
                        "central_planner": "plannerResultDigest",
                        "central_executor": "executorResultDigest",
                        "central_reviewer": "reviewerResultDigest",
                    }[result["role"]]
                ]
                for result in results
            ),
        )
        plans = plan_retention((record,), now=1, limit=2)
        prepared = {plan.target_phase: plan for plan in plans}[sys.argv[2]]
        engine = RetentionFilesystem(
            RetentionRoots(
                Path("/data/controller"),
                Path("/data/jobs"),
                Path("/data/agent"),
                Path("/data/results"),
                Path("/data/workspaces"),
                Path("/data/bundles"),
            ),
            RetentionOwnership(1001, 1002, 1001, 2000),
        )
        command = sys.argv[1]
        if command == "id":
            print(prepared.tombstone_id)
        elif command == "jobs":
            print(*prepared.job_ids)
        elif command == "seed":
            for job, result, model in zip(jobs, results, models, strict=True):
                job_id = job["jobId"]
                job_directory = Path("/data/jobs") / job_id
                job_directory.mkdir()
                (job_directory / "job.json").write_bytes(central_protocol.canonical_json(job))
                (job_directory / "inputs").mkdir()
                (job_directory / "inputs" / "0000-input.txt").write_bytes(b"input\\n")
                workspace = Path("/data/workspaces") / job_id
                workspace.mkdir()
                contract = central_protocol.central_workspace_contract(job)
                (workspace / "inputs").mkdir()
                (workspace / "inputs" / "0000-input.txt").write_bytes(b"input\\n")
                (workspace / "result-schema.json").write_bytes(
                    contract["resultSchemaJson"].encode("utf-8")
                )
                (workspace / "last-message.json").write_bytes(
                    central_protocol.canonical_json(model)
                )
                if job["role"] == "central_executor":
                    (workspace / "outputs").mkdir()
                    (workspace / "outputs" / "report.md").write_bytes(b"verified artifact\\n")
                (Path("/data/results") / f"{job_id}.json").write_bytes(
                    central_protocol.canonical_json(result)
                )
        elif command == "commit":
            engine.commit(prepared, committed_at=1)
        elif command == "agent":
            engine.agent_consume(prepared.tombstone_id, acknowledged_at=2, now=2)
        elif command == "ack":
            engine.controller_consume_ack(prepared.tombstone_id)
        else:
            raise RuntimeError("unknown retention test command")
        """
    )
    script = textwrap.dedent(
        """\
        set -eu
        groupadd -g 1001 controller
        groupadd -g 2000 retention
        useradd -u 1001 -g controller controller
        useradd -u 1002 -g retention agent

        run_controller() {
            runuser -u controller -- env PYTHONPATH=/repo/src \\
                python -c "$RETENTION_PROGRAM" "$@"
        }

        run_agent() {
            runuser -u agent -- env PYTHONPATH=/repo/src \\
                python -c "$RETENTION_PROGRAM" "$@"
        }

        assert_stat() {
            test "$(stat -c '%u:%g:%a' "$1")" = "$2"
        }

        job_id() {
            printf '%064d' 0 | tr 0 "$1"
        }

        jobs() {
            env PYTHONPATH=/repo/src python -c "$RETENTION_PROGRAM" jobs scratch
        }

        setup_layout() {
            rm -rf /data
            mkdir -p /data/controller /data/jobs /data/agent
            mkdir -p /data/results /data/workspaces /data/bundles
            chown controller:controller /data/controller
            chown controller:retention /data/jobs
                chown agent:retention /data/agent /data/workspaces
                chown agent:controller /data/bundles
            chown agent:controller /data/results
                chmod 750 /data/controller
                chmod 700 /data/agent /data/workspaces
                chmod 2750 /data/bundles
            chmod 2750 /data/jobs /data/results
            mkdir -p /data/jobs/.retention/committed
            mkdir -p /data/jobs/.retention/barriers
            mkdir -p /data/jobs/.retention/locks
            mkdir -p /data/results/.retention/acks
            chown -R controller:retention /data/jobs/.retention
            chown -R agent:controller /data/results/.retention
            chmod 2750 /data/jobs/.retention
            chmod 2750 /data/jobs/.retention/committed
            chmod 2750 /data/jobs/.retention/barriers
            chmod 2750 /data/jobs/.retention/locks
            chmod 2750 /data/results/.retention
            chmod 2750 /data/results/.retention/acks
            env PYTHONPATH=/repo/src python -c "$RETENTION_PROGRAM" seed scratch
                for id in $(jobs); do
                    chown -R controller:retention /data/jobs/$id
                    find /data/jobs/$id -type d -exec chmod 2750 {} +
                    find /data/jobs/$id -type f -exec chmod 640 {} +
                    chown -R agent:retention /data/workspaces/$id
                    chmod 700 /data/workspaces/$id
                    chown agent:controller /data/results/$id.json
                    chmod 640 /data/results/$id.json
                done
            bundle=$(python -c 'import hashlib; print(hashlib.sha256(b"bundle").hexdigest())')
                printf bundle >/data/bundles/$bundle.zip
                    chown agent:controller /data/bundles/$bundle.zip
                chmod 640 /data/bundles/$bundle.zip
        }

        assert_layout() {
            assert_stat /data/controller 1001:1001:750
            assert_stat /data/jobs 1001:2000:2750
            assert_stat /data/agent 1002:2000:700
            assert_stat /data/results 1002:1001:2750
            assert_stat /data/workspaces 1002:2000:700
            assert_stat /data/bundles 1002:1001:2750
            assert_stat /data/jobs/.retention 1001:2000:2750
            assert_stat /data/jobs/.retention/committed 1001:2000:2750
            assert_stat /data/jobs/.retention/barriers 1001:2000:2750
            assert_stat /data/jobs/.retention/locks 1001:2000:2750
            assert_stat /data/results/.retention 1002:1001:2750
            assert_stat /data/results/.retention/acks 1002:1001:2750
        }

        assert_targets_unchanged() {
            test -f /data/bundles/$bundle.zip
            for id in $(jobs); do
                test -d /data/jobs/$id
                test -f /data/results/$id.json
                test -d /data/workspaces/$id
            done
        }

        corrupt() {
            case "$2:$3" in
                missing:*) rm -rf "$1" ;;
                uid:jobs) chown agent:retention "$1" ;;
                gid:jobs) chgrp controller "$1" ;;
                mode:jobs) chmod 700 "$1" ;;
                uid:acks) chown controller:controller "$1" ;;
                gid:acks) chgrp retention "$1" ;;
                mode:acks) chmod 700 "$1" ;;
            esac
        }

        setup_layout
        assert_layout
        if ! runuser -u controller -- test -r /data/bundles/$bundle.zip; then exit 85; fi
        if runuser -u controller -- rm -f /data/bundles/$bundle.zip; then exit 86; fi
        id=$(jobs | awk '{print $1}')
        if runuser -u agent -- rm -rf /data/jobs/$id; then exit 81; fi
        if runuser -u controller -- rm -f /data/results/$id.json; then exit 82; fi
        if runuser -u controller -- rm -rf /data/workspaces/$id; then exit 83; fi
        tid=$(run_controller id scratch)
        run_controller commit scratch
        assert_stat /data/controller/retention/prepared/$tid.json 1001:1001:600
        assert_stat /data/jobs/.retention/committed/$tid.json 1001:2000:640
        assert_stat /data/jobs/.retention/barriers/$id.json 1001:2000:640
        assert_stat /data/jobs/.retention/locks/$id.lock 1001:2000:640
        run_agent agent scratch
        assert_stat /data/agent/retention/intents/$tid.json 1002:2000:600
        assert_stat /data/agent/retention/locks/$id.lock 1002:2000:600
        assert_stat /data/results/.retention/acks/$tid.json 1002:1001:640
            run_controller ack scratch
            test -f /data/controller/retention/completed/$tid.json
            test -f /data/results/.retention/acks/$tid.json
            if runuser -u controller -- rm -f /data/results/.retention/acks/$tid.json; then
                exit 84
            fi
            test ! -e /data/jobs/$id
        test ! -e /data/results/$id.json
        test ! -e /data/workspaces/$id

        for path in \\
            /data/jobs/.retention \\
            /data/jobs/.retention/committed \\
            /data/jobs/.retention/barriers \\
            /data/jobs/.retention/locks; do
            for poison in missing uid gid mode; do
                setup_layout
                tid=$(run_controller id scratch)
                corrupt "$path" "$poison" jobs
                if run_controller commit scratch; then exit 91; fi
                test ! -e /data/controller/retention/prepared/$tid.json
                test ! -e /data/jobs/.retention/committed/$tid.json
            done
        done

        for path in /data/results/.retention /data/results/.retention/acks; do
            for poison in missing uid gid mode; do
                setup_layout
                tid=$(run_controller id evidence)
                run_controller commit evidence
                corrupt "$path" "$poison" acks
                if run_agent agent evidence; then exit 92; fi
                test ! -e /data/agent/retention/intents/$tid.json
                test ! -e /data/results/.retention/acks/$tid.json
                assert_targets_unchanged
            done
        done
        """
    )
    repository = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            f"RETENTION_PROGRAM={program}",
            "-e",
            f"RETENTION_FIXTURE={fixture}",
            "-v",
            f"{repository}:/repo:ro",
            "python:3.13-slim",
            "sh",
            "-ceu",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
