from __future__ import annotations

import os
from pathlib import Path

import pytest

from moodle_autotask.adapters.moodle.config import MoodleConfigurationError, MoodleConnectionConfig


def test_current_bootstrap_token_file_bom_and_obtained_at_loads(tmp_path: Path) -> None:
    token_file = tmp_path / "moodle-token.json"
    token_file.write_text(
        '{"baseUrl":"http://127.0.0.1:8000","token":"opaque","obtainedAt":"2026-01-01T00:00:00Z"}',
        encoding="utf-8-sig",
    )
    if os.name != "nt":
        token_file.chmod(0o600)
    config = MoodleConnectionConfig.from_token_file(token_file)
    assert config.base_url == "http://127.0.0.1:8000"


def test_https_is_accepted() -> None:
    assert MoodleConnectionConfig("https://example.test", "x").base_url == "https://example.test"


def test_config_repr_never_contains_token() -> None:
    sentinel = "SENTINEL_SECRET_DO_NOT_LOG"
    assert sentinel not in repr(MoodleConnectionConfig("https://example.test", sentinel))


def test_rfc1918_http_is_accepted() -> None:
    assert MoodleConnectionConfig("http://192.168.1.1", "x").base_url.endswith("1.1")


def test_tailscale_http_is_accepted() -> None:
    assert MoodleConnectionConfig("http://100.64.0.1", "x").base_url.endswith("0.1")


def test_public_http_is_rejected() -> None:
    with pytest.raises(MoodleConfigurationError):
        MoodleConnectionConfig("http://8.8.8.8", "x")


def test_http_hostname_is_rejected() -> None:
    with pytest.raises(MoodleConfigurationError):
        MoodleConnectionConfig("http://example.test", "x")


def test_url_query_is_rejected() -> None:
    with pytest.raises(MoodleConfigurationError):
        MoodleConnectionConfig("https://example.test?a=1", "x")


def test_url_trailing_slash_is_rejected() -> None:
    with pytest.raises(MoodleConfigurationError):
        MoodleConnectionConfig("https://example.test/", "x")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.test/.",
        "https://example.test/..",
        "https://example.test/safe/.",
        "https://example.test/safe/..",
        r"https://example.test/safe\child",
        r"https://example.test/\safe",
    ],
)
def test_noncanonical_dot_and_backslash_paths_are_rejected(base_url: str) -> None:
    with pytest.raises(MoodleConfigurationError):
        MoodleConnectionConfig(base_url, "x")


def test_canonical_subpath_is_accepted() -> None:
    assert MoodleConnectionConfig("https://example.test/safe/path", "x").base_url.endswith(
        "/safe/path"
    )


def test_download_bounds_allow_64_gib() -> None:
    assert MoodleConnectionConfig("https://example.test", "x", max_download_bytes=64 * 1024**3)


def test_token_file_symlink_is_rejected_before_read(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    target.write_text('{"baseUrl":"http://127.0.0.1:8000","token":"x"}', encoding="utf-8")
    link = tmp_path / "token-link.json"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    with pytest.raises(MoodleConfigurationError, match="unsafe"):
        MoodleConnectionConfig.from_token_file(link)
