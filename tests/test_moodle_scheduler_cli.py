from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.scheduler import CycleResult, SchedulerOptions
from moddle_autotask.adapters.moodle.scheduler_cli import _parser, main
from moddle_autotask.adapters.moodle.service import MoodleService


def test_scheduler_rejects_raw_token_without_echoing_it(capsys: pytest.CaptureFixture[str]) -> None:
    sentinel = "SENTINEL_SECRET_DO_NOT_LOG"
    with pytest.raises(SystemExit) as error:
        main(["once", "--state", "state.sqlite3", "--token", sentinel])
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_scheduler_rejects_unbounded_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "run",
                "--state",
                str(tmp_path / "state.sqlite3"),
                "--token-file",
                str(tmp_path / "token.json"),
                "--interval-seconds",
                "604801",
            ]
        )
        == 1
    )
    assert "604801" not in capsys.readouterr().err


def test_scheduler_has_no_url_or_max_attempts_option(capsys: pytest.CaptureFixture[str]) -> None:
    sentinel = "SENTINEL_OPTION_VALUE"
    with pytest.raises(SystemExit):
        main(["once", "--state", "state.sqlite3", "--url", sentinel])
    with pytest.raises(SystemExit):
        main(["once", "--state", "state.sqlite3", "--max-attempts", sentinel])
    captured = capsys.readouterr()
    assert sentinel not in captured.out and sentinel not in captured.err


def test_defaults_help_and_missing_configuration_does_not_create_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOODLE_AUTOTASK_BASE_URL", raising=False)
    monkeypatch.delenv("MOODLE_AUTOTASK_TOKEN", raising=False)
    parsed = _parser().parse_args(
        ["run", "--state", str(tmp_path / "state.sqlite3"), "--token-file", "token.json"]
    )
    assert (parsed.interval_seconds, parsed.lease_seconds, parsed.batch_size) == (86400, 30, 20)
    assert parsed.request_timeout_seconds == 15
    assert (
        main(
            [
                "once",
                "--state",
                str(tmp_path / "missing.sqlite3"),
                "--token-file",
                str(tmp_path / "missing-token.json"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "missing.sqlite3").exists()
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "SENTINEL" not in capsys.readouterr().err


def test_scheduler_requires_token_file_and_never_uses_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "SENTINEL_ENV_TOKEN"
    monkeypatch.setenv("MOODLE_AUTOTASK_BASE_URL", "https://example.test")
    monkeypatch.setenv("MOODLE_AUTOTASK_TOKEN", sentinel)
    state_path = tmp_path / "state.sqlite3"
    with pytest.raises(SystemExit) as error:
        main(["once", "--state", str(state_path)])
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert not state_path.exists()
    assert sentinel not in captured.out and sentinel not in captured.err


@pytest.mark.parametrize(
    ("option", "valid", "invalid", "extra"),
    (
        ("--interval-seconds", ("1", "604800"), ("0", "-1", "604801", "not-an-int"), ()),
        ("--lease-seconds", ("6", "3600"), ("0", "-1", "3601", "not-an-int"), ()),
        ("--batch-size", ("1", "100"), ("0", "-1", "101", "not-an-int"), ()),
        (
            "--retry-base-seconds",
            ("1", "3600"),
            ("0", "-1", "3601", "not-an-int"),
            (),
        ),
        (
            "--retry-max-seconds",
            ("1", "86400"),
            ("0", "-1", "86401", "not-an-int"),
            ("--retry-base-seconds", "1"),
        ),
        (
            "--request-timeout-seconds",
            ("1", "120"),
            ("0", "-1", "121", "not-an-int"),
            (),
        ),
    ),
)
def test_scheduler_numeric_option_boundaries_are_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    valid: tuple[str, str],
    invalid: tuple[str, str, str, str],
    extra: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.MoodleConnectionConfig.from_token_file",
        lambda path: MoodleConnectionConfig("https://example.test", "safe"),
    )
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.MoodleState",
        lambda path: object(),
    )
    command = "run" if option == "--interval-seconds" else "once"
    timeouts: list[float] = []

    def service_factory(config: MoodleConnectionConfig) -> MoodleService:
        timeouts.append(config.timeout_seconds)
        return cast(MoodleService, object())

    for value in valid:
        argv = [
            command,
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--token-file",
            str(tmp_path / "token.json"),
            option,
            value,
            *extra,
        ]
        assert (
            main(
                argv,
                service_factory=service_factory,
                once_runner=lambda *args: CycleResult(True, 0, 0, 0),
                runner=lambda *args, **kwargs: None,
            )
            == 0
        )
    expected_timeout = [
        float(value) if option == "--request-timeout-seconds" else 15.0 for value in valid
    ]
    assert timeouts == expected_timeout
    capsys.readouterr()
    for value in invalid:
        argv = [
            command,
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--token-file",
            str(tmp_path / "token.json"),
            option,
            value,
            *extra,
        ]
        if value == "not-an-int":
            with pytest.raises(SystemExit) as error:
                main(argv)
            assert error.value.code == 2
        else:
            assert main(argv) == 1
        captured = capsys.readouterr()
        assert value not in captured.out and value not in captured.err


def test_injected_once_and_runner_are_used_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token.json"
    token.write_text('{"baseUrl":"https://example.test","token":"safe"}', encoding="utf-8")
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.MoodleState", lambda path: object()
    )
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.MoodleConnectionConfig.from_token_file",
        lambda path: MoodleConnectionConfig("https://example.test", "safe"),
    )
    seen: list[object] = []
    service = cast(MoodleService, object())
    assert (
        main(
            ["once", "--state", str(tmp_path / "s"), "--token-file", str(token)],
            service_factory=lambda config: service,
            once_runner=lambda *args: CycleResult(True, 0, 0, 0),
        )
        == 0
    )
    assert (
        main(
            ["once", "--state", str(tmp_path / "s"), "--token-file", str(token)],
            service_factory=lambda config: service,
            once_runner=lambda *args: CycleResult(False, 0, 0, 0),
        )
        == 1
    )
    assert (
        main(
            [
                "run",
                "--state",
                str(tmp_path / "s"),
                "--token-file",
                str(token),
                "--interval-seconds",
                "9",
            ],
            service_factory=lambda config: service,
            runner=lambda *args, **kwargs: seen.append(kwargs["interval_seconds"]),
        )
        == 0
    )
    assert seen == [9]


def test_telegram_options_must_be_complete_before_state_creation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.sqlite3"
    assert (
        main(
            [
                "once",
                "--state",
                str(state),
                "--token-file",
                str(tmp_path / "moodle.json"),
                "--telegram-config-file",
                str(tmp_path / "telegram.json"),
            ]
        )
        == 1
    )
    assert not state.exists()
    assert "telegram.json" not in capsys.readouterr().err


def test_scheduler_constructs_telegram_sink_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.MoodleConnectionConfig.from_token_file",
        lambda path: MoodleConnectionConfig("https://example.test", "safe"),
    )
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.TelegramConfig.from_file",
        lambda path: object(),
    )
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.MoodleState", lambda path: object()
    )
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.ApprovalState", lambda path: object()
    )
    client = object()
    sink = object()
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.TelegramClient", lambda config: client
    )
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.TelegramApprovalSink",
        lambda config, selected_client, state: sink,
    )
    seen: list[object] = []

    def once_runner(
        state: object, service: object, selected_sink: object, options: object
    ) -> CycleResult:
        seen.append(selected_sink)
        return CycleResult(True, 0, 0, 0)

    assert (
        main(
            [
                "once",
                "--state",
                str(tmp_path / "moodle-state.sqlite3"),
                "--token-file",
                str(tmp_path / "moodle.json"),
                "--telegram-config-file",
                str(tmp_path / "telegram.json"),
                "--approval-state",
                str(tmp_path / "approval.sqlite3"),
            ],
            service_factory=lambda config: cast(MoodleService, object()),
            once_runner=once_runner,
        )
        == 0
    )
    assert seen == [sink]


def test_scheduler_config_file_is_exclusive_and_sets_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "scheduler.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.load_scheduler_config",
        lambda path: SchedulerOptions(course_shortnames=("ASIX",), max_new_events_per_cycle=4),
    )
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.MoodleConnectionConfig.from_token_file",
        lambda path: MoodleConnectionConfig("https://example.test", "safe"),
    )
    monkeypatch.setattr(
        "moddle_autotask.adapters.moodle.scheduler_cli.MoodleState", lambda path: object()
    )
    seen: list[object] = []

    def once_runner(*args: object) -> CycleResult:
        seen.append(args[-1])
        return CycleResult(True, 0, 0, 0)

    assert main(
        [
            "once", "--state", str(tmp_path / "state"), "--token-file", str(tmp_path / "token"),
            "--scheduler-config-file", str(config),
        ],
        service_factory=lambda config: cast(MoodleService, object()),
        once_runner=once_runner,
    ) == 0
    assert isinstance(seen[0], SchedulerOptions)
    assert seen[0].course_shortnames == ("ASIX",)
    assert main(
        [
            "once", "--state", str(tmp_path / "state"), "--token-file", str(tmp_path / "token"),
            "--scheduler-config-file", str(config), "--course-shortname", "OTHER",
        ],
        service_factory=lambda config: cast(MoodleService, object()),
    ) == 1
