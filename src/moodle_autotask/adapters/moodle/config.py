"""Configuration loading and canonical endpoint validation."""

from __future__ import annotations

import ipaddress
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .path_safety import assert_no_indirection


class MoodleConfigurationError(ValueError):
    """Raised before any network request is made."""


def _canonical_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MoodleConfigurationError(
            "base URL must be nonblank and have no surrounding whitespace"
        )
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise MoodleConfigurationError("base URL is invalid") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MoodleConfigurationError("base URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise MoodleConfigurationError("base URL must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise MoodleConfigurationError("base URL must not contain a query or fragment")
    if parsed.hostname is None:
        raise MoodleConfigurationError("base URL must contain a host")
    try:
        port = parsed.port
    except ValueError as error:
        raise MoodleConfigurationError("base URL port is invalid") from error
    host = parsed.hostname
    if host != host.lower() or host.endswith("."):
        raise MoodleConfigurationError("base URL host must be canonical")
    if "%" in host or host == "*":
        raise MoodleConfigurationError("base URL host is invalid")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (not isinstance(ip, ipaddress.IPv4Address) or str(ip) != host):
        raise MoodleConfigurationError("base URL IP address must be canonical IPv4")
    if parsed.scheme == "http":
        if not isinstance(ip, ipaddress.IPv4Address) or not _allowed_http_ip(ip):
            raise MoodleConfigurationError("HTTP is allowed only for local private IPv4 endpoints")
    default_port = 443 if parsed.scheme == "https" else 80
    if port == default_port:
        raise MoodleConfigurationError("base URL must not specify the default port")
    if port is not None and not 1 <= port <= 65535:
        raise MoodleConfigurationError("base URL port is invalid")
    if parsed.path and (
        not parsed.path.startswith("/")
        or "\\" in parsed.path
        or "//" in parsed.path
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        or parsed.path.endswith("/")
    ):
        raise MoodleConfigurationError("base URL path must be canonical")
    if "%" in parsed.path:
        raise MoodleConfigurationError("base URL path must not be percent-encoded")
    path = parsed.path or ""
    netloc = host if port is None else f"{host}:{port}"
    canonical = urlunsplit(SplitResult(parsed.scheme, netloc, path, "", ""))
    if canonical != value:
        raise MoodleConfigurationError("base URL must be canonical")
    return canonical


def _allowed_http_ip(address: ipaddress.IPv4Address) -> bool:
    octets = tuple(int(part) for part in str(address).split("."))
    return (
        address.is_loopback
        or octets[0] == 10
        or (octets[0] == 172 and 16 <= octets[1] <= 31)
        or (octets[0] == 192 and octets[1] == 168)
        or (octets[0] == 100 and 64 <= octets[1] <= 127)
    )


@dataclass(frozen=True, slots=True)
class MoodleConnectionConfig:
    """Opaque mobile-service credentials and bounded transport settings."""

    base_url: str
    token: str = field(repr=False)
    timeout_seconds: float = 15.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_download_bytes: int = 16 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _canonical_url(self.base_url))
        if (
            not isinstance(self.token, str)
            or not self.token.strip()
            or self.token != self.token.strip()
        ):
            raise MoodleConfigurationError("token must be nonblank")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 120
        ):
            raise MoodleConfigurationError("timeout must be between zero and 120 seconds")
        for name, value, upper in (
            ("response limit", self.max_response_bytes, 16 * 1024 * 1024),
            ("download limit", self.max_download_bytes, 64 * 1024 * 1024 * 1024),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= upper:
                raise MoodleConfigurationError(f"{name} is out of bounds")

    @classmethod
    def load(cls, token_file: Path | None = None) -> MoodleConnectionConfig:
        if token_file is not None:
            try:
                assert_no_indirection(token_file)
            except ValueError as error:
                raise MoodleConfigurationError("Moodle token file path is unsafe") from error
            if os.name != "nt":
                try:
                    if token_file.stat().st_mode & 0o077:
                        raise MoodleConfigurationError(
                            "Moodle token file permissions are too broad"
                        )
                except OSError as error:
                    raise MoodleConfigurationError("could not read Moodle token file") from error
            try:
                raw = json.loads(token_file.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as error:
                raise MoodleConfigurationError("could not read Moodle token file") from error
            if (
                not isinstance(raw, dict)
                or set(raw) - {"baseUrl", "token", "obtainedAt"}
                or not isinstance(raw.get("baseUrl"), str)
                or not isinstance(raw.get("token"), str)
                or ("obtainedAt" in raw and not isinstance(raw["obtainedAt"], str))
            ):
                raise MoodleConfigurationError(
                    "token file must contain only baseUrl and token strings"
                )
            return cls(raw["baseUrl"], raw["token"])
        base_url = os.environ.get("MOODLE_AUTOTASK_BASE_URL")
        token = os.environ.get("MOODLE_AUTOTASK_TOKEN")
        if base_url is None or token is None:
            raise MoodleConfigurationError(
                "set Moodle token-file or MOODLE_AUTOTASK_BASE_URL and MOODLE_AUTOTASK_TOKEN"
            )
        return cls(base_url, token)

    @classmethod
    def from_token_file(cls, path: Path) -> MoodleConnectionConfig:
        return cls.load(path)

    @classmethod
    def from_environment(cls) -> MoodleConnectionConfig:
        return cls.load()

    @property
    def rest_path(self) -> str:
        return f"{urlsplit(self.base_url).path}/webservice/rest/server.php"

    @property
    def pluginfile_prefix(self) -> str:
        return f"{urlsplit(self.base_url).path}/webservice/pluginfile.php/"
