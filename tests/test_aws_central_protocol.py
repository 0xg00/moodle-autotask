from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from moddle_autotask.adapters.aws import agent_cli
from moddle_autotask.adapters.aws.agent_spool import (
    _CENTRAL_JOB_KIND,
    _CENTRAL_RESULT_KIND,
    _CENTRAL_ROLES,
    _MAX_CENTRAL_RESULT_BYTES,
    AgentSpoolError,
    _canonical,
    _central_digest,
    _central_plan,
    _validate_artifact_manifest,
    _validate_central_result,
)
from moddle_autotask.adapters.aws.central_protocol import (
    CENTRAL_JOB_KIND,
    CENTRAL_RESULT_KIND,
    CENTRAL_ROLES,
    MAX_CENTRAL_RESULT_BYTES,
    CentralProtocolError,
    canonical_digest,
    canonical_json,
    central_model_schema,
    central_plan,
    central_workspace_contract,
    terminal_provenance,
    validate_artifact_manifest,
    validate_central_job,
    validate_central_job_chain,
    validate_central_job_prefix,
    validate_central_model_context,
    validate_central_model_result,
    validate_central_model_result_binding,
    validate_central_result,
    validate_central_result_chain,
    validate_central_result_context,
    validate_central_terminal_chain,
    validate_new_central_plan,
    validate_prepared_inputs,
    validate_terminal_provenance,
)
from moddle_autotask.adapters.aws.central_protocol import _safe_filename as _leaf_safe_filename


def _plan() -> dict[str, object]:
    return {
        "steps": ["Produce the report."],
        "acceptanceCriteria": [{"id": "report", "text": "A report exists."}],
        "expectedArtifacts": ["report.md"],
    }


def _manifest() -> dict[str, object]:
    data = b"verified artifact\n"
    return {
        "kind": "artifact-manifest-v1",
        "files": [
            {"path": "report.md", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        ],
        "totals": {"files": 1, "bytes": len(data)},
    }


def _result(role: str) -> dict[str, object]:
    common: dict[str, object] = {
        "kind": "moodle-agent-result-v2",
        "jobId": "a" * 64,
        "role": role,
        "succeeded": True,
        "summary": "verified",
        "reportMarkdown": "# Informe\nVerified evidence.",
    }
    if role == "central_planner":
        plan = _plan()
        common.update({"plan": plan, "planDigest": canonical_digest(plan)})
        common["plannerResultDigest"] = canonical_digest(common)
    elif role == "central_executor":
        manifest = _manifest()
        bundle_digest = "b" * 64
        common.update(
            {
                "evidence": {"report": "outputs/report.md"},
                "artifactManifest": manifest,
                "artifactManifestDigest": canonical_digest(manifest),
                "artifactBundleDigest": bundle_digest,
                "bundleLocator": f"bundles/{bundle_digest}.zip",
            }
        )
        common["executorResultDigest"] = canonical_digest(common)
    else:
        common.update(
            {
                "accepted": True,
                "decisions": {"report": "accepted"},
                "findings": [],
                "dependencyDigests": {"plannerResultDigest": "c" * 64},
            }
        )
        common["reviewerResultDigest"] = canonical_digest(common)
    return common


def test_canonical_compatibility_has_exact_bytes_and_digest() -> None:
    value = {"z": ["á", 1], "a": {"b": True}}
    expected = b'{"a":{"b":true},"z":["\xc3\xa1",1]}'
    expected_digest = "3dcafb84bcb7b6fb621feadb83aa00305a132bca6be9c3fc01bfcfd88b25c06d"

    assert canonical_json(value) == expected
    assert _canonical(value) == expected
    assert canonical_digest(value) == expected_digest
    assert _central_digest(value) == expected_digest
    assert _CENTRAL_ROLES == CENTRAL_ROLES
    assert _CENTRAL_JOB_KIND == CENTRAL_JOB_KIND
    assert _CENTRAL_RESULT_KIND == CENTRAL_RESULT_KIND
    assert _MAX_CENTRAL_RESULT_BYTES == MAX_CENTRAL_RESULT_BYTES


def test_plan_and_manifest_compatibility_accepts_identical_values() -> None:
    plan = _plan()
    manifest = _manifest()

    assert central_plan(plan) == plan
    assert _central_plan(plan) == plan
    assert validate_artifact_manifest(manifest) == manifest
    assert _validate_artifact_manifest(manifest) == manifest


@pytest.mark.parametrize("path", ["outputs/report.md", "OUTPUTS/report.md"])
def test_new_plan_rejects_output_root_prefixed_artifacts(path: str) -> None:
    plan = _plan()
    plan["expectedArtifacts"] = [path]

    with pytest.raises(CentralProtocolError, match="expected artifact path"):
        validate_new_central_plan(plan)


def test_v2_decoder_accepts_persisted_output_root_prefixed_plan() -> None:
    result = _result("central_planner")
    plan = cast(dict[str, object], result["plan"])
    plan["expectedArtifacts"] = ["outputs/report.md"]
    result["planDigest"] = canonical_digest(plan)
    result["plannerResultDigest"] = canonical_digest(
        {key: value for key, value in result.items() if key != "plannerResultDigest"}
    )

    assert central_plan(plan) == plan
    validate_central_result(result, "central_planner")


def test_new_planner_wrapper_rejects_output_root_prefixed_plan(tmp_path: Path) -> None:
    job = _central_corpus()[0]
    model = {
        "succeeded": True,
        "summary": "planned",
        "reportMarkdown": "# Informe\nPlan.",
        "plan": {
            "steps": ["Produce the report."],
            "acceptanceCriteria": [{"id": "report", "text": "A report exists."}],
            "expectedArtifacts": ["outputs/report.md"],
        },
    }

    with pytest.raises(AgentSpoolError, match="expected artifact path"):
        agent_cli._wrap_central_result(job, model, tmp_path / "workspace", tmp_path / "bundles")


def test_planner_schema_documents_output_relative_artifacts() -> None:
    schema = central_model_schema(_central_corpus()[0])
    properties = cast(dict[str, object], schema["properties"])
    plan = cast(dict[str, object], properties["plan"])
    plan_properties = cast(dict[str, object], plan["properties"])
    expected = cast(dict[str, object], plan_properties["expectedArtifacts"])
    items = cast(dict[str, object], expected["items"])

    assert items["description"] == (
        "Ruta POSIX relativa a outputs/, sin el prefijo outputs/."
    )


def test_planner_prompt_requires_output_relative_artifacts() -> None:
    prompt = agent_cli._central_prompt(_central_corpus()[0])

    assert "relativas a outputs/" in prompt
    assert "sin incluir el prefijo outputs/" in prompt


@pytest.mark.parametrize("role", ["central_planner", "central_executor", "central_reviewer"])
def test_result_compatibility_accepts_each_role(role: str) -> None:
    result = _result(role)

    validate_central_result(result, role)
    _validate_central_result(result, role)


@pytest.mark.parametrize("role", ["central_planner", "central_executor", "central_reviewer"])
def test_result_compatibility_rejects_tampering_with_matching_message(role: str) -> None:
    result = deepcopy(_result(role))
    result["summary"] = "tampered"

    with pytest.raises(CentralProtocolError, match="result digest is invalid") as leaf_error:
        validate_central_result(result, role)
    with pytest.raises(AgentSpoolError, match="result digest is invalid") as spool_error:
        _validate_central_result(result, role)

    assert str(spool_error.value) == str(leaf_error.value)


def test_leaf_import_does_not_load_spool_cli_or_retention_modules() -> None:
    source_root = Path(__file__).parents[1] / "src"
    environment = os.environ | {"PYTHONPATH": str(source_root)}
    command = (
        "import sys; "
        "import moddle_autotask.adapters.aws.central_protocol; "
        "blocked = {'moddle_autotask.adapters.aws.agent_spool', "
        "'moddle_autotask.adapters.aws.agent_cli', "
        "'moddle_autotask.adapters.aws.retention_fs'}; "
        "raise SystemExit(bool(blocked & sys.modules.keys()))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr


def _prepared_inputs() -> list[dict[str, object]]:
    return [
        {
            "attachmentKey": "moodle-attachment-v1:" + "a" * 64,
            "filename": "input.txt",
            "sizeBytes": 7,
            "sha256": "b" * 64,
            "path": "inputs/0000-input.txt",
        }
    ]


def _prepared_inputs_with_sizes(*sizes: int) -> list[dict[str, object]]:
    return [
        {
            "attachmentKey": f"moodle-attachment-v1:{index:064x}",
            "filename": f"input-{index}.bin",
            "sizeBytes": size,
            "sha256": "b" * 64,
            "path": f"inputs/{index:04d}-input-{index}.bin",
        }
        for index, size in enumerate(sizes)
    ]


def test_prepared_input_storage_envelope_has_exact_and_plus_one_boundaries() -> None:
    mib = 1024 * 1024
    assert len(validate_prepared_inputs(_prepared_inputs_with_sizes(*([0] * 128)))) == 128
    with pytest.raises(CentralProtocolError, match="attachments"):
        validate_prepared_inputs(_prepared_inputs_with_sizes(*([0] * 129)))
    assert validate_prepared_inputs(_prepared_inputs_with_sizes(512 * mib))
    with pytest.raises(CentralProtocolError, match="metadata"):
        validate_prepared_inputs(_prepared_inputs_with_sizes((512 * mib) + 1))
    assert validate_prepared_inputs(_prepared_inputs_with_sizes(*([512 * mib] * 4)))
    with pytest.raises(CentralProtocolError, match="aggregate"):
        validate_prepared_inputs(_prepared_inputs_with_sizes(*([512 * mib] * 5)))


def _central_job(
    role: str,
    dependencies: dict[str, str],
    *,
    plan: dict[str, object] | None = None,
    executor_result: dict[str, object] | None = None,
) -> dict[str, object]:
    prepared_inputs = _prepared_inputs()
    manifest = [
        {
            "attachmentKey": item["attachmentKey"],
            "filename": item["filename"],
            "sizeBytes": item["sizeBytes"],
            "sha256": item["sha256"],
            "path": item["path"],
        }
        for item in prepared_inputs
    ]
    job: dict[str, object] = {
        "kind": CENTRAL_JOB_KIND,
        "role": role,
        "eventId": "moodle-notification-event-v1:" + "c" * 64,
        "taskKey": "moodle-task-v1:" + "d" * 64,
        "revisionDigest": "moodle-assignment-v1:" + "e" * 64,
        "selectedMode": "central",
        "specificationDigest": "f" * 64,
        "preparedInputManifestDigest": canonical_digest(manifest),
        "assignmentSnapshot": {
            "courseName": "ASIX",
            "courseShortname": "ASIX-M06",
            "title": "Controlled task",
            "intro": "Produce evidence.",
        },
        "preparedInputs": prepared_inputs,
        "dependencies": dependencies,
    }
    if plan is not None:
        job["plan"] = plan
    if executor_result is not None:
        job["executorResult"] = executor_result
    job["jobId"] = canonical_digest(job)
    return job


def _bound_result(role: str, job_id: str) -> dict[str, object]:
    result = _result(role)
    result["jobId"] = job_id
    digest_key = {
        "central_planner": "plannerResultDigest",
        "central_executor": "executorResultDigest",
        "central_reviewer": "reviewerResultDigest",
    }[role]
    result.pop(digest_key)
    result[digest_key] = canonical_digest(result)
    return result


def _rehash_job(job: dict[str, object]) -> None:
    job["jobId"] = canonical_digest({key: value for key, value in job.items() if key != "jobId"})


def _rehash_result(result: dict[str, object], digest_key: str) -> None:
    result[digest_key] = canonical_digest(
        {key: value for key, value in result.items() if key != digest_key}
    )


def _central_corpus() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    plan = _plan()
    planner = _central_job("central_planner", {})
    planner_job_id = cast(str, planner["jobId"])
    planner_result = _bound_result("central_planner", planner_job_id)
    planner_result_digest = cast(str, planner_result["plannerResultDigest"])
    executor = _central_job(
        "central_executor",
        {
            "plannerJobId": planner_job_id,
            "planDigest": canonical_digest(plan),
            "plannerResultDigest": planner_result_digest,
        },
        plan=plan,
    )
    executor_result = _bound_result("central_executor", str(executor["jobId"]))
    executor_job_id = cast(str, executor["jobId"])
    executor_result_digest = cast(str, executor_result["executorResultDigest"])
    artifact_manifest_digest = cast(str, executor_result["artifactManifestDigest"])
    artifact_bundle_digest = cast(str, executor_result["artifactBundleDigest"])
    reviewer = _central_job(
        "central_reviewer",
        {
            "plannerJobId": planner_job_id,
            "planDigest": canonical_digest(plan),
            "plannerResultDigest": planner_result_digest,
            "executorJobId": executor_job_id,
            "executorResultDigest": executor_result_digest,
            "artifactManifestDigest": artifact_manifest_digest,
            "artifactBundleDigest": artifact_bundle_digest,
        },
        plan=deepcopy(plan),
        executor_result=executor_result,
    )
    reviewer_result = _bound_result("central_reviewer", str(reviewer["jobId"]))
    reviewer_result["dependencyDigests"] = {
        key: value
        for key, value in cast(dict[str, str], reviewer["dependencies"]).items()
        if key.endswith("Digest")
    }
    reviewer_result.pop("reviewerResultDigest")
    reviewer_result["reviewerResultDigest"] = canonical_digest(reviewer_result)
    return planner, executor, reviewer, planner_result, executor_result, reviewer_result


def _failed_result(job: dict[str, object]) -> dict[str, object]:
    role = cast(str, job["role"])
    digest_key = {
        "central_planner": "plannerResultDigest",
        "central_executor": "executorResultDigest",
        "central_reviewer": "reviewerResultDigest",
    }[role]
    value: dict[str, object] = {
        "kind": CENTRAL_RESULT_KIND,
        "jobId": job["jobId"],
        "role": role,
        "succeeded": False,
        "summary": "terminal failure",
        "reportMarkdown": "",
    }
    value[digest_key] = canonical_digest(value)
    return value


@pytest.mark.parametrize("terminal_index", [0, 1, 2])
def test_terminal_provenance_binds_exact_failed_prefix(terminal_index: int) -> None:
    corpus = _central_corpus()
    jobs = [deepcopy(item) for item in corpus[:3]]
    results = [deepcopy(item) for item in corpus[3:]]
    results[terminal_index] = _failed_result(jobs[terminal_index])
    prefix_jobs = jobs[: terminal_index + 1]
    prefix_results = results[: terminal_index + 1]
    ids = tuple(cast(str, item["jobId"]) for item in prefix_jobs)

    validated_jobs, validated_results = validate_central_terminal_chain(
        prefix_jobs,
        prefix_results,
        expected_job_ids=ids,
        expected_event_id=cast(str, jobs[0]["eventId"]),
        expected_task_key=cast(str, jobs[0]["taskKey"]),
        expected_revision_digest=cast(str, jobs[0]["revisionDigest"]),
    )
    provenance = terminal_provenance(prefix_jobs, prefix_results)

    assert tuple(item["jobId"] for item in validated_jobs) == ids
    assert tuple(item["role"] for item in validated_results) == CENTRAL_ROLES[: len(ids)]
    assert provenance["jobIds"] == list(ids)
    assert provenance["roles"] == list(CENTRAL_ROLES[: len(ids)])
    assert provenance["terminalRole"] == CENTRAL_ROLES[terminal_index]
    assert provenance["terminalStatus"] == "failed"
    assert validate_terminal_provenance(provenance) == provenance

    tampered = deepcopy(provenance)
    tampered["terminalRole"] = "other"
    with pytest.raises(CentralProtocolError, match="terminal provenance"):
        validate_terminal_provenance(tampered)


@pytest.mark.parametrize(
    "hostile",
    [
        "bundle-on-planner-failure",
        "bundle-on-executor-failure",
        "bundle-on-executor-budget",
        "missing-bundle-after-executor-success",
        "wrong-manifest-digest",
        "wrong-bundle-digest",
        "wrong-bundle-locator",
        "wrong-terminal-role",
        "wrong-terminal-status",
    ],
)
def test_terminal_provenance_rejects_hostile_bundle_and_terminal_tuples(hostile: str) -> None:
    corpus = _central_corpus()
    jobs = [deepcopy(item) for item in corpus[:3]]
    results = [deepcopy(item) for item in corpus[3:]]
    bundle_source = terminal_provenance(
        jobs[:2],
        results[:2],
        terminal_role="central_reviewer",
        terminal_status="budget_error",
    )
    bundle_keys = (
        "artifactManifest",
        "artifactManifestDigest",
        "artifactBundleDigest",
        "bundleLocator",
    )
    if hostile == "bundle-on-planner-failure":
        value = terminal_provenance(jobs[:1], [_failed_result(jobs[0])])
        value.update({key: bundle_source[key] for key in bundle_keys})
    elif hostile == "bundle-on-executor-failure":
        value = terminal_provenance(jobs[:2], [results[0], _failed_result(jobs[1])])
        value.update({key: bundle_source[key] for key in bundle_keys})
    elif hostile == "bundle-on-executor-budget":
        value = terminal_provenance(
            jobs[:1], results[:1], terminal_role="central_executor", terminal_status="budget_error"
        )
        value.update({key: bundle_source[key] for key in bundle_keys})
    else:
        value = deepcopy(bundle_source)
        if hostile == "missing-bundle-after-executor-success":
            for key in bundle_keys:
                del value[key]
        elif hostile == "wrong-manifest-digest":
            value["artifactManifestDigest"] = "f" * 64
        elif hostile == "wrong-bundle-digest":
            value["artifactBundleDigest"] = "f" * 64
        elif hostile == "wrong-bundle-locator":
            value["bundleLocator"] = "bundles/other.zip"
        elif hostile == "wrong-terminal-role":
            value["terminalRole"] = "central_executor"
        else:
            value["terminalStatus"] = "failed"
    with pytest.raises(CentralProtocolError, match="terminal provenance"):
        validate_terminal_provenance(value)


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_central_job_prefix_validates_each_exact_ordered_length(prefix: int) -> None:
    planner, executor, reviewer, _planner_result, _executor_result, _reviewer_result = (
        _central_corpus()
    )
    jobs = [planner, executor, reviewer][:prefix]
    expected_ids = tuple(cast(str, job["jobId"]) for job in jobs)

    assert validate_central_job_prefix(
        jobs,
        expected_job_ids=expected_ids,
        expected_event_id=cast(str, planner["eventId"]),
        expected_task_key=cast(str, planner["taskKey"]),
        expected_revision_digest=cast(str, planner["revisionDigest"]),
    ) == tuple(jobs)


def test_budget_provenance_requires_the_exact_successful_planner_executor_chain() -> None:
    planner, executor, _reviewer, planner_result, executor_result, _reviewer_result = (
        _central_corpus()
    )
    provenance = terminal_provenance(
        [planner, executor],
        [planner_result, executor_result],
        terminal_role="central_reviewer",
        terminal_status="budget_error",
    )
    assert provenance["terminalRole"] == "central_reviewer"
    assert provenance["terminalStatus"] == "budget_error"

    for field, value in (
        ("plannerJobId", "0" * 64),
        ("planDigest", "0" * 64),
        ("plannerResultDigest", "0" * 64),
    ):
        mixed_executor = deepcopy(executor)
        cast(dict[str, str], mixed_executor["dependencies"])[field] = value
        _rehash_job(mixed_executor)
        mixed_result = deepcopy(executor_result)
        mixed_result["jobId"] = mixed_executor["jobId"]
        _rehash_result(mixed_result, "executorResultDigest")
        with pytest.raises(CentralProtocolError):
            terminal_provenance(
                [planner, mixed_executor],
                [planner_result, mixed_result],
                terminal_role="central_reviewer",
                terminal_status="budget_error",
            )

    mixed_executor = deepcopy(executor)
    mixed_executor["specificationDigest"] = "0" * 64
    _rehash_job(mixed_executor)
    mixed_result = deepcopy(executor_result)
    mixed_result["jobId"] = mixed_executor["jobId"]
    _rehash_result(mixed_result, "executorResultDigest")
    with pytest.raises(CentralProtocolError):
        terminal_provenance(
            [planner, mixed_executor],
            [planner_result, mixed_result],
            terminal_role="central_reviewer",
            terminal_status="budget_error",
        )


def _chain_binding(
    jobs: list[dict[str, object]],
) -> tuple[tuple[str, str, str], str, str, str]:
    return (
        (
            cast(str, jobs[0]["jobId"]),
            cast(str, jobs[1]["jobId"]),
            cast(str, jobs[2]["jobId"]),
        ),
        cast(str, jobs[0]["eventId"]),
        cast(str, jobs[0]["taskKey"]),
        cast(str, jobs[0]["revisionDigest"]),
    )


def _validate_job_chain(
    jobs: list[dict[str, object]],
    binding: tuple[tuple[str, str, str], str, str, str],
) -> tuple[dict[str, object], ...]:
    expected_job_ids, expected_event_id, expected_task_key, expected_revision_digest = binding
    return validate_central_job_chain(
        jobs,
        expected_job_ids=expected_job_ids,
        expected_event_id=expected_event_id,
        expected_task_key=expected_task_key,
        expected_revision_digest=expected_revision_digest,
    )


def _validate_result_chain(
    jobs: list[dict[str, object]],
    results: list[dict[str, object]],
    binding: tuple[tuple[str, str, str], str, str, str],
) -> tuple[dict[str, object], ...]:
    expected_job_ids, expected_event_id, expected_task_key, expected_revision_digest = binding
    return validate_central_result_chain(
        jobs,
        results,
        expected_job_ids=expected_job_ids,
        expected_event_id=expected_event_id,
        expected_task_key=expected_task_key,
        expected_revision_digest=expected_revision_digest,
    )


def _forge_chain(
    jobs: list[dict[str, object]], *, event_id: str | None = None, specification: str | None = None
) -> None:
    planner, executor, reviewer = jobs
    for job in jobs:
        if event_id is not None:
            job["eventId"] = event_id
        if specification is not None:
            job["specificationDigest"] = specification
    _rehash_job(planner)
    executor_dependencies = cast(dict[str, str], executor["dependencies"])
    executor_dependencies["plannerJobId"] = cast(str, planner["jobId"])
    _rehash_job(executor)
    embedded = cast(dict[str, object], reviewer["executorResult"])
    embedded["jobId"] = executor["jobId"]
    _rehash_result(embedded, "executorResultDigest")
    reviewer_dependencies = cast(dict[str, str], reviewer["dependencies"])
    reviewer_dependencies["plannerJobId"] = cast(str, planner["jobId"])
    reviewer_dependencies["executorJobId"] = cast(str, executor["jobId"])
    reviewer_dependencies["executorResultDigest"] = cast(str, embedded["executorResultDigest"])
    _rehash_job(reviewer)


def _rehash_reviewer_embedded_executor(jobs: list[dict[str, object]]) -> None:
    reviewer = jobs[2]
    embedded = cast(dict[str, object], reviewer["executorResult"])
    _rehash_result(embedded, "executorResultDigest")
    dependencies = cast(dict[str, str], reviewer["dependencies"])
    dependencies["executorResultDigest"] = cast(str, embedded["executorResultDigest"])
    _rehash_job(reviewer)


def _posix_producer_safe_filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
        and PurePosixPath(value).name == value
    )


def test_central_chain_validators_accept_exact_canonical_corpus() -> None:
    corpus = _central_corpus()
    jobs = list(corpus[:3])
    results = list(corpus[3:])
    binding = _chain_binding(jobs)

    assert _validate_job_chain(jobs, binding) == tuple(jobs)
    assert _validate_result_chain(jobs, results, binding) == tuple(results)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_central_workspace_contract_is_exact_role_golden(index: int) -> None:
    job = _central_corpus()[index]
    role = cast(str, job["role"])
    output_expectations = ["report.md"] if role == "central_executor" else []
    root_entries = ["inputs", "last-message.json", "result-schema.json"]
    if role == "central_executor":
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
    expected = {
        "kind": "central-workspace-contract-v1",
        "jobId": job["jobId"],
        "role": role,
        "rootEntries": sorted(root_entries),
        "preparedInputs": deepcopy(job["preparedInputs"]),
        "resultSchemaJson": canonical_json(central_model_schema(job)).decode("utf-8"),
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

    assert central_workspace_contract(job) == expected
    assert cast(str, expected["resultSchemaJson"]).encode("utf-8") == canonical_json(
        central_model_schema(job)
    )


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("role-order", "role order"),
        ("identity", "job binding"),
        ("prepared-input-manifest", "job binding"),
        ("plan", "job binding"),
        ("dependency", "job binding"),
    ],
)
def test_central_job_chain_rejects_hostile_tampering(
    name: str,
    message: str,
) -> None:
    jobs = list(deepcopy(_central_corpus()[:3]))
    binding = _chain_binding(jobs)
    if name == "role-order":
        jobs[0] = jobs[1]
    elif name == "identity":
        jobs[1]["eventId"] = "moodle-notification-event-v1:" + "0" * 64
        _rehash_job(jobs[1])
    elif name == "prepared-input-manifest":
        jobs[1]["preparedInputs"] = []
        jobs[1]["preparedInputManifestDigest"] = canonical_digest([])
        _rehash_job(jobs[1])
    elif name == "plan":
        cast(dict[str, object], jobs[2]["plan"])["expectedArtifacts"] = ["review.md"]
        _rehash_job(jobs[2])
    else:
        cast(dict[str, str], jobs[1]["dependencies"])["plannerJobId"] = "0" * 64
        _rehash_job(jobs[1])

    with pytest.raises(CentralProtocolError, match=message):
        _validate_job_chain(jobs, binding)


def test_central_job_chain_rejects_rehashed_reviewer_planner_digest() -> None:
    jobs = list(deepcopy(_central_corpus()[:3]))
    cast(dict[str, str], jobs[2]["dependencies"])["plannerResultDigest"] = "0" * 64
    _rehash_job(jobs[2])

    with pytest.raises(CentralProtocolError, match="dependencies"):
        _validate_job_chain(jobs, _chain_binding(jobs))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("kind", "moodle-agent-result-v1", "identity"),
        ("role", "central_planner", "identity"),
        ("evidence", {"wrong": "outputs/report.md"}, "coverage"),
    ],
    ids=["kind", "role", "criterion-coverage"],
)
def test_central_job_chain_rejects_rehashed_embedded_executor_context(
    field: str,
    value: object,
    message: str,
) -> None:
    jobs = list(deepcopy(_central_corpus()[:3]))
    embedded = cast(dict[str, object], jobs[2]["executorResult"])
    embedded[field] = value
    _rehash_reviewer_embedded_executor(jobs)

    with pytest.raises(CentralProtocolError, match=message):
        _validate_job_chain(jobs, _chain_binding(jobs))


@pytest.mark.parametrize(
    ("event_id", "specification", "message"),
    [
        ("moodle-notification-event-v1:" + "0" * 64, None, "identity binding"),
        (None, "0" * 64, "job binding"),
    ],
    ids=["cross-identity", "cross-job-id"],
)
def test_central_job_chain_rejects_forged_self_consistent_tombstone_binding(
    event_id: str | None,
    specification: str | None,
    message: str,
) -> None:
    jobs = list(deepcopy(_central_corpus()[:3]))
    original_binding = _chain_binding(jobs)
    _forge_chain(jobs, event_id=event_id, specification=specification)
    forged_binding = _chain_binding(jobs)

    assert _validate_job_chain(jobs, forged_binding) == tuple(jobs)
    if event_id is not None:
        binding = (forged_binding[0], *original_binding[1:])
    else:
        binding = original_binding
    with pytest.raises(CentralProtocolError, match=message):
        _validate_job_chain(jobs, binding)


def test_central_result_chain_rejects_result_manifest_and_reviewer_tampering() -> None:
    corpus = _central_corpus()
    jobs = list(deepcopy(corpus[:3]))
    results = list(deepcopy(corpus[3:]))
    binding = _chain_binding(jobs)
    planner_result, executor_result, reviewer_result = results

    planner_result["summary"] = "different but valid"
    _rehash_result(planner_result, "plannerResultDigest")
    with pytest.raises(CentralProtocolError, match="planner binding"):
        _validate_result_chain(jobs, results, binding)

    results = list(deepcopy(corpus[3:]))
    executor_result = results[1]
    cast(dict[str, object], executor_result["artifactManifest"])["totals"] = {
        "files": 1,
        "bytes": 0,
    }
    _rehash_result(executor_result, "executorResultDigest")
    with pytest.raises(CentralProtocolError, match="artifact manifest totals"):
        _validate_result_chain(jobs, results, binding)

    results = list(deepcopy(corpus[3:]))
    reviewer_result = results[2]
    reviewer_result["accepted"] = False
    reviewer_result["decisions"] = {"report": "rejected"}
    _rehash_result(reviewer_result, "reviewerResultDigest")
    with pytest.raises(CentralProtocolError, match="reviewer acceptance"):
        _validate_result_chain(jobs, results, binding)


def test_central_result_chain_rejects_rehashed_planner_digest_binding() -> None:
    corpus = _central_corpus()
    jobs = list(deepcopy(corpus[:3]))
    results = list(deepcopy(corpus[3:]))
    executor = jobs[1]
    reviewer = jobs[2]
    executor_dependencies = cast(dict[str, str], executor["dependencies"])
    executor_dependencies["plannerResultDigest"] = "0" * 64
    _rehash_job(executor)
    executor_result = results[1]
    executor_result["jobId"] = executor["jobId"]
    _rehash_result(executor_result, "executorResultDigest")
    reviewer["executorResult"] = deepcopy(executor_result)
    reviewer_dependencies = cast(dict[str, str], reviewer["dependencies"])
    reviewer_dependencies["plannerResultDigest"] = "0" * 64
    reviewer_dependencies["executorJobId"] = cast(str, executor["jobId"])
    reviewer_dependencies["executorResultDigest"] = cast(
        str, executor_result["executorResultDigest"]
    )
    _rehash_job(reviewer)
    reviewer_result = results[2]
    reviewer_result["jobId"] = reviewer["jobId"]
    reviewer_result["dependencyDigests"] = {
        key: value for key, value in reviewer_dependencies.items() if key.endswith("Digest")
    }
    _rehash_result(reviewer_result, "reviewerResultDigest")

    with pytest.raises(CentralProtocolError, match="planner binding"):
        _validate_result_chain(jobs, results, _chain_binding(jobs))


def test_central_result_chain_rejects_noncanonical_executor_binding() -> None:
    corpus = _central_corpus()
    jobs = list(deepcopy(corpus[:3]))
    results = list(deepcopy(corpus[3:]))
    binding = _chain_binding(jobs)
    reviewer = jobs[2]
    embedded = cast(dict[str, object], reviewer["executorResult"])
    embedded["summary"] = "different but valid"
    _rehash_result(embedded, "executorResultDigest")
    dependencies = cast(dict[str, str], reviewer["dependencies"])
    dependencies["executorResultDigest"] = cast(str, embedded["executorResultDigest"])
    _rehash_job(reviewer)
    reviewer_result = results[2]
    reviewer_result["jobId"] = reviewer["jobId"]
    reviewer_result["dependencyDigests"] = {
        key: value for key, value in dependencies.items() if key.endswith("Digest")
    }
    _rehash_result(reviewer_result, "reviewerResultDigest")
    binding = _chain_binding(jobs)

    with pytest.raises(CentralProtocolError, match="executor binding"):
        _validate_result_chain(jobs, results, binding)


def test_central_workspace_contract_rejects_hostile_prepared_input_tampering() -> None:
    job = deepcopy(_central_corpus()[0])
    job["preparedInputs"] = []

    with pytest.raises(CentralProtocolError, match="central prepared input manifest"):
        central_workspace_contract(job)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("input.txt", True),
        ("C:foo", True),
        ("nested/input.txt", False),
        ("nested\\input.txt", False),
        (".", False),
        ("..", False),
        ("", False),
        ("nul\x00name", False),
        ("control\x1fname", True),
    ],
    ids=["plain", "drive-like", "slash", "backslash", "dot", "dotdot", "empty", "nul", "control"],
)
def test_leaf_safe_filename_matches_posix_producer_vectors(filename: str, expected: bool) -> None:
    prepared = _prepared_inputs()
    prepared[0]["filename"] = filename
    prepared[0]["path"] = f"inputs/0000-{filename}"

    assert _posix_producer_safe_filename(filename) is expected
    assert _leaf_safe_filename(filename) is expected
    if expected:
        assert validate_prepared_inputs(prepared) == tuple(prepared)
    else:
        with pytest.raises(CentralProtocolError):
            validate_prepared_inputs(prepared)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_central_job_and_model_contracts_match_cli(index: int, tmp_path: Path) -> None:
    jobs = _central_corpus()[:3]
    results = _central_corpus()[3:]
    job = jobs[index]
    role = str(job["role"])
    model = {
        "central_planner": {
            "succeeded": True,
            "summary": "planned",
            "reportMarkdown": "# Informe\nPlan evidence.",
            "plan": _plan(),
        },
        "central_executor": {
            "succeeded": True,
            "summary": "executed",
            "reportMarkdown": "# Informe\nExecution evidence.",
            "evidence": {"report": "outputs/report.md"},
        },
        "central_reviewer": {
            "succeeded": True,
            "summary": "reviewed",
            "reportMarkdown": "# Informe\nReview evidence.",
            "accepted": True,
            "decisions": {"report": "accepted"},
            "findings": [],
        },
    }[role]

    prepared_inputs = cast(list[dict[str, object]], job["preparedInputs"])
    assert validate_prepared_inputs(prepared_inputs) == tuple(prepared_inputs)
    assert validate_central_job(job, str(job["jobId"])) == job
    assert agent_cli._load_central_job(tmp_path / str(job["jobId"]), job) == job
    assert central_model_schema(job) == agent_cli._schema_for_job(job)
    assert validate_central_model_result(model, role) == model
    model_path = tmp_path / f"{role}.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    assert agent_cli._load_central_model_result(model_path, role) == model
    validate_central_model_context(job, model)
    agent_cli._validate_central_model_context(job, model)
    validate_central_result_context(job, results[index])
    agent_cli._validate_central_result_context(job, results[index])
    wrapped = results[index]
    keys = {"succeeded", "summary", "reportMarkdown"}
    keys.add(
        {
            "central_planner": "plan",
            "central_executor": "evidence",
            "central_reviewer": "accepted",
        }[role]
    )
    binding_model = {key: wrapped[key] for key in keys}
    if role == "central_reviewer":
        binding_model["decisions"] = wrapped["decisions"]
        binding_model["findings"] = wrapped["findings"]
    validate_central_model_result_binding(job, binding_model, wrapped)
    agent_cli._validate_central_model_result_binding(job, binding_model, wrapped)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_central_model_result_binding_rejects_independently_valid_mismatch(index: int) -> None:
    corpus = _central_corpus()
    job = deepcopy(corpus[index])
    wrapped = deepcopy(corpus[index + 3])
    role = cast(str, job["role"])
    model = {
        "central_planner": {
            "succeeded": True,
            "summary": "planned differently",
            "reportMarkdown": "# Informe\nPlan evidence.",
            "plan": _plan(),
        },
        "central_executor": {
            "succeeded": True,
            "summary": "executed",
            "reportMarkdown": "# Informe\nExecution evidence.",
            "evidence": {"report": "outputs/other-report.md"},
        },
        "central_reviewer": {
            "succeeded": True,
            "summary": "reviewed",
            "reportMarkdown": "# Informe\nReview evidence.",
            "accepted": False,
            "decisions": {"report": "rejected"},
            "findings": ["Needs revision."],
        },
    }[role]

    validate_central_model_context(job, model)
    validate_central_result_context(job, wrapped)
    with pytest.raises(CentralProtocolError, match="model result binding"):
        validate_central_model_result_binding(job, model, wrapped)
    with pytest.raises(AgentSpoolError, match="model result binding"):
        agent_cli._validate_central_model_result_binding(job, model, wrapped)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_central_model_schema_uses_strict_supported_object_shapes(index: int) -> None:
    schema = central_model_schema(_central_corpus()[index])

    def verify(value: object) -> None:
        if isinstance(value, dict):
            assert not ({"allOf", "if", "then"} & set(value))
            if value.get("type") == "object":
                properties = cast(dict[str, object], value["properties"])
                assert value["additionalProperties"] is False
                assert set(cast(list[str], value["required"])) == set(properties)
            for child in value.values():
                verify(child)
        elif isinstance(value, list):
            for child in value:
                verify(child)

    verify(schema)


def test_central_model_result_uses_serialized_utf8_budget() -> None:
    model = {
        "succeeded": False,
        "summary": "failed",
        "reportMarkdown": "é" * (MAX_CENTRAL_RESULT_BYTES // 2),
    }

    with pytest.raises(CentralProtocolError, match="serialized size budget"):
        validate_central_model_result(model, "central_planner")


@pytest.mark.parametrize(
    ("index", "mutator", "message"),
    [
        (0, lambda job: job.__setitem__("role", "other"), "identity"),
        (0, lambda job: job["assignmentSnapshot"].pop("intro"), "snapshot"),
        (0, lambda job: job["preparedInputs"][0].__setitem__("path", "input.txt"), "metadata"),
        (0, lambda job: job.__setitem__("preparedInputManifestDigest", "0" * 64), "manifest"),
        (0, lambda job: job.__setitem__("dependencies", {"extra": "value"}), "dependency chain"),
    ],
)
def test_central_job_tampering_matches_cli(
    index: int,
    mutator: Callable[[dict[str, object]], None],
    message: str,
    tmp_path: Path,
) -> None:
    job = deepcopy(_central_corpus()[index])
    mutator(job)
    if message != "identity":
        body = {key: value for key, value in job.items() if key != "jobId"}
        job["jobId"] = canonical_digest(body)

    with pytest.raises(CentralProtocolError, match=message) as leaf_error:
        validate_central_job(job, str(job["jobId"]))
    with pytest.raises(AgentSpoolError, match=message) as cli_error:
        agent_cli._load_central_job(tmp_path / str(job["jobId"]), job)

    assert str(cli_error.value) == str(leaf_error.value)


def test_central_context_rejects_plan_evidence_and_decision_tampering() -> None:
    _planner, executor, reviewer, _planner_result, executor_result, reviewer_result = (
        _central_corpus()
    )
    invalid_plan = deepcopy(executor)
    cast(dict[str, str], invalid_plan["dependencies"])["planDigest"] = "0" * 64
    with pytest.raises(CentralProtocolError, match="plan dependency"):
        validate_central_result_context(invalid_plan, executor_result)
    with pytest.raises(AgentSpoolError, match="plan dependency"):
        agent_cli._validate_central_result_context(invalid_plan, executor_result)

    invalid_evidence = {
        "succeeded": True,
        "summary": "executed",
        "reportMarkdown": "# Informe\nExecution evidence.",
        "evidence": {"wrong": "outputs/report.md"},
    }
    with pytest.raises(CentralProtocolError, match="evidence"):
        validate_central_model_context(executor, invalid_evidence)
    with pytest.raises(AgentSpoolError, match="evidence"):
        agent_cli._validate_central_model_context(executor, invalid_evidence)

    invalid_decisions = {
        "succeeded": True,
        "summary": "reviewed",
        "reportMarkdown": "# Informe\nReview evidence.",
        "accepted": True,
        "decisions": {"report": "rejected"},
        "findings": [],
    }
    with pytest.raises(CentralProtocolError, match="reviewer model"):
        validate_central_model_context(reviewer, invalid_decisions)
    with pytest.raises(AgentSpoolError, match="reviewer model"):
        agent_cli._validate_central_model_context(reviewer, invalid_decisions)

    reviewer_result["decisions"] = {"wrong": "accepted"}
    with pytest.raises(CentralProtocolError, match="reviewer result digest"):
        validate_central_result_context(reviewer, reviewer_result)
    with pytest.raises(AgentSpoolError, match="reviewer result digest"):
        agent_cli._validate_central_result_context(reviewer, reviewer_result)


def test_prepared_input_order_and_reviewer_executor_binding_match_cli() -> None:
    prepared_inputs = _prepared_inputs()
    second = deepcopy(prepared_inputs[0])
    second["attachmentKey"] = "moodle-attachment-v1:" + "c" * 64
    second["path"] = "inputs/0001-input.txt"
    prepared_inputs.append(second)
    assert validate_prepared_inputs(prepared_inputs) == tuple(prepared_inputs)
    reversed_inputs = list(reversed(prepared_inputs))
    with pytest.raises(CentralProtocolError, match="metadata"):
        validate_prepared_inputs(reversed_inputs)
    with pytest.raises(AgentSpoolError, match="metadata"):
        agent_cli._attachments({"kind": CENTRAL_JOB_KIND, "preparedInputs": reversed_inputs})

    _planner, _executor, reviewer, _planner_result, _executor_result, reviewer_result = (
        _central_corpus()
    )
    cast(dict[str, str], reviewer["dependencies"])["executorJobId"] = "0" * 64
    with pytest.raises(CentralProtocolError, match="executor dependency"):
        validate_central_result_context(reviewer, reviewer_result)
    with pytest.raises(AgentSpoolError, match="executor dependency"):
        agent_cli._validate_central_result_context(reviewer, reviewer_result)
