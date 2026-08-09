"""Direct, bounded Moodle REST client with no proxy or redirect behaviour."""

from __future__ import annotations

import http.client
import json
import ssl
from collections.abc import Mapping
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
