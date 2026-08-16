from __future__ import annotations

import copy
from typing import cast

import pytest

from moodle_autotask.adapters.aws import lab_protocol

_TASK = "moodle-task-v1:" + "1" * 64
_REVISION = "moodle-assignment-v1:" + "2" * 64
_TRANSFER = "3" * 64


def _job_id(phase: str, context_digest: str) -> str:
    return lab_protocol.canonical_digest(
        {
            "contextDigest": context_digest,
            "phase": phase,
            "revisionDigest": _REVISION,
            "taskKey": _TASK,
        }
    )


def _corpus() -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
]:
    common: dict[str, object] = {
        "kind": lab_protocol.LAB_JOB_KIND,
        "taskKey": _TASK,
        "revisionDigest": _REVISION,
        "courseName": "Course",
        "courseShortname": "C",
        "title": "Laboratorio",
        "intro": "Intro",
        "attachments": [],
        "guestInputTransfer": {
            "transferDigest": _TRANSFER,
            "guestPaths": [],
        },
    }
    plan: dict[str, object] = {
        **common,
        "jobId": _job_id("lab_plan", _TRANSFER),
        "phase": "lab_plan",
        "context": None,
    }
    plan_result: dict[str, object] = {
        "kind": lab_protocol.LAB_RESULT_KIND,
        "jobId": plan["jobId"],
        "phase": "lab_plan",
        "succeeded": True,
        "summary": "plan",
        "reportMarkdown": "",
        "powershellCommands": ["Write-Output ok"],
    }
    plan_digest = lab_protocol.canonical_digest(plan_result)
    report_context = {
        "planDigest": plan_digest,
        "labSucceeded": True,
        "transcript": "ok",
        "transferDigest": _TRANSFER,
    }
    report_context_digest = lab_protocol.canonical_digest(
        {"planDigest": plan_digest, "transferDigest": _TRANSFER}
    )
    report: dict[str, object] = {
        **common,
        "jobId": _job_id("lab_report", report_context_digest),
        "phase": "lab_report",
        "context": report_context,
    }
    report_result: dict[str, object] = {
        "kind": lab_protocol.LAB_RESULT_KIND,
        "jobId": report["jobId"],
        "phase": "lab_report",
        "succeeded": True,
        "summary": "done",
        "reportMarkdown": "# Report",
        "powershellCommands": [],
    }
    dispatch: dict[str, object] = {
        "kind": lab_protocol.LAB_DISPATCH_KIND,
        "executionKey": report["jobId"],
        "labHandle": "lab:test",
        "planDigest": plan_digest,
        "commandsDigest": lab_protocol.canonical_digest(["Write-Output ok"]),
        "state": "dispatched",
        "commandId": "12345678-1234-1234-1234-123456789abc",
    }
    return [plan, report], [plan_result, report_result], dispatch


def test_lab_chain_and_provenance_bind_exact_two_phase_execution() -> None:
    jobs, results, dispatch = _corpus()
    job_ids = tuple(cast(str, job["jobId"]) for job in jobs)
    assert lab_protocol.validate_chain(jobs, results, expected_job_ids=job_ids) == (
        tuple(jobs),
        tuple(results),
    )
    provenance = lab_protocol.build_provenance(
        jobs,
        results,
        selected_mode="hybrid",
        specification_digest="4" * 64,
        barrier_ids=job_ids,
        terminal_status="succeeded",
        dispatch=dispatch,
    )
    assert lab_protocol.validate_provenance(provenance) == provenance


@pytest.mark.parametrize(
    "poison",
    [
        "phase-order",
        "mixed-task",
        "mixed-revision",
        "plan-digest",
        "result-job",
        "dispatch-report",
        "dispatch-plan",
    ],
)
def test_lab_protocol_rejects_cross_identity_and_dispatch_tampering(poison: str) -> None:
    jobs, results, dispatch = _corpus()
    if poison == "phase-order":
        jobs.reverse()
        results.reverse()
    elif poison == "mixed-task":
        jobs[1]["taskKey"] = "moodle-task-v1:" + "9" * 64
    elif poison == "mixed-revision":
        jobs[1]["revisionDigest"] = "moodle-assignment-v1:" + "9" * 64
    elif poison == "plan-digest":
        context = cast(dict[str, object], jobs[1]["context"])
        context["planDigest"] = "9" * 64
        context_digest = lab_protocol.canonical_digest(
            {"planDigest": "9" * 64, "transferDigest": _TRANSFER}
        )
        jobs[1]["jobId"] = _job_id("lab_report", context_digest)
        results[1]["jobId"] = jobs[1]["jobId"]
    elif poison == "result-job":
        results[1]["jobId"] = "9" * 64
    elif poison == "dispatch-report":
        dispatch["executionKey"] = "9" * 64
    else:
        dispatch["planDigest"] = "9" * 64

    job_ids = tuple(cast(str, job["jobId"]) for job in jobs)
    if poison.startswith("dispatch-"):
        with pytest.raises(lab_protocol.LabProtocolError):
            lab_protocol.build_provenance(
                jobs,
                results,
                selected_mode="hybrid",
                specification_digest="4" * 64,
                barrier_ids=job_ids,
                terminal_status="succeeded",
                dispatch=dispatch,
            )
    else:
        with pytest.raises(lab_protocol.LabProtocolError):
            lab_protocol.validate_chain(jobs, results, expected_job_ids=job_ids)


@pytest.mark.parametrize(
    "poison",
    [
        "short-success",
        "full-without-dispatch",
        "dispatch-one-barrier",
        "intent-command-id",
        "unknown-dispatched",
        "success-intent",
    ],
)
def test_lab_provenance_rejects_impossible_terminal_shapes(poison: str) -> None:
    jobs, results, dispatch = _corpus()
    job_ids = tuple(cast(str, job["jobId"]) for job in jobs)
    provenance = lab_protocol.build_provenance(
        jobs,
        results,
        selected_mode="hybrid",
        specification_digest="4" * 64,
        barrier_ids=job_ids,
        terminal_status="succeeded",
        dispatch=dispatch,
    )
    candidate = copy.deepcopy(provenance)
    if poison == "short-success":
        candidate["phases"] = ["lab_plan"]
        candidate["jobIds"] = [job_ids[0]]
        candidate["barrierIds"] = [job_ids[0]]
        candidate["resultDigests"] = cast(list[str], candidate["resultDigests"])[:1]
        candidate["dispatch"] = None
    elif poison == "full-without-dispatch":
        candidate["dispatch"] = None
    elif poison == "dispatch-one-barrier":
        candidate["phases"] = ["lab_plan"]
        candidate["jobIds"] = [job_ids[0]]
        candidate["barrierIds"] = [job_ids[0]]
        candidate["resultDigests"] = cast(list[str], candidate["resultDigests"])[:1]
        candidate["terminalStatus"] = "capacity_error"
    elif poison == "intent-command-id":
        candidate["phases"] = ["lab_plan"]
        candidate["jobIds"] = [job_ids[0]]
        candidate["resultDigests"] = cast(list[str], candidate["resultDigests"])[:1]
        candidate["terminalStatus"] = "dispatch_unknown"
        cast(dict[str, object], candidate["dispatch"])["state"] = "intent"
    elif poison == "unknown-dispatched":
        candidate["phases"] = ["lab_plan"]
        candidate["jobIds"] = [job_ids[0]]
        candidate["resultDigests"] = cast(list[str], candidate["resultDigests"])[:1]
        candidate["terminalStatus"] = "dispatch_unknown"
    else:
        record = cast(dict[str, object], candidate["dispatch"])
        record["state"] = "intent"
        record.pop("commandId")

    with pytest.raises(lab_protocol.LabProtocolError):
        lab_protocol.validate_provenance(candidate)
