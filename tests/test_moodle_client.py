from __future__ import annotations

import pytest
from moodle_http_support import moodle_server

from moddle_autotask.adapters.moodle.client import MoodleClient, MoodleClientError
from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig


def _client(base: str, limit: int = 100) -> MoodleClient:
    return MoodleClient(MoodleConnectionConfig(base, "token secret", max_response_bytes=limit))


def test_post_uses_exact_rest_path_and_form_token_only() -> None:
    with moodle_server(body=b'{"ok":true}') as (base, handler):
        assert _client(base).call("core_webservice_get_site_info") == {"ok": True}
        method, target, body = handler.requests[0]
    assert method == "POST"
    assert target == "/webservice/rest/server.php"
    assert "token" not in target
    assert b"wstoken=token+secret" in body


def test_moodle_exception_is_sanitized() -> None:
    with moodle_server(body=b'{"exception":"token secret"}') as (base, _):
        with pytest.raises(MoodleClientError, match="Moodle REST call failed") as error:
            _client(base).call("core_webservice_get_site_info")
    assert "token secret" not in str(error.value)


def test_redirect_is_rejected_without_follow() -> None:
    with moodle_server(302, b"", {"Location": "/next"}) as (base, handler):
        with pytest.raises(MoodleClientError, match="redirect"):
            _client(base).call("core_webservice_get_site_info")
        assert len(handler.requests) == 1


def test_http_failure_is_sanitized() -> None:
    with moodle_server(500, b"token secret") as (base, _):
        with pytest.raises(MoodleClientError, match="status 500") as error:
            _client(base).call("core_webservice_get_site_info")
    assert "token secret" not in str(error.value)


def test_invalid_json_is_rejected() -> None:
    with moodle_server(body=b"not-json") as (base, _):
        with pytest.raises(MoodleClientError, match="invalid JSON"):
            _client(base).call("core_webservice_get_site_info")


def test_declared_oversize_response_is_rejected() -> None:
    with moodle_server(body=b"{}", headers={"Content-Length": "101"}) as (base, _):
        with pytest.raises(MoodleClientError, match="size limit"):
            _client(base).call("core_webservice_get_site_info")


def test_short_declared_response_is_rejected() -> None:
    with moodle_server(body=b"{}", headers={"Content-Length": "3"}) as (base, _):
        with pytest.raises(MoodleClientError, match="Content-Length"):
            _client(base).call("core_webservice_get_site_info")


def test_streamed_oversize_response_is_rejected() -> None:
    with moodle_server(body=b"{" + b"x" * 200 + b"}") as (base, _):
        with pytest.raises(MoodleClientError, match="size limit"):
            _client(base, 100).call("core_webservice_get_site_info")


def test_invalid_function_is_rejected_before_network() -> None:
    with pytest.raises(MoodleClientError, match="invalid Moodle function"):
        _client("http://127.0.0.1:9").call("bad-function")
