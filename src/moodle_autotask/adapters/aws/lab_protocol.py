"""Pure canonical contracts for two-phase lab execution and retention."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import cast

LAB_JOB_KIND = "moodle-agent-job-v1"
LAB_RESULT_KIND = "moodle-agent-result-v1"
LAB_DISPATCH_KIND = "moodle-lab-dispatch-v1"
LAB_PROVENANCE_KIND = "moodle-lab-provenance-v1"
LAB_PHASES = ("lab_plan", "lab_report")

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMAND_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_TASK = re.compile(r"^moodle-task-v1:[0-9a-f]{64}$")
_REVISION = re.compile(r"^moodle-assignment-v1:[0-9a-f]{64}$")
_MAX_BYTES = 2 * 1024 * 1024
_MODEL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["succeeded", "summary", "reportMarkdown", "powershellCommands"],
    "properties": {
        "succeeded": {"type": "boolean"},
        "summary": {"type": "string", "maxLength": 16384},
        "reportMarkdown": {"type": "string", "maxLength": 2000000},
        "powershellCommands": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 24576},
        },
    },
}


class LabProtocolError(RuntimeError):
    """A lab wire record is malformed, non-canonical, or cross-bound."""


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LabProtocolError("lab record is not canonical JSON") from error


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def model_schema_json() -> bytes:
    return canonical_json(_MODEL_SCHEMA)


def validate_job(value: object, expected_job_id: str | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise LabProtocolError("lab job has an invalid shape")
    job = cast(dict[str, object], value)
    expected_keys = {
        "kind",
        "jobId",
        "phase",
        "taskKey",
        "revisionDigest",
        "courseName",
        "courseShortname",
        "title",
        "intro",
        "attachments",
        "context",
        "guestInputTransfer",
    }
    if set(job) != expected_keys or job.get("kind") != LAB_JOB_KIND:
        raise LabProtocolError("lab job has an invalid shape")
    job_id, phase = job.get("jobId"), job.get("phase")
    if (
        not isinstance(job_id, str)
        or _DIGEST.fullmatch(job_id) is None
        or (expected_job_id is not None and job_id != expected_job_id)
        or phase not in LAB_PHASES
        or not isinstance(job.get("taskKey"), str)
        or _TASK.fullmatch(cast(str, job["taskKey"])) is None
        or not isinstance(job.get("revisionDigest"), str)
        or _REVISION.fullmatch(cast(str, job["revisionDigest"])) is None
        or any(
            not isinstance(job.get(key), str)
            for key in ("courseName", "courseShortname", "title", "intro")
        )
    ):
        raise LabProtocolError("lab job identity is invalid")
    attachments = _validate_attachments(job.get("attachments"))
    transfer_digest, _guest_paths = _validate_transfer(job.get("guestInputTransfer"))
    context_digest: str
    if phase == "lab_plan":
        if job.get("context") is not None:
            raise LabProtocolError("lab plan context is invalid")
        context_digest = transfer_digest
    else:
        context = job.get("context")
        if not isinstance(context, dict) or set(context) != {
            "planDigest",
            "labSucceeded",
            "transcript",
            "transferDigest",
        }:
            raise LabProtocolError("lab report context is invalid")
        plan_digest = context.get("planDigest")
        transcript = context.get("transcript")
        if (
            not isinstance(plan_digest, str)
            or _DIGEST.fullmatch(plan_digest) is None
            or context.get("transferDigest") != transfer_digest
            or not isinstance(context.get("labSucceeded"), bool)
            or not isinstance(transcript, str)
            or len(transcript.encode("utf-8")) > _MAX_BYTES
        ):
            raise LabProtocolError("lab report context is invalid")
        context_digest = canonical_digest(
            {"planDigest": plan_digest, "transferDigest": transfer_digest}
        )
    expected_id = canonical_digest(
        {
            "contextDigest": context_digest,
            "phase": phase,
            "revisionDigest": job["revisionDigest"],
            "taskKey": job["taskKey"],
        }
    )
    if job_id != expected_id:
        raise LabProtocolError("lab job digest is invalid")
    # Force validation to consume the attachment result rather than merely shape-check it.
    if len(attachments) != len(cast(list[object], job["attachments"])):
        raise LabProtocolError("lab job attachments are invalid")
    return job


def validate_result(
    value: object, expected_job_id: str, expected_phase: str
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "jobId",
        "phase",
        "succeeded",
        "summary",
        "reportMarkdown",
        "powershellCommands",
    }:
        raise LabProtocolError("lab result has an invalid shape")
    result = cast(dict[str, object], value)
    summary, report, commands = (
        result.get("summary"),
        result.get("reportMarkdown"),
        result.get("powershellCommands"),
    )
    if (
        result.get("kind") != LAB_RESULT_KIND
        or result.get("jobId") != expected_job_id
        or result.get("phase") != expected_phase
        or expected_phase not in LAB_PHASES
        or not isinstance(result.get("succeeded"), bool)
        or not isinstance(summary, str)
        or len(summary.encode("utf-8")) > 16_384
        or not isinstance(report, str)
        or len(report.encode("utf-8")) > _MAX_BYTES
        or not isinstance(commands, list)
        or len(commands) > 32
        or not all(isinstance(command, str) and command.strip() for command in commands)
        or sum(len(cast(str, command).encode("utf-8")) for command in commands) > 24 * 1024
    ):
        raise LabProtocolError("lab result is invalid")
    if expected_phase == "lab_plan":
        if result["succeeded"] and not commands:
            raise LabProtocolError("successful lab plan has no commands")
    elif commands or (result["succeeded"] and not report.strip()):
        raise LabProtocolError("lab report is invalid")
    return result


def validate_dispatch(
    value: object, *, expected_report_id: str, expected_plan_digest: str
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LabProtocolError("lab dispatch has an invalid shape")
    record = cast(dict[str, object], value)
    state = record.get("state")
    keys = {
        "kind",
        "executionKey",
        "labHandle",
        "planDigest",
        "commandsDigest",
        "state",
    }
    if state == "dispatched":
        keys.add("commandId")
    if (
        set(record) != keys
        or record.get("kind") != LAB_DISPATCH_KIND
        or record.get("executionKey") != expected_report_id
        or record.get("planDigest") != expected_plan_digest
        or not isinstance(record.get("labHandle"), str)
        or not cast(str, record["labHandle"]).strip()
        or not isinstance(record.get("commandsDigest"), str)
        or _DIGEST.fullmatch(cast(str, record["commandsDigest"])) is None
        or state not in {"intent", "dispatched"}
        or (
            state == "dispatched"
            and (
                not isinstance(record.get("commandId"), str)
                or _COMMAND_ID.fullmatch(cast(str, record["commandId"])) is None
            )
        )
    ):
        raise LabProtocolError("lab dispatch is invalid")
    return record


def validate_chain(
    jobs: Sequence[object], results: Sequence[object], *, expected_job_ids: tuple[str, ...]
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    if not 1 <= len(jobs) <= 2 or len(results) != len(jobs) or len(expected_job_ids) != len(jobs):
        raise LabProtocolError("lab chain length is invalid")
    validated_jobs = tuple(
        validate_job(job, job_id) for job, job_id in zip(jobs, expected_job_ids, strict=True)
    )
    if tuple(cast(str, job["phase"]) for job in validated_jobs) != LAB_PHASES[: len(jobs)]:
        raise LabProtocolError("lab chain phase order is invalid")
    first = validated_jobs[0]
    for job in validated_jobs[1:]:
        for key in (
            "taskKey",
            "revisionDigest",
            "courseName",
            "courseShortname",
            "title",
            "intro",
            "attachments",
            "guestInputTransfer",
        ):
            if job[key] != first[key]:
                raise LabProtocolError("lab chain identity is invalid")
    validated_results = tuple(
        validate_result(result, cast(str, job["jobId"]), cast(str, job["phase"]))
        for job, result in zip(validated_jobs, results, strict=True)
    )
    if len(validated_jobs) == 2:
        plan_digest = canonical_digest(validated_results[0])
        context = cast(dict[str, object], validated_jobs[1]["context"])
        if context.get("planDigest") != plan_digest:
            raise LabProtocolError("lab report is not bound to its plan")
    return validated_jobs, validated_results


def build_provenance(
    jobs: Sequence[object],
    results: Sequence[object],
    *,
    selected_mode: str,
    specification_digest: str,
    barrier_ids: tuple[str, ...],
    terminal_status: str,
    dispatch: object | None = None,
) -> dict[str, object]:
    if selected_mode not in {"hybrid", "in_guest"} or _DIGEST.fullmatch(
        specification_digest
    ) is None:
        raise LabProtocolError("lab provenance authority is invalid")
    job_ids = tuple(
        cast(str, cast(dict[str, object], job).get("jobId")) for job in jobs
    )
    validated_jobs, validated_results = validate_chain(
        jobs, results, expected_job_ids=job_ids
    )
    if (
        not barrier_ids
        or any(_DIGEST.fullmatch(item) is None for item in barrier_ids)
        or len(set(barrier_ids)) != len(barrier_ids)
        or tuple(barrier_ids[: len(job_ids)]) != job_ids
        or terminal_status not in {"succeeded", "failed", "capacity_error", "dispatch_unknown"}
    ):
        raise LabProtocolError("lab provenance terminal state is invalid")
    dispatch_binding: dict[str, object] | None = None
    if dispatch is not None:
        report_id = barrier_ids[-1]
        plan_digest = canonical_digest(validated_results[0])
        record = validate_dispatch(
            dispatch, expected_report_id=report_id, expected_plan_digest=plan_digest
        )
        dispatch_binding = {
            "dispatchId": report_id,
            "dispatchDigest": canonical_digest(record),
            "state": record["state"],
        }
        if record["state"] == "dispatched":
            dispatch_binding["commandId"] = record["commandId"]
    if len(validated_jobs) == 2 and dispatch_binding is None:
        raise LabProtocolError("lab report provenance requires dispatch")
    provenance: dict[str, object] = {
        "kind": LAB_PROVENANCE_KIND,
        "selectedMode": selected_mode,
        "taskKey": validated_jobs[0]["taskKey"],
        "revisionDigest": validated_jobs[0]["revisionDigest"],
        "specificationDigest": specification_digest,
        "phases": [job["phase"] for job in validated_jobs],
        "jobIds": list(job_ids),
        "barrierIds": list(barrier_ids),
        "resultDigests": [canonical_digest(result) for result in validated_results],
        "terminalStatus": terminal_status,
        "dispatch": dispatch_binding,
    }
    validate_provenance(provenance)
    return provenance


def validate_provenance(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "selectedMode",
        "taskKey",
        "revisionDigest",
        "specificationDigest",
        "phases",
        "jobIds",
        "barrierIds",
        "resultDigests",
        "terminalStatus",
        "dispatch",
    }:
        raise LabProtocolError("lab provenance has an invalid shape")
    provenance = cast(dict[str, object], value)
    phases, job_ids, barrier_ids, digests = (
        provenance.get("phases"),
        provenance.get("jobIds"),
        provenance.get("barrierIds"),
        provenance.get("resultDigests"),
    )
    if (
        provenance.get("kind") != LAB_PROVENANCE_KIND
        or provenance.get("selectedMode") not in {"hybrid", "in_guest"}
        or not isinstance(provenance.get("taskKey"), str)
        or _TASK.fullmatch(cast(str, provenance["taskKey"])) is None
        or not isinstance(provenance.get("revisionDigest"), str)
        or _REVISION.fullmatch(cast(str, provenance["revisionDigest"])) is None
        or not isinstance(provenance.get("specificationDigest"), str)
        or _DIGEST.fullmatch(cast(str, provenance["specificationDigest"])) is None
        or not isinstance(phases, list)
        or tuple(phases) not in {("lab_plan",), LAB_PHASES}
        or not isinstance(job_ids, list)
        or len(job_ids) != len(phases)
        or not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in job_ids)
        or not isinstance(barrier_ids, list)
        or not len(job_ids) <= len(barrier_ids) <= 2
        or barrier_ids[: len(job_ids)] != job_ids
        or len(set(cast(list[str], barrier_ids))) != len(barrier_ids)
        or not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in barrier_ids)
        or not isinstance(digests, list)
        or len(digests) != len(job_ids)
        or not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in digests)
        or provenance.get("terminalStatus")
        not in {"succeeded", "failed", "capacity_error", "dispatch_unknown"}
    ):
        raise LabProtocolError("lab provenance is invalid")
    dispatch = provenance.get("dispatch")
    if dispatch is None:
        if len(job_ids) != 1 or len(barrier_ids) != 1:
            raise LabProtocolError("lab provenance dispatch binding is missing")
    elif (
        not isinstance(dispatch, dict)
        or set(dispatch) not in (
            {"dispatchId", "dispatchDigest", "state"},
            {"dispatchId", "dispatchDigest", "state", "commandId"},
        )
        or dispatch.get("dispatchId") != barrier_ids[-1]
        or not isinstance(dispatch.get("dispatchDigest"), str)
        or _DIGEST.fullmatch(cast(str, dispatch["dispatchDigest"])) is None
        or dispatch.get("state") not in {"intent", "dispatched"}
        or (
            dispatch.get("state") == "dispatched"
            and (
                set(dispatch) != {"dispatchId", "dispatchDigest", "state", "commandId"}
                or not isinstance(dispatch.get("commandId"), str)
                or _COMMAND_ID.fullmatch(cast(str, dispatch["commandId"])) is None
            )
        )
        or (dispatch.get("state") == "intent" and "commandId" in dispatch)
    ):
        raise LabProtocolError("lab provenance dispatch binding is invalid")
    dispatch_record = None if dispatch is None else cast(dict[str, object], dispatch)
    if dispatch_record is not None and len(barrier_ids) != 2:
        raise LabProtocolError("lab provenance dispatch barrier is invalid")
    terminal_status = provenance["terminalStatus"]
    if (
        (
            len(job_ids) == 2
            and (dispatch_record is None or dispatch_record.get("state") != "dispatched")
        )
        or (
            terminal_status == "succeeded"
            and (
                len(job_ids) != 2
                or dispatch_record is None
                or dispatch_record.get("state") != "dispatched"
            )
        )
        or (
            terminal_status == "dispatch_unknown"
            and (
                len(job_ids) != 1
                or dispatch_record is None
                or dispatch_record.get("state") != "intent"
            )
        )
    ):
        raise LabProtocolError("lab provenance terminal binding is invalid")
    return provenance


def _validate_attachments(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise LabProtocolError("lab job attachments are invalid")
    validated: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {
            "filename",
            "sizeBytes",
            "sha256",
            "path",
        }:
            raise LabProtocolError("lab job attachments are invalid")
        filename, size, digest, path = (
            item.get("filename"),
            item.get("sizeBytes"),
            item.get("sha256"),
            item.get("path"),
        )
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or path != f"inputs/{index:04d}-{filename}"
        ):
            raise LabProtocolError("lab job attachments are invalid")
        validated.append(cast(dict[str, object], item))
    return tuple(validated)


def _validate_transfer(value: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {"transferDigest", "guestPaths"}:
        raise LabProtocolError("lab input transfer is invalid")
    digest, paths = value.get("transferDigest"), value.get("guestPaths")
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or not isinstance(paths, list)
        or len(paths) > 32
    ):
        raise LabProtocolError("lab input transfer is invalid")
    root = f"C:\\ProgramData\\MoodleAutotask\\inputs\\{digest}\\"
    validated: list[str] = []
    for path in paths:
        if (
            not isinstance(path, str)
            or not path.startswith(root)
            or len(path.encode("utf-8")) > 512
            or not path.removeprefix(root)
            or any(character in path.removeprefix(root) for character in "/\\\x00")
        ):
            raise LabProtocolError("lab input transfer is invalid")
        validated.append(path)
    if len({item.casefold() for item in validated}) != len(validated):
        raise LabProtocolError("lab input transfer is invalid")
    return digest, tuple(validated)
