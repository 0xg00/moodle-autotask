"""Deliver bounded execution outcomes to the already-authorized Telegram chat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from moddle_autotask.adapters.moodle.approval_state import (
    SubmissionButtons,
    SubmissionManifest,
    SubmissionNotification,
)
from moddle_autotask.adapters.moodle.state import NotificationEvent
from moddle_autotask.adapters.moodle.telegram import TelegramConfig

from .agent_spool import ExecutionProgress


class CompletionTransport(Protocol):
    def send_message(
        self, chat_id: int, text: str, buttons: SubmissionButtons | None = None
    ) -> int: ...

    def send_document(self, chat_id: int, filename: str, content: bytes, caption: str) -> int: ...


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
            f"La práctica no pudo completarse: {event.assignment_title}. {progress.summary}"[:4096],
        )

    def notify_submission_ready(
        self, manifest: SubmissionManifest, buttons: SubmissionButtons
    ) -> None:
        self.client.send_message(
            self.config.chat_id,
            f"Revisar entrega: {manifest.event.assignment_title}\n"
            f"Archivo: {manifest.filename}\n"
            f"SHA-256: {manifest.report_digest}",
            buttons,
        )

    def notify_submission_result(self, notification: SubmissionNotification) -> None:
        if notification.status == "submitted":
            text = f"Entrega confirmada: {notification.manifest.event.assignment_title}"
        else:
            text = f"Entrega no confirmada: {notification.manifest.event.assignment_title}"
        self.client.send_message(self.config.chat_id, text)

    def notify_submission_blocked(self, event: NotificationEvent, reason: str) -> None:
        self.client.send_message(
            self.config.chat_id,
            f"Entrega no ofrecida: {event.assignment_title}. {reason}."[:4096],
        )
