"""Command line entrypoint for outbound Telegram callback polling."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Never

from .approval_state import ApprovalState
from .telegram import (
    TelegramClient,
    TelegramConfig,
    TelegramTransport,
    process_updates,
    run_polling,
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(prog="moodle-autotask-telegram", allow_abbrev=False)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_SafeArgumentParser
    )
    for command in ("poll-once", "run"):
        child = commands.add_parser(command, allow_abbrev=False)
        child.add_argument("--config-file", type=Path, required=True)
        child.add_argument("--state", type=Path, required=True)
        if command == "run":
            child.add_argument("--poll-timeout-seconds", type=int, default=30)
            child.add_argument("--retry-seconds", type=int, default=5)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[TelegramConfig], TelegramTransport] = TelegramClient,
    poll_once: Callable[..., int] = process_updates,
    runner: Callable[..., None] = run_polling,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run" and (
            not 1 <= args.poll_timeout_seconds <= 50 or not 1 <= args.retry_seconds <= 300
        ):
            raise ValueError("polling options are invalid")
        config = TelegramConfig.from_file(args.config_file)
        state = ApprovalState(args.state)
        client = client_factory(config)
        if args.command == "poll-once":
            poll_once(config, client, state, timeout_seconds=0)
        else:
            runner(
                config,
                client,
                state,
                timeout_seconds=args.poll_timeout_seconds,
                retry_seconds=args.retry_seconds,
            )
        return 0
    except (OSError, RuntimeError, ValueError):
        print("Telegram approval service failed", file=sys.stderr)
        return 1
