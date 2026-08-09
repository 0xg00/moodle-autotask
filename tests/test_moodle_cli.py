from __future__ import annotations

from pathlib import Path

import pytest

from moddle_autotask.adapters.moodle.cli import main


def test_acknowledge_uses_state_only_and_emits_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task = "moodle-task-v1:" + "a" * 64
    revision = "moodle-assignment-v1:" + "b" * 64
    assert (
        main(
            [
                "acknowledge",
                "--state",
                str(tmp_path / "state.sqlite3"),
                "--task-key",
                task,
                "--revision-digest",
                revision,
            ]
        )
        == 0
    )
    assert f'"task_key":"{task}"' in capsys.readouterr().out


def test_acknowledge_rejects_bad_task(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "acknowledge",
                "--state",
                str(tmp_path / "s"),
                "--task-key",
                "bad",
                "--revision-digest",
                "bad",
            ]
        )
        == 1
    )
    assert "bad" not in capsys.readouterr().err


def test_acknowledge_is_repeatable(tmp_path: Path) -> None:
    task = "moodle-task-v1:" + "a" * 64
    revision = "moodle-assignment-v1:" + "b" * 64
    args = [
        "acknowledge",
        "--state",
        str(tmp_path / "s"),
        "--task-key",
        task,
        "--revision-digest",
        revision,
    ]
    assert main(args) == main(args) == 0


def test_scan_requires_configuration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "--state", str(tmp_path / "s")]) == 1
    assert "token" in capsys.readouterr().err.lower()


def test_download_requires_configuration(tmp_path: Path) -> None:
    assert (
        main(
            [
                "download",
                "--task-key",
                "x",
                "--attachment-key",
                "x",
                "--output-directory",
                str(tmp_path),
            ]
        )
        == 1
    )


def test_cli_parser_has_no_raw_url_option() -> None:
    with pytest.raises(SystemExit):
        main(["download", "--url", "https://example.test"])


def test_cli_parser_has_no_raw_token_option() -> None:
    with pytest.raises(SystemExit):
        main(["scan", "--token", "secret", "--state", "s"])


def test_cli_parser_never_echoes_rejected_token_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "SENTINEL_SECRET_DO_NOT_LOG"
    with pytest.raises(SystemExit) as error:
        main(["scan", "--state", str(tmp_path / "state.sqlite3"), "--token", sentinel])
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert sentinel not in captured.out
    assert sentinel not in captured.err
