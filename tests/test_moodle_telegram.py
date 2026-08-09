from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path
from typing import Any, cast

import pytest

from moddle_autotask.adapters.moodle.approval_state import ApprovalButtons, ApprovalState
from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationDraft,
    NotificationEvent,
)
from moddle_autotask.adapters.moodle.telegram import (
    TelegramApprovalSink,
    TelegramClient,
    TelegramConfig,
    TelegramError,
    process_updates,
)

TOKEN = "123456:" + "A" * 35


def _event(tmp_path: Path) -> NotificationEvent:
    event = MoodleState(tmp_path / "moodle.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + "a" * 64,
            "moodle-assignment-v1:" + "b" * 64,
            "Course",
            "ASIX-M01",
            "Assignment",
            0,
            100,
            0,
            0,
            1,
            (),
        ),
        now=1,
    )
    assert event is not None
    return event


class _Response:
    def __init__(self, payload: object, status: int = 200, declared: str | None = None) -> None:
        self.status = status
        self.raw = json.dumps(payload).encode()
        self.declared = declared

    def getheader(self, name: str) -> str | None:
        assert name == "Content-Length"
        return self.declared

    def read(self, limit: int) -> bytes:
        return self.raw[:limit]


class _Connection:
    def __init__(self, response: _Response, calls: list[object]) -> None:
        self.response = response
        self.calls = calls

    def request(
        self, method: str, path: str, body: bytes, headers: dict[str, str]
    ) -> None:
        self.calls.append((method, path, body, headers))

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.calls.append("closed")


def _client(payload: object) -> tuple[TelegramClient, list[object]]:
    calls: list[object] = []
    response = _Response(payload)

    def factory(*args: object, **kwargs: object) -> Any:
        assert args[:2] == ("api.telegram.org", 443)
        assert kwargs["timeout"] == 15
        return _Connection(response, calls)

    return TelegramClient(TelegramConfig(TOKEN, 42, 42), connection_factory=factory), calls


def test_config_file_is_exact_and_secret_is_not_in_repr(tmp_path: Path) -> None:
    path = tmp_path / "telegram.json"
    path.write_text(
        json.dumps({"botToken": TOKEN, "chatId": 42, "allowedUserId": 42}),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)
    config = TelegramConfig.from_file(path)
    assert config.chat_id == config.allowed_user_id == 42
    assert TOKEN not in repr(config)
    path.write_text(
        json.dumps(
            {"botToken": TOKEN, "chatId": 42, "allowedUserId": 42, "extra": True}
        ),
        encoding="utf-8",
    )
    with pytest.raises(TelegramError, match="shape"):
        TelegramConfig.from_file(path)


@pytest.mark.parametrize(
    "token",
    ("", "123:short", "012345:" + "A" * 35, "123456:" + "/" * 35),
)
def test_config_rejects_invalid_tokens(token: str) -> None:
    with pytest.raises(TelegramError, match="token"):
        TelegramConfig(token, 42, 42)


def test_send_message_uses_post_form_and_exact_inline_buttons() -> None:
    client, calls = _client(
        {"ok": True, "result": {"message_id": 7, "chat": {"id": 42}}}
    )
    buttons = ApprovalButtons("a" * 32, "b" * 32, "c" * 32)
    assert client.send_message(42, "Hola á", buttons) == 7
    method, path, body, headers = cast(tuple[str, str, bytes, dict[str, str]], calls[0])
    assert method == "POST" and path == f"/bot{TOKEN}/sendMessage"
    fields = urllib.parse.parse_qs(body.decode())
    assert fields["chat_id"] == ["42"] and fields["text"] == ["Hola á"]
    assert "parse_mode" not in fields
    markup = json.loads(fields["reply_markup"][0])
    assert [button["callback_data"] for row in markup["inline_keyboard"] for button in row] == [
        "ma:" + "a" * 32,
        "ma:" + "b" * 32,
        "ma:" + "c" * 32,
    ]
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert calls[-1] == "closed"


@pytest.mark.parametrize(
    "payload",
    (
        {"ok": False, "description": "secret"},
        {"ok": True},
        {"ok": True, "result": []},
        {"ok": True, "result": {"message_id": 7, "chat": {"id": 99}}},
    ),
)
def test_send_message_rejects_failed_or_malformed_responses(payload: object) -> None:
    client, _ = _client(payload)
    with pytest.raises(TelegramError):
        client.send_message(42, "safe")


@pytest.mark.parametrize(
    ("status", "declared", "case"),
    (
        (302, None, "redirect"),
        (200, "999", "incomplete"),
        (200, "invalid", "bad-length"),
        (200, None, "bad-json"),
        (200, None, "oversize"),
    ),
)
def test_transport_rejects_redirects_incomplete_invalid_and_oversize_bodies(
    status: int, declared: str | None, case: str
) -> None:
    calls: list[object] = []
    response = _Response({}, status, declared)
    response.raw = {
        "redirect": b"{}",
        "incomplete": b'{"ok":true,"result":{}}',
        "bad-length": b"{}",
        "bad-json": b"{",
        "oversize": b"x" * (1024 * 1024 + 1),
    }[case]

    def factory(*args: object, **kwargs: object) -> Any:
        return _Connection(response, calls)

    client = TelegramClient(
        TelegramConfig(TOKEN, 42, 42), connection_factory=factory
    )
    with pytest.raises(TelegramError) as error:
        client.send_message(42, "safe")
    assert TOKEN not in str(error.value)
    assert calls[-1] == "closed"


def test_get_updates_and_callback_answer_use_bounded_post_parameters() -> None:
    calls: list[object] = []
    responses = iter(
        (
            _Response({"ok": True, "result": [{"update_id": 7}]}),
            _Response({"ok": True, "result": True}),
        )
    )

    def factory(*args: object, **kwargs: object) -> Any:
        return _Connection(next(responses), calls)

    client = TelegramClient(
        TelegramConfig(TOKEN, 42, 42), connection_factory=factory
    )
    assert client.get_updates(7, 0) == ({"update_id": 7},)
    client.answer_callback("callback", "Hecho")
    requests = [item for item in calls if isinstance(item, tuple)]
    first = urllib.parse.parse_qs(requests[0][2].decode())
    second = urllib.parse.parse_qs(requests[1][2].decode())
    assert first == {
        "offset": ["7"],
        "timeout": ["0"],
        "limit": ["100"],
        "allowed_updates": ['["callback_query"]'],
    }
    assert second == {"callback_query_id": ["callback"], "text": ["Hecho"]}


def test_network_failure_does_not_expose_token_or_original_exception() -> None:
    def factory(*args: object, **kwargs: object) -> Any:
        raise OSError(TOKEN)

    client = TelegramClient(
        TelegramConfig(TOKEN, 42, 42), connection_factory=factory
    )
    with pytest.raises(TelegramError) as caught:
        client.send_message(42, "safe")
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


class _Transport:
    def __init__(self, updates: tuple[object, ...] = ()) -> None:
        self.updates = updates
        self.sent: list[tuple[int, str, ApprovalButtons | None]] = []
        self.answers: list[tuple[str, str]] = []

    def send_message(
        self, chat_id: int, text: str, buttons: ApprovalButtons | None = None
    ) -> int:
        self.sent.append((chat_id, text, buttons))
        return len(self.sent)

    def get_updates(self, offset: int, timeout_seconds: int) -> tuple[object, ...]:
        return self.updates

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.answers.append((callback_id, text))


def _update(update_id: int, data: str, user: int = 42, chat: int = 42) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user},
            "message": {"chat": {"id": chat}},
            "data": data,
        },
    }


def test_sink_reuses_callbacks_on_delivery_retry(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    transport = _Transport()
    sink = TelegramApprovalSink(TelegramConfig(TOKEN, 42, 42), transport, state)
    event = _event(tmp_path)
    sink(event)
    sink(event)
    assert len(transport.sent) == 2
    assert transport.sent[0][2] == transport.sent[1][2]
    assert state.decision(event.task_key, event.revision_digest) == "pending"


def test_authorized_callbacks_approve_and_show_details_idempotently(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path)
    buttons = state.prepare(event, now=1)
    transport = _Transport(
        (
            _update(1, "ma:" + buttons.details),
            _update(2, "ma:" + buttons.approve),
            _update(3, "ma:" + buttons.approve),
        )
    )
    assert process_updates(TelegramConfig(TOKEN, 42, 42), transport, state) == 3
    assert len(transport.sent) == 1 and "Detalles" in transport.sent[0][1]
    assert [text for _, text in transport.answers] == [
        "Detalles enviados",
        "Tarea aprobada para planificación",
        "Tarea aprobada para planificación",
    ]
    assert state.decision(event.task_key, event.revision_digest) == "approved"
    assert state.next_update_id() == 4
    assert process_updates(TelegramConfig(TOKEN, 42, 42), transport, state) == 0


@pytest.mark.parametrize(("user", "chat"), ((41, 42), (42, 41)))
def test_unauthorized_callback_is_answered_without_decision(
    tmp_path: Path, user: int, chat: int
) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path)
    buttons = state.prepare(event, now=1)
    transport = _Transport((_update(1, "ma:" + buttons.approve, user, chat),))
    process_updates(TelegramConfig(TOKEN, 42, 42), transport, state)
    assert state.decision(event.task_key, event.revision_digest) == "pending"
    assert transport.answers == [("callback-1", "No autorizado")]
    assert state.next_update_id() == 2


def test_malformed_callback_does_not_mutate_and_is_consumed(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    transport = _Transport((_update(5, "task-key-secret"), {"update_id": 6}))
    assert process_updates(TelegramConfig(TOKEN, 42, 42), transport, state) == 2
    assert transport.answers == [("callback-5", "Acción no válida")]
    assert state.next_update_id() == 7


def test_updates_are_ordered_and_duplicate_ids_are_consumed_once(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path)
    buttons = state.prepare(event, now=1)
    transport = _Transport(
        (
            _update(3, "ma:" + buttons.approve),
            _update(2, "ma:" + buttons.details),
            _update(2, "ma:" + buttons.details),
        )
    )
    assert process_updates(TelegramConfig(TOKEN, 42, 42), transport, state) == 2
    assert len(transport.sent) == 1
    assert state.next_update_id() == 4
