from __future__ import annotations

from pathlib import Path
from typing import assert_type, cast

import pytest

from moodle_autotask.domain.models import (
    ArtifactReference,
    Digest,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ManifestReference,
    SubmissionIntent,
    TaskId,
    WorkflowRevision,
)


def test_digest_is_stable_for_equivalent_json_mappings() -> None:
    assert Digest.of_json({"b": 2, "a": 1}) == Digest.of_json({"a": 1, "b": 2})


@pytest.mark.parametrize("value", [Path("not-json"), float("nan"), float("inf"), float("-inf")])
def test_digest_rejects_noncanonical_json_values(value: object) -> None:
    with pytest.raises(ValueError):
        Digest.of_json({"value": value})


def test_execution_request_rejects_unselected_auto_mode() -> None:
    with pytest.raises(ValueError, match="must be selected"):
        ExecutionRequest(
            task_id=TaskId("task-1"),
            workflow_revision=WorkflowRevision("rev-1"),
            execution_digest=Digest.of_json({"work": "run"}),
            selected_mode=ExecutionMode.AUTO,
        )


def test_execution_request_normalizes_mutable_capabilities() -> None:
    capabilities = {"workspace:write"}
    request = ExecutionRequest(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        Digest.of_json({"work": "run"}),
        ExecutionMode.IN_GUEST,
        cast(frozenset[str], capabilities),
    )
    capabilities.add("network:open")

    assert_type(request.capabilities, frozenset[str])
    assert isinstance(request.capabilities, frozenset)
    assert request.capabilities == frozenset({"workspace:write"})


def test_submission_intent_normalizes_mutable_artifacts() -> None:
    artifact = ArtifactReference("memory://artifact", Digest.of_json({"artifact": 1}))
    artifacts = [artifact]
    intent = SubmissionIntent(
        TaskId("task-1"),
        WorkflowRevision("rev-1"),
        Digest.of_json({"submission": 1}),
        ManifestReference("memory://manifest", Digest.of_json({"manifest": 1})),
        cast(tuple[ArtifactReference, ...], artifacts),
    )
    artifacts.append(ArtifactReference("memory://later", Digest.of_json({"artifact": 2})))

    assert_type(intent.artifacts, tuple[ArtifactReference, ...])
    assert isinstance(intent.artifacts, tuple)
    assert intent.artifacts == (artifact,)


def test_execution_result_normalizes_mutable_artifacts() -> None:
    artifact = ArtifactReference("memory://artifact", Digest.of_json({"artifact": 1}))
    artifacts = [artifact]
    result = ExecutionResult(True, cast(tuple[ArtifactReference, ...], artifacts))
    artifacts.append(ArtifactReference("memory://later", Digest.of_json({"artifact": 2})))

    assert_type(result.artifacts, tuple[ArtifactReference, ...])
    assert isinstance(result.artifacts, tuple)
    assert result.artifacts == (artifact,)
