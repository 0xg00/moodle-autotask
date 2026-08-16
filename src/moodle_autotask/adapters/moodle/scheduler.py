"""Continuously runnable, at-least-once local Moodle notification scheduler."""

from __future__ import annotations

import hashlib
import json
import math
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TextIO

from moodle_autotask.health import pulse

from .models import MoodleAssignmentSnapshot
from .state import MoodleState, NotificationAttachment, NotificationDraft, OutboxClaim

MAX_SCHEDULER_COURSES = 64
MAX_SCHEDULER_COURSE_UTF8_BYTES = 2048


class NotificationSink(Protocol):
    def __call__(self, event: object) -> None: ...


class AssignmentService(Protocol):
    def assignments(self) -> tuple[MoodleAssignmentSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class SchedulerOptions:
    lease_seconds: int = 30
    batch_size: int = 20
    retry_base_seconds: int = 5
    retry_max_seconds: int = 3600
    course_shortnames: tuple[str, ...] = ()
    max_new_events_per_cycle: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.lease_seconds, int) or not 6 <= self.lease_seconds <= 3600:
            raise ValueError("lease seconds are invalid")
        if not isinstance(self.batch_size, int) or not 1 <= self.batch_size <= 100:
            raise ValueError("batch size is invalid")
        if not isinstance(self.retry_base_seconds, int) or not 1 <= self.retry_base_seconds <= 3600:
            raise ValueError("retry base seconds are invalid")
        if (
            not isinstance(self.retry_max_seconds, int)
            or not self.retry_base_seconds <= self.retry_max_seconds <= 86400
        ):
            raise ValueError("retry maximum seconds are invalid")
        if (
            not isinstance(self.course_shortnames, tuple)
            or len(self.course_shortnames) > MAX_SCHEDULER_COURSES
        ):
            raise ValueError("course shortnames are invalid")
        if any(
            not isinstance(item, str)
            or not item
            or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in item)
            for item in self.course_shortnames
        ):
            raise ValueError("course shortnames are invalid")
        try:
            course_bytes = tuple(item.encode("utf-8") for item in self.course_shortnames)
        except UnicodeEncodeError as error:
            raise ValueError("course shortnames are invalid") from error
        if any(len(item) > 255 for item in course_bytes):
            raise ValueError("course shortnames are invalid")
        if sum(len(item) for item in course_bytes) > MAX_SCHEDULER_COURSE_UTF8_BYTES:
            raise ValueError("course shortnames are too large")
        if len(set(self.course_shortnames)) != len(self.course_shortnames):
            raise ValueError("course shortnames must be unique")
        object.__setattr__(self, "course_shortnames", tuple(sorted(self.course_shortnames)))
        if (
            not isinstance(self.max_new_events_per_cycle, int)
            or not 1 <= self.max_new_events_per_cycle <= 100
        ):
            raise ValueError("maximum new events per cycle is invalid")

    @property
    def scope_digest(self) -> str:
        payload = json.dumps(
            {
                "course_shortnames": self.course_shortnames,
                "max_new_events_per_cycle": self.max_new_events_per_cycle,
                "version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class CycleResult:
    scan_ok: bool
    enqueued: int
    delivered: int
    delivery_failed: int
    scope_digest: str = ""

    @property
    def ok(self) -> bool:
        return self.scan_ok and self.delivery_failed == 0


class LocalJsonSink:
    """Observable development/service-log sink; consumers deduplicate ``event_id``."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def __call__(self, event: object) -> None:
        from .state import NotificationEvent

        if not isinstance(event, NotificationEvent):
            raise TypeError("notification event is invalid")
        self.stream.write(
            json.dumps(event.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        )
        self.stream.write("\n")
        self.stream.flush()


def draft_from_assignment(assignment: MoodleAssignmentSnapshot) -> NotificationDraft:
    """Copy only the immutable allowlist that local notification may persist or print."""
    artifacts = {".ova", ".ovf", ".iso", ".vdi", ".vmdk", ".vmx"}
    attachments = tuple(
        NotificationAttachment(
            filename=item.filename,
            size_bytes=item.size_bytes,
            mimetype=item.mimetype,
            is_lab_artifact=item.filename.lower().endswith(tuple(artifacts)),
        )
        for item in assignment.attachments
    )
    return NotificationDraft(
        task_key=assignment.task_key,
        revision_digest=assignment.revision_digest,
        course_name=assignment.course_name,
        course_shortname=assignment.course_shortname,
        assignment_title=assignment.title,
        allows_submissions_from=assignment.allows_submissions_from,
        due_date=assignment.due_date,
        cutoff_date=assignment.cutoff_date,
        grading_due_date=assignment.grading_due_date,
        time_modified=assignment.time_modified,
        attachments=attachments,
        assignment_id=assignment.assignment_id,
        submission_drafts=assignment.submission_drafts,
        requires_submission_statement=assignment.requires_submission_statement,
        submission_statement=assignment.submission_statement,
        submission_statement_format=assignment.submission_statement_format,
        team_submission=assignment.team_submission,
        no_submissions=assignment.no_submissions,
        file_submission_enabled=assignment.file_submission_enabled,
        file_submission_max_files=assignment.file_submission_max_files,
        file_submission_max_bytes=assignment.file_submission_max_bytes,
        file_submission_filetypes=assignment.file_submission_filetypes,
    )


def once(
    state: MoodleState,
    service: AssignmentService,
    sink: NotificationSink,
    options: SchedulerOptions | None = None,
    *,
    clock: Callable[[], float] = time.time,
    owner: str | None = None,
) -> CycleResult:
    """Scan then drain persisted events; scan errors never prevent draining."""
    selected = options or SchedulerOptions()
    lease_owner = owner or f"scheduler-{uuid.uuid4().hex}"
    enqueued = 0
    scan_ok = True
    try:
        for assignment in service.assignments():
            if (
                selected.course_shortnames
                and assignment.course_shortname not in selected.course_shortnames
            ):
                continue
            if enqueued >= selected.max_new_events_per_cycle:
                break
            if state.enqueue(draft_from_assignment(assignment), _clock(clock)) is not None:
                enqueued += 1
    except Exception:  # Connector errors are deliberately not rendered to stdout/stderr.
        scan_ok = False
    delivered, failed = drain(state, sink, selected, clock=clock, owner=lease_owner)
    return CycleResult(scan_ok, enqueued, delivered, failed, selected.scope_digest)


def drain(
    state: MoodleState,
    sink: NotificationSink,
    options: SchedulerOptions,
    *,
    clock: Callable[[], float] = time.time,
    owner: str,
) -> tuple[int, int]:
    delivered = 0
    failed = 0
    while True:
        claims = state.claim(owner, options.batch_size, options.lease_seconds, _clock(clock))
        if not claims:
            return delivered, failed
        for claim in claims:
            if _deliver(state, claim, sink, options, clock):
                delivered += 1
            else:
                failed += 1


def _deliver(
    state: MoodleState,
    claim: OutboxClaim,
    sink: NotificationSink,
    options: SchedulerOptions,
    clock: Callable[[], float],
) -> bool:
    stop = threading.Event()
    ownership_lost = threading.Event()

    def heartbeat() -> None:
        # A dedicated MoodleState gives this thread its own SQLite connection.
        interval = options.lease_seconds / 3
        while not stop.wait(interval):
            try:
                if not state.renew(claim, options.lease_seconds, _clock(clock)):
                    ownership_lost.set()
                    return
            except Exception:
                ownership_lost.set()
                return

    worker = threading.Thread(target=heartbeat, name="moodle-outbox-lease", daemon=True)
    worker.start()
    try:
        sink(claim.event)
    except Exception:
        delivery_failed = True
    else:
        delivery_failed = False
    finally:
        stop.set()
        worker.join(timeout=options.lease_seconds / 3 + 1)
    if delivery_failed:
        if not ownership_lost.is_set():
            state.fail(
                claim,
                options.retry_base_seconds,
                options.retry_max_seconds,
                "sink_failed",
                _clock(clock),
            )
        return False
    if ownership_lost.is_set():
        return False
    return state.complete(claim, _clock(clock))


def run(
    state: MoodleState,
    service: AssignmentService,
    sink: NotificationSink,
    options: SchedulerOptions | None = None,
    *,
    interval_seconds: int = 86400,
    clock: Callable[[], float] = time.time,
    wait: Callable[[float], object] = time.sleep,
    emit_summary: Callable[[CycleResult], None] | None = None,
    cycle: Callable[..., CycleResult] = once,
) -> None:
    if not isinstance(interval_seconds, int) or not 1 <= interval_seconds <= 7 * 86400:
        raise ValueError("interval seconds are invalid")
    selected = options or SchedulerOptions()
    while True:
        try:
            pulse("scheduler")
            try:
                result = cycle(state, service, sink, selected, clock=clock)
            except Exception:
                result = CycleResult(False, 0, 0, 1, selected.scope_digest)
            if emit_summary is not None:
                emit_summary(result)
            delay = (
                interval_seconds
                if result.ok
                else min(selected.retry_base_seconds, interval_seconds)
            )
            while delay > 0:
                slice_seconds = min(delay, 60)
                pulse("scheduler")
                wait(slice_seconds)
                delay -= slice_seconds
                pulse("scheduler")
        except KeyboardInterrupt:
            return


def summary_json(result: CycleResult, stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    output.write(
        json.dumps(
            {
                "kind": "moodle-scheduler-cycle-v1",
                "scan": "ok" if result.scan_ok else "error",
                "enqueued": result.enqueued,
                "delivered": result.delivered,
                "delivery_failed": result.delivery_failed,
                "scope_digest": result.scope_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    output.flush()


def _clock(clock: Callable[[], float]) -> int:
    value = clock()
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("clock is invalid")
    return int(value)
