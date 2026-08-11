from __future__ import annotations

import json
from pathlib import Path

import pytest

from moddle_autotask.adapters.moodle import scheduler_config
from moddle_autotask.adapters.moodle.scheduler_config import (
    SchedulerConfigError,
    load_scheduler_config,
    parse_scheduler_config,
)


def test_explicit_scope_config_exactly_preserves_unicode_and_canonicalizes_at_runtime() -> None:
    options = parse_scheduler_config(
        {
            "courseShortnames": ["Straße", "STRASSE", "A😀", "\u200eformat name"],
            "maxNewEventsPerCycle": 4,
        }
    )
    assert options.course_shortnames == ("A😀", "STRASSE", "Straße", "\u200eformat name")
    assert options.max_new_events_per_cycle == 4
    all_courses = parse_scheduler_config({"allCourses": True, "maxNewEventsPerCycle": 100})
    assert all_courses.course_shortnames == ()


@pytest.mark.parametrize(
    "value",
    (
        {},
        {"maxNewEventsPerCycle": 4},
        {"allCourses": False, "maxNewEventsPerCycle": 4},
        {"allCourses": True, "courseShortnames": ["ASIX"], "maxNewEventsPerCycle": 4},
        {"courseShortnames": [], "maxNewEventsPerCycle": 4},
        {"courseShortnames": ["ASIX", "ASIX"], "maxNewEventsPerCycle": 4},
        {"courseShortnames": ["ASIX\u0000"], "maxNewEventsPerCycle": 4},
        {"courseShortnames": ["\ud800"], "maxNewEventsPerCycle": 4},
        {"courseShortnames": ["ASIX"], "maxNewEventsPerCycle": True},
        {"courseShortnames": ["ASIX"], "maxNewEventsPerCycle": 101},
    ),
)
def test_scope_config_rejects_ambiguous_or_unbounded_values(value: object) -> None:
    with pytest.raises(SchedulerConfigError):
        parse_scheduler_config(value)


def test_loader_rejects_duplicate_json_keys_and_uses_no_scope_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "scheduler.json"
    config.write_text(
        '{"courseShortnames":["ASIX"],"courseShortnames":["OTHER"],"maxNewEventsPerCycle":4}',
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler_config, "_validate_parent", lambda path: None)
    monkeypatch.setattr(scheduler_config, "_is_root_owned_config", lambda info: True)
    with pytest.raises(SchedulerConfigError, match="duplicate"):
        load_scheduler_config(config)


def test_loader_accepts_exact_root_owned_shape_after_safe_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "scheduler.json"
    config.write_text(
        json.dumps({"courseShortnames": ["ASIX"], "maxNewEventsPerCycle": 4}),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler_config, "_validate_parent", lambda path: None)
    monkeypatch.setattr(scheduler_config, "_is_root_owned_config", lambda info: True)
    assert load_scheduler_config(config).course_shortnames == ("ASIX",)


def test_scope_config_enforces_shared_count_and_utf8_byte_limits() -> None:
    assert parse_scheduler_config(
        {"courseShortnames": ["😀" * 63], "maxNewEventsPerCycle": 4}
    ).course_shortnames == ("😀" * 63,)
    with pytest.raises(SchedulerConfigError):
        parse_scheduler_config(
            {"courseShortnames": ["A"] * 65, "maxNewEventsPerCycle": 4}
        )
    with pytest.raises(SchedulerConfigError):
        parse_scheduler_config(
            {"courseShortnames": ["😀" * 64], "maxNewEventsPerCycle": 4}
        )
    with pytest.raises(SchedulerConfigError):
        parse_scheduler_config(
            {
                "courseShortnames": [f"{index}" + "A" * 254 for index in range(9)],
                "maxNewEventsPerCycle": 4,
            }
        )
