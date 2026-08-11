"""Safe command line entrypoint for the local Moodle notification scheduler."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Never, cast

from .approval_state import ApprovalState
from .config import MoodleConnectionConfig
from .scheduler import (
    CycleResult,
    LocalJsonSink,
    NotificationSink,
    SchedulerOptions,
    once,
    run,
    summary_json,
)
from .scheduler_config import load_scheduler_config
from .service import MoodleService
from .state import MoodleState
from .telegram import TelegramApprovalSink, TelegramClient, TelegramConfig


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
        child.add_argument("--course-shortname", action="append", default=[])
        child.add_argument("--max-new-events-per-cycle", type=int)
        child.add_argument("--scheduler-config-file", type=Path)
        child.add_argument("--request-timeout-seconds", type=int, default=15)
        child.add_argument("--telegram-config-file", type=Path)
        child.add_argument("--approval-state", type=Path)
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
        if args.scheduler_config_file is not None:
            if args.course_shortname or args.max_new_events_per_cycle is not None:
                raise ValueError("scheduler config cannot be combined with scope arguments")
            configured = load_scheduler_config(args.scheduler_config_file)
            options = SchedulerOptions(
                args.lease_seconds,
                args.batch_size,
                args.retry_base_seconds,
                args.retry_max_seconds,
                configured.course_shortnames,
                configured.max_new_events_per_cycle,
            )
        else:
            course_shortnames = tuple(args.course_shortname)
            if tuple(sorted(set(course_shortnames))) != course_shortnames:
                raise ValueError("course shortnames must be sorted and unique")
            options = SchedulerOptions(
                args.lease_seconds,
                args.batch_size,
                args.retry_base_seconds,
                args.retry_max_seconds,
                course_shortnames,
                100 if args.max_new_events_per_cycle is None else args.max_new_events_per_cycle,
            )
        if args.command == "run" and not 1 <= args.interval_seconds <= 7 * 86400:
            raise ValueError("interval seconds are invalid")
        if not 1 <= args.request_timeout_seconds <= 120:
            raise ValueError("request timeout is invalid")
        if (args.telegram_config_file is None) != (args.approval_state is None):
            raise ValueError("Telegram configuration is incomplete")
        config = replace(
            MoodleConnectionConfig.from_token_file(args.token_file),
            timeout_seconds=args.request_timeout_seconds,
        )
        service = service_factory(config)
        state = MoodleState(args.state)
        sink: NotificationSink
        if args.telegram_config_file is None:
            sink = LocalJsonSink()
        else:
            telegram_config = TelegramConfig.from_file(args.telegram_config_file)
            approval_state = ApprovalState(args.approval_state)
            sink = cast(
                NotificationSink,
                TelegramApprovalSink(
                    telegram_config, TelegramClient(telegram_config), approval_state
                ),
            )
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
