"""Immutable Moodle values and strict Moodle 5.2 external-files parsing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit


class MoodlePayloadError(ValueError):
    pass


def _int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MoodlePayloadError(f"{name} must be an integer")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MoodlePayloadError(f"{name} must be nonblank text")
    return value


def _hash(namespace: str, value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{namespace}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class MoodleAttachment:
    attachment_key: str
    area: str
    filename: str
    filepath: str
    file_url: str
    size_bytes: int
    time_modified: int
    mimetype: str | None = None

    def __post_init__(self) -> None:
        if not self.attachment_key.startswith("moodle-attachment-v1:"):
            raise ValueError("attachment key is invalid")
        if self.size_bytes < 0 or self.time_modified < 0:
            raise ValueError("attachment values must not be negative")
        if (
            not self.filename
            or self.filename in {".", ".."}
            or "/" in self.filename
            or "\\" in self.filename
            or "\x00" in self.filename
        ):
            raise ValueError("attachment filename is unsafe")


@dataclass(frozen=True, slots=True)
class MoodleAssignmentSnapshot:
    task_key: str
    revision_digest: str
    site_url: str
    assignment_id: int
    course_id: int
    course_name: str
    course_shortname: str
    course_module_id: int
    title: str
    intro: str
    allows_submissions_from: int
    due_date: int
    cutoff_date: int
    grading_due_date: int
    time_modified: int
    attachments: tuple[MoodleAttachment, ...]
    submission_drafts: bool = False
    requires_submission_statement: bool = False


def parse_assignments(payload: object, site_url: str) -> tuple[MoodleAssignmentSnapshot, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("courses"), list):
        raise MoodlePayloadError("assignments response is malformed")
    if not isinstance(payload.get("warnings", []), list) or payload.get("warnings"):
        raise MoodlePayloadError("assignments response contains warnings")
    snapshots: list[MoodleAssignmentSnapshot] = []
    assignment_ids: set[int] = set()
    attachment_keys: set[str] = set()
    for course in payload["courses"]:
        if not isinstance(course, dict):
            raise MoodlePayloadError("course must be an object")
        course_id = _int(course.get("id"), "course id")
        course_name = _text(course.get("fullname"), "course fullname")
        course_shortname = _text(course.get("shortname"), "course shortname")
        assignments = course.get("assignments")
        if course_id <= 0 or not isinstance(assignments, list):
            raise MoodlePayloadError("course is malformed")
        for assignment in assignments:
            if not isinstance(assignment, dict):
                raise MoodlePayloadError("assignment must be an object")
            assignment_id = _int(assignment.get("id"), "assignment id")
            cmid = _int(assignment.get("cmid"), "course module id")
            if assignment_id <= 0 or cmid <= 0 or assignment_id in assignment_ids:
                raise MoodlePayloadError("duplicate or invalid assignment identity")
            assignment_ids.add(assignment_id)
            title = _text(assignment.get("name"), "assignment title")
            intro = assignment.get("intro", "")
            if not isinstance(intro, str):
                raise MoodlePayloadError("assignment intro must be text")
            fields = {
                "allows_submissions_from": assignment.get("allowsubmissionsfromdate", 0),
                "due_date": assignment.get("duedate", 0),
                "cutoff_date": assignment.get("cutoffdate", 0),
                "grading_due_date": assignment.get("gradingduedate", 0),
                "time_modified": assignment.get("timemodified", 0),
            }
            if any(_int(value, name) < 0 for name, value in fields.items()):
                raise MoodlePayloadError("assignment date is invalid")
            submission_drafts = assignment.get("submissiondrafts")
            if (
                not isinstance(submission_drafts, int)
                or isinstance(submission_drafts, bool)
                or submission_drafts not in {0, 1}
            ):
                raise MoodlePayloadError("assignment submission draft policy is invalid")
            requires_submission_statement = assignment.get("requiresubmissionstatement")
            if (
                not isinstance(requires_submission_statement, int)
                or isinstance(requires_submission_statement, bool)
                or requires_submission_statement not in {0, 1}
            ):
                raise MoodlePayloadError("assignment submission statement policy is invalid")
            attachments: list[MoodleAttachment] = []
            for area in ("introfiles", "introattachments", "activityattachments"):
                files = assignment.get(area, [])
                if not isinstance(files, list):
                    raise MoodlePayloadError(f"{area} must be a list")
                for file in files:
                    attachments.append(
                        _attachment(file, area, site_url, assignment_id, attachment_keys)
                    )
            task_key = _hash(
                "moodle-task-v1", {"site_url": site_url, "assignment_id": assignment_id}
            )
            revision = {
                "schema": "moodle-assignment-v1",
                "site_url": site_url,
                "assignment_id": assignment_id,
                "course_id": course_id,
                "course_name": course_name,
                "course_shortname": course_shortname,
                "course_module_id": cmid,
                "title": title,
                "intro": intro,
                "submission_drafts": bool(submission_drafts),
                "requires_submission_statement": bool(requires_submission_statement),
                **fields,
                "attachments": [
                    {
                        "key": item.attachment_key,
                        "area": item.area,
                        "filename": item.filename,
                        "filepath": item.filepath,
                        "size": item.size_bytes,
                        "modified": item.time_modified,
                        "mimetype": item.mimetype,
                    }
                    for item in attachments
                ],
            }
            snapshots.append(
                MoodleAssignmentSnapshot(
                    task_key,
                    _hash("moodle-assignment-v1", revision),
                    site_url,
                    assignment_id,
                    course_id,
                    course_name,
                    course_shortname,
                    cmid,
                    title,
                    intro,
                    attachments=tuple(attachments),
                    submission_drafts=bool(submission_drafts),
                    requires_submission_statement=bool(requires_submission_statement),
                    **fields,
                )
            )
    return tuple(snapshots)


def _attachment(
    file: object, area: str, site_url: str, assignment_id: int, known_keys: set[str]
) -> MoodleAttachment:
    if not isinstance(file, dict) or file.get("isexternalfile", False) is not False:
        raise MoodlePayloadError("external or malformed attachment")
    filename = _text(file.get("filename"), "attachment filename")
    filepath = file.get("filepath")
    if (
        not isinstance(filepath, str)
        or not filepath.startswith("/")
        or "\\" in filepath
        or "/../" in filepath
    ):
        raise MoodlePayloadError("attachment filepath is unsafe")
    file_url = _text(file.get("fileurl"), "attachment URL")
    parsed = urlsplit(file_url)
    try:
        port = parsed.port
    except ValueError as error:
        raise MoodlePayloadError("attachment URL is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise MoodlePayloadError("attachment URL is unsafe")
    size = _int(file.get("filesize"), "attachment size")
    modified = _int(file.get("timemodified"), "attachment modification time")
    mimetype = file.get("mimetype")
    if size < 0 or modified < 0 or (mimetype is not None and not isinstance(mimetype, str)):
        raise MoodlePayloadError("attachment metadata is invalid")
    key = _hash(
        "moodle-attachment-v1",
        {
            "site_url": site_url,
            "assignment_id": assignment_id,
            "area": area,
            "filepath": filepath,
            "filename": filename,
        },
    )
    if key in known_keys:
        raise MoodlePayloadError("duplicate attachment identity")
    known_keys.add(key)
    return MoodleAttachment(key, area, filename, filepath, file_url, size, modified, mimetype)
