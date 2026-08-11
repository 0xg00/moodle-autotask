from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from moddle_autotask.adapters.moodle.approval_state import SubmissionManifest, _submission_manifest
from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.service import (
    MoodleRequiredFunctionCapabilityError,
    MoodleService,
    MoodleServiceError,
)
from moddle_autotask.adapters.moodle.state import MoodleState, NotificationDraft
from moddle_autotask.adapters.moodle.submission import (
    MoodleSubmissionClient,
    MoodleSubmissionError,
    PermanentSubmissionOfferError,
)


def _manifest(tmp_path: Path) -> SubmissionManifest:
    event = MoodleState(tmp_path / "state.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + "b" * 64,
            "moodle-assignment-v1:" + "c" * 64,
            "Course",
            "C",
            "Assignment",
            0,
            0,
            0,
            0,
            0,
            (),
            43,
        ),
        now=1,
    )
    assert event is not None
    return _submission_manifest(event, "report")


class _StatusClient:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    def call(self, function: str, parameters: object = None) -> object:
        assert function == "mod_assign_get_submission_status"
        return {
            "assignmentdata": {"attachments": {}},
            "warnings": [],
            "gradingsummary": {
                "participantcount": 1,
                "submissiondraftscount": 0,
                "submissionsenabled": True,
                "submissionssubmittedcount": 1,
                "submissionsneedgradingcount": 1,
                "warnofungroupedusers": "",
            },
            "lastattempt": {
                "submission": {
                    "id": 9,
                    "status": "submitted",
                    "plugins": [
                        {
                            "type": "file",
                            "fileareas": [
                                {
                                    "area": "submission_files",
                                    "files": [
                                        {
                                            "filename": "autotask-cccccccccccccccc.md",
                                            "filesize": 6,
                                            "fileurl": "https://moodle.test/pluginfile.php/1/x",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }

    def pluginfile_digest(self, file_url: str, expected_size: int) -> str:
        assert file_url.startswith("https://moodle.test/pluginfile.php/")
        assert expected_size == 6
        return self.digest

    def upload_draft_file(self, filename: str, content: bytes) -> int:
        raise AssertionError("verify must not upload")


class _Site:
    def __init__(self, assignments: tuple[object, ...]) -> None:
        self._assignments = assignments

    def verified_site_url(
        self,
        required_functions: frozenset[str] = frozenset(),
        *,
        require_uploadfiles: bool = False,
    ) -> str:
        assert "mod_assign_save_submission" in required_functions
        assert require_uploadfiles
        return "https://moodle.test"

    def assignments(self) -> tuple[object, ...]:
        return self._assignments


@pytest.mark.parametrize(
    "error",
    (
        MoodleRequiredFunctionCapabilityError(
            "Moodle does not advertise required mobile functions"
        ),
        MoodleServiceError("could not verify Moodle site"),
    ),
)
def test_offer_only_translates_definitive_service_capability_errors(
    tmp_path: Path, error: MoodleServiceError
) -> None:
    manifest = _manifest(tmp_path)

    class FailingSite:
        def verified_site_url(
            self,
            required_functions: frozenset[str] = frozenset(),
            *,
            require_uploadfiles: bool = False,
        ) -> str:
            del required_functions, require_uploadfiles
            raise error

        def assignments(self) -> tuple[object, ...]:
            raise AssertionError("site verification must fail first")

    client = MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"),
        _StatusClient(manifest.report_digest),
        FailingSite(),
    )
    with pytest.raises(MoodleSubmissionError) as raised:
        client.can_offer_submission(manifest.event)
    assert isinstance(raised.value, PermanentSubmissionOfferError) is isinstance(
        error, MoodleRequiredFunctionCapabilityError
    )


@pytest.mark.parametrize("include_uploadfiles", (False, True))
def test_offer_turns_definitive_missing_or_disabled_upload_capability_permanent(
    tmp_path: Path, include_uploadfiles: bool
) -> None:
    manifest = _manifest(tmp_path)

    class SiteInfoClient:
        def call(
            self, function: str, parameters: Mapping[str, str | int] | None = None
        ) -> object:
            del parameters
            assert function == "core_webservice_get_site_info"
            result: dict[str, object] = {
                "siteurl": "https://moodle.test",
                "downloadfiles": True,
                "functions": [
                    {"name": "core_webservice_get_site_info"},
                    {"name": "mod_assign_get_assignments"},
                    {"name": "mod_assign_save_submission"},
                    {"name": "mod_assign_get_submission_status"},
                ],
            }
            if include_uploadfiles:
                result["uploadfiles"] = False
            return result

    config = MoodleConnectionConfig("https://moodle.test", "token")
    client = MoodleSubmissionClient(
        config,
        _StatusClient(manifest.report_digest),
        MoodleService(config, SiteInfoClient()),
    )
    with pytest.raises(PermanentSubmissionOfferError, match="upload capability"):
        client.can_offer_submission(manifest.event)


def test_final_submission_requires_downloaded_file_digest(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    client = MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"), _StatusClient("0" * 64)
    )
    client._verified = True
    with pytest.raises(MoodleSubmissionError, match="digest differs"):
        client.verify(manifest)
    client.client = _StatusClient(sha256(b"report").hexdigest())
    receipt = client.verify(manifest)
    assert receipt is not None and receipt.reference == "moodle-submission:9"


def test_save_rejects_current_assignment_with_changed_revision(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    changed = SimpleNamespace(
        assignment_id=43,
        task_key=manifest.event.task_key,
        revision_digest="moodle-assignment-v1:" + "d" * 64,
    )
    client = MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"),
        _StatusClient(manifest.report_digest),
        _Site((changed,)),
    )
    with pytest.raises(PermanentSubmissionOfferError, match="revision changed"):
        client.save(manifest, 7)


@pytest.mark.parametrize(
    ("assignments", "message"),
    (
        ((), "no longer exists"),
        (
            (SimpleNamespace(assignment_id=43, task_key="other", revision_digest="other"),),
            "revision changed",
        ),
    ),
)
def test_offer_rejects_enumerated_missing_or_changed_assignment_permanently(
    tmp_path: Path, assignments: tuple[object, ...], message: str
) -> None:
    manifest = _manifest(tmp_path)
    client = MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"),
        _StatusClient(manifest.report_digest),
        _Site(assignments),
    )
    with pytest.raises(PermanentSubmissionOfferError, match=message):
        client.can_offer_submission(manifest.event)


@pytest.mark.parametrize("response", ({}, [{"item": "warning"}], "[]"))
def test_save_accepts_only_the_pinned_empty_warning_list(tmp_path: Path, response: object) -> None:
    manifest = _manifest(tmp_path)

    class SaveClient(_StatusClient):
        def call(self, function: str, parameters: object = None) -> object:
            assert function == "mod_assign_save_submission"
            assert parameters == {
                "assignmentid": 43,
                "plugindata[files_filemanager]": 7,
            }
            return response

    current = SimpleNamespace(
        assignment_id=43,
        task_key=manifest.event.task_key,
        revision_digest=manifest.event.revision_digest,
    )
    client = MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"),
        SaveClient(manifest.report_digest),
        _Site((current,)),
    )
    with pytest.raises(MoodleSubmissionError, match="response is invalid"):
        client.save(manifest, 7)


def test_save_accepts_pinned_empty_warning_list(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    class SaveClient(_StatusClient):
        def call(self, function: str, parameters: object = None) -> object:
            assert function == "mod_assign_save_submission"
            assert parameters == {
                "assignmentid": 43,
                "plugindata[files_filemanager]": 7,
            }
            return []

    current = SimpleNamespace(
        assignment_id=43,
        task_key=manifest.event.task_key,
        revision_digest=manifest.event.revision_digest,
    )
    MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"),
        SaveClient(manifest.report_digest),
        _Site((current,)),
    ).save(manifest, 7)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.update({"unexpected": 1}),
        lambda payload: payload.update({"warnings": [{"item": "warning"}]}),
        lambda payload: payload.update({"warnings": {}}),
        lambda payload: payload.pop("assignmentdata"),
        lambda payload: payload.update({"assignmentdata": [{}]}),
        lambda payload: payload.update({"gradingsummary": []}),
    ),
)
def test_submission_status_rejects_noncontract_payloads(tmp_path: Path, mutate: object) -> None:
    manifest = _manifest(tmp_path)
    status = _StatusClient(manifest.report_digest)
    payload = status.call("mod_assign_get_submission_status")
    assert isinstance(payload, dict) and callable(mutate)
    mutate(payload)

    class InvalidStatusClient(_StatusClient):
        def call(self, function: str, parameters: object = None) -> object:
            return payload

    client = MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"), InvalidStatusClient("x")
    )
    client._verified = True
    with pytest.raises(MoodleSubmissionError, match="status is invalid"):
        client.verify(manifest)


def test_submission_status_accepts_pinned_empty_assignmentdata_list(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    status = _StatusClient(manifest.report_digest)
    payload = status.call("mod_assign_get_submission_status")
    assert isinstance(payload, dict)
    payload["assignmentdata"] = []

    class EmptyAssignmentDataClient(_StatusClient):
        def call(self, function: str, parameters: object = None) -> object:
            return payload

    client = MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"),
        EmptyAssignmentDataClient(manifest.report_digest),
    )
    client._verified = True
    assert client.verify(manifest) is not None


@pytest.mark.parametrize(
    "mutation", ("extra", "duplicate-area", "duplicate-plugin", "zero", "malformed")
)
def test_submission_receipt_requires_one_exact_file_in_one_relevant_area(
    tmp_path: Path, mutation: str
) -> None:
    manifest = _manifest(tmp_path)
    status = _StatusClient(manifest.report_digest)
    payload = status.call("mod_assign_get_submission_status")
    assert isinstance(payload, dict)
    submission = payload["lastattempt"]["submission"]
    assert isinstance(submission, dict)
    plugins = submission["plugins"]
    assert isinstance(plugins, list) and isinstance(plugins[0], dict)
    areas = plugins[0]["fileareas"]
    assert isinstance(areas, list) and isinstance(areas[0], dict)
    files = areas[0]["files"]
    assert isinstance(files, list)
    if mutation == "extra":
        files.append({"filename": "different.md", "filesize": 6, "fileurl": "https://moodle.test/x"})
    elif mutation == "duplicate-area":
        areas.append(deepcopy(areas[0]))
    elif mutation == "duplicate-plugin":
        plugins.append(deepcopy(plugins[0]))
    elif mutation == "zero":
        files.clear()
    else:
        files[0] = {"filename": manifest.filename, "filesize": 6}

    class InvalidReceiptClient(_StatusClient):
        def call(self, function: str, parameters: object = None) -> object:
            return payload

    client = MoodleSubmissionClient(
        MoodleConnectionConfig("https://moodle.test", "token"),
        InvalidReceiptClient(manifest.report_digest),
    )
    client._verified = True
    with pytest.raises(MoodleSubmissionError, match="submission file"):
        client.verify(manifest)
