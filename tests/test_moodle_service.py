from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.service import MoodleService, MoodleServiceError
from moddle_autotask.adapters.moodle.state import MoodleState


class _Client:
    def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
        assert function == "core_webservice_get_site_info"
        return {
            "siteurl": "https://example.test",
            "downloadfiles": True,
            "functions": [{"name": function}, {"name": "mod_assign_get_assignments"}],
        }


def test_site_verification_requires_advertised_mobile_functions() -> None:
    service = MoodleService(
        MoodleConnectionConfig("https://example.test", "opaque-token"), _Client()
    )
    assert service.verified_site_url() == "https://example.test"


def test_site_url_mismatch_rejected() -> None:
    class Client(_Client):
        def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
            return {"siteurl": "https://other.test", "downloadfiles": True, "functions": []}

    with __import__("pytest").raises(Exception):
        MoodleService(
            MoodleConnectionConfig("https://example.test", "x"), Client()
        ).verified_site_url()


def test_disabled_downloadfiles_rejected() -> None:
    class Client(_Client):
        def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
            return {"siteurl": "https://example.test", "downloadfiles": False, "functions": []}

    with __import__("pytest").raises(Exception):
        MoodleService(
            MoodleConnectionConfig("https://example.test", "x"), Client()
        ).verified_site_url()


def test_malformed_function_entry_rejected() -> None:
    class Client(_Client):
        def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
            return {"siteurl": "https://example.test", "downloadfiles": True, "functions": ["bad"]}

    with __import__("pytest").raises(Exception):
        MoodleService(
            MoodleConnectionConfig("https://example.test", "x"), Client()
        ).verified_site_url()


def test_missing_function_rejected() -> None:
    class Client(_Client):
        def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
            return {
                "siteurl": "https://example.test",
                "downloadfiles": True,
                "functions": [{"name": "other"}],
            }

    with __import__("pytest").raises(Exception):
        MoodleService(
            MoodleConnectionConfig("https://example.test", "x"), Client()
        ).verified_site_url()


def test_assignment_warning_is_wrapped() -> None:
    class Client(_Client):
        def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
            if function == "core_webservice_get_site_info":
                return {
                    "siteurl": "https://example.test",
                    "downloadfiles": True,
                    "functions": [
                        {"name": "core_webservice_get_site_info"},
                        {"name": "mod_assign_get_assignments"},
                    ],
                }
            return {"warnings": ["x"], "courses": []}

    with __import__("pytest").raises(Exception):
        MoodleService(MoodleConnectionConfig("https://example.test", "x"), Client()).assignments()


def test_duplicate_function_advertisement_is_rejected() -> None:
    class Client(_Client):
        def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
            return {
                "siteurl": "https://example.test",
                "downloadfiles": True,
                "functions": [{"name": function}, {"name": function}],
            }

    with pytest.raises(MoodleServiceError, match="advertisement"):
        MoodleService(
            MoodleConnectionConfig("https://example.test", "x"), Client()
        ).verified_site_url()


def test_scan_acknowledgement_lifecycle_reports_updated(tmp_path: Path) -> None:
    class Client:
        modified = 1

        def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
            if function == "core_webservice_get_site_info":
                return {
                    "siteurl": "https://example.test",
                    "downloadfiles": True,
                    "functions": [
                        {"name": "core_webservice_get_site_info"},
                        {"name": "mod_assign_get_assignments"},
                    ],
                }
            return {
                "warnings": [],
                "courses": [
                    {
                        "id": 1,
                        "fullname": "Course",
                        "shortname": "COURSE",
                        "assignments": [
                            {
                                "id": 2,
                                "cmid": 3,
                                "name": "Assignment",
                                "timemodified": self.modified,
                            }
                        ],
                    }
                ],
            }

    client = Client()
    service = MoodleService(MoodleConnectionConfig("https://example.test", "x"), client)
    state = MoodleState(tmp_path / "state.sqlite3")
    first = service.scan(state)
    assert [candidate.status for candidate in first] == ["NEW"]
    state.acknowledge(first[0].assignment.task_key, first[0].assignment.revision_digest)
    assert service.scan(state) == ()
    client.modified = 2
    assert [candidate.status for candidate in service.scan(state)] == ["UPDATED"]
