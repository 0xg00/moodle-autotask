"""Pure lifecycle transition rules."""

from __future__ import annotations

from enum import StrEnum


class InvalidTaskTransitionError(ValueError):
    """Raised when a lifecycle change would bypass a control boundary."""


class TaskState(StrEnum):
    DISCOVERED = "discovered"
    AWAITING_START_APPROVAL = "awaiting_start_approval"
    PLANNING = "planning"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    COLLECTING = "collecting"
    REVIEW_READY = "review_ready"
    AWAITING_SUBMIT_APPROVAL = "awaiting_submit_approval"
    SUBMITTING = "submitting"
    COMPLETED = "completed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLEANING_UP = "cleaning_up"
    CLEANED = "cleaned"


_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
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


def transition(current: TaskState, target: TaskState) -> TaskState:
    """Return a valid target state, otherwise fail closed."""
    if target not in _TRANSITIONS[current]:
        raise InvalidTaskTransitionError(f"cannot transition from {current} to {target}")
    return target
