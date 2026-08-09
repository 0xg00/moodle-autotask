"""Machine-readable command line interface for the Moodle boundary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Never

from .config import MoodleConnectionConfig
from .downloads import download_attachment
from .service import MoodleService
from .state import MoodleState


class _SafeArgumentParser(argparse.ArgumentParser):
    """Avoid reflecting rejected argv values, which may contain a credential."""

    def error(self, message: str) -> Never:
        del message
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(prog="moodle-autotask-moodle", allow_abbrev=False)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_SafeArgumentParser
    )
    scan = commands.add_parser("scan", allow_abbrev=False)
    scan.add_argument("--state", type=Path, required=True)
    scan.add_argument("--token-file", type=Path)
    acknowledge = commands.add_parser("acknowledge", allow_abbrev=False)
    acknowledge.add_argument("--state", type=Path, required=True)
    acknowledge.add_argument("--task-key", required=True)
    acknowledge.add_argument("--revision-digest", required=True)
    download = commands.add_parser("download", allow_abbrev=False)
    download.add_argument("--token-file", type=Path)
    download.add_argument("--task-key", required=True)
    download.add_argument("--attachment-key", required=True)
    download.add_argument("--output-directory", type=Path, required=True)
    download.add_argument("--max-size", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "acknowledge":
            acknowledgement = MoodleState(args.state).acknowledge(
                args.task_key, args.revision_digest
            )
            _output(
                {
                    "task_key": acknowledgement.task_key,
                    "revision_digest": acknowledgement.revision_digest,
                }
            )
            return 0
        config = MoodleConnectionConfig.load(args.token_file)
        service = MoodleService(config)
        if args.command == "scan":
            candidates = service.scan(MoodleState(args.state))
            _output({"candidates": [_candidate(candidate) for candidate in candidates]})
            return 0
        assignment = next(
            (item for item in service.assignments() if item.task_key == args.task_key), None
        )
        if assignment is None:
            raise ValueError("task key was not found at verified Moodle site")
        receipt = download_attachment(
            config, assignment, args.attachment_key, args.output_directory, args.max_size
        )
        _output(
            {"path": str(receipt.path), "size_bytes": receipt.size_bytes, "sha256": receipt.sha256}
        )
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(_sanitize(str(error)), file=sys.stderr)
        return 1


def _candidate(candidate: object) -> dict[str, object]:
    from .service import MoodleCandidate

    if not isinstance(candidate, MoodleCandidate):
        raise TypeError("invalid candidate")
    assignment = candidate.assignment
    return {
        "status": candidate.status,
        "task_key": assignment.task_key,
        "revision_digest": assignment.revision_digest,
        "assignment_id": assignment.assignment_id,
        "course_id": assignment.course_id,
        "course_name": assignment.course_name,
        "course_shortname": assignment.course_shortname,
        "course_module_id": assignment.course_module_id,
        "title": assignment.title,
        "intro": assignment.intro,
        "allows_submissions_from": assignment.allows_submissions_from,
        "due_date": assignment.due_date,
        "cutoff_date": assignment.cutoff_date,
        "time_modified": assignment.time_modified,
        "attachments": [
            {
                "attachment_key": item.attachment_key,
                "filename": item.filename,
                "size_bytes": item.size_bytes,
                "mimetype": item.mimetype,
                "time_modified": item.time_modified,
            }
            for item in assignment.attachments
        ],
    }


def _output(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _sanitize(message: str) -> str:
    return message.replace("wstoken", "credential").replace("token=", "credential=")
