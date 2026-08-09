from __future__ import annotations

from copy import deepcopy

import pytest

from moddle_autotask.adapters.moodle.models import MoodlePayloadError, parse_assignments


def test_assignment_keys_and_revision_are_stable() -> None:
    payload = {
        "warnings": [],
        "courses": [
            {
                "id": 4,
                "fullname": "Course",
                "shortname": "COURSE",
                "assignments": [
                    {
                        "id": 5,
                        "cmid": 6,
                        "name": "Task",
                        "intro": "Read",
                        "timemodified": 1,
                        "introattachments": [
                            {
                                "filename": "brief.txt",
                                "filepath": "/",
                                "filesize": 2,
                                "timemodified": 1,
                                "mimetype": "text/plain",
                                "isexternalfile": False,
                                "fileurl": "https://example.test/webservice/pluginfile.php/a",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    first = parse_assignments(payload, "https://example.test")[0]
    second = parse_assignments(payload, "https://example.test")[0]
    assert first.task_key == second.task_key
    assert first.revision_digest == second.revision_digest
    assert first.attachments[0].attachment_key.startswith("moodle-attachment-v1:")


def _valid() -> dict[str, object]:
    return {
        "warnings": [],
        "courses": [
            {
                "id": 1,
                "fullname": "C",
                "shortname": "C",
                "assignments": [
                    {
                        "id": 2,
                        "cmid": 3,
                        "name": "A",
                        "timemodified": 1,
                        "introattachments": [
                            {
                                "filename": "x.txt",
                                "filepath": "/",
                                "filesize": 1,
                                "timemodified": 1,
                                "isexternalfile": False,
                                "fileurl": "https://example.test/webservice/pluginfile.php/x",
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_warnings_rejected() -> None:
    payload = _valid()
    payload["warnings"] = ["warning"]
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")


def test_missing_or_blank_shortname_rejected() -> None:
    payload = _valid()
    payload["courses"][0].pop("shortname")  # type: ignore[index]
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")


def test_missing_external_file_fields_rejected() -> None:
    payload = _valid()
    del payload["courses"][0]["assignments"][0]["introattachments"][0]["filename"]  # type: ignore[index]
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")


def test_external_file_rejected() -> None:
    payload = _valid()
    payload["courses"][0]["assignments"][0]["introattachments"][0]["isexternalfile"] = True  # type: ignore[index]
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")


def test_boolean_assignment_id_rejected() -> None:
    payload = _valid()
    payload["courses"][0]["assignments"][0]["id"] = True  # type: ignore[index]
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")


def test_duplicate_assignment_rejected() -> None:
    payload = _valid()
    payload["courses"][0]["assignments"].append(deepcopy(payload["courses"][0]["assignments"][0]))  # type: ignore[index]
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")


def test_duplicate_attachment_identity_rejected() -> None:
    attachment = {
        "filename": "x",
        "filepath": "/",
        "filesize": 1,
        "timemodified": 1,
        "isexternalfile": False,
        "fileurl": "https://example.test/webservice/pluginfile.php/x",
    }
    payload = {
        "warnings": [],
        "courses": [
            {
                "id": 1,
                "fullname": "C",
                "assignments": [
                    {
                        "id": 2,
                        "cmid": 3,
                        "name": "A",
                        "timemodified": 1,
                        "introattachments": [attachment, attachment],
                    }
                ],
            }
        ],
    }
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")


def test_userinfo_attachment_url_rejected() -> None:
    attachment = {
        "filename": "x",
        "filepath": "/",
        "filesize": 1,
        "timemodified": 1,
        "isexternalfile": False,
        "fileurl": "https://x@example.test/webservice/pluginfile.php/x",
    }
    payload = {
        "warnings": [],
        "courses": [
            {
                "id": 1,
                "fullname": "C",
                "assignments": [
                    {
                        "id": 2,
                        "cmid": 3,
                        "name": "A",
                        "timemodified": 1,
                        "introattachments": [attachment],
                    }
                ],
            }
        ],
    }
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")
