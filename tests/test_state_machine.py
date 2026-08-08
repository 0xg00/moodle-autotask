from __future__ import annotations

import pytest

from moddle_autotask.domain.state_machine import (
    InvalidTaskTransitionError,
    TaskState,
    transition,
)

EXPECTED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DISCOVERED: frozenset({TaskState.AWAITING_START_APPROVAL, TaskState.CANCELLED}),
    TaskState.AWAITING_START_APPROVAL: frozenset(
        {TaskState.PLANNING, TaskState.DENIED, TaskState.CANCELLED}
    ),
    TaskState.PLANNING: frozenset({TaskState.PROVISIONING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.PROVISIONING: frozenset({TaskState.RUNNING, TaskState.FAILED, TaskState.CLEANING_UP}),
    TaskState.RUNNING: frozenset({TaskState.COLLECTING, TaskState.FAILED, TaskState.CLEANING_UP}),
    TaskState.COLLECTING: frozenset(
        {TaskState.REVIEW_READY, TaskState.FAILED, TaskState.CLEANING_UP}
    ),
    TaskState.REVIEW_READY: frozenset({TaskState.AWAITING_SUBMIT_APPROVAL, TaskState.CANCELLED}),
    TaskState.AWAITING_SUBMIT_APPROVAL: frozenset(
        {TaskState.SUBMITTING, TaskState.DENIED, TaskState.CANCELLED}
    ),
    TaskState.SUBMITTING: frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CLEANING_UP}),
    TaskState.COMPLETED: frozenset({TaskState.CLEANING_UP}),
    TaskState.DENIED: frozenset({TaskState.CLEANING_UP}),
    TaskState.CANCELLED: frozenset({TaskState.CLEANING_UP}),
    TaskState.FAILED: frozenset({TaskState.CLEANING_UP}),
    TaskState.CLEANING_UP: frozenset({TaskState.CLEANED}),
    TaskState.CLEANED: frozenset(),
}
ALLOWED_PAIRS = [
    (current, target)
    for current, targets in EXPECTED_TRANSITIONS.items()
    for target in targets
]
UNLISTED_PAIRS = [
    (current, target)
    for current in TaskState
    for target in TaskState
    if target not in EXPECTED_TRANSITIONS[current]
]


@pytest.mark.parametrize(("current", "target"), ALLOWED_PAIRS)
def test_all_allowed_transitions_return_the_target(current: TaskState, target: TaskState) -> None:
    assert transition(current, target) is target


@pytest.mark.parametrize(("current", "target"), UNLISTED_PAIRS)
def test_all_unlisted_transitions_fail_closed(current: TaskState, target: TaskState) -> None:
    with pytest.raises(InvalidTaskTransitionError, match="cannot transition"):
        transition(current, target)
