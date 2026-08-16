"""Pure validation for the central agent plan and result protocol."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import cast

CENTRAL_ROLES = ("central_planner", "central_executor", "central_reviewer")
CENTRAL_JOB_KIND = "moodle-agent-job-v2"
CENTRAL_RESULT_KIND = "moodle-agent-result-v2"
# Central results are embedded in subsequent immutable jobs.  Keep their
# envelope deliberately small so an accepted upstream result cannot produce a
# downstream job that exceeds the agent's maximum readable size.
MAX_CENTRAL_RESULT_BYTES = 256 * 1024
MAX_CENTRAL_ARTIFACT_PATH_BYTES = 240
MAX_CENTRAL_ARTIFACT_DEPTH = 8
MAX_CENTRAL_ARTIFACT_FILES = 64
MAX_CENTRAL_ARTIFACT_TOTAL_BYTES = 1_900_000
MAX_CENTRAL_PREPARED_INPUT_FILES = 128
MAX_CENTRAL_PREPARED_INPUT_BYTES = 512 * 1024 * 1024
MAX_CENTRAL_PREPARED_INPUT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class CentralProtocolError(RuntimeError):
    pass


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _central_text(value: object, name: str, maximum: int = 2_000_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise CentralProtocolError(f"central result {name} is invalid")
    return value


def safe_artifact_path(value: str) -> bool:
    if (
        not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or any(ord(character) < 32 for character in value)
        or len(value.encode("utf-8")) > MAX_CENTRAL_ARTIFACT_PATH_BYTES
    ):
        return False
    parts = value.split("/")
    return len(parts) <= MAX_CENTRAL_ARTIFACT_DEPTH and all(
        part and part not in {".", ".."} for part in parts
    )


_safe_artifact_path = safe_artifact_path


def validate_artifact_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"kind", "files", "totals"}:
        raise CentralProtocolError("artifact manifest is invalid")
    files, totals = value.get("files"), value.get("totals")
    if (
        value.get("kind") != "artifact-manifest-v1"
        or not isinstance(files, list)
        or not 1 <= len(files) <= MAX_CENTRAL_ARTIFACT_FILES
    ):
        raise CentralProtocolError("artifact manifest is invalid")
    if not isinstance(totals, dict) or set(totals) != {"files", "bytes"}:
        raise CentralProtocolError("artifact manifest is invalid")
    previous = b""
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise CentralProtocolError("artifact manifest is invalid")
        path, size, digest = item.get("path"), item.get("size"), item.get("sha256")
        if (
            not isinstance(path, str)
            or not _safe_artifact_path(path)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            raise CentralProtocolError("artifact manifest is invalid")
        encoded = path.encode("utf-8")
        normalized = unicodedata.normalize("NFC", path).casefold()
        if encoded <= previous or normalized in seen:
            raise CentralProtocolError("artifact manifest order is invalid")
        previous = encoded
        seen.add(normalized)
        total += size
    if total > MAX_CENTRAL_ARTIFACT_TOTAL_BYTES or totals != {
        "files": len(files),
        "bytes": total,
    }:
        raise CentralProtocolError("artifact manifest totals are invalid")
    return value


def central_plan(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "steps",
        "acceptanceCriteria",
        "expectedArtifacts",
    }:
        raise CentralProtocolError("central plan is invalid")
    steps, criteria, artifacts = (
        value["steps"],
        value["acceptanceCriteria"],
        value["expectedArtifacts"],
    )
    if (
        not isinstance(steps, list)
        or not 1 <= len(steps) <= 64
        or not all(isinstance(x, str) and x.strip() for x in steps)
    ):
        raise CentralProtocolError("central plan steps are invalid")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 64:
        raise CentralProtocolError("central plan criteria are invalid")
    ids: set[str] = set()
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) != {"id", "text"}:
            raise CentralProtocolError("central plan criteria are invalid")
        identifier = _central_text(criterion.get("id"), "criterion id", 256)
        _central_text(criterion.get("text"), "criterion text", 16384)
        if identifier in ids:
            raise CentralProtocolError("central plan criterion IDs are not unique")
        ids.add(identifier)
    seen: set[str] = set()
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_CENTRAL_ARTIFACT_FILES:
        raise CentralProtocolError("central expected artifacts are invalid")
    for item in artifacts:
        if not isinstance(item, str) or not _safe_artifact_path(item):
            raise CentralProtocolError("central expected artifact path is invalid")
        key = unicodedata.normalize("NFC", item).casefold()
        if key in seen:
            raise CentralProtocolError("central expected artifacts collide")
        seen.add(key)
    return value


def validate_central_result(result: dict[str, object], role: str) -> None:
    try:
        if len(canonical_json(result)) > MAX_CENTRAL_RESULT_BYTES:
            raise CentralProtocolError("central result exceeds serialized size budget")
    except (TypeError, ValueError) as error:
        raise CentralProtocolError("central result is invalid") from error
    common = {"kind", "jobId", "role", "succeeded", "summary", "reportMarkdown"}
    if not isinstance(result.get("succeeded"), bool):
        raise CentralProtocolError("central result success flag is invalid")
    _central_text(result.get("summary"), "summary", 16_384)
    report = result.get("reportMarkdown")
    if not isinstance(report, str) or len(report.encode("utf-8")) > 2_000_000:
        raise CentralProtocolError("central result report is invalid")
    if result["succeeded"] and (not report.strip() or report.strip() == "# Informe"):
        raise CentralProtocolError("central report has no evidence")
    if not result["succeeded"]:
        digest_key = {
            "central_planner": "plannerResultDigest",
            "central_executor": "executorResultDigest",
            "central_reviewer": "reviewerResultDigest",
        }[role]
        if set(result) != common | {digest_key}:
            raise CentralProtocolError("central failure shape is invalid")
        unsigned = {k: v for k, v in result.items() if k != digest_key}
        if result.get(digest_key) != canonical_digest(unsigned):
            raise CentralProtocolError("central failure digest is invalid")
        return
    if role == "central_planner":
        if set(result) != common | {"plan", "planDigest", "plannerResultDigest"}:
            raise CentralProtocolError("planner result shape is invalid")
        plan = central_plan(result["plan"])
        if result["planDigest"] != canonical_digest(plan):
            raise CentralProtocolError("planner plan digest is invalid")
        unsigned = {k: v for k, v in result.items() if k != "plannerResultDigest"}
        if result["plannerResultDigest"] != canonical_digest(unsigned):
            raise CentralProtocolError("planner result digest is invalid")
    elif role == "central_executor":
        fields = {
            "evidence",
            "artifactManifest",
            "artifactManifestDigest",
            "artifactBundleDigest",
            "bundleLocator",
            "executorResultDigest",
        }
        if set(result) != common | fields or not isinstance(result["evidence"], dict):
            raise CentralProtocolError("executor result shape is invalid")
        if not all(
            isinstance(k, str) and isinstance(v, str) and v.strip()
            for k, v in result["evidence"].items()
        ):
            raise CentralProtocolError("executor evidence is invalid")
        validate_artifact_manifest(result["artifactManifest"])
        if result["artifactManifestDigest"] != canonical_digest(result["artifactManifest"]):
            raise CentralProtocolError("artifact manifest digest is invalid")
        if (
            not isinstance(result["artifactBundleDigest"], str)
            or _DIGEST.fullmatch(result["artifactBundleDigest"]) is None
            or result["bundleLocator"] != f"bundles/{result['artifactBundleDigest']}.zip"
        ):
            raise CentralProtocolError("artifact bundle is invalid")
        unsigned = {k: v for k, v in result.items() if k != "executorResultDigest"}
        if result["executorResultDigest"] != canonical_digest(unsigned):
            raise CentralProtocolError("executor result digest is invalid")
    else:
        fields = {"accepted", "decisions", "findings", "dependencyDigests", "reviewerResultDigest"}
        if set(result) != common | fields or not isinstance(result["accepted"], bool):
            raise CentralProtocolError("reviewer result shape is invalid")
        if (
            not isinstance(result["decisions"], dict)
            or not result["decisions"]
            or not all(
                isinstance(k, str) and v in {"accepted", "rejected"}
                for k, v in result["decisions"].items()
            )
        ):
            raise CentralProtocolError("reviewer decisions are invalid")
        if (
            not isinstance(result["findings"], list)
            or len(result["findings"]) > 64
            or not all(isinstance(x, str) and len(x) <= 4096 for x in result["findings"])
        ):
            raise CentralProtocolError("reviewer findings are invalid")
        if not isinstance(result["dependencyDigests"], dict) or not all(
            isinstance(k, str) and isinstance(v, str) and _DIGEST.fullmatch(v)
            for k, v in result["dependencyDigests"].items()
        ):
            raise CentralProtocolError("reviewer dependency digests are invalid")
        if bool(result["accepted"]) != all(
            decision == "accepted" for decision in result["decisions"].values()
        ):
            raise CentralProtocolError("reviewer acceptance is incoherent")
        unsigned = {k: v for k, v in result.items() if k != "reviewerResultDigest"}
        if result["reviewerResultDigest"] != canonical_digest(unsigned):
            raise CentralProtocolError("reviewer result digest is invalid")


def validate_prepared_inputs(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or len(value) > MAX_CENTRAL_PREPARED_INPUT_FILES:
        raise CentralProtocolError("agent job attachments are invalid")
    result: list[dict[str, object]] = []
    total = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            raise CentralProtocolError("agent job attachment is invalid")
        attachment = cast(dict[str, object], item)
        if set(attachment) != {
            "attachmentKey",
            "filename",
            "sizeBytes",
            "sha256",
            "path",
        } or not _identity(attachment.get("attachmentKey"), "moodle-attachment-v1:"):
            raise CentralProtocolError("central prepared input is invalid")
        filename = attachment.get("filename")
        size = attachment.get("sizeBytes")
        digest = attachment.get("sha256")
        path = attachment.get("path")
        if (
            not _safe_filename(filename)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_CENTRAL_PREPARED_INPUT_BYTES
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or path != f"inputs/{index:04d}-{filename}"
        ):
            raise CentralProtocolError("agent job attachment metadata is invalid")
        total += size
        result.append(attachment)
    if total > MAX_CENTRAL_PREPARED_INPUT_TOTAL_BYTES:
        raise CentralProtocolError("agent job attachments exceed aggregate size budget")
    return tuple(result)


def validate_declared_prepared_input_envelope(value: object) -> int:
    """Validate central attachment declarations before any artifact preparation."""
    if not isinstance(value, tuple) or len(value) > MAX_CENTRAL_PREPARED_INPUT_FILES:
        raise CentralProtocolError("central input envelope is invalid")
    total = 0
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not _safe_filename(item[0])
            or not isinstance(item[1], int)
            or isinstance(item[1], bool)
            or item[1] < 0
            or item[1] > MAX_CENTRAL_PREPARED_INPUT_BYTES
        ):
            raise CentralProtocolError("central input envelope is invalid")
        total += item[1]
    if total > MAX_CENTRAL_PREPARED_INPUT_TOTAL_BYTES:
        raise CentralProtocolError("central input envelope exceeds aggregate size budget")
    return total


def validate_central_job(job: dict[str, object], expected_job_id: str) -> dict[str, object]:
    role = job.get("role")
    required = {
        "kind",
        "jobId",
        "role",
        "eventId",
        "taskKey",
        "revisionDigest",
        "selectedMode",
        "specificationDigest",
        "preparedInputManifestDigest",
        "assignmentSnapshot",
        "preparedInputs",
        "dependencies",
    }
    optional = {"plan", "executorResult"}
    if not set(job).issubset(required | optional) or not required.issubset(job):
        raise CentralProtocolError("central job shape is invalid")
    if (
        job.get("kind") != CENTRAL_JOB_KIND
        or job.get("jobId") != expected_job_id
        or role not in CENTRAL_ROLES
        or not _identity(job.get("eventId"), "moodle-notification-event-v1:")
        or not _identity(job.get("taskKey"), "moodle-task-v1:")
        or not _identity(job.get("revisionDigest"), "moodle-assignment-v1:")
        or job.get("selectedMode") != "central"
    ):
        raise CentralProtocolError("central job identity is invalid")
    for field in ("specificationDigest", "preparedInputManifestDigest"):
        if not isinstance(job.get(field), str) or _DIGEST.fullmatch(cast(str, job[field])) is None:
            raise CentralProtocolError("central job digest is invalid")
    snapshot = job.get("assignmentSnapshot")
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"courseName", "courseShortname", "title", "intro"}
        or not all(isinstance(value, str) for value in snapshot.values())
    ):
        raise CentralProtocolError("central snapshot is invalid")
    prepared_inputs = validate_prepared_inputs(job["preparedInputs"])
    prepared_manifest = [
        {
            "attachmentKey": item["attachmentKey"],
            "filename": item["filename"],
            "sizeBytes": item["sizeBytes"],
            "sha256": item["sha256"],
            "path": item["path"],
        }
        for item in prepared_inputs
    ]
    if canonical_digest(prepared_manifest) != job["preparedInputManifestDigest"]:
        raise CentralProtocolError("central prepared input manifest is invalid")
    dependencies = job.get("dependencies")
    if not isinstance(dependencies, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or (key.endswith("Digest") and _DIGEST.fullmatch(value) is None)
        for key, value in dependencies.items()
    ):
        raise CentralProtocolError("central dependencies are invalid")
    body = {key: value for key, value in job.items() if key != "jobId"}
    if canonical_digest(body) != job["jobId"]:
        raise CentralProtocolError("central job digest is invalid")
    expected_dependencies = {
        "central_planner": set(),
        "central_executor": {"plannerJobId", "planDigest", "plannerResultDigest"},
        "central_reviewer": {
            "plannerJobId",
            "planDigest",
            "plannerResultDigest",
            "executorJobId",
            "executorResultDigest",
            "artifactManifestDigest",
            "artifactBundleDigest",
        },
    }
    if set(dependencies) != expected_dependencies[role]:
        raise CentralProtocolError("central dependency chain is invalid")
    if role == "central_planner" and ("plan" in job or "executorResult" in job):
        raise CentralProtocolError("planner has dependencies")
    if role == "central_executor" and (
        not isinstance(job.get("plan"), dict) or "executorResult" in job
    ):
        raise CentralProtocolError("executor plan is invalid")
    if role == "central_reviewer" and (
        not isinstance(job.get("plan"), dict) or not isinstance(job.get("executorResult"), dict)
    ):
        raise CentralProtocolError("reviewer dependencies are invalid")
    return job


def central_model_schema(job: dict[str, object]) -> dict[str, object]:
    role = cast(str, job["role"])
    if role not in CENTRAL_ROLES:
        raise CentralProtocolError("central model job context is invalid")
    common: dict[str, dict[str, object]] = {
        "succeeded": {"type": "boolean"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 16384},
        "reportMarkdown": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CENTRAL_RESULT_BYTES,
        },
    }
    if role == "central_planner":
        success: dict[str, dict[str, object]] = {
            "plan": {
                "type": "object",
                "additionalProperties": False,
                "required": ["steps", "acceptanceCriteria", "expectedArtifacts"],
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "acceptanceCriteria": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["id", "text"],
                            "properties": {
                                "id": {"type": "string", "minLength": 1},
                                "text": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    "expectedArtifacts": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_CENTRAL_ARTIFACT_FILES,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            }
        }
    elif role == "central_executor":
        plan = central_plan(job.get("plan"))
        criterion_ids = [
            cast(str, item["id"])
            for item in cast(list[dict[str, object]], plan["acceptanceCriteria"])
        ]
        success = {
            "evidence": {
                "type": "object",
                "additionalProperties": False,
                "required": criterion_ids,
                "properties": {
                    criterion_id: {"type": "string", "minLength": 1}
                    for criterion_id in criterion_ids
                },
            }
        }
    else:
        plan = central_plan(job.get("plan"))
        criterion_ids = [
            cast(str, item["id"])
            for item in cast(list[dict[str, object]], plan["acceptanceCriteria"])
        ]
        success = {
            "accepted": {"type": "boolean"},
            "decisions": {
                "type": "object",
                "additionalProperties": False,
                "required": criterion_ids,
                "properties": {
                    criterion_id: {"type": "string", "enum": ["accepted", "rejected"]}
                    for criterion_id in criterion_ids
                },
            },
            "findings": {
                "type": "array",
                "maxItems": 64,
                "items": {"type": "string", "maxLength": 4096},
            },
        }
    properties = common | success
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def validate_central_model_result(value: object, role: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CentralProtocolError("Codex returned invalid central result")
    try:
        if len(canonical_json(value)) > MAX_CENTRAL_RESULT_BYTES:
            raise CentralProtocolError("central model result exceeds serialized size budget")
    except (TypeError, ValueError) as error:
        raise CentralProtocolError("Codex returned invalid central result") from error
    if (
        not isinstance(value.get("succeeded"), bool)
        or not isinstance(value.get("summary"), str)
        or not isinstance(value.get("reportMarkdown"), str)
    ):
        raise CentralProtocolError("Codex returned invalid central result")
    common = {"succeeded", "summary", "reportMarkdown"}
    role_fields = {
        "central_planner": {"plan"},
        "central_executor": {"evidence"},
        "central_reviewer": {"accepted", "decisions", "findings"},
    }[role]
    if not value["succeeded"]:
        if frozenset(value) not in {frozenset(common), frozenset(common | role_fields)}:
            raise CentralProtocolError("Codex returned invalid central failure")
        return cast(dict[str, object], value)
    required = common | role_fields
    if set(value) != required:
        raise CentralProtocolError("Codex returned invalid central result")
    return cast(dict[str, object], value)


def validate_central_model_result_binding(
    job: dict[str, object], model: dict[str, object], wrapped_result: dict[str, object]
) -> None:
    """Require a model response and its immutable wrapper to describe the same work."""
    role = job.get("role")
    if role not in CENTRAL_ROLES:
        raise CentralProtocolError("central model job context is invalid")
    validate_central_model_result(model, role)
    validate_central_model_context(job, model)
    validate_central_result_context(job, wrapped_result)
    for field in ("succeeded", "summary", "reportMarkdown"):
        if model.get(field) != wrapped_result.get(field):
            raise CentralProtocolError("central model result binding is invalid")
    if not model["succeeded"]:
        return
    if role == "central_planner":
        plan = model.get("plan")
        if (
            not isinstance(plan, dict)
            or canonical_json(plan) != canonical_json(wrapped_result.get("plan"))
            or wrapped_result.get("planDigest") != canonical_digest(plan)
        ):
            raise CentralProtocolError("planner model result binding is invalid")
        return
    if role == "central_executor":
        if canonical_json(model.get("evidence")) != canonical_json(wrapped_result.get("evidence")):
            raise CentralProtocolError("executor model result binding is invalid")
        return
    for field in ("accepted", "decisions", "findings"):
        if canonical_json(model.get(field)) != canonical_json(wrapped_result.get(field)):
            raise CentralProtocolError("reviewer model result binding is invalid")


def validate_central_model_context(job: dict[str, object], model: dict[str, object]) -> None:
    role = job.get("role")
    succeeded = model.get("succeeded")
    summary = model.get("summary")
    report = model.get("reportMarkdown")
    if (
        role not in CENTRAL_ROLES
        or not isinstance(succeeded, bool)
        or not isinstance(summary, str)
        or not summary.strip()
        or len(summary.encode("utf-8")) > 16_384
        or not isinstance(report, str)
        or len(report.encode("utf-8")) > MAX_CENTRAL_RESULT_BYTES
        or (succeeded and (not report.strip() or report.strip() == "# Informe"))
    ):
        raise CentralProtocolError("central model result is invalid")
    if not succeeded:
        return
    if role == "central_planner":
        central_plan(model.get("plan"))
        return
    plan = job.get("plan")
    if not isinstance(plan, dict):
        raise CentralProtocolError("central model job context is invalid")
    central_plan(plan)
    criteria = {
        criterion["id"] for criterion in cast(list[dict[str, object]], plan["acceptanceCriteria"])
    }
    if role == "central_executor":
        evidence = model.get("evidence")
        if (
            not isinstance(evidence, dict)
            or set(evidence) != criteria
            or not all(
                isinstance(key, str) and isinstance(value, str) and value.strip()
                for key, value in evidence.items()
            )
        ):
            raise CentralProtocolError("executor model evidence is invalid")
        return
    decisions = model.get("decisions")
    findings = model.get("findings")
    accepted = model.get("accepted")
    if (
        not isinstance(accepted, bool)
        or not isinstance(decisions, dict)
        or set(decisions) != criteria
        or not all(
            isinstance(key, str) and value in {"accepted", "rejected"}
            for key, value in decisions.items()
        )
        or bool(accepted) != all(value == "accepted" for value in decisions.values())
        or not isinstance(findings, list)
        or len(findings) > 64
        or not all(isinstance(item, str) and len(item.encode("utf-8")) <= 4096 for item in findings)
    ):
        raise CentralProtocolError("reviewer model result is invalid")


def validate_central_result_context(job: dict[str, object], result: dict[str, object]) -> None:
    role = job.get("role")
    if (
        role not in CENTRAL_ROLES
        or result.get("kind") != CENTRAL_RESULT_KIND
        or result.get("jobId") != job.get("jobId")
        or result.get("role") != role
    ):
        raise CentralProtocolError("central result identity does not match job")
    validate_central_result(result, role)
    if not result["succeeded"]:
        return
    if role == "central_planner":
        return
    plan = job.get("plan")
    dependencies = job.get("dependencies")
    if not isinstance(plan, dict) or not isinstance(dependencies, dict):
        raise CentralProtocolError("central result job context is invalid")
    central_plan(plan)
    plan_digest = canonical_digest(plan)
    if dependencies.get("planDigest") != plan_digest:
        raise CentralProtocolError("central result plan dependency is invalid")
    criteria = {
        criterion["id"] for criterion in cast(list[dict[str, object]], plan["acceptanceCriteria"])
    }
    if role == "central_executor":
        evidence = result.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != criteria:
            raise CentralProtocolError("executor criterion coverage is invalid")
        return
    decisions = result.get("decisions")
    expected_digests = {key: value for key, value in dependencies.items() if key.endswith("Digest")}
    if not isinstance(decisions, dict) or set(decisions) != criteria:
        raise CentralProtocolError("reviewer criterion coverage is invalid")
    if result.get("dependencyDigests") != expected_digests:
        raise CentralProtocolError("reviewer dependency binding is invalid")
    executor_result = job.get("executorResult")
    if not isinstance(executor_result, dict):
        raise CentralProtocolError("reviewer executor context is invalid")
    validate_central_result(executor_result, "central_executor")
    if (
        executor_result.get("jobId") != dependencies.get("executorJobId")
        or executor_result.get("executorResultDigest") != dependencies.get("executorResultDigest")
        or executor_result.get("artifactManifestDigest")
        != dependencies.get("artifactManifestDigest")
        or executor_result.get("artifactBundleDigest") != dependencies.get("artifactBundleDigest")
    ):
        raise CentralProtocolError("reviewer executor dependency is invalid")


def validate_central_job_chain(
    value: object,
    *,
    expected_job_ids: tuple[str, str, str],
    expected_event_id: str,
    expected_task_key: str,
    expected_revision_digest: str,
) -> tuple[dict[str, object], ...]:
    if (
        not isinstance(expected_job_ids, tuple)
        or len(expected_job_ids) != len(CENTRAL_ROLES)
        or not all(
            isinstance(job_id, str) and _DIGEST.fullmatch(job_id) for job_id in expected_job_ids
        )
        or not _identity(expected_event_id, "moodle-notification-event-v1:")
        or not _identity(expected_task_key, "moodle-task-v1:")
        or not _identity(expected_revision_digest, "moodle-assignment-v1:")
    ):
        raise CentralProtocolError("central job chain expectation is invalid")
    if not isinstance(value, list) or len(value) != len(CENTRAL_ROLES):
        raise CentralProtocolError("central job chain is invalid")
    jobs: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            raise CentralProtocolError("central job chain is invalid")
        job = cast(dict[str, object], item)
        job_id = job.get("jobId")
        if not isinstance(job_id, str):
            raise CentralProtocolError("central job chain is invalid")
        jobs.append(validate_central_job(job, job_id))
    if tuple(job.get("role") for job in jobs) != CENTRAL_ROLES:
        raise CentralProtocolError("central job chain role order is invalid")
    planner, executor, reviewer = jobs
    if tuple(job["jobId"] for job in jobs) != expected_job_ids:
        raise CentralProtocolError("central job chain job binding is invalid")
    if (
        planner["eventId"] != expected_event_id
        or planner["taskKey"] != expected_task_key
        or planner["revisionDigest"] != expected_revision_digest
    ):
        raise CentralProtocolError("central job chain identity binding is invalid")
    for field in (
        "eventId",
        "taskKey",
        "revisionDigest",
        "specificationDigest",
        "preparedInputManifestDigest",
        "assignmentSnapshot",
        "preparedInputs",
    ):
        if any(job[field] != planner[field] for job in (executor, reviewer)):
            raise CentralProtocolError("central job chain identity is invalid")
    planner_id = planner["jobId"]
    executor_id = executor["jobId"]
    executor_plan = executor.get("plan")
    reviewer_plan = reviewer.get("plan")
    if not isinstance(executor_plan, dict) or not isinstance(reviewer_plan, dict):
        raise CentralProtocolError("central job chain plan is invalid")
    central_plan(executor_plan)
    central_plan(reviewer_plan)
    if canonical_json(executor_plan) != canonical_json(reviewer_plan):
        raise CentralProtocolError("central job chain plan is invalid")
    plan_digest = canonical_digest(executor_plan)
    executor_dependencies = cast(dict[str, str], executor["dependencies"])
    reviewer_dependencies = cast(dict[str, str], reviewer["dependencies"])
    if (
        executor_dependencies.get("plannerJobId") != planner_id
        or executor_dependencies.get("planDigest") != plan_digest
        or reviewer_dependencies.get("plannerResultDigest")
        != executor_dependencies.get("plannerResultDigest")
        or reviewer_dependencies.get("plannerJobId") != planner_id
        or reviewer_dependencies.get("planDigest") != plan_digest
        or reviewer_dependencies.get("executorJobId") != executor_id
    ):
        raise CentralProtocolError("central job chain dependencies are invalid")
    executor_result = reviewer.get("executorResult")
    if not isinstance(executor_result, dict):
        raise CentralProtocolError("central job chain executor result is invalid")
    validate_central_result_context(executor, executor_result)
    if (
        executor_result.get("jobId") != executor_id
        or executor_result.get("executorResultDigest")
        != reviewer_dependencies.get("executorResultDigest")
        or executor_result.get("artifactManifestDigest")
        != reviewer_dependencies.get("artifactManifestDigest")
        or executor_result.get("artifactBundleDigest")
        != reviewer_dependencies.get("artifactBundleDigest")
    ):
        raise CentralProtocolError("central job chain executor result is invalid")
    return tuple(jobs)


def validate_central_result_chain(
    jobs_value: object,
    results_value: object,
    *,
    expected_job_ids: tuple[str, str, str],
    expected_event_id: str,
    expected_task_key: str,
    expected_revision_digest: str,
) -> tuple[dict[str, object], ...]:
    planner, executor, reviewer = validate_central_job_chain(
        jobs_value,
        expected_job_ids=expected_job_ids,
        expected_event_id=expected_event_id,
        expected_task_key=expected_task_key,
        expected_revision_digest=expected_revision_digest,
    )
    if not isinstance(results_value, list) or len(results_value) != len(CENTRAL_ROLES):
        raise CentralProtocolError("central result chain is invalid")
    results: list[dict[str, object]] = []
    for role, job, item in zip(
        CENTRAL_ROLES, (planner, executor, reviewer), results_value, strict=True
    ):
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            raise CentralProtocolError("central result chain is invalid")
        result = cast(dict[str, object], item)
        if result.get("jobId") != job["jobId"] or result.get("role") != role:
            raise CentralProtocolError("central result chain identity is invalid")
        validate_central_result(result, role)
        validate_central_result_context(job, result)
        if not result["succeeded"]:
            raise CentralProtocolError("central result chain is incomplete")
        results.append(result)
    planner_result, executor_result, reviewer_result = results
    executor_dependencies = cast(dict[str, str], executor["dependencies"])
    reviewer_dependencies = cast(dict[str, str], reviewer["dependencies"])
    if (
        executor_dependencies.get("plannerResultDigest")
        != planner_result.get("plannerResultDigest")
        or reviewer_dependencies.get("plannerResultDigest")
        != planner_result.get("plannerResultDigest")
        or reviewer_dependencies.get("plannerResultDigest")
        != executor_dependencies.get("plannerResultDigest")
        or not isinstance(planner_result.get("plan"), dict)
        or canonical_json(executor["plan"]) != canonical_json(planner_result["plan"])
        or executor_dependencies.get("planDigest") != planner_result.get("planDigest")
    ):
        raise CentralProtocolError("central result chain planner binding is invalid")
    if (
        not isinstance(reviewer.get("executorResult"), dict)
        or canonical_json(reviewer["executorResult"]) != canonical_json(executor_result)
        or reviewer_dependencies.get("executorResultDigest")
        != executor_result.get("executorResultDigest")
        or reviewer_dependencies.get("artifactManifestDigest")
        != executor_result.get("artifactManifestDigest")
        or reviewer_dependencies.get("artifactBundleDigest")
        != executor_result.get("artifactBundleDigest")
    ):
        raise CentralProtocolError("central result chain executor binding is invalid")
    decisions = reviewer_result.get("decisions")
    if (
        reviewer_result.get("accepted") is not True
        or not isinstance(decisions, dict)
        or any(value != "accepted" for value in decisions.values())
    ):
        raise CentralProtocolError("central result chain reviewer acceptance is invalid")
    if reviewer_result.get("dependencyDigests") != {
        key: value for key, value in reviewer_dependencies.items() if key.endswith("Digest")
    }:
        raise CentralProtocolError("central result chain reviewer dependencies are invalid")
    return tuple(results)


def validate_central_terminal_chain(
    jobs_value: object,
    results_value: object,
    *,
    expected_job_ids: tuple[str, ...],
    expected_event_id: str,
    expected_task_key: str,
    expected_revision_digest: str,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    """Validate one exact failed/rejected central role prefix without synthesis."""
    if (
        not isinstance(expected_job_ids, tuple)
        or not 1 <= len(expected_job_ids) <= len(CENTRAL_ROLES)
        or not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in expected_job_ids)
        or len(set(expected_job_ids)) != len(expected_job_ids)
        or not _identity(expected_event_id, "moodle-notification-event-v1:")
        or not _identity(expected_task_key, "moodle-task-v1:")
        or not _identity(expected_revision_digest, "moodle-assignment-v1:")
        or not isinstance(jobs_value, list)
        or not isinstance(results_value, list)
        or len(jobs_value) != len(expected_job_ids)
        or len(results_value) != len(expected_job_ids)
    ):
        raise CentralProtocolError("central terminal chain is invalid")
    jobs: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for index, (item, result_item, job_id, role) in enumerate(
        zip(
            jobs_value,
            results_value,
            expected_job_ids,
            CENTRAL_ROLES[: len(expected_job_ids)],
            strict=True,
        )
    ):
        if not isinstance(item, dict) or not isinstance(result_item, dict):
            raise CentralProtocolError("central terminal chain is invalid")
        job = validate_central_job(item, job_id)
        result = cast(dict[str, object], result_item)
        if job.get("role") != role:
            raise CentralProtocolError("central terminal chain role order is invalid")
        validate_central_result_context(job, result)
        jobs.append(job)
        results.append(result)
        if index == 0:
            if (
                job["eventId"] != expected_event_id
                or job["taskKey"] != expected_task_key
                or job["revisionDigest"] != expected_revision_digest
            ):
                raise CentralProtocolError("central terminal chain identity is invalid")
            continue
        planner, planner_result = jobs[0], results[0]
        if not planner_result["succeeded"]:
            raise CentralProtocolError("central terminal chain has extra role")
        for field in (
            "eventId",
            "taskKey",
            "revisionDigest",
            "specificationDigest",
            "preparedInputManifestDigest",
            "assignmentSnapshot",
            "preparedInputs",
        ):
            if job[field] != planner[field]:
                raise CentralProtocolError("central terminal chain identity is invalid")
        dependencies = cast(dict[str, str], job["dependencies"])
        if (
            dependencies.get("plannerJobId") != planner["jobId"]
            or dependencies.get("plannerResultDigest") != planner_result.get("plannerResultDigest")
            or dependencies.get("planDigest") != planner_result.get("planDigest")
        ):
            raise CentralProtocolError("central terminal chain planner binding is invalid")
        if index == 2:
            executor, executor_result = jobs[1], results[1]
            if not executor_result["succeeded"]:
                raise CentralProtocolError("central terminal chain has extra role")
            if (
                dependencies.get("executorJobId") != executor["jobId"]
                or dependencies.get("executorResultDigest")
                != executor_result.get("executorResultDigest")
                or dependencies.get("artifactManifestDigest")
                != executor_result.get("artifactManifestDigest")
                or dependencies.get("artifactBundleDigest")
                != executor_result.get("artifactBundleDigest")
                or job.get("executorResult") != executor_result
            ):
                raise CentralProtocolError("central terminal chain executor binding is invalid")
    terminal = results[-1]
    if terminal["succeeded"] and not (len(results) == 3 and terminal.get("accepted") is False):
        raise CentralProtocolError("central terminal chain is not terminal")
    return tuple(jobs), tuple(results)


def validate_central_job_prefix(
    value: object,
    *,
    expected_job_ids: tuple[str, ...],
    expected_event_id: str,
    expected_task_key: str,
    expected_revision_digest: str,
) -> tuple[dict[str, object], ...]:
    """Validate the durable ordered prefix when results were already reclaimed."""
    if (
        not isinstance(value, list)
        or not 1 <= len(expected_job_ids) <= len(CENTRAL_ROLES)
        or len(value) != len(expected_job_ids)
    ):
        raise CentralProtocolError("central job prefix is invalid")
    jobs: list[dict[str, object]] = []
    for item, job_id, role in zip(
        value, expected_job_ids, CENTRAL_ROLES[: len(expected_job_ids)], strict=True
    ):
        if not isinstance(item, dict) or not isinstance(job_id, str):
            raise CentralProtocolError("central job prefix is invalid")
        job = validate_central_job(item, job_id)
        if job["role"] != role:
            raise CentralProtocolError("central job prefix role order is invalid")
        jobs.append(job)
    first = jobs[0]
    if (
        first["eventId"] != expected_event_id
        or first["taskKey"] != expected_task_key
        or first["revisionDigest"] != expected_revision_digest
    ):
        raise CentralProtocolError("central job prefix identity is invalid")
    for job in jobs[1:]:
        for field in (
            "eventId",
            "taskKey",
            "revisionDigest",
            "specificationDigest",
            "preparedInputManifestDigest",
            "assignmentSnapshot",
            "preparedInputs",
        ):
            if job[field] != first[field]:
                raise CentralProtocolError("central job prefix identity is invalid")
    return tuple(jobs)


def terminal_provenance(
    jobs_value: object,
    results_value: object,
    *,
    terminal_role: str | None = None,
    terminal_status: str | None = None,
) -> dict[str, object]:
    """Construct the sole v3 failure/rejection provenance from durable records."""
    if not isinstance(jobs_value, list) or not jobs_value or not isinstance(jobs_value[0], dict):
        raise CentralProtocolError("central terminal provenance is invalid")
    first = cast(dict[str, object], jobs_value[0])
    ids = tuple(cast(str, item.get("jobId")) for item in jobs_value if isinstance(item, dict))
    if len(ids) != len(jobs_value):
        raise CentralProtocolError("central terminal provenance is invalid")
    if terminal_status == "budget_error":
        if (
            len(ids) not in {1, 2}
            or not isinstance(jobs_value, list)
            or not isinstance(results_value, list)
        ):
            raise CentralProtocolError("central terminal provenance is invalid")
        jobs = tuple(
            validate_central_job(item, job_id)
            for item, job_id in zip(jobs_value, ids, strict=True)
        )
        results = tuple(cast(dict[str, object], item) for item in results_value)
        if (
            len(results) != len(jobs)
            or any(job["role"] != CENTRAL_ROLES[index] for index, job in enumerate(jobs))
            or any(not result.get("succeeded") for result in results)
        ):
            raise CentralProtocolError("central terminal provenance is invalid")
        for job, result in zip(jobs, results, strict=True):
            validate_central_result_context(job, result)
        planner, planner_result = jobs[0], results[0]
        if (
            planner["eventId"] != first.get("eventId")
            or planner["taskKey"] != first.get("taskKey")
            or planner["revisionDigest"] != first.get("revisionDigest")
            or not isinstance(planner_result.get("plan"), dict)
        ):
            raise CentralProtocolError("central terminal provenance is invalid")
        if len(jobs) == 2:
            executor, executor_result = jobs[1], results[1]
            for field in (
                "eventId",
                "taskKey",
                "revisionDigest",
                "specificationDigest",
                "preparedInputManifestDigest",
                "assignmentSnapshot",
                "preparedInputs",
            ):
                if executor[field] != planner[field]:
                    raise CentralProtocolError("central terminal provenance is invalid")
            dependencies = cast(dict[str, str], executor["dependencies"])
            if (
                dependencies.get("plannerJobId") != planner["jobId"]
                or dependencies.get("plannerResultDigest")
                != planner_result.get("plannerResultDigest")
                or dependencies.get("planDigest") != planner_result.get("planDigest")
                or canonical_json(executor["plan"]) != canonical_json(planner_result["plan"])
                or not executor_result.get("succeeded")
            ):
                raise CentralProtocolError("central terminal provenance is invalid")
        if terminal_role is None:
            terminal_role = CENTRAL_ROLES[len(jobs)]
        if terminal_role != CENTRAL_ROLES[len(jobs)]:
            raise CentralProtocolError("central terminal provenance is invalid")
    else:
        jobs, results = validate_central_terminal_chain(
            jobs_value,
            results_value,
            expected_job_ids=ids,
            expected_event_id=cast(str, first.get("eventId")),
            expected_task_key=cast(str, first.get("taskKey")),
            expected_revision_digest=cast(str, first.get("revisionDigest")),
        )
    terminal = results[-1]
    if terminal_role is None:
        terminal_role = cast(str, terminal["role"])
    if terminal_status is None:
        terminal_status = "rejected" if terminal.get("accepted") is False else "failed"
    value: dict[str, object] = {
        "kind": "moodle-central-provenance-v3",
        "selectedMode": "central",
        "eventId": jobs[0]["eventId"],
        "taskKey": jobs[0]["taskKey"],
        "revisionDigest": jobs[0]["revisionDigest"],
        "specificationDigest": jobs[0]["specificationDigest"],
        "preparedInputManifestDigest": jobs[0]["preparedInputManifestDigest"],
        "roles": [job["role"] for job in jobs],
        "jobIds": [job["jobId"] for job in jobs],
        "terminalRole": terminal_role,
        "terminalStatus": terminal_status,
        "resultDigests": [
            result[
                {
                    "central_planner": "plannerResultDigest",
                    "central_executor": "executorResultDigest",
                    "central_reviewer": "reviewerResultDigest",
                }[cast(str, result["role"])]
            ]
            for result in results
        ],
    }
    if len(results) >= 2 and results[1]["succeeded"]:
        value["artifactManifest"] = results[1]["artifactManifest"]
        value["artifactManifestDigest"] = results[1]["artifactManifestDigest"]
        value["artifactBundleDigest"] = results[1]["artifactBundleDigest"]
        value["bundleLocator"] = results[1]["bundleLocator"]
    validate_terminal_provenance(value)
    return value


def validate_terminal_provenance(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CentralProtocolError("central terminal provenance is invalid")
    required = {
        "kind",
        "selectedMode",
        "eventId",
        "taskKey",
        "revisionDigest",
        "specificationDigest",
        "preparedInputManifestDigest",
        "roles",
        "jobIds",
        "terminalRole",
        "terminalStatus",
        "resultDigests",
    }
    optional = {
        "artifactManifest",
        "artifactManifestDigest",
        "artifactBundleDigest",
        "bundleLocator",
    }
    if (
        set(value) != required
        and set(value) != required | optional
        or value.get("kind") != "moodle-central-provenance-v3"
    ):
        raise CentralProtocolError("central terminal provenance is invalid")
    roles, ids, digests = value.get("roles"), value.get("jobIds"), value.get("resultDigests")
    terminal_role, terminal_status = value.get("terminalRole"), value.get("terminalStatus")
    if (
        value.get("selectedMode") != "central"
        or not _identity(value.get("eventId"), "moodle-notification-event-v1:")
        or not _identity(value.get("taskKey"), "moodle-task-v1:")
        or not _identity(value.get("revisionDigest"), "moodle-assignment-v1:")
        or not isinstance(value.get("specificationDigest"), str)
        or _DIGEST.fullmatch(cast(str, value.get("specificationDigest"))) is None
        or not isinstance(value.get("preparedInputManifestDigest"), str)
        or _DIGEST.fullmatch(cast(str, value.get("preparedInputManifestDigest"))) is None
        or not isinstance(roles, list)
        or not isinstance(ids, list)
        or not isinstance(digests, list)
        or not 1 <= len(roles) <= 3
        or len(ids) != len(roles)
        or len(digests) != len(roles)
        or roles != list(CENTRAL_ROLES[: len(roles)])
        or not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in ids + digests)
        or len(set(ids)) != len(ids)
        or not isinstance(terminal_role, str)
        or terminal_status not in {"failed", "rejected", "budget_error"}
        or (
            terminal_status == "rejected"
            and (roles != list(CENTRAL_ROLES) or terminal_role != roles[-1])
        )
        or (terminal_status == "failed" and terminal_role != roles[-1])
        or (
            terminal_status == "budget_error"
            and (len(roles) >= len(CENTRAL_ROLES) or terminal_role != CENTRAL_ROLES[len(roles)])
        )
    ):
        raise CentralProtocolError("central terminal provenance is invalid")
    has_bundle = set(value) == required | optional
    requires_bundle = len(roles) == 3 or (terminal_status == "budget_error" and len(roles) == 2)
    if has_bundle:
        validate_artifact_manifest(value.get("artifactManifest"))
        if (
            value.get("artifactManifestDigest") != canonical_digest(value["artifactManifest"])
            or not isinstance(value.get("artifactBundleDigest"), str)
            or _DIGEST.fullmatch(cast(str, value.get("artifactBundleDigest"))) is None
            or value.get("bundleLocator") != f"bundles/{value['artifactBundleDigest']}.zip"
        ):
            raise CentralProtocolError("central terminal provenance is invalid")
    elif requires_bundle:
        raise CentralProtocolError("central terminal provenance is invalid")
    if has_bundle != requires_bundle:
        raise CentralProtocolError("central terminal provenance is invalid")
    return value


def central_workspace_contract(job: dict[str, object]) -> dict[str, object]:
    job_id = job.get("jobId")
    if not isinstance(job_id, str):
        raise CentralProtocolError("central workspace contract is invalid")
    validated = validate_central_job(job, job_id)
    role = cast(str, validated["role"])
    prepared_inputs = validate_prepared_inputs(validated["preparedInputs"])
    root_entries = ["inputs", "last-message.json", "result-schema.json"]
    output_expectations: list[str] = []
    if role == "central_executor":
        plan = central_plan(validated["plan"])
        output_expectations = cast(list[str], plan["expectedArtifacts"])
        root_entries.append("outputs")
    model_required = {
        "central_planner": ["succeeded", "summary", "reportMarkdown", "plan"],
        "central_executor": ["succeeded", "summary", "reportMarkdown", "evidence"],
        "central_reviewer": [
            "succeeded",
            "summary",
            "reportMarkdown",
            "accepted",
            "decisions",
            "findings",
        ],
    }[role]
    return {
        "kind": "central-workspace-contract-v1",
        "jobId": job_id,
        "role": role,
        "rootEntries": sorted(root_entries),
        "preparedInputs": [dict(item) for item in prepared_inputs],
        "resultSchemaJson": canonical_json(central_model_schema(validated)).decode("utf-8"),
        "modelContract": {
            "role": role,
            "required": model_required,
            "validator": "validate_central_model_context",
        },
        "resultContract": {
            "kind": CENTRAL_RESULT_KIND,
            "role": role,
            "validator": "validate_central_result_context",
        },
        "outputExpectations": output_expectations,
    }


def _identity(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and _DIGEST.fullmatch(value.removeprefix(prefix)) is not None
    )


def _safe_filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
    )
