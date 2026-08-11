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
                        "submissiondrafts": 0,
                        "requiresubmissionstatement": 0,
                        "configs": _file_submission_configs(),
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


def test_submission_policies_are_strict_and_revision_bound() -> None:
    payload = _valid()
    assignment = payload["courses"][0]["assignments"][0]  # type: ignore[index]
    assignment["submissiondrafts"] = 0
    direct = parse_assignments(payload, "https://example.test")[0]
    assignment["submissiondrafts"] = 1
    draft = parse_assignments(payload, "https://example.test")[0]

    assert not direct.submission_drafts and draft.submission_drafts
    assert direct.task_key == draft.task_key
    assert direct.revision_digest != draft.revision_digest
    assignment["submissiondrafts"] = True
    with pytest.raises(MoodlePayloadError, match="draft policy"):
        parse_assignments(payload, "https://example.test")
    assignment["submissiondrafts"] = 0
    assignment["requiresubmissionstatement"] = 1
    statement = parse_assignments(payload, "https://example.test")[0]
    assert statement.requires_submission_statement
    assert statement.revision_digest != direct.revision_digest
    assignment.pop("requiresubmissionstatement")
    with pytest.raises(MoodlePayloadError, match="statement policy"):
        parse_assignments(payload, "https://example.test")
    assignment["requiresubmissionstatement"] = 0
    assignment.pop("submissiondrafts")
    with pytest.raises(MoodlePayloadError, match="draft policy"):
        parse_assignments(payload, "https://example.test")


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
                        "submissiondrafts": 0,
                        "requiresubmissionstatement": 0,
                        "configs": _file_submission_configs(),
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


def _file_submission_configs() -> list[dict[str, str]]:
    return [
        {"plugin": "file", "subtype": "assignsubmission", "name": "enabled", "value": "1"},
        {
            "plugin": "file",
            "subtype": "assignsubmission",
            "name": "maxfilesubmissions",
            "value": "1",
        },
        {
            "plugin": "file",
            "subtype": "assignsubmission",
            "name": "maxsubmissionsizebytes",
            "value": "2097152",
        },
        {
            "plugin": "file",
            "subtype": "assignsubmission",
            "name": "filetypeslist",
            "value": ".md",
        },
    ]


def test_warnings_rejected() -> None:
    payload = _valid()
    payload["warnings"] = ["warning"]
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")


def test_assignment_without_file_submission_configs_is_safely_unsupported() -> None:
    payload = _valid()
    assignment = payload["courses"][0]["assignments"][0]  # type: ignore[index]
    assert isinstance(assignment, dict)
    assignment["configs"] = []

    snapshot = parse_assignments(payload, "https://example.test")[0]

    assert not snapshot.file_submission_enabled
    assert (snapshot.file_submission_max_files, snapshot.file_submission_max_bytes) == (0, 0)
    assert snapshot.file_submission_filetypes == ""


def test_feedback_only_configs_do_not_enable_file_submission() -> None:
    payload = _valid()
    assignment = payload["courses"][0]["assignments"][0]  # type: ignore[index]
    assert isinstance(assignment, dict)
    assignment["configs"] = [
        {
            "plugin": "comments",
            "subtype": "assignfeedback",
            "name": "enabled",
            "value": "1",
        }
    ]

    snapshot = parse_assignments(payload, "https://example.test")[0]

    assert not snapshot.file_submission_enabled
    assert (snapshot.file_submission_max_files, snapshot.file_submission_max_bytes) == (0, 0)
    assert snapshot.file_submission_filetypes == ""


@pytest.mark.parametrize("enabled", ("0", "1"))
def test_complete_file_submission_quartet_parses_enabled_and_disabled(enabled: str) -> None:
    payload = _valid()
    assignment = payload["courses"][0]["assignments"][0]  # type: ignore[index]
    assert isinstance(assignment, dict)
    configs = assignment["configs"]
    assert isinstance(configs, list) and isinstance(configs[0], dict)
    configs[0]["value"] = enabled

    snapshot = parse_assignments(payload, "https://example.test")[0]

    assert snapshot.file_submission_enabled is (enabled == "1")
    assert snapshot.file_submission_max_files == 1
    assert snapshot.file_submission_max_bytes == 2_097_152
    assert snapshot.file_submission_filetypes == ".md"


@pytest.mark.parametrize(
    "mutation", ("missing-list", "partial", "duplicate", "malformed", "lookalike")
)
def test_file_submission_configs_match_moodle_521_and_fail_closed(mutation: str) -> None:
    payload = _valid()
    assignment = payload["courses"][0]["assignments"][0]  # type: ignore[index]
    assert isinstance(assignment, dict)
    configs = assignment["configs"]
    assert isinstance(configs, list) and isinstance(configs[0], dict)
    if mutation == "missing-list":
        assignment.pop("configs")
    elif mutation == "partial":
        configs.pop()
    elif mutation == "duplicate":
        configs.append(deepcopy(configs[0]))
    elif mutation == "malformed":
        configs[0] = {"plugin": "file", "subtype": "assignsubmission", "name": "enabled"}
    else:
        configs[0]["name"] = "enabled-typo"
    with pytest.raises(MoodlePayloadError, match="submission configuration"):
        parse_assignments(payload, "https://example.test")


@pytest.mark.parametrize(
    ("index", "value"),
    ((0, "true"), (1, "1.0"), (2, "-1")),
)
def test_file_submission_config_values_fail_closed(index: int, value: str) -> None:
    payload = _valid()
    assignment = payload["courses"][0]["assignments"][0]  # type: ignore[index]
    assert isinstance(assignment, dict)
    configs = assignment["configs"]
    assert isinstance(configs, list) and isinstance(configs[index], dict)
    configs[index]["value"] = value

    with pytest.raises(MoodlePayloadError, match="file submission configuration"):
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
                        "submissiondrafts": 0,
                        "requiresubmissionstatement": 0,
                        "configs": _file_submission_configs(),
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
                        "submissiondrafts": 0,
                        "requiresubmissionstatement": 0,
                        "configs": _file_submission_configs(),
                        "introattachments": [attachment],
                    }
                ],
            }
        ],
    }
    with pytest.raises(MoodlePayloadError):
        parse_assignments(payload, "https://example.test")
