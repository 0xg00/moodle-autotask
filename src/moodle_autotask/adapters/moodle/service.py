"""Verified-site assignment enumeration and candidate selection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .client import MoodleClient, MoodleClientError
from .config import MoodleConnectionConfig
from .models import MoodleAssignmentSnapshot, MoodlePayloadError, parse_assignments
from .state import MoodleState


class MoodleServiceError(RuntimeError):
    pass


class MoodleUploadCapabilityError(MoodleServiceError):
    """The authenticated site-info response definitively disallows uploads."""


class MoodleRequiredFunctionCapabilityError(MoodleServiceError):
    """The authenticated site-info response definitively lacks a required function."""


class MoodleCaller(Protocol):
    def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object: ...


@dataclass(frozen=True, slots=True)
class MoodleCandidate:
    status: str
    assignment: MoodleAssignmentSnapshot


class MoodleService:
    def __init__(self, config: MoodleConnectionConfig, client: MoodleCaller | None = None) -> None:
        self.config = config
        self.client = client or MoodleClient(config)
        self._site_url: str | None = None
        self._functions: frozenset[str] | None = None
        self._uploadfiles: bool | None = None

    def verified_site_url(
        self,
        required_functions: frozenset[str] = frozenset(),
        *,
        require_uploadfiles: bool = False,
    ) -> str:
        if self._site_url is not None and self._functions is not None:
            if not required_functions <= self._functions:
                raise MoodleRequiredFunctionCapabilityError(
                    "Moodle does not advertise required mobile functions"
                )
            if require_uploadfiles and self._uploadfiles is not True:
                raise MoodleUploadCapabilityError("Moodle upload capability was not verified")
            return self._site_url
        try:
            result = self.client.call("core_webservice_get_site_info")
        except MoodleClientError as error:
            raise MoodleServiceError("could not verify Moodle site") from error
        if not isinstance(result, dict):
            raise MoodleServiceError("Moodle site information is malformed")
        site_url = result.get("siteurl")
        downloadfiles = result.get("downloadfiles")
        if (
            not isinstance(site_url, str)
            or site_url != self.config.base_url
            or not (downloadfiles is True or (type(downloadfiles) is int and downloadfiles == 1))
        ):
            raise MoodleServiceError("Moodle site identity or download capability was not verified")
        functions = result.get("functions")
        if not isinstance(functions, list):
            raise MoodleServiceError("Moodle function advertisement is malformed")
        if any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in functions
        ):
            raise MoodleServiceError("Moodle function advertisement is malformed")
        advertised = {item["name"] for item in functions}
        if len(advertised) != len(functions):
            raise MoodleServiceError("Moodle function advertisement is malformed")
        required = {
            "core_webservice_get_site_info",
            "mod_assign_get_assignments",
        } | required_functions
        if not required <= advertised:
            raise MoodleRequiredFunctionCapabilityError(
                "Moodle does not advertise required mobile functions"
            )
        # Moodle serializes external booleans as either JSON true or integer 1.
        # Missing, null, false, and strings are not authorization to upload.
        raw_uploadfiles = result.get("uploadfiles")
        uploadfiles = raw_uploadfiles is True or (
            type(raw_uploadfiles) is int and raw_uploadfiles == 1
        )
        if require_uploadfiles and uploadfiles is not True:
            raise MoodleUploadCapabilityError("Moodle upload capability was not verified")
        self._site_url = site_url
        self._functions = frozenset(advertised)
        self._uploadfiles = uploadfiles
        return site_url

    def assignments(self) -> tuple[MoodleAssignmentSnapshot, ...]:
        site_url = self.verified_site_url()
        try:
            response = self.client.call("mod_assign_get_assignments")
            return parse_assignments(response, site_url)
        except (MoodleClientError, MoodlePayloadError) as error:
            raise MoodleServiceError("could not enumerate Moodle assignments") from error

    def scan(self, state: MoodleState) -> tuple[MoodleCandidate, ...]:
        candidates: list[MoodleCandidate] = []
        for assignment in self.assignments():
            status = state.status(assignment.task_key, assignment.revision_digest)
            if status is not None:
                candidates.append(MoodleCandidate(status, assignment))
        return tuple(candidates)
