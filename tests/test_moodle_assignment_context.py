from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from moodle_autotask.adapters.moodle.assignment_context import apply_assignment_context
from moodle_autotask.adapters.moodle.models import MoodleAssignmentSnapshot, MoodlePayloadError


def _assignment() -> MoodleAssignmentSnapshot:
    return MoodleAssignmentSnapshot(
        "moodle-task-v1:" + "a" * 64,
        "moodle-assignment-v1:" + "b" * 64,
        "https://example.test",
        5,
        4,
        "Course",
        "C",
        6,
        "Assignment",
        "Original",
        0,
        0,
        0,
        0,
        0,
        (),
    )


def _context() -> dict[str, object]:
    return {
        "schema": "moodle-assignment-context-v1",
        "siteUrl": "https://example.test",
        "assignments": [
            {
                "assignmentId": 5,
                "baseRevision": _assignment().revision_digest,
                "text": "Source: https://example.test/page/1\nCreate <XML> inside a ZIP.",
                "submissionFormat": "archive",
            }
        ],
    }


def _write(tmp_path: Path, raw: object) -> Path:
    path = tmp_path / "context.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_context_binds_source_and_output_format_without_changing_task_identity(
    tmp_path: Path,
) -> None:
    assignment = _assignment()
    raw = _context()
    path = _write(tmp_path, raw)
    (first,) = apply_assignment_context((assignment,), assignment.site_url, path)
    assert first.task_key == assignment.task_key
    assert first.revision_digest != assignment.revision_digest
    assert first.intro.startswith("Original\n") and "&lt;XML&gt;" in first.intro
    assert first.submission_format == "archive"
    assert apply_assignment_context((assignment,), assignment.site_url, path) == (first,)
    for field, value in (("text", "Changed source"), ("submissionFormat", "markdown")):
        modified = _context()
        modified["assignments"][0][field] = value  # type: ignore[index]
        (second,) = apply_assignment_context(
            (assignment,), assignment.site_url, _write(tmp_path, modified)
        )
        assert second.revision_digest != first.revision_digest


def test_absent_context_preserves_existing_revisions() -> None:
    assignment = _assignment()
    assert apply_assignment_context((assignment,), assignment.site_url, None) == (assignment,)


def test_context_does_not_attach_to_other_assignments(tmp_path: Path) -> None:
    assignment = replace(_assignment(), assignment_id=10)
    assert apply_assignment_context(
        (assignment,), assignment.site_url, _write(tmp_path, _context())
    ) == (assignment,)


def test_changed_remote_assignment_requires_updated_context(tmp_path: Path) -> None:
    assignment = replace(_assignment(), revision_digest="moodle-assignment-v1:" + "c" * 64)
    with pytest.raises(MoodlePayloadError, match="updated source revision"):
        apply_assignment_context((assignment,), assignment.site_url, _write(tmp_path, _context()))


@pytest.mark.parametrize("case", ("site", "duplicate", "field", "format", "id", "revision", "text"))
def test_invalid_context_fails_closed(tmp_path: Path, case: str) -> None:
    raw = _context()
    entries = raw["assignments"]
    assert isinstance(entries, list)
    if case == "site":
        raw["siteUrl"] = "https://other.test"
    elif case == "duplicate":
        entries.append(entries[0])
    else:
        key, value = {
            "field": ("unknown", "x"),
            "format": ("submissionFormat", "binary"),
            "id": ("assignmentId", True),
            "revision": ("baseRevision", "bad"),
            "text": ("text", "x" * (128 * 1024 + 1)),
        }[case]
        entries[0][key] = value
    with pytest.raises(MoodlePayloadError):
        apply_assignment_context((_assignment(),), "https://example.test", _write(tmp_path, raw))


def test_configured_missing_file_is_not_silently_ignored(tmp_path: Path) -> None:
    with pytest.raises(MoodlePayloadError):
        apply_assignment_context((_assignment(),), "https://example.test", tmp_path / "missing")


def test_context_rejects_duplicate_json_keys_and_oversize_file(tmp_path: Path) -> None:
    path = _write(tmp_path, _context())
    for content in ('{"schema":1,"schema":2}', " " * (1024 * 1024 + 1)):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(MoodlePayloadError):
            apply_assignment_context((_assignment(),), "https://example.test", path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_writable_or_linked_context_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, _context())
    path.chmod(0o666)
    with pytest.raises(MoodlePayloadError):
        apply_assignment_context((_assignment(),), "https://example.test", path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(MoodlePayloadError):
        apply_assignment_context((_assignment(),), "https://example.test", link)
