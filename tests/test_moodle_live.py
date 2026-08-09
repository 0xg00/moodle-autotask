"""Optional full-path verification against the deterministic local Moodle fixture."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import TypeVar

import pytest

from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.downloads import download_attachment
from moddle_autotask.adapters.moodle.models import MoodleAssignmentSnapshot
from moddle_autotask.adapters.moodle.scheduler import LocalJsonSink, once
from moddle_autotask.adapters.moodle.service import MoodleService
from moddle_autotask.adapters.moodle.state import MoodleState

_Result = TypeVar("_Result")


def _live_or_fail(operation: Callable[[], _Result]) -> _Result:
    """Keep connector request internals, including credentials, out of pytest output."""
    try:
        return operation()
    except Exception:
        pytest.fail("local Moodle live connector operation failed", pytrace=False)


def _live_config(token_file: Path) -> MoodleConnectionConfig:
    return replace(MoodleConnectionConfig.from_token_file(token_file), timeout_seconds=120)


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_fixture_attachment_downloads(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    service = MoodleService(_live_config(token_file))
    assignment = next(
        item for item in _live_or_fail(service.assignments) if item.title == "AutoTask assignment"
    )
    attachment = next(
        item for item in assignment.attachments if item.filename == "autotask-brief.txt"
    )
    receipt = _live_or_fail(
        lambda: download_attachment(service.config, assignment, attachment.attachment_key, tmp_path)
    )
    assert receipt.path.read_text(encoding="utf-8").startswith("AutoTask local fixture brief.")
    assert receipt.size_bytes > 0


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_fixture_scheduler_once_is_idempotent(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    live = MoodleService(_live_config(token_file))

    class FixtureService:
        def assignments(self) -> tuple[MoodleAssignmentSnapshot, ...]:
            return tuple(
                item
                for item in _live_or_fail(live.assignments)
                if item.title == "AutoTask assignment"
            )

    stream = StringIO()
    state = MoodleState(tmp_path / "scheduler.sqlite3")
    first = _live_or_fail(lambda: once(state, FixtureService(), LocalJsonSink(stream)))
    assert first.enqueued == first.delivered == 1
    record = json.loads(stream.getvalue())
    assert record["assignment_title"] == "AutoTask assignment"
    assert record["attachments"] == [
        {
            "filename": "autotask-brief.txt",
            "size_bytes": 76,
            "mimetype": "text/plain",
            "is_lab_artifact": False,
        }
    ]
    second = _live_or_fail(lambda: once(state, FixtureService(), LocalJsonSink(StringIO())))
    assert second.enqueued == second.delivered == 0


def test_live_failure_helper_redacts_exception_text() -> None:
    sentinel = "SENTINEL_LIVE_TOKEN"
    with pytest.raises(pytest.fail.Exception) as failure:
        _live_or_fail(lambda: (_ for _ in ()).throw(RuntimeError(sentinel)))
    assert str(failure.value) == "local Moodle live connector operation failed"
