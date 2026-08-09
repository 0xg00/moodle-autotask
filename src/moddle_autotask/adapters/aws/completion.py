"""Deliver bounded execution outcomes to the already-authorized Telegram chat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from moddle_autotask.adapters.moodle.state import NotificationEvent
from moddle_autotask.adapters.moodle.telegram import TelegramConfig

from .agent_spool import ExecutionProgress


class CompletionTransport(Protocol):
    def send_message(self, chat_id: int, text: str) -> int: ...

    def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class TelegramExecutionNotifier:
    """At-least-once execution-report transport; callers persist only after success."""

    config: TelegramConfig
    client: CompletionTransport

    def notify(self, event: NotificationEvent, progress: ExecutionProgress) -> None:
        if progress.status == "succeeded":
            revision = event.revision_digest.removeprefix("moodle-assignment-v1:")
            filename = f"practica-{revision[:16]}.md"
            caption = f"Práctica terminada: {event.assignment_title}"[:1024]
            self.client.send_document(
                self.config.chat_id,
                filename,
                progress.report_markdown.encode("utf-8"),
                caption,
            )
            return
        self.client.send_message(
            self.config.chat_id,
            f"La práctica no pudo completarse: {event.assignment_title}. {progress.summary}"[
                :4096
            ],
        )
