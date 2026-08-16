"""Safe selected attachment download implementation."""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, unquote, urlencode, urlsplit

from .config import MoodleConnectionConfig
from .models import MoodleAssignmentSnapshot, MoodleAttachment
from .path_safety import assert_no_indirection


class MoodleDownloadError(RuntimeError):
    pass


_NAMESPACED_DIGEST = re.compile(
    r"(?P<namespace>moodle-(?:task|assignment|attachment)-v1):(?P<digest>[0-9a-f]{64})\Z"
)
_MAX_PLUGINFILE_DECODE_DEPTH = 4


@dataclass(frozen=True, slots=True)
class MoodleDownloadReceipt:
    path: Path
    size_bytes: int
    sha256: str


def download_attachment(
    config: MoodleConnectionConfig,
    assignment: MoodleAssignmentSnapshot,
    attachment_key: str,
    output_directory: Path,
    max_size_bytes: int | None = None,
) -> MoodleDownloadReceipt:
    if not attachment_key:
        raise MoodleDownloadError("attachment key is required")
    attachment = next(
        (item for item in assignment.attachments if item.attachment_key == attachment_key), None
    )
    if attachment is None:
        raise MoodleDownloadError("selected attachment does not belong to task")
    if assignment.site_url != config.base_url:
        raise MoodleDownloadError("task belongs to a different Moodle site")
    limit = config.max_download_bytes if max_size_bytes is None else max_size_bytes
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= config.max_download_bytes
    ):
        raise MoodleDownloadError("download size limit is invalid")
    if attachment.size_bytes > limit:
        raise MoodleDownloadError("attachment declared size exceeds configured size limit")
    task_digest = _namespaced_digest(assignment.task_key, "moodle-task-v1", "task key")
    revision_digest = _namespaced_digest(
        assignment.revision_digest, "moodle-assignment-v1", "revision digest"
    )
    attachment_digest = _namespaced_digest(
        attachment.attachment_key, "moodle-attachment-v1", "attachment key"
    )
    parsed = _validated_pluginfile(config, attachment)
    _safe_output_directory(output_directory)
    final_path = _safe_destination(
        output_directory
        / task_digest
        / revision_digest
        / attachment_digest,
        attachment.filename,
    )
    temporary: Path | None = None
    connection: http.client.HTTPConnection | None = None
    try:
        connection = _connection(config, parsed.hostname, parsed.port)
        request_path = f"{parsed.path}?{urlencode({'token': config.token})}"
        connection.request("GET", request_path, headers={"Accept": "application/octet-stream"})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise MoodleDownloadError("Moodle refused a redirect response")
        if not 200 <= response.status < 300:
            raise MoodleDownloadError(f"Moodle download failed with status {response.status}")
        _validate_length(response.getheader("Content-Length"), attachment.size_bytes, limit)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".moodle-", suffix=".part", dir=final_path.parent
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "wb") as stream:
            while True:
                chunk = response.read(min(64 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise MoodleDownloadError("download exceeds configured size limit")
                stream.write(chunk)
                digest.update(chunk)
        if total != attachment.size_bytes:
            raise MoodleDownloadError("download size does not match Moodle metadata")
        os.replace(temporary, final_path)
        temporary = None
        return MoodleDownloadReceipt(final_path, total, digest.hexdigest())
    except (OSError, http.client.HTTPException) as error:
        raise MoodleDownloadError("Moodle download failed") from error
    finally:
        if connection is not None:
            connection.close()
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _validated_pluginfile(
    config: MoodleConnectionConfig, attachment: MoodleAttachment
) -> SplitResult:
    parsed = urlsplit(attachment.file_url)
    configured = urlsplit(config.base_url)
    if (
        parsed.scheme != configured.scheme
        or parsed.hostname != configured.hostname
        or parsed.port != configured.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(config.pluginfile_prefix)
    ):
        raise MoodleDownloadError("attachment URL is not an allowed Moodle pluginfile URL")
    remainder = parsed.path[len(config.pluginfile_prefix) :]
    if not remainder or not _safe_pluginfile_remainder(remainder):
        raise MoodleDownloadError("attachment URL is not an allowed Moodle pluginfile URL")
    return parsed


def _safe_pluginfile_remainder(remainder: str) -> bool:
    """Reject encoded routing changes before a credential-bearing request is made."""

    candidate = remainder
    for _ in range(_MAX_PLUGINFILE_DECODE_DEPTH):
        if (
            "\x00" in candidate
            or "\\" in candidate
            or "//" in candidate
            or any(segment in {".", ".."} for segment in candidate.split("/"))
        ):
            return False
        decoded = unquote(candidate)
        if decoded == candidate:
            return True
        routing_changed = (
            decoded.count("/") != candidate.count("/")
            or decoded.count("\\") != candidate.count("\\")
        )
        if routing_changed:
            return False
        candidate = decoded
    return False


def _namespaced_digest(value: str, namespace: str, label: str) -> str:
    match = _NAMESPACED_DIGEST.fullmatch(value)
    if match is None or match.group("namespace") != namespace:
        raise MoodleDownloadError(f"{label} is invalid")
    return match.group("digest")


def _connection(
    config: MoodleConnectionConfig, host: str | None, port: int | None
) -> http.client.HTTPConnection:
    if host is None:
        raise MoodleDownloadError("attachment URL is invalid")
    if urlsplit(config.base_url).scheme == "https":
        return http.client.HTTPSConnection(
            host, port, timeout=config.timeout_seconds, context=ssl.create_default_context()
        )
    return http.client.HTTPConnection(host, port, timeout=config.timeout_seconds)


def _validate_length(raw: str | None, expected: int, limit: int) -> None:
    if raw is None:
        return
    try:
        length = int(raw)
    except ValueError as error:
        raise MoodleDownloadError("download has invalid Content-Length") from error
    if length < 0 or length > limit or length != expected:
        raise MoodleDownloadError("download Content-Length does not match allowed attachment size")


def _safe_output_directory(path: Path) -> None:
    try:
        assert_no_indirection(path)
    except ValueError as error:
        raise MoodleDownloadError("output directory is unsafe") from error
    path.mkdir(parents=True, exist_ok=True)
    try:
        assert_no_indirection(path)
    except ValueError as error:
        raise MoodleDownloadError("output directory is unsafe") from error
    if not path.is_dir():
        raise MoodleDownloadError("output directory is unsafe")


def _safe_destination(directory: Path, filename: str) -> Path:
    _safe_output_directory(directory)
    if not filename or filename in {".", ".."} or any(char in filename for char in "/\\\x00"):
        raise MoodleDownloadError("attachment filename is unsafe")
    resolved_directory = directory.resolve(strict=True)
    destination = directory / filename
    if destination.exists() and destination.is_symlink():
        raise MoodleDownloadError("output file is a symlink")
    if destination.resolve(strict=False).parent != resolved_directory:
        raise MoodleDownloadError("output path escapes the selected directory")
    return destination
