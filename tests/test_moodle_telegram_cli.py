from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from moddle_autotask.adapters.moodle.approval_state import ApprovalState
from moddle_autotask.adapters.moodle.telegram import TelegramTransport
from moddle_autotask.adapters.moodle.telegram_cli import _parser, main

TOKEN = "123456:" + "A" * 35


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "telegram.json"
    path.write_text(
        json.dumps({"botToken": TOKEN, "chatId": 42, "allowedUserId": 42}),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)
    return path


def test_cli_rejects_raw_token_without_echoing_it(capsys: pytest.CaptureFixture[str]) -> None:
    sentinel = "SENTINEL_TELEGRAM_TOKEN"
    with pytest.raises(SystemExit) as error:
        main(["poll-once", "--state", "state.sqlite3", "--token", sentinel])
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert sentinel not in captured.out and sentinel not in captured.err


def test_cli_loads_config_before_creating_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "approval.sqlite3"
    assert (
        main(
            [
                "poll-once",
                "--config-file",
                str(tmp_path / "missing.json"),
                "--state",
                str(state),
            ]
        )
        == 1
    )
    assert not state.exists()
    assert TOKEN not in capsys.readouterr().err


def test_cli_uses_injected_poll_and_runner(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    state_path = tmp_path / "approval.sqlite3"
    client = cast(TelegramTransport, object())
    seen: list[tuple[str, int]] = []

    def poll_once(config: object, selected: object, state: object, timeout_seconds: int) -> int:
        seen.append(("once", timeout_seconds))
        return 0

    assert (
        main(
            ["poll-once", "--config-file", str(config_path), "--state", str(state_path)],
            client_factory=lambda config: client,
            poll_once=poll_once,
        )
        == 0
    )
    assert (
        main(
            [
                "run",
                "--config-file",
                str(config_path),
                "--state",
                str(state_path),
                "--poll-timeout-seconds",
                "50",
                "--retry-seconds",
                "300",
            ],
            client_factory=lambda config: client,
            runner=lambda config, selected, state, timeout_seconds, retry_seconds: seen.append(
                ("run", timeout_seconds + retry_seconds)
            ),
        )
        == 0
    )
    assert seen == [("once", 0), ("run", 350)]
    assert ApprovalState(state_path).next_update_id() == 0


@pytest.mark.parametrize(
    ("option", "bad"),
    (
        ("--poll-timeout-seconds", "0"),
        ("--poll-timeout-seconds", "51"),
        ("--retry-seconds", "0"),
        ("--retry-seconds", "301"),
    ),
)
def test_cli_rejects_unbounded_polling_options_without_value_echo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
    bad: str,
) -> None:
    assert (
        main(
            [
                "run",
                "--config-file",
                str(_config(tmp_path)),
                "--state",
                str(tmp_path / "state.sqlite3"),
                option,
                bad,
            ]
        )
        == 1
    )
    assert bad not in capsys.readouterr().err


def test_parser_defaults_are_bounded() -> None:
    parsed = _parser().parse_args(
        ["run", "--config-file", "telegram.json", "--state", "approval.sqlite3"]
    )
    assert parsed.poll_timeout_seconds == 50 and parsed.poll_timeout_seconds <= 55
    assert parsed.retry_seconds == 5
