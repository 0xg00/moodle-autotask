"""Outbound-only Telegram transport and exact human approval handling."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import ssl
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .approval_state import ApprovalButtons, ApprovalState, ApprovalStateError
from .path_safety import assert_no_indirection
from .state import NotificationEvent


class TelegramError(RuntimeError):
    pass


_BOT_TOKEN = re.compile(r"^[1-9][0-9]{5,15}:[A-Za-z0-9_-]{30,100}$")
_CALLBACK_DATA = re.compile(r"^ma:([A-Za-z0-9_-]{32})$")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_MESSAGE_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    chat_id: int
    allowed_user_id: int
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not isinstance(self.bot_token, str) or not _BOT_TOKEN.fullmatch(self.bot_token):
            raise TelegramError("Telegram bot token is invalid")
        for value in (self.chat_id, self.allowed_user_id):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value < 2**63:
                raise TelegramError("Telegram identity is invalid")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or not 1 <= self.timeout_seconds <= 120
        ):
            raise TelegramError("Telegram timeout is invalid")

    @classmethod
    def from_file(cls, path: Path) -> TelegramConfig:
        try:
            assert_no_indirection(path)
        except ValueError as error:
            raise TelegramError("Telegram configuration path is unsafe") from error
        if os.name != "nt":
            try:
                if path.stat().st_mode & 0o077:
                    raise TelegramError("Telegram configuration permissions are too broad")
            except OSError as error:
                raise TelegramError("could not read Telegram configuration") from error
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise TelegramError("could not read Telegram configuration") from error
        if not isinstance(raw, dict) or set(raw) != {"botToken", "chatId", "allowedUserId"}:
            raise TelegramError("Telegram configuration shape is invalid")
        return cls(raw["botToken"], raw["chatId"], raw["allowedUserId"])


class TelegramTransport(Protocol):
    def send_message(
        self, chat_id: int, text: str, buttons: ApprovalButtons | None = None
    ) -> int: ...

    def get_updates(self, offset: int, timeout_seconds: int) -> tuple[object, ...]: ...

    def answer_callback(self, callback_id: str, text: str) -> None: ...


class TelegramClient:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        connection_factory: Callable[
            ..., http.client.HTTPSConnection
        ] = http.client.HTTPSConnection,
    ) -> None:
        self.config = config
        self._connection_factory = connection_factory

    def send_message(
        self, chat_id: int, text: str, buttons: ApprovalButtons | None = None
    ) -> int:
        if chat_id != self.config.chat_id or not isinstance(text, str) or not text:
            raise TelegramError("Telegram message is invalid")
        if len(text) > _MAX_MESSAGE_LENGTH:
            raise TelegramError("Telegram message is too long")
        parameters: dict[str, str] = {"chat_id": str(chat_id), "text": text}
        if buttons is not None:
            parameters["reply_markup"] = json.dumps(
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": "Hacer tarea",
                                "callback_data": f"ma:{buttons.approve}",
                            },
                            {"text": "Ignorar", "callback_data": f"ma:{buttons.ignore}"},
                        ],
                        [
                            {
                                "text": "Ver detalles",
                                "callback_data": f"ma:{buttons.details}",
                            }
                        ],
                    ]
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        result = self._post("sendMessage", parameters)
        if not isinstance(result, dict):
            raise TelegramError("Telegram response is invalid")
        message_id = result.get("message_id")
        chat = result.get("chat")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id < 1
            or not isinstance(chat, dict)
            or chat.get("id") != chat_id
        ):
            raise TelegramError("Telegram response is invalid")
        return message_id

    def get_updates(self, offset: int, timeout_seconds: int) -> tuple[object, ...]:
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 0 <= timeout_seconds <= 50
        ):
            raise TelegramError("Telegram polling options are invalid")
        result = self._post(
            "getUpdates",
            {
                "offset": str(offset),
                "timeout": str(timeout_seconds),
                "limit": "100",
                "allowed_updates": '["callback_query"]',
            },
            timeout_seconds=max(self.config.timeout_seconds, timeout_seconds + 5),
        )
        if not isinstance(result, list) or len(result) > 100:
            raise TelegramError("Telegram response is invalid")
        return tuple(result)

    def answer_callback(self, callback_id: str, text: str) -> None:
        if (
            not isinstance(callback_id, str)
            or not callback_id
            or len(callback_id) > 256
            or not isinstance(text, str)
            or not text
            or len(text) > 200
        ):
            raise TelegramError("Telegram callback answer is invalid")
        self._post(
            "answerCallbackQuery", {"callback_query_id": callback_id, "text": text}
        )

    def _post(
        self, method: str, parameters: dict[str, str], timeout_seconds: float | None = None
    ) -> object:
        body = urllib.parse.urlencode(parameters).encode("utf-8")
        connection: http.client.HTTPSConnection | None = None
        try:
            connection = self._connection_factory(
                "api.telegram.org",
                443,
                timeout=timeout_seconds or self.config.timeout_seconds,
                context=ssl.create_default_context(),
            )
            connection.request(
                "POST",
                f"/bot{self.config.bot_token}/{method}",
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    size = int(declared)
                except ValueError as error:
                    raise TelegramError("Telegram response is invalid") from error
                if not 0 <= size <= _MAX_RESPONSE_BYTES:
                    raise TelegramError("Telegram response is too large")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise TelegramError("Telegram response is too large")
            if declared is not None and len(raw) != size:
                raise TelegramError("Telegram response is incomplete")
            if response.status != 200:
                raise TelegramError("Telegram request failed")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise TelegramError("Telegram response is invalid") from error
            if (
                not isinstance(payload, dict)
                or payload.get("ok") is not True
                or "result" not in payload
            ):
                raise TelegramError("Telegram request failed")
            return payload["result"]
        except TelegramError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise TelegramError("Telegram request failed") from None
        finally:
            if connection is not None:
                connection.close()


class TelegramApprovalSink:
    def __init__(
        self, config: TelegramConfig, client: TelegramTransport, state: ApprovalState
    ) -> None:
        self.config = config
        self.client = client
        self.state = state

    def __call__(self, value: object) -> None:
        if not isinstance(value, NotificationEvent):
            raise TelegramError("notification event is invalid")
        buttons = self.state.prepare(value)
        message_id = self.client.send_message(
            self.config.chat_id, _summary_message(value), buttons
        )
        self.state.mark_notified(value, self.config.chat_id, message_id)


def process_updates(
    config: TelegramConfig,
    client: TelegramTransport,
    state: ApprovalState,
    *,
    timeout_seconds: int = 0,
) -> int:
    updates = sorted(
        client.get_updates(state.next_update_id(), timeout_seconds), key=_update_id
    )
    processed = 0
    for raw in updates:
        update_id = _update_id(raw)
        if update_id < state.next_update_id():
            continue
        callback = raw.get("callback_query") if isinstance(raw, dict) else None
        if callback is not None:
            _process_callback(config, client, state, callback)
        state.advance_update_id(update_id)
        processed += 1
    return processed


def run_polling(
    config: TelegramConfig,
    client: TelegramTransport,
    state: ApprovalState,
    *,
    timeout_seconds: int = 30,
    retry_seconds: int = 5,
    wait: Callable[[float], object] = time.sleep,
) -> None:
    if not 1 <= timeout_seconds <= 50 or not 1 <= retry_seconds <= 300:
        raise TelegramError("Telegram polling options are invalid")
    while True:
        try:
            try:
                process_updates(config, client, state, timeout_seconds=timeout_seconds)
            except TelegramError:
                wait(retry_seconds)
        except KeyboardInterrupt:
            return


def _process_callback(
    config: TelegramConfig,
    client: TelegramTransport,
    state: ApprovalState,
    callback: object,
) -> None:
    if not isinstance(callback, dict):
        return
    callback_id = callback.get("id")
    sender = callback.get("from")
    message = callback.get("message")
    data = callback.get("data")
    if (
        not isinstance(callback_id, str)
        or not callback_id
        or len(callback_id) > 256
        or not isinstance(sender, dict)
        or not isinstance(message, dict)
        or not isinstance(message.get("chat"), dict)
        or not isinstance(data, str)
    ):
        return
    user_id = sender.get("id")
    chat_id = message["chat"].get("id")
    if user_id != config.allowed_user_id or chat_id != config.chat_id:
        client.answer_callback(callback_id, "No autorizado")
        return
    match = _CALLBACK_DATA.fullmatch(data)
    if match is None:
        client.answer_callback(callback_id, "Acción no válida")
        return
    try:
        outcome = state.resolve(match.group(1), user_id, config.allowed_user_id)
    except ApprovalStateError:
        client.answer_callback(callback_id, "Acción caducada o no válida")
        return
    if outcome.result == "details":
        client.send_message(config.chat_id, _details_message(outcome.event))
        answer = "Detalles enviados"
    elif outcome.result in {"approved", "already_approved"}:
        answer = "Tarea aprobada para planificación"
    elif outcome.result in {"ignored", "already_ignored"}:
        answer = "Tarea ignorada"
    else:
        answer = "La tarea ya tiene otra decisión"
    client.answer_callback(callback_id, answer)


def _update_id(raw: object) -> int:
    if not isinstance(raw, dict):
        raise TelegramError("Telegram update is invalid")
    value = raw.get("update_id")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TelegramError("Telegram update is invalid")
    return value


def _summary_message(event: NotificationEvent) -> str:
    status = "Nueva tarea" if event.status == "NEW" else "Tarea actualizada"
    labs = [item.filename for item in event.attachments if item.is_lab_artifact]
    lines = [status, f"{event.course_shortname} · {event.assignment_title}"]
    if event.due_date:
        lines.append("Entrega: " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(event.due_date)))
    lines.append(f"Adjuntos: {len(event.attachments)}")
    if labs:
        lines.append("Laboratorio requerido: " + ", ".join(labs[:5]))
    return _bounded_message(lines)


def _details_message(event: NotificationEvent) -> str:
    lines = [
        "Detalles de la tarea",
        f"Curso: {event.course_name} ({event.course_shortname})",
        f"Actividad: {event.assignment_title}",
        f"Estado: {event.status}",
    ]
    if event.allows_submissions_from:
        lines.append(
            "Disponible: "
            + time.strftime(
                "%Y-%m-%d %H:%M UTC", time.gmtime(event.allows_submissions_from)
            )
        )
    if event.due_date:
        lines.append("Entrega: " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(event.due_date)))
    if event.cutoff_date:
        lines.append(
            "Cierre: "
            + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(event.cutoff_date))
        )
    lines.append("Adjuntos:")
    if event.attachments:
        lines.extend(
            f"- {item.filename} ({item.size_bytes} bytes)"
            for item in event.attachments[:50]
        )
    else:
        lines.append("- Ninguno")
    return _bounded_message(lines)


def _bounded_message(lines: list[str]) -> str:
    message = "\n".join(lines)
    if len(message) <= _MAX_MESSAGE_LENGTH:
        return message
    return message[: _MAX_MESSAGE_LENGTH - 1] + "…"
