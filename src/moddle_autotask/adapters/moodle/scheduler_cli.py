"""Safe command line entrypoint for the local Moodle notification scheduler."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Never

from .config import MoodleConnectionConfig
from .scheduler import CycleResult, LocalJsonSink, SchedulerOptions, once, run, summary_json
from .service import MoodleService
from .state import MoodleState


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(prog="moodle-autotask-scheduler", allow_abbrev=False)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_SafeArgumentParser
    )
    for command in ("once", "run"):
        child = commands.add_parser(command, allow_abbrev=False)
        child.add_argument("--state", type=Path, required=True)
        child.add_argument("--token-file", type=Path, required=True)
        child.add_argument("--lease-seconds", type=int, default=30)
        child.add_argument("--batch-size", type=int, default=20)
        child.add_argument("--retry-base-seconds", type=int, default=5)
        child.add_argument("--retry-max-seconds", type=int, default=3600)
        child.add_argument("--request-timeout-seconds", type=int, default=15)
        if command == "run":
            child.add_argument("--interval-seconds", type=int, default=86400)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    service_factory: Callable[[MoodleConnectionConfig], MoodleService] = MoodleService,
    once_runner: Callable[..., CycleResult] = once,
    runner: Callable[..., None] = run,
) -> int:
    args = _parser().parse_args(argv)
    try:
        options = SchedulerOptions(
            args.lease_seconds, args.batch_size, args.retry_base_seconds, args.retry_max_seconds
        )
        if args.command == "run" and not 1 <= args.interval_seconds <= 7 * 86400:
            raise ValueError("interval seconds are invalid")
        if not 1 <= args.request_timeout_seconds <= 120:
            raise ValueError("request timeout is invalid")
        config = replace(
            MoodleConnectionConfig.from_token_file(args.token_file),
            timeout_seconds=args.request_timeout_seconds,
        )
        service = service_factory(config)
        state = MoodleState(args.state)
        sink = LocalJsonSink()
        if args.command == "once":
            result = once_runner(state, service, sink, options)
            summary_json(result)
            return 0 if result.ok else 1
        runner(
            state,
            service,
            sink,
            options,
            interval_seconds=args.interval_seconds,
            emit_summary=summary_json,
        )
        return 0
    except (OSError, RuntimeError, ValueError):
        print("scheduler failed", file=sys.stderr)
        return 1
