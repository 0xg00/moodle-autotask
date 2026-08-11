"""Strict, root-owned scheduler scope configuration for controller services."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .scheduler import (
    MAX_SCHEDULER_COURSE_UTF8_BYTES,
    MAX_SCHEDULER_COURSES,
    SchedulerOptions,
)

_MAX_CONFIG_BYTES = 16 * 1024


class SchedulerConfigError(ValueError):
    """The scheduler configuration is unsafe or does not define one exact scope."""


def parse_scheduler_config(value: object) -> SchedulerOptions:
    """Parse the intentionally small on-disk scheduler configuration schema."""
    if not isinstance(value, Mapping):
        raise SchedulerConfigError("scheduler config must be an object")
    keys = set(value)
    has_courses = "courseShortnames" in value
    all_courses = value.get("allCourses")
    if has_courses:
        if keys != {"courseShortnames", "maxNewEventsPerCycle"}:
            raise SchedulerConfigError("explicit scope has unexpected keys")
        raw_courses = value["courseShortnames"]
        if not isinstance(raw_courses, list) or not raw_courses:
            raise SchedulerConfigError("courseShortnames must be a non-empty list")
        courses = tuple(raw_courses)
        if len(courses) > MAX_SCHEDULER_COURSES or any(
            not isinstance(name, str)
            or not name
            or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in name)
            for name in courses
        ):
            raise SchedulerConfigError("courseShortnames are invalid")
        try:
            course_bytes = tuple(name.encode("utf-8") for name in courses)
        except UnicodeEncodeError as error:
            raise SchedulerConfigError("courseShortnames are invalid") from error
        if any(len(name) > 255 for name in course_bytes):
            raise SchedulerConfigError("courseShortnames are invalid")
        if sum(len(name) for name in course_bytes) > MAX_SCHEDULER_COURSE_UTF8_BYTES:
            raise SchedulerConfigError("courseShortnames are too large")
        if len(set(courses)) != len(courses):
            raise SchedulerConfigError("courseShortnames must be unique")
    else:
        if keys != {"allCourses", "maxNewEventsPerCycle"} or all_courses is not True:
            raise SchedulerConfigError("allCourses must be exactly true")
        courses = ()
    cap = value.get("maxNewEventsPerCycle")
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 100:
        raise SchedulerConfigError("maxNewEventsPerCycle is invalid")
    return SchedulerOptions(course_shortnames=courses, max_new_events_per_cycle=cap)


def load_scheduler_config(path: Path) -> SchedulerOptions:
    """Load a bounded root-owned non-link configuration without a fallback scope."""
    if not path.is_absolute():
        raise SchedulerConfigError("scheduler config path must be absolute")
    _validate_parent(path.parent)
    try:
        initial = path.lstat()
    except OSError as error:
        raise SchedulerConfigError("scheduler config is unsafe") from error
    if (
        not stat.S_ISREG(initial.st_mode)
        or stat.S_ISLNK(initial.st_mode)
        or _is_reparse_point(initial)
    ):
        raise SchedulerConfigError("scheduler config is not a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SchedulerConfigError("scheduler config cannot be opened safely") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or _is_reparse_point(info)
            or (initial.st_dev, initial.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise SchedulerConfigError("scheduler config is not a regular file")
        if os.name == "posix" and not _is_root_owned_config(info):
            raise SchedulerConfigError("scheduler config ownership or mode is unsafe")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise SchedulerConfigError("scheduler config is too large")
    finally:
        os.close(descriptor)
    try:
        return parse_scheduler_config(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchedulerConfigError("scheduler config is not valid JSON") from error


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SchedulerConfigError("scheduler config has duplicate keys")
        result[key] = value
    return result


def _validate_parent(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise SchedulerConfigError("scheduler config parent is unsafe") from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        raise SchedulerConfigError("scheduler config parent is unsafe")
    if os.name == "posix" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
        raise SchedulerConfigError("scheduler config parent is unsafe")


def _is_root_owned_config(info: os.stat_result) -> bool:
    if os.name != "posix":
        return True
    try:
        group_id = __import__("grp").getgrnam("moodle-autotask").gr_gid
    except KeyError:
        return False
    return info.st_uid == 0 and info.st_gid == group_id and stat.S_IMODE(info.st_mode) == 0o640
