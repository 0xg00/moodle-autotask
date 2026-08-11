"""Deliver bounded execution outcomes to the already-authorized Telegram chat."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from moddle_autotask.adapters.moodle.approval_state import (
    SubmissionButtons,
    SubmissionManifest,
    SubmissionNotification,
)
from moddle_autotask.adapters.moodle.state import NotificationEvent
from moddle_autotask.adapters.moodle.telegram import TelegramConfig

from .agent_spool import (
    AgentSpoolError,
    ExecutionProgress,
    _canonical,
    _validate_artifact_manifest,
)


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
    bundles_root: Path | None = None

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
            if progress.provenance is not None:
                bundle = _verified_bundle(self.bundles_root, progress.provenance)
                self.client.send_document(
                    self.config.chat_id,
                    str(progress.provenance["artifactBundleDigest"]) + ".zip",
                    bundle,
                    "Evidencias verificadas",
                )
            return
        self.client.send_message(
            self.config.chat_id,
            f"La práctica no pudo completarse: {event.assignment_title}. {progress.summary}"[:4096],
        )

    def notify_submission_ready(
        self, manifest: SubmissionManifest, buttons: SubmissionButtons
    ) -> None:
        statement = manifest.submission_statement_plain
        if statement is not None:
            content = statement.encode("utf-8")
            if not content:
                raise RuntimeError("Moodle submission statement is empty")
            if len(content) <= 2 * 1024 * 1024:
                self.client.send_document(
                    self.config.chat_id,
                    "moodle-submission-statement.txt",
                    content,
                    "Declaración de entrega completa",
                )
            else:
                raise RuntimeError("Moodle submission statement is too large")
        self.client.send_message(
            self.config.chat_id,
            f"Revisar entrega: {manifest.event.assignment_title}\n"
            f"Archivo: {manifest.filename}\n"
            f"SHA-256: {manifest.report_digest}"
            + (
                f"\nDeclaración SHA-256: {manifest.submission_statement_digest}"
                if statement is not None
                else ""
            ),
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


def _verified_bundle(root: Path | None, provenance: dict[str, object]) -> bytes:

    from moddle_autotask.adapters.moodle.path_safety import assert_no_indirection

    digest, locator, manifest, manifest_digest = (
        provenance.get("artifactBundleDigest"),
        provenance.get("bundleLocator"),
        provenance.get("artifactManifest"),
        provenance.get("artifactManifestDigest"),
    )
    if (
        root is None
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or locator != f"bundles/{digest}.zip"
        or not isinstance(manifest, dict)
        or not isinstance(manifest_digest, str)
        or len(manifest_digest) != 64
        or any(character not in "0123456789abcdef" for character in manifest_digest)
    ):
        raise RuntimeError("execution bundle locator is invalid")
    try:
        _validate_artifact_manifest(manifest)
    except AgentSpoolError as error:
        raise RuntimeError("execution artifact manifest is invalid") from error
    if hashlib.sha256(_canonical(manifest)).hexdigest() != manifest_digest:
        raise RuntimeError("execution artifact manifest digest is invalid")
    path = root / f"{digest}.zip"
    try:
        assert_no_indirection(root)
        assert_no_indirection(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > 2 * 1024 * 1024
        ):
            os.close(descriptor)
            raise RuntimeError("execution bundle is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        assert_no_indirection(path)
        after = path.lstat()
        if len(data) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_size,
        ) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
            raise RuntimeError("execution bundle changed while reading")
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("execution bundle digest is invalid")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise RuntimeError("execution artifact manifest is invalid")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.namelist() != [item.get("path") for item in files if isinstance(item, dict)]:
                raise RuntimeError("execution bundle entries are invalid")
            for info, item in zip(archive.infolist(), files, strict=True):
                if not isinstance(item, dict):
                    raise RuntimeError("execution artifact manifest is invalid")
                content = archive.read(info)
                if (
                    info.is_dir()
                    or info.compress_type != zipfile.ZIP_STORED
                    or len(content) != item.get("size")
                    or hashlib.sha256(content).hexdigest() != item.get("sha256")
                ):
                    raise RuntimeError("execution bundle contents are invalid")
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise RuntimeError("execution bundle is unavailable") from error
    return data
