"""Explicit, revision-bound context imported from related course resources.

The controller never follows URLs from this file or sends Moodle credentials to
external resources. Operators import source text (including image transcriptions)
and provenance before approval. Every change requires a new assignment approval.
"""

from __future__ import annotations

import html
import json
import os
import re
import stat
from dataclasses import replace
from pathlib import Path

from .models import MoodleAssignmentSnapshot, MoodlePayloadError, _hash
from .path_safety import assert_no_indirection

_MAX_BYTES = 1024 * 1024
_REVISION = re.compile(r"moodle-assignment-v1:[0-9a-f]{64}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MoodlePayloadError("duplicate assignment context field")
        result[key] = value
    return result


def apply_assignment_context(
    assignments: tuple[MoodleAssignmentSnapshot, ...], site_url: str, path: Path | None
) -> tuple[MoodleAssignmentSnapshot, ...]:
    if path is None:
        return assignments
    try:
        assert_no_indirection(path)
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (os.name != "nt" and metadata.st_mode & 0o022)
            ):
                raise MoodlePayloadError("assignment context file is unsafe")
            data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise MoodlePayloadError("assignment context is too large")
        raw = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as error:
        raise MoodlePayloadError("could not read assignment context") from error
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema", "siteUrl", "assignments"}
        or raw["schema"] != "moodle-assignment-context-v1"
        or raw["siteUrl"] != site_url
        or not isinstance(raw["assignments"], list)
        or len(raw["assignments"]) > 100
    ):
        raise MoodlePayloadError("assignment context identity is invalid")
    contexts: dict[int, dict[str, str]] = {}
    for entry in raw["assignments"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"assignmentId", "baseRevision", "text", "submissionFormat"}
            or type(entry["assignmentId"]) is not int
            or entry["assignmentId"] <= 0
            or entry["assignmentId"] in contexts
            or not isinstance(entry["baseRevision"], str)
            or not _REVISION.fullmatch(entry["baseRevision"])
            or not isinstance(entry["text"], str)
            or not entry["text"].strip()
            or len(entry["text"].encode("utf-8")) > 128 * 1024
            or entry["submissionFormat"] not in ("markdown", "archive")
        ):
            raise MoodlePayloadError("assignment context entry is invalid")
        contexts[entry["assignmentId"]] = entry
    enriched: list[MoodleAssignmentSnapshot] = []
    for assignment in assignments:
        context = contexts.get(assignment.assignment_id)
        if context is None:
            enriched.append(assignment)
            continue
        if assignment.revision_digest != context["baseRevision"]:
            raise MoodlePayloadError("assignment context requires an updated source revision")
        enriched.append(
            replace(
                assignment,
                intro=assignment.intro
                + "\n<section><h2>Contexto de recursos relacionados</h2><pre>"
                + html.escape(context["text"])
                + "</pre></section>",
                revision_digest=_hash(
                    "moodle-assignment-v1",
                    {"schema": raw["schema"], "siteUrl": site_url, "context": context},
                ),
                submission_format=context["submissionFormat"],
            )
        )
    return tuple(enriched)
