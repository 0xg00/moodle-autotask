"""Bounded service-loop coordination for the durable retention protocol."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from moddle_autotask.adapters.moodle.approval_state import ApprovalState

from .retention import plan_retention
from .retention_fs import (
    RetentionCapacityError,
    RetentionFilesystem,
    RetentionFilesystemError,
    RetentionOwnership,
    RetentionRoots,
)

DEFAULT_SCRATCH_TTL = 86_400
DEFAULT_EVIDENCE_TTL = 604_800
DEFAULT_SCAN_LIMIT = 1_024
DEFAULT_CANDIDATE_LIMIT = 1_024


def production_roots() -> RetentionRoots:
    """Return the reviewed host paths; no caller-relative defaults are allowed."""
    return RetentionRoots(
        controller_private=Path("/var/lib/moodle-autotask"),
        shared_jobs=Path("/var/spool/moodle-autotask/jobs"),
        agent_private=Path("/var/lib/moodle-agent"),
        agent_results=Path("/var/spool/moodle-autotask/results"),
        agent_workspaces=Path("/var/lib/moodle-agent/workspaces"),
        agent_bundles=Path("/var/spool/moodle-autotask/results/bundles"),
    )


def production_ownership() -> RetentionOwnership:
    """Resolve the split service accounts and reject an unsafe host identity map."""
    try:
        grp = importlib.import_module("grp")
        pwd = importlib.import_module("pwd")
        controller = pwd.getpwnam("moodle-autotask")
        agent = pwd.getpwnam("moodle-agent")
        controller_group = grp.getgrnam("moodle-autotask")
        agent_group = grp.getgrnam("moodle-agent")
    except (ImportError, KeyError) as error:
        raise RetentionFilesystemError("retention service identity is unavailable") from error
    if (
        controller.pw_uid == 0
        or agent.pw_uid == 0
        or controller.pw_gid == 0
        or agent.pw_gid == 0
        or controller.pw_uid == agent.pw_uid
        or controller_group.gr_gid == agent_group.gr_gid
        or controller.pw_gid != controller_group.gr_gid
        or agent.pw_gid != agent_group.gr_gid
    ):
        raise RetentionFilesystemError("retention service identity is unsafe")
    return RetentionOwnership(
        controller_uid=controller.pw_uid,
        agent_uid=agent.pw_uid,
        controller_gid=controller_group.gr_gid,
        agent_gid=agent_group.gr_gid,
    )


def production_engine() -> RetentionFilesystem:
    return RetentionFilesystem(production_roots(), production_ownership())


@dataclass(slots=True)
class ControllerRetentionCoordinator:
    """Perform at most one controller-owned retention mutation per service loop."""

    state: ApprovalState
    engine: RetentionFilesystem
    scratch_ttl: int = DEFAULT_SCRATCH_TTL
    evidence_ttl: int = DEFAULT_EVIDENCE_TTL
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT
    scan_limit: int = DEFAULT_SCAN_LIMIT
    clock: Callable[[], int] = lambda: int(time.time())
    _skip_reconciliation_once: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_runtime_limits(
            self.scratch_ttl, self.evidence_ttl, self.candidate_limit, self.scan_limit
        )

    def cycle(self, *, now: int | None = None) -> str:
        moment = _runtime_now(self.clock if now is None else lambda: now)
        if self._skip_reconciliation_once:
            self._skip_reconciliation_once = False
        else:
            reconciliation = self.state.retention_reconciliation_page(limit=self.scan_limit)
            if reconciliation.state == "wrapped":
                self._skip_reconciliation_once = True
                return "reconciliation-wrapped"
            if reconciliation.state == "empty_initial":
                page = None
            else:
                page = reconciliation.page
            if page is not None and not all(
                self.engine.is_completed(item.tombstone_id) for item in page.receipts
            ):
                raise RetentionFilesystemError("retention completion receipt conflicts")
            if page is not None:
                self.state.advance_retention_reconciliation(page)
                return "reconciliation-advanced"
        acknowledgements = self.engine.actionable_acks(limit=1, scan_limit=self.scan_limit)
        if acknowledgements:
            self.engine.controller_consume_ack(acknowledgements[0])
            return "ack-consumed"
        records = self.state.retention_records(
            moment, self.scratch_ttl, self.evidence_ttl, self.candidate_limit
        )
        plans = plan_retention(records, now=moment, limit=self.candidate_limit)
        completed = tuple(plan for plan in plans if self.engine.is_completed(plan))
        if completed:
            inserted = self.state.record_retention_completions(completed, completed_at=moment)
            if inserted is not False:
                return "reconciled"
        actionable = plan_retention(
            records,
            now=moment,
            limit=1,
            completed=self.engine.is_completed,
        )
        if not actionable:
            return "idle"
        try:
            self.engine.commit(actionable[0], committed_at=moment)
        except RetentionCapacityError:
            # Soft closure is transient: no prepared publication was made for a
            # new chain, so the next bounded loop may retry safely.
            return "capacity-closed"
        return "committed"


@dataclass(slots=True)
class AgentRetentionCoordinator:
    """Perform at most one agent-owned tombstone consumption per service loop."""

    engine: RetentionFilesystem
    scan_limit: int = DEFAULT_SCAN_LIMIT
    clock: Callable[[], int] = lambda: int(time.time())

    def __post_init__(self) -> None:
        if type(self.scan_limit) is not int or not 1 <= self.scan_limit <= 10_000:
            raise ValueError("retention scan limit is invalid")

    def cycle(self, *, now: int | None = None) -> str:
        moment = _runtime_now(self.clock if now is None else lambda: now)
        committed = self.engine.actionable_committed(limit=1, scan_limit=self.scan_limit)
        if not committed:
            return "idle"
        self.engine.agent_consume(committed[0], acknowledged_at=moment, now=moment)
        return "consumed"


def _runtime_now(clock: Callable[[], int]) -> int:
    moment = clock()
    if type(moment) is not int or moment < 0:
        raise ValueError("retention clock is invalid")
    return moment


def _validate_runtime_limits(
    scratch_ttl: int, evidence_ttl: int, candidate_limit: int, scan_limit: int
) -> None:
    if type(scratch_ttl) is not int or not 1 <= scratch_ttl <= 90 * 24 * 3600:
        raise ValueError("scratch retention TTL is invalid")
    if type(evidence_ttl) is not int or not 1 <= evidence_ttl <= 90 * 24 * 3600:
        raise ValueError("evidence retention TTL is invalid")
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 10_000:
        raise ValueError("retention candidate limit is invalid")
    if type(scan_limit) is not int or not 1 <= scan_limit <= 10_000:
        raise ValueError("retention scan limit is invalid")
