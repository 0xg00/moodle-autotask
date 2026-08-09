from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast

import pytest

import moddle_autotask.adapters.moodle.scheduler as scheduler_module
from moddle_autotask.adapters.moodle.models import MoodleAssignmentSnapshot, MoodleAttachment
from moddle_autotask.adapters.moodle.scheduler import (
    CycleResult,
    LocalJsonSink,
    SchedulerOptions,
    draft_from_assignment,
    once,
    run,
    summary_json,
)
from moddle_autotask.adapters.moodle.state import MoodleState


def _assignment(letter: str = "a") -> MoodleAssignmentSnapshot:
    attachment = MoodleAttachment(
        "moodle-attachment-v1:" + "c" * 64,
        "introfiles",
        "lab.ova",
        "/",
        "https://example.test/pluginfile.php/1/lab.ova",
        3,
        1,
        "application/octet-stream",
    )
    return MoodleAssignmentSnapshot(
        "moodle-task-v1:" + letter * 64,
        "moodle-assignment-v1:" + "b" * 64,
        "https://example.test",
        1,
        2,
        "Course",
        "COURSE",
        3,
        "Assignment",
        "secret intro https://example.test/private",
        0,
        1,
        2,
        3,
        4,
        (attachment,),
    )


class _Service:
    def __init__(self, values: tuple[MoodleAssignmentSnapshot, ...], fail: bool = False) -> None:
        self.values = values
        self.fail = fail

    def assignments(self) -> tuple[MoodleAssignmentSnapshot, ...]:
        if self.fail:
            raise RuntimeError("https://token.example/secret")
        return self.values


def test_once_outputs_allowlisted_stable_json_and_acknowledges(tmp_path: Path) -> None:
    stream = StringIO()
    state = MoodleState(tmp_path / "state.sqlite3")
    result = once(state, _Service((_assignment(),)), LocalJsonSink(stream), clock=lambda: 10)
    assert result.ok and result.enqueued == result.delivered == 1
    record = json.loads(stream.getvalue())
    assert record["kind"] == "moodle-notification-v1"
    assert record["attachments"] == [
        {
            "filename": "lab.ova",
            "is_lab_artifact": True,
            "mimetype": "application/octet-stream",
            "size_bytes": 3,
        }
    ]
    text = stream.getvalue()
    assert "intro" not in text and "https://" not in text and "fileurl" not in text
    assert (
        once(state, _Service((_assignment(),)), LocalJsonSink(stream), clock=lambda: 10).delivered
        == 0
    )


def test_scan_failure_still_drains_persisted_event(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    first = once(
        state,
        _Service((_assignment(),)),
        lambda event: (_ for _ in ()).throw(RuntimeError()),
        clock=lambda: 10,
    )
    assert not first.ok
    stream = StringIO()
    result = once(
        state,
        _Service((), fail=True),
        LocalJsonSink(stream),
        SchedulerOptions(retry_base_seconds=1),
        clock=lambda: 15,
    )
    assert not result.scan_ok and result.delivered == 1


def test_multiple_pending_revisions_artifacts_and_retry_keep_stable_id(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    first = _assignment()
    second = replace(first, revision_digest="moodle-assignment-v1:" + "d" * 64, title="Changed")
    first_event = state.enqueue(draft_from_assignment(first), now=1)
    second_event = state.enqueue(draft_from_assignment(second), now=1)
    assert first_event is not None and first_event.status == "NEW"
    assert second_event is not None and second_event.status == "UPDATED"
    claimed = state.claim("owner", 2, 6, now=1)
    assert len(claimed) == 2
    assert state.fail(claimed[0], 1, 1, now=1)
    again = state.claim("owner", 1, 6, now=2)[0]
    assert again.event.event_id == claimed[0].event.event_id
    stable = MoodleState(tmp_path / "stable.sqlite3").enqueue(
        draft_from_assignment(replace(first, title="Safe metadata change")), now=1
    )
    assert stable is not None and stable.event_id == first_event.event_id
    positive = ("x.ova", "x.OVF", "x.ISO", "x.vDi", "x.vmdk", "x.vmx")
    negative = ("x.txt", "x.ova.zip", "ova")
    for name in positive + negative:
        attachment = replace(first.attachments[0], filename=name)
        assert draft_from_assignment(replace(first, attachments=(attachment,))).attachments[
            0
        ].is_lab_artifact == (name in positive)

    drained_stream = StringIO()
    drained = once(
        MoodleState(tmp_path / "drained.sqlite3"),
        _Service((first, second)),
        LocalJsonSink(drained_stream),
        clock=lambda: 10,
    )
    assert drained.delivered == 2 and len(drained_stream.getvalue().splitlines()) == 2


def test_local_json_sink_flushes_exact_line(tmp_path: Path) -> None:
    class Stream(StringIO):
        flushed = 0

        def flush(self) -> None:
            self.flushed += 1
            super().flush()

    stream = Stream()
    event = MoodleState(tmp_path / "state.sqlite3").enqueue(
        draft_from_assignment(_assignment()), now=1
    )
    assert event is not None
    LocalJsonSink(stream)(event)
    assert (
        stream.getvalue()
        == json.dumps(event.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    assert stream.flushed == 1


def test_run_recovers_without_leaking_error() -> None:
    stream = StringIO()
    waits: list[float] = []
    calls = 0

    def cycle(*args: object, **kwargs: object) -> CycleResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("SENTINEL_SECRET")
        return CycleResult(True, 0, 0, 0)

    def wait(value: float) -> None:
        waits.append(value)
        if len(waits) == 2:
            raise KeyboardInterrupt

    run(
        cast(MoodleState, object()),
        cast(Any, object()),
        cast(Any, object()),
        SchedulerOptions(retry_base_seconds=3),
        interval_seconds=9,
        wait=wait,
        emit_summary=lambda result: summary_json(result, stream),
        cycle=cycle,
    )
    assert waits == [3, 9]
    assert [json.loads(line) for line in stream.getvalue().splitlines()] == [
        {
            "kind": "moodle-scheduler-cycle-v1",
            "scan": "error",
            "enqueued": 0,
            "delivered": 0,
            "delivery_failed": 1,
        },
        {
            "kind": "moodle-scheduler-cycle-v1",
            "scan": "ok",
            "enqueued": 0,
            "delivered": 0,
            "delivery_failed": 0,
        },
    ]
    assert "SENTINEL_SECRET" not in stream.getvalue()


def test_run_interrupts_cleanly_during_recovery_backoff() -> None:
    stream = StringIO()
    waits: list[float] = []

    def cycle(*args: object, **kwargs: object) -> CycleResult:
        raise RuntimeError("SENTINEL_RECOVERY_SECRET")

    def wait(value: float) -> None:
        waits.append(value)
        raise KeyboardInterrupt

    run(
        cast(MoodleState, object()),
        cast(Any, object()),
        cast(Any, object()),
        SchedulerOptions(retry_base_seconds=3),
        interval_seconds=9,
        wait=wait,
        emit_summary=lambda result: summary_json(result, stream),
        cycle=cycle,
    )
    assert waits == [3]
    assert json.loads(stream.getvalue()) == {
        "kind": "moodle-scheduler-cycle-v1",
        "scan": "error",
        "enqueued": 0,
        "delivered": 0,
        "delivery_failed": 1,
    }
    assert "SENTINEL_RECOVERY_SECRET" not in stream.getvalue()


def test_run_interrupts_cleanly_while_emitting_recovery_summary() -> None:
    waits: list[float] = []

    def cycle(*args: object, **kwargs: object) -> CycleResult:
        raise RuntimeError("SENTINEL_EMIT_SECRET")

    def emit_summary(result: CycleResult) -> None:
        assert result == CycleResult(False, 0, 0, 1)
        raise KeyboardInterrupt

    run(
        cast(MoodleState, object()),
        cast(Any, object()),
        cast(Any, object()),
        SchedulerOptions(retry_base_seconds=3),
        interval_seconds=9,
        wait=lambda value: waits.append(value),
        emit_summary=emit_summary,
        cycle=cycle,
    )
    assert waits == []


def test_heartbeat_renews_and_ownership_loss_skips_complete(tmp_path: Path) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    state.enqueue(draft_from_assignment(_assignment()), now=1)
    claim = state.claim("owner", 1, 6, now=1)[0]
    entered, renewed, release = Event(), Event(), Event()

    class Wrapper:
        path = state.path
        complete_calls = 0

        def renew(self, item: object, seconds: int, now: int) -> bool:
            renewed.set()
            return bool(state.renew(claim, seconds, now=1))

        def complete(self, item: object, now: int) -> bool:
            self.complete_calls += 1
            return bool(state.complete(claim, now=1))

        def fail(self, *args: object) -> bool:
            return bool(state.fail(claim, 1, 1, now=1))

    wrapper = Wrapper()

    def sink(value: object) -> None:
        entered.set()
        assert release.wait(4)

    worker = Thread(
        target=lambda: scheduler_module._deliver(
            cast(MoodleState, wrapper),
            claim,
            cast(Any, sink),
            SchedulerOptions(lease_seconds=6),
            lambda: 1,
        )
    )
    worker.start()
    assert entered.wait(1) and renewed.wait(4)
    assert not state.claim("other", 1, 6, now=2)
    release.set()
    worker.join(5)
    assert wrapper.complete_calls == 1

    class Lost(Wrapper):
        loss_renewed = Event()

        def renew(self, item: object, seconds: int, now: int) -> bool:
            self.loss_renewed.set()
            return False

    lost = Lost()
    release.clear()
    entered.clear()
    renewed.clear()
    worker = Thread(
        target=lambda: scheduler_module._deliver(
            cast(MoodleState, lost),
            claim,
            cast(Any, sink),
            SchedulerOptions(lease_seconds=6),
            lambda: 1,
        )
    )
    worker.start()
    assert entered.wait(1)
    assert lost.loss_renewed.wait(4)
    release.set()
    worker.join(5)
    assert lost.complete_calls == 0


def test_keyboard_interrupt_stops_heartbeat_without_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = MoodleState(tmp_path / "state.sqlite3")
    state.enqueue(draft_from_assignment(_assignment()), now=1)
    claim = state.claim("owner", 1, 6, now=1)[0]
    created: list[Thread] = []
    real_thread = Thread

    def capture_thread(*args: Any, **kwargs: Any) -> Thread:
        worker = real_thread(*args, **kwargs)
        created.append(worker)
        return worker

    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler.threading.Thread", capture_thread
    )

    class State:
        complete_calls = 0
        fail_calls = 0

        def renew(self, *args: object) -> bool:
            return True

        def complete(self, *args: object) -> bool:
            self.complete_calls += 1
            return True

        def fail(self, *args: object) -> bool:
            self.fail_calls += 1
            return True

    wrapper = State()

    def sink(event: object) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scheduler_module._deliver(
            cast(MoodleState, wrapper),
            claim,
            cast(Any, sink),
            SchedulerOptions(lease_seconds=6),
            lambda: 1,
        )
    assert len(created) == 1
    assert not created[0].is_alive()
    assert wrapper.complete_calls == 0
    assert wrapper.fail_calls == 0
