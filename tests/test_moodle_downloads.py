from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from moodle_http_support import moodle_server

from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.downloads import MoodleDownloadError, download_attachment
from moddle_autotask.adapters.moodle.models import (
    MoodleAssignmentSnapshot,
    MoodlePayloadError,
    parse_assignments,
)


def _assignment(base: str, url: str, size: int = 3) -> MoodleAssignmentSnapshot:
    return parse_assignments(
        {
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
                                    "filename": "same.txt",
                                    "filepath": "/",
                                    "filesize": size,
                                    "timemodified": 1,
                                    "mimetype": "text/plain",
                                    "isexternalfile": False,
                                    "fileurl": url,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        base,
    )[0]


def test_pluginfile_download_receipt_and_percent_encoded_token(tmp_path: Path) -> None:
    with moodle_server(body=b"abc", headers={"Content-Length": "3"}) as (base, handler):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/space%20name.txt")
        receipt = download_attachment(
            MoodleConnectionConfig(base, "a b"),
            assignment,
            assignment.attachments[0].attachment_key,
            tmp_path,
        )
        assert receipt.sha256 == hashlib.sha256(b"abc").hexdigest()
        assert receipt.path.read_bytes() == b"abc"
        assert "token=a+b" in handler.requests[0][1]
        assert "space%20name.txt" in handler.requests[0][1]
        assert assignment.task_key.rsplit(":", 1)[-1] in str(receipt.path)


@pytest.mark.parametrize(
    "remainder",
    ["../file", "%2e%2e/file", "%252e%252e/file", "file%2fname", "file%5cname"],
)
def test_pluginfile_route_confusion_is_rejected_before_network(
    tmp_path: Path, remainder: str
) -> None:
    sentinel = "SENTINEL_SECRET_DO_NOT_LOG"
    with moodle_server(body=b"abc", headers={"Content-Length": "3"}) as (base, handler):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/{remainder}")
        with pytest.raises(MoodleDownloadError, match="allowed") as error:
            download_attachment(
                MoodleConnectionConfig(base, sentinel),
                assignment,
                assignment.attachments[0].attachment_key,
                tmp_path,
            )
    assert not handler.requests
    assert sentinel not in str(error.value)


def test_overdepth_pluginfile_encoding_is_rejected_before_output_or_network(tmp_path: Path) -> None:
    output = tmp_path / "output"
    remainder = "safe/%252525252e/file"
    with moodle_server(body=b"abc", headers={"Content-Length": "3"}) as (base, handler):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/{remainder}")
        with pytest.raises(MoodleDownloadError, match="allowed"):
            download_attachment(
                MoodleConnectionConfig(base, "x"),
                assignment,
                assignment.attachments[0].attachment_key,
                output,
            )
    assert not handler.requests
    assert not output.exists()


def test_download_redirect_is_not_followed(tmp_path: Path) -> None:
    with moodle_server(302, b"", {"Location": "/x"}) as (base, handler):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/x")
        with pytest.raises(MoodleDownloadError, match="redirect"):
            download_attachment(
                MoodleConnectionConfig(base, "x"),
                assignment,
                assignment.attachments[0].attachment_key,
                tmp_path,
            )
        assert len(handler.requests) == 1


def test_download_cross_origin_rejected_before_network(tmp_path: Path) -> None:
    assignment = _assignment(
        "http://127.0.0.1:8000", "http://127.0.0.1:8001/webservice/pluginfile.php/x"
    )
    with pytest.raises(MoodleDownloadError, match="allowed"):
        download_attachment(
            MoodleConnectionConfig("http://127.0.0.1:8000", "x"),
            assignment,
            assignment.attachments[0].attachment_key,
            tmp_path,
        )


def test_download_query_url_rejected(tmp_path: Path) -> None:
    with pytest.raises(MoodlePayloadError):
        _assignment(
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8000/webservice/pluginfile.php/x?q=1",
        )


def test_download_declared_size_cap_rejected(tmp_path: Path) -> None:
    with moodle_server(body=b"abc") as (base, _):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/x", 4)
        with pytest.raises(MoodleDownloadError, match="declared"):
            download_attachment(
                MoodleConnectionConfig(base, "x"),
                assignment,
                assignment.attachments[0].attachment_key,
                tmp_path,
                3,
            )


def test_download_content_length_mismatch_cleans_partial(tmp_path: Path) -> None:
    with moodle_server(body=b"abc", headers={"Content-Length": "2"}) as (base, _):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/x")
        with pytest.raises(MoodleDownloadError):
            download_attachment(
                MoodleConnectionConfig(base, "x"),
                assignment,
                assignment.attachments[0].attachment_key,
                tmp_path,
            )
    assert not list(tmp_path.rglob("*.part"))


def test_download_short_body_cleans_partial(tmp_path: Path) -> None:
    with moodle_server(body=b"ab", headers={"Content-Length": "2"}) as (base, _):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/x", 3)
        with pytest.raises(MoodleDownloadError, match="size"):
            download_attachment(
                MoodleConnectionConfig(base, "x"),
                assignment,
                assignment.attachments[0].attachment_key,
                tmp_path,
            )
    assert not list(tmp_path.rglob("*.part"))


def test_download_wrong_attachment_key_rejected(tmp_path: Path) -> None:
    assignment = _assignment(
        "http://127.0.0.1:8000", "http://127.0.0.1:8000/webservice/pluginfile.php/x"
    )
    with pytest.raises(MoodleDownloadError, match="does not belong"):
        download_attachment(
            MoodleConnectionConfig("http://127.0.0.1:8000", "x"), assignment, "bad", tmp_path
        )


def test_download_streamed_overrun_cleans_partial(tmp_path: Path) -> None:
    with moodle_server(body=b"abcd", headers={"Content-Length": "4"}) as (base, _):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/x", 3)
        with pytest.raises(MoodleDownloadError, match="Content-Length"):
            download_attachment(
                MoodleConnectionConfig(base, "x"),
                assignment,
                assignment.attachments[0].attachment_key,
                tmp_path,
            )
    assert not list(tmp_path.rglob("*.part"))


def test_same_name_different_revisions_do_not_collide(tmp_path: Path) -> None:
    with moodle_server(body=b"abc", headers={"Content-Length": "3"}) as (base, _):
        first = _assignment(base, f"{base}/webservice/pluginfile.php/one")
        payload = {
            "warnings": [],
            "courses": [
                {
                    "id": 1,
                    "fullname": "C",
                    "shortname": "C",
                    "assignments": [
                        {
                            "id": 9,
                            "cmid": 10,
                            "name": "B",
                            "timemodified": 1,
                            "introattachments": [
                                {
                                    "filename": "same.txt",
                                    "filepath": "/",
                                    "filesize": 3,
                                    "timemodified": 1,
                                    "isexternalfile": False,
                                    "fileurl": f"{base}/webservice/pluginfile.php/two",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        second = parse_assignments(payload, base)[0]
        config = MoodleConnectionConfig(base, "x")
        a = download_attachment(config, first, first.attachments[0].attachment_key, tmp_path)
        b = download_attachment(config, second, second.attachments[0].attachment_key, tmp_path)
    assert a.path != b.path


def test_same_task_revision_attachment_names_do_not_collide(tmp_path: Path) -> None:
    with moodle_server(body=b"one", headers={"Content-Length": "3"}) as (base, handler):
        assignment = parse_assignments(
            {
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
                                "introfiles": [
                                    {
                                        "filename": "same.txt",
                                        "filepath": "/",
                                        "filesize": 3,
                                        "timemodified": 1,
                                        "isexternalfile": False,
                                        "fileurl": f"{base}/webservice/pluginfile.php/one",
                                    }
                                ],
                                "activityattachments": [
                                    {
                                        "filename": "same.txt",
                                        "filepath": "/other/",
                                        "filesize": 3,
                                        "timemodified": 1,
                                        "isexternalfile": False,
                                        "fileurl": f"{base}/webservice/pluginfile.php/two",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            base,
        )[0]
        first, second = assignment.attachments
        config = MoodleConnectionConfig(base, "x")
        first_receipt = download_attachment(config, assignment, first.attachment_key, tmp_path)
        handler.body = b"two"
        second_receipt = download_attachment(config, assignment, second.attachment_key, tmp_path)
        handler.body = b"one"
        repeated_first = download_attachment(config, assignment, first.attachment_key, tmp_path)
    assert first_receipt.path != second_receipt.path
    assert first_receipt.path.read_bytes() == b"one"
    assert second_receipt.path.read_bytes() == b"two"
    assert repeated_first.path == first_receipt.path


def test_forged_attachment_key_is_rejected_before_network(tmp_path: Path) -> None:
    with moodle_server(body=b"abc", headers={"Content-Length": "3"}) as (base, handler):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/x")
        forged_attachment = replace(
            assignment.attachments[0], attachment_key="moodle-attachment-v1:not-a-digest"
        )
        forged_assignment = replace(assignment, attachments=(forged_attachment,))
        with pytest.raises(MoodleDownloadError, match="key is invalid"):
            download_attachment(
                MoodleConnectionConfig(base, "x"),
                forged_assignment,
                forged_attachment.attachment_key,
                tmp_path,
            )
    assert not handler.requests


@pytest.mark.parametrize(
    ("field", "forged_value", "message"),
    [
        ("task_key", "moodle-task-v1:../../escape", "task key"),
        ("revision_digest", "moodle-assignment-v1:../../escape", "revision digest"),
    ],
)
def test_forged_task_or_revision_key_is_rejected_before_output_or_network(
    tmp_path: Path, field: str, forged_value: str, message: str
) -> None:
    output = tmp_path / "output"
    with moodle_server(body=b"abc", headers={"Content-Length": "3"}) as (base, handler):
        assignment = _assignment(base, f"{base}/webservice/pluginfile.php/x")
        if field == "task_key":
            forged_assignment = replace(assignment, task_key=forged_value)
        else:
            forged_assignment = replace(assignment, revision_digest=forged_value)
        with pytest.raises(MoodleDownloadError, match=message):
            download_attachment(
                MoodleConnectionConfig(base, "x"),
                forged_assignment,
                forged_assignment.attachments[0].attachment_key,
                output,
            )
    assert not handler.requests
    assert not output.exists()


def test_output_symlink_is_rejected_without_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "output"
    try:
        output.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    assignment = _assignment(
        "http://127.0.0.1:8000", "http://127.0.0.1:8000/webservice/pluginfile.php/x"
    )
    with pytest.raises(MoodleDownloadError, match="unsafe"):
        download_attachment(
            MoodleConnectionConfig("http://127.0.0.1:8000", "x"),
            assignment,
            assignment.attachments[0].attachment_key,
            output,
        )
    assert not list(target.rglob("*.part"))
