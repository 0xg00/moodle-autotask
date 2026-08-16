from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest

from moddle_autotask.adapters.aws import retention_runtime
from moddle_autotask.adapters.aws.retention_fs import RetentionCapacityError, RetentionFilesystem
from moddle_autotask.adapters.aws.retention_runtime import (
    AgentRetentionCoordinator,
    ControllerRetentionCoordinator,
    production_roots,
)
from moddle_autotask.adapters.moodle.approval_state import (
    ApprovalState,
    RetentionCompletionReceipt,
    RetentionReconciliationPage,
    RetentionReconciliationResult,
)


@dataclass
class _Engine:
    acks: tuple[str, ...] = ()
    committed: tuple[str, ...] = ()
    completed_items: tuple[object, ...] = ()
    complete: bool = False
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def actionable_acks(self, *, limit: int, scan_limit: int) -> tuple[str, ...]:
        self.calls.append(("acks", limit, scan_limit))
        return self.acks

    def controller_consume_ack(self, tombstone_id: str) -> None:
        self.calls.append(("controller-consume", tombstone_id))
        self.acks = ()

    def actionable_committed(self, *, limit: int, scan_limit: int) -> tuple[str, ...]:
        self.calls.append(("committed", limit, scan_limit))
        return self.committed

    def agent_consume(self, tombstone_id: str, *, acknowledged_at: int, now: int) -> None:
        self.calls.append(("agent-consume", tombstone_id, acknowledged_at, now))

    def is_completed(self, prepared: object) -> bool:
        self.calls.append(("completed", prepared))
        return self.complete or prepared in self.completed_items

    def commit(self, prepared: object, *, committed_at: int) -> None:
        self.calls.append(("commit", prepared, committed_at))


@dataclass
class _State:
    calls: list[tuple[int, int, int, int]] = field(default_factory=list)
    completions: list[tuple[tuple[object, ...], int]] = field(default_factory=list)
    records: tuple[object, ...] = ()
    reconciliation: RetentionReconciliationResult = field(
        default_factory=lambda: RetentionReconciliationResult("empty_initial")
    )
    advanced: list[RetentionReconciliationPage] = field(default_factory=list)

    def retention_records(
        self, now: int, scratch_ttl: int, evidence_ttl: int, limit: int
    ) -> tuple[object, ...]:
        self.calls.append((now, scratch_ttl, evidence_ttl, limit))
        return self.records

    def retention_reconciliation_page(self, *, limit: int) -> RetentionReconciliationResult:
        assert limit == 1024
        return self.reconciliation

    def advance_retention_reconciliation(self, page: RetentionReconciliationPage) -> None:
        self.advanced.append(page)

    def record_retention_completions(
        self, completed: tuple[object, ...], *, completed_at: int
    ) -> None:
        self.completions.append((completed, completed_at))


@dataclass
class _PagingState(_State):
    pages: tuple[RetentionReconciliationResult, ...] = ()
    page_index: int = 0

    def retention_reconciliation_page(self, *, limit: int) -> RetentionReconciliationResult:
        assert limit == 1024
        return self.pages[self.page_index]

    def advance_retention_reconciliation(self, page: RetentionReconciliationPage) -> None:
        super().advance_retention_reconciliation(page)
        self.page_index += 1


@dataclass
class _SequentialReconciliationState(_State):
    reconciliations: tuple[RetentionReconciliationResult, ...] = ()
    reconciliation_index: int = 0

    def retention_reconciliation_page(self, *, limit: int) -> RetentionReconciliationResult:
        assert limit == 1024
        result = self.reconciliations[self.reconciliation_index]
        self.reconciliation_index += 1
        return result


def test_controller_ack_precedes_state_planning() -> None:
    engine = _Engine(acks=("a" * 64,))
    state = _State()

    result = ControllerRetentionCoordinator(
        cast(ApprovalState, state), cast(RetentionFilesystem, engine), clock=lambda: 17
    ).cycle()

    assert result == "ack-consumed"
    assert engine.calls == [
        ("acks", 1, 1024),
        ("controller-consume", "a" * 64),
    ]
    assert state.calls == []


def test_controller_commits_one_plan_at_cycle_time(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine()
    state = _State()
    prepared = object()
    monkeypatch.setattr(retention_runtime, "plan_retention", lambda *args, **kwargs: (prepared,))

    result = ControllerRetentionCoordinator(
        cast(ApprovalState, state), cast(RetentionFilesystem, engine), clock=lambda: 23
    ).cycle()

    assert result == "committed"
    assert state.calls == [(23, 86_400, 604_800, 1024)]
    assert engine.calls[-1] == ("commit", prepared, 23)


def test_controller_treats_new_chain_capacity_closure_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine()
    state = _State()
    prepared = object()
    monkeypatch.setattr(retention_runtime, "plan_retention", lambda *args, **kwargs: (prepared,))

    def close(_prepared: object, *, committed_at: int) -> None:
        del committed_at
        raise RetentionCapacityError("retention metadata admission is closed")

    engine.commit = close  # type: ignore[assignment]

    result = ControllerRetentionCoordinator(
        cast(ApprovalState, state), cast(RetentionFilesystem, engine), clock=lambda: 23
    ).cycle()

    assert result == "capacity-closed"


def test_controller_persists_filesystem_receipt_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(complete=True)
    state = _State()
    prepared = object()
    monkeypatch.setattr(retention_runtime, "plan_retention", lambda *args, **kwargs: (prepared,))
    result = ControllerRetentionCoordinator(
        cast(ApprovalState, state), cast(RetentionFilesystem, engine), clock=lambda: 23
    ).cycle()

    assert result == "reconciled"
    assert state.completions == [((prepared,), 23)]
    assert not any(call[0] == "commit" for call in engine.calls)


def test_controller_advances_validated_completion_page_before_normal_work() -> None:
    receipt = RetentionCompletionReceipt(
        "a" * 64, 1, "moodle-notification-event-v1:" + "b" * 64, "scratch"
    )
    page = RetentionReconciliationPage((-1, "", ""), (receipt,))
    engine = _Engine(complete=True, acks=("c" * 64,))
    state = _State(reconciliation=RetentionReconciliationResult("page", page))

    result = ControllerRetentionCoordinator(
        cast(ApprovalState, state), cast(RetentionFilesystem, engine), clock=lambda: 23
    ).cycle()

    assert result == "reconciliation-advanced"
    assert state.advanced == [page]
    assert engine.calls == [("completed", "a" * 64)]


def test_controller_refuses_unmatched_completion_page_without_advancing() -> None:
    receipt = RetentionCompletionReceipt(
        "a" * 64, 1, "moodle-notification-event-v1:" + "b" * 64, "scratch"
    )
    page = RetentionReconciliationPage((-1, "", ""), (receipt,))
    state = _State(reconciliation=RetentionReconciliationResult("page", page))

    with pytest.raises(RuntimeError, match="receipt conflicts"):
        ControllerRetentionCoordinator(
            cast(ApprovalState, state), cast(RetentionFilesystem, _Engine()), clock=lambda: 23
        ).cycle()

    assert state.advanced == []


def test_controller_wrap_is_the_only_mutation_before_later_bounded_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = object()
    actionable = object()
    state = _SequentialReconciliationState(
        records=(object(),),
        reconciliations=(
            RetentionReconciliationResult("wrapped"),
            RetentionReconciliationResult("empty_initial"),
            RetentionReconciliationResult("empty_initial"),
            RetentionReconciliationResult("empty_initial"),
        ),
    )
    engine = _Engine(acks=("a" * 64,), completed_items=(completed,))
    plans = iter(((completed,), (actionable,), (actionable,)))
    monkeypatch.setattr(retention_runtime, "plan_retention", lambda *args, **kwargs: next(plans))
    coordinator = ControllerRetentionCoordinator(
        cast(ApprovalState, state), cast(RetentionFilesystem, engine), clock=lambda: 23
    )

    assert coordinator.cycle() == "reconciliation-wrapped"
    assert engine.calls == []
    assert state.calls == []
    assert state.completions == []

    assert coordinator.cycle() == "ack-consumed"
    assert coordinator.cycle() == "reconciled"
    assert state.completions == [((completed,), 23)]
    assert coordinator.cycle() == "committed"
    assert engine.calls[-1] == ("commit", actionable, 23)


def test_controller_reaches_second_reconciliation_page_before_failing_closed() -> None:
    event_prefix = "moodle-notification-event-v1:"
    first = tuple(
        RetentionCompletionReceipt(
            f"{index:064x}", 1, event_prefix + f"{index:064x}", "scratch"
        )
        for index in range(1024)
    )
    second = RetentionCompletionReceipt("f" * 64, 2, event_prefix + "f" * 64, "evidence")
    first_page = RetentionReconciliationPage((-1, "", ""), first)
    second_page = RetentionReconciliationPage(
        (1, first[-1].event_id, "scratch"), (second,)
    )
    state = _PagingState(
        pages=(
            RetentionReconciliationResult("page", first_page),
            RetentionReconciliationResult("page", second_page),
        )
    )
    coordinator = ControllerRetentionCoordinator(
        cast(ApprovalState, state),
        cast(RetentionFilesystem, _Engine(complete=True)),
        clock=lambda: 23,
    )

    assert coordinator.cycle() == "reconciliation-advanced"
    assert state.advanced == [first_page]
    coordinator.engine = cast(RetentionFilesystem, _Engine())

    with pytest.raises(RuntimeError, match="receipt conflicts"):
        coordinator.cycle()

    assert state.advanced == [first_page]


def test_agent_consumes_before_normal_spool_work() -> None:
    engine = _Engine(committed=("b" * 64,))

    result = AgentRetentionCoordinator(cast(RetentionFilesystem, engine), clock=lambda: 31).cycle()

    assert result == "consumed"
    assert engine.calls == [
        ("committed", 1, 1024),
        ("agent-consume", "b" * 64, 31, 31),
    ]


def test_reviewed_production_roots_are_explicit() -> None:
    roots = production_roots()

    assert roots.controller_private.as_posix() == "/var/lib/moodle-autotask"
    assert roots.shared_jobs.as_posix() == "/var/spool/moodle-autotask/jobs"
    assert roots.agent_private.as_posix() == "/var/lib/moodle-agent"
    assert roots.agent_results.as_posix() == "/var/spool/moodle-autotask/results"
    assert roots.agent_workspaces.as_posix() == "/var/lib/moodle-agent/workspaces"
    assert roots.agent_bundles.as_posix() == "/var/spool/moodle-autotask/results/bundles"
