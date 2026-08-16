"""Direct, bounded Moodle REST client with no proxy or redirect behaviour."""
# ruff: noqa: E501

from __future__ import annotations

import http.client
import json
import ssl
from collections.abc import Mapping
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

from .config import MoodleConnectionConfig


class MoodleClientError(RuntimeError):
    """Sanitized remote service failure."""


class MoodleClient:
    def __init__(self, config: MoodleConnectionConfig) -> None:
        self.config = config

    def call(self, function: str, parameters: Mapping[str, str | int] | None = None) -> object:
        if not function or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in function
        ):
            raise MoodleClientError("invalid Moodle function name")
        body: dict[str, str | int] = {
            "wstoken": self.config.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        if parameters:
            body.update(parameters)
        data = urlencode(body).encode("ascii")
        response = self._request(
            "POST", self.config.rest_path, data, "application/x-www-form-urlencoded"
        )
        try:
            decoded = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MoodleClientError("Moodle returned invalid JSON") from error
        if not isinstance(decoded, (dict, list)):
            raise MoodleClientError("Moodle returned an invalid JSON type")
        if isinstance(decoded, dict) and ("exception" in decoded or "errorcode" in decoded):
            raise MoodleClientError("Moodle REST call failed")
        return decoded

    def upload_draft_file(self, filename: str, content: bytes) -> int:
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 128
            or any(char in filename for char in "/\\\x00\r\n")
            or not isinstance(content, bytes)
            or not content
            or len(content) > self.config.max_download_bytes
        ):
            raise MoodleClientError("Moodle upload is invalid")
        boundary = f"moodle-autotask-{sha256(content).hexdigest()[:24]}"
        fields = (
            ("token", self.config.token),
            ("filepath", "/"),
            ("itemid", "0"),
        )
        body = b"".join(
            [
                *(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                    for name, value in fields
                ),
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file_1"; '
                    f'filename="{filename}"\r\nContent-Type: text/markdown; charset=utf-8\r\n\r\n'
                ).encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode("ascii"),
            ]
        )
        prefix = urlsplit(self.config.base_url).path.rstrip("/")
        response = self._request(
            "POST",
            f"{prefix}/webservice/upload.php",
            body,
            f"multipart/form-data; boundary={boundary}",
        )
        try:
            decoded = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MoodleClientError("Moodle returned invalid upload JSON") from error
        if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
            raise MoodleClientError("Moodle upload response is invalid")
        uploaded = decoded[0]
        item_id = uploaded.get("itemid")
        if uploaded.get("filename") != filename or not isinstance(item_id, int) or item_id <= 0:
            raise MoodleClientError("Moodle upload response is invalid")
        return item_id

    def pluginfile_digest(self, file_url: str, expected_size: int) -> str:
        parsed = urlsplit(file_url)
        configured = urlsplit(self.config.base_url)
        if (
            parsed.scheme != configured.scheme
            or parsed.hostname != configured.hostname
            or parsed.port != configured.port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith(self.config.pluginfile_prefix)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or not 0 < expected_size <= self.config.max_download_bytes
        ):
            raise MoodleClientError("Moodle submission file URL is invalid")
        response = self._request(
            "GET",
            f"{parsed.path}?{urlencode({'token': self.config.token})}",
            b"",
            "application/octet-stream",
        )
        if len(response) != expected_size:
            raise MoodleClientError("Moodle submission file size differs")
        return sha256(response).hexdigest()

    def _request(self, method: str, path: str, body: bytes, content_type: str) -> bytes:
        parsed = urlsplit(self.config.base_url)
        connection: http.client.HTTPConnection
        host = parsed.hostname
        if host is None:
            raise MoodleClientError("configured Moodle endpoint is invalid")
        try:
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(
                    host,
                    parsed.port,
                    timeout=self.config.timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                connection = http.client.HTTPConnection(
                    host, parsed.port, timeout=self.config.timeout_seconds
                )
            connection.request(
                method,
                path,
                body=body,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise MoodleClientError("Moodle refused a redirect response")
            if not 200 <= response.status < 300:
                raise MoodleClientError(f"Moodle HTTP request failed with status {response.status}")
            return _read_limited(response, self.config.max_response_bytes)
        except (OSError, http.client.HTTPException) as error:
            raise MoodleClientError("Moodle HTTP request failed") from error
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass


def _read_limited(response: http.client.HTTPResponse, limit: int) -> bytes:
    raw_length = response.getheader("Content-Length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError as error:
            raise MoodleClientError("Moodle response has invalid Content-Length") from error
        if length < 0 or length > limit:
            raise MoodleClientError("Moodle response exceeds configured size limit")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            if raw_length is not None and total != length:
                raise MoodleClientError("Moodle response Content-Length does not match its body")
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise MoodleClientError("Moodle response exceeds configured size limit")
        chunks.append(chunk)
