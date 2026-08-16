"""Bounded, digest-bound transfer of approved inputs into a Windows lab.

The only bearer credential used here is an S3 presigned URL.  It deliberately
exists only in the controller process and the AWS SSM command body; it is never
returned, persisted, or included in a job/report/transcript.
"""
# ruff: noqa: E501  # PowerShell command fragments remain auditable as complete commands.

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlsplit

from moodle_autotask.adapters.moodle.state import NotificationEvent
from moodle_autotask.domain.models import LabHandle

from .artifacts import PreparedArtifact, PreparedAssignment
from .labs import LabTranscript

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_MAX_ARTIFACTS = 32
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_MAX_FILENAME_BYTES = 128
_MAX_COMMAND_SOURCE_BYTES = 24 * 1024
_MAX_PRESIGNED_URL_BYTES = 8 * 1024
_GUEST_ROOT = r"C:\ProgramData\MoodleAutotask\inputs"


class GuestInputTransferError(RuntimeError):
    pass


class PresignRunner(Protocol):
    def run_text(self, arguments: tuple[str, ...]) -> str: ...


class GuestCommandExecutor(Protocol):
    def run_ephemeral_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> LabTranscript: ...


@dataclass(frozen=True, slots=True)
class GuestInputArtifact:
    attachment_key: str
    filename: str
    size_bytes: int
    sha256: str
    bucket: str
    object_key: str
    guest_path: str


@dataclass(frozen=True, slots=True)
class GuestInputManifest:
    transfer_digest: str
    manifest_bytes: bytes
    artifacts: tuple[GuestInputArtifact, ...]

    @property
    def guest_root(self) -> str | None:
        return None if not self.artifacts else f"{_GUEST_ROOT}\\{self.transfer_digest}"

    @property
    def guest_paths(self) -> tuple[str, ...]:
        return tuple(item.guest_path for item in self.artifacts)


@dataclass(frozen=True, slots=True)
class GuestInputReady:
    transfer_digest: str
    guest_root: str | None
    guest_paths: tuple[str, ...]


def canonical_guest_input_manifest(
    event: NotificationEvent,
    prepared: PreparedAssignment,
    *,
    excluded_attachment_keys: frozenset[str] = frozenset(),
) -> GuestInputManifest:
    """Build the exact guest input manifest before any SSM side effect.

    Appliance topology is selected outside this adapter.  A direct-AMI path
    supplies the one imported attachment key; a nested Hyper-V path supplies no
    exclusions and deliberately transfers its OVA bytes.
    """
    if (event.task_key, event.revision_digest) != (prepared.task_key, prepared.revision_digest):
        raise GuestInputTransferError("prepared assignment does not match approval")
    if _DIGEST.fullmatch(prepared.specification_digest) is None:
        raise GuestInputTransferError("guest input specification digest is invalid")
    if len(event.attachments) != len(prepared.artifacts):
        raise GuestInputTransferError("prepared attachment count does not match approval")
    prepared_keys = {item.attachment_key for item in prepared.artifacts}
    if not excluded_attachment_keys <= prepared_keys:
        raise GuestInputTransferError("guest input exclusions do not match approval")
    artifacts: list[GuestInputArtifact] = []
    total = 0
    casefolded: set[str] = set()
    for advertised, prepared_artifact in zip(event.attachments, prepared.artifacts, strict=True):
        _validate_pair(advertised.filename, advertised.size_bytes, prepared_artifact)
        if prepared_artifact.attachment_key in excluded_attachment_keys:
            continue
        filename = _safe_windows_filename(prepared_artifact.filename)
        collision = filename.casefold()
        if collision in casefolded:
            raise GuestInputTransferError("guest input filenames collide on Windows")
        casefolded.add(collision)
        if len(artifacts) >= _MAX_ARTIFACTS:
            raise GuestInputTransferError("guest input artifact count exceeds limit")
        if not 0 <= prepared_artifact.size_bytes <= _MAX_ARTIFACT_BYTES:
            raise GuestInputTransferError("guest input artifact size exceeds limit")
        total += prepared_artifact.size_bytes
        if total > _MAX_TOTAL_BYTES:
            raise GuestInputTransferError("guest input total size exceeds limit")
        _validate_object(prepared_artifact)
        artifacts.append(
            GuestInputArtifact(
                prepared_artifact.attachment_key,
                filename,
                prepared_artifact.size_bytes,
                prepared_artifact.sha256,
                prepared_artifact.bucket,
                prepared_artifact.object_key,
                "",  # Filled after the digest is known.
            )
        )
    binding = {
        "artifacts": [
            {
                "attachmentKey": item.attachment_key,
                "bucket": item.bucket,
                "filename": item.filename,
                "objectKey": item.object_key,
                "sha256": item.sha256,
                "sizeBytes": item.size_bytes,
            }
            for item in artifacts
        ],
        "kind": "moodle-guest-input-transfer-v1",
        "revisionDigest": event.revision_digest,
        "specificationDigest": prepared.specification_digest,
        "taskKey": event.task_key,
    }
    transfer_digest = hashlib.sha256(_canonical(binding)).hexdigest()
    _validate_protocol_names(artifacts, transfer_digest)
    paths = tuple(
        GuestInputArtifact(
            item.attachment_key,
            item.filename,
            item.size_bytes,
            item.sha256,
            item.bucket,
            item.object_key,
            f"{_GUEST_ROOT}\\{transfer_digest}\\{item.filename}",
        )
        for item in artifacts
    )
    # The guest needs the derived identity too.  It is not an input to the hash,
    # so there is no recursive digest definition.
    manifest = {**binding, "transferDigest": transfer_digest}
    return GuestInputManifest(transfer_digest, _canonical(manifest), paths)


@dataclass(frozen=True, slots=True)
class AwsGuestInputTransfer:
    """Use short-lived exact-object presigns and one bounded SSM command per file."""

    runner: PresignRunner
    region: str
    url_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", self.region):
            raise ValueError("guest input region is invalid")
        if not 60 <= self.url_ttl_seconds <= 900:
            raise ValueError("guest input URL TTL is invalid")

    def ensure(
        self,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        handle: LabHandle,
        executor: GuestCommandExecutor,
        *,
        excluded_attachment_keys: frozenset[str] = frozenset(),
    ) -> GuestInputReady:
        manifest = canonical_guest_input_manifest(
            event, prepared, excluded_attachment_keys=excluded_attachment_keys
        )
        if not manifest.artifacts:
            return GuestInputReady(manifest.transfer_digest, None, ())
        _validate_command_source_budget(manifest)
        for index, artifact in enumerate(manifest.artifacts):
            url = self._presign(artifact)
            try:
                transcript = executor.run_ephemeral_powershell(
                    handle,
                    (_artifact_script(manifest, artifact, url),),
                    execution_key=_execution_key(manifest.transfer_digest, f"artifact:{index}:{url}"),
                )
            finally:
                # Do not retain a bearer credential in an adapter field or local variable longer
                # than command submission.  The SSM command history is the documented residual.
                url = ""
            _require_success(transcript)
        transcript = executor.run_ephemeral_powershell(
            handle,
            (_manifest_script(manifest),),
            execution_key=_execution_key(manifest.transfer_digest, "manifest"),
        )
        _require_success(transcript)
        return GuestInputReady(manifest.transfer_digest, manifest.guest_root, manifest.guest_paths)

    def _presign(self, artifact: GuestInputArtifact) -> str:
        value = self.runner.run_text(
            (
                "s3",
                "presign",
                f"s3://{artifact.bucket}/{artifact.object_key}",
                "--region",
                self.region,
                "--expires-in",
                str(self.url_ttl_seconds),
            )
        ).strip()
        _validate_presigned_url(value, artifact, self.region, self.url_ttl_seconds)
        return value


def _validate_pair(filename: str, size_bytes: int, artifact: PreparedArtifact) -> None:
    if (filename, size_bytes) != (artifact.filename, artifact.size_bytes):
        raise GuestInputTransferError("prepared attachment metadata does not match approval")


def _validate_object(artifact: PreparedArtifact) -> None:
    if (
        _BUCKET.fullmatch(artifact.bucket) is None
        or _DIGEST.fullmatch(artifact.sha256) is None
        or not isinstance(artifact.attachment_key, str)
        or not artifact.attachment_key
        or not isinstance(artifact.object_key, str)
        or not artifact.object_key
        or len(artifact.object_key.encode("utf-8")) > 1024
        or any(ord(char) < 32 for char in artifact.object_key)
    ):
        raise GuestInputTransferError("guest input object identity is invalid")


def _safe_windows_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized in {".", ".."}
        or len(normalized.encode("utf-8")) > _MAX_FILENAME_BYTES
        or normalized[-1] in {".", " "}
        or any(char in '<>:"/\\|?*\'' or ord(char) < 32 for char in normalized)
        or normalized.split(".", 1)[0].casefold()
        in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    ):
        raise GuestInputTransferError("guest input filename is unsafe")
    return normalized


def _validate_protocol_names(artifacts: list[GuestInputArtifact], transfer_digest: str) -> None:
    artifact_names = {item.filename.casefold() for item in artifacts}
    reserved = {"manifest.json", ".manifest.part"}
    temporary = {
        f".{item.filename}.{_execution_key(transfer_digest, item.filename)[:16]}.part".casefold()
        for item in artifacts
    }
    if artifact_names & (reserved | temporary):
        raise GuestInputTransferError("guest input filename collides with protocol file")


def _validate_command_source_budget(manifest: GuestInputManifest) -> None:
    sources = [_manifest_script(manifest)]
    # The URL is safely base64-transported in the command.  Reserve its complete
    # accepted maximum before generating a presign, so an accepted manifest can
    # never cause a partially started transfer due to the SSM payload ceiling.
    sources.extend(
        _artifact_script(manifest, artifact, "x" * _MAX_PRESIGNED_URL_BYTES)
        for artifact in manifest.artifacts
    )
    if any(len(source.encode("utf-8")) > _MAX_COMMAND_SOURCE_BYTES for source in sources):
        raise GuestInputTransferError("guest input command source exceeds limit")


def _validate_presigned_url(
    value: str, artifact: GuestInputArtifact, region: str, ttl_seconds: int
) -> None:
    if len(value.encode("utf-8")) > _MAX_PRESIGNED_URL_BYTES:
        raise GuestInputTransferError("generated guest input URL exceeds command limit")
    parsed = urlsplit(value)
    expected_hosts = {f"{artifact.bucket}.s3.{region}.amazonaws.com"}
    if region == "us-east-1":
        expected_hosts.add(f"{artifact.bucket}.s3.amazonaws.com")
    expected_path = "/" + quote(artifact.object_key, safe="/")
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise GuestInputTransferError("generated guest input URL has invalid query fields") from error
    query: dict[str, str] = {}
    for key, field in pairs:
        if key in query:
            raise GuestInputTransferError("generated guest input URL has duplicate query fields")
        query[key] = field
    required = {
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Signature",
    }
    allowed = required | {"X-Amz-Security-Token"}
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname not in expected_hosts
        or parsed.netloc != parsed.hostname
        or parsed.port is not None
        or parsed.fragment
        or parsed.path != expected_path
        or set(query) - allowed
        or not required <= set(query)
        or query["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256"
        or query["X-Amz-Expires"] != str(ttl_seconds)
        or query["X-Amz-SignedHeaders"] != "host"
        or re.fullmatch(r"[0-9a-fA-F]{64}", query["X-Amz-Signature"]) is None
        or ("X-Amz-Security-Token" in query and not query["X-Amz-Security-Token"])
    ):
        raise GuestInputTransferError("generated guest input URL is not an exact HTTPS S3 object URL")
    credential = query["X-Amz-Credential"].split("/")
    date = query["X-Amz-Date"]
    try:
        datetime.strptime(date, "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise GuestInputTransferError("generated guest input URL has invalid signing time") from error
    if (
        len(credential) != 5
        or not credential[0]
        or credential[1] != date[:8]
        or credential[2] != region
        or credential[3:] != ["s3", "aws4_request"]
    ):
        raise GuestInputTransferError("generated guest input URL has invalid credential scope")


def _execution_key(transfer_digest: str, purpose: str) -> str:
    return hashlib.sha256(f"moodle-guest-input-v1:{transfer_digest}:{purpose}".encode()).hexdigest()


def _require_success(transcript: LabTranscript) -> None:
    if not transcript.succeeded:
        # Never surface command output: error rendering can contain a URL from an AWS/HTTP failure.
        raise GuestInputTransferError("guest input transfer command failed")


def _artifact_script(manifest: GuestInputManifest, artifact: GuestInputArtifact, url: str) -> str:
    root = manifest.guest_root
    assert root is not None
    temporary = f".{artifact.filename}.{_execution_key(manifest.transfer_digest, artifact.filename)[:16]}.part"
    temporary_names = ",".join(
        f"'.{item.filename}.{_execution_key(manifest.transfer_digest, item.filename)[:16]}.part'"
        for item in manifest.artifacts
    )
    allowed = ",".join(f"'{item.filename}'" for item in manifest.artifacts)
    allowed += f",'manifest.json',{temporary_names}"
    encoded_url = base64.b64encode(url.encode("utf-8")).decode("ascii")
    return "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            f"$root = '{root}'",
            f"$target = Join-Path $root '{artifact.filename}'",
            f"$temporary = Join-Path $root '{temporary}'",
            f"$expectedSize = {artifact.size_bytes}",
            f"$expectedSha256 = '{artifact.sha256}'",
            f"$url = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_url}'))",
            f"$expectedHost = '{urlsplit(url).hostname}'",
            f"$allowedNames = @({allowed})",
            "function Assert-SafeDirectory([string]$path) { if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }; $item = Get-Item -LiteralPath $path -Force; if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'guest input directory is unsafe' } }",
            "function Assert-ExactFile([string]$path) { $item = Get-Item -LiteralPath $path -Force; if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -ne $expectedSize -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedSha256) { throw 'guest input integrity check failed' } }",
            "try {",
            "  Assert-SafeDirectory 'C:\\ProgramData'; Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask'; Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask\\inputs'; Assert-SafeDirectory $root",
            "  foreach ($entry in Get-ChildItem -LiteralPath $root -Force) { if ($entry.Name -notin $allowedNames) { throw 'guest input directory contains unexpected entry' } }",
            "  $uri = [Uri]$url; if ($uri.Scheme -ne 'https' -or $uri.Host -ne $expectedHost) { throw 'guest input URL is unsafe' }",
            "  if (Test-Path -LiteralPath $target) { Assert-ExactFile $target; [Console]::Out.Write('guest input reused'); exit 0 }",
            "  if (Test-Path -LiteralPath $temporary) { $item = Get-Item -LiteralPath $temporary -Force; if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'guest input partial conflicts' }; Remove-Item -LiteralPath $temporary -Force -ErrorAction Stop }",
            "  try { Invoke-WebRequest -UseBasicParsing -MaximumRedirection 0 -Uri $uri -OutFile $temporary -ErrorAction Stop | Out-Null } catch { throw 'guest input download failed' }",
            "  Assert-ExactFile $temporary; if (Test-Path -LiteralPath $target) { throw 'guest input target conflicts' }; [IO.File]::Move($temporary, $target); Assert-ExactFile $target; Assert-SafeDirectory 'C:\\ProgramData'; Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask'; Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask\\inputs'; Assert-SafeDirectory $root",
            "  [Console]::Out.Write('guest input transferred')",
            "} finally { $url = $null; Remove-Variable url -ErrorAction SilentlyContinue }",
        )
    )


def _manifest_script(manifest: GuestInputManifest) -> str:
    encoded = base64.b64encode(manifest.manifest_bytes).decode("ascii")
    root = manifest.guest_root
    assert root is not None
    allowed = ",".join(f"'{item.filename}'" for item in manifest.artifacts) + ",'manifest.json','.manifest.part'"
    return "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            f"$root = '{root}'",
            "$target = Join-Path $root 'manifest.json'",
            f"$expected = [Convert]::FromBase64String('{encoded}')",
            f"$allowedNames = @({allowed})",
            "function Assert-SafeDirectory([string]$path) { if (-not (Test-Path -LiteralPath $path)) { throw 'guest input root is missing' }; $item = Get-Item -LiteralPath $path -Force; if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'guest input directory is unsafe' } }",
            "Assert-SafeDirectory 'C:\\ProgramData'; Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask'; Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask\\inputs'; Assert-SafeDirectory $root",
            "foreach ($entry in Get-ChildItem -LiteralPath $root -Force) { if ($entry.Name -notin $allowedNames) { throw 'guest input directory contains unexpected entry' } }",
            "$manifest = [Text.Encoding]::UTF8.GetString($expected) | ConvertFrom-Json -ErrorAction Stop",
            "foreach ($entry in $manifest.artifacts) { $file = Join-Path $root $entry.filename; if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw 'guest input artifact is missing' }; $item = Get-Item -LiteralPath $file -Force; if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -ne [int64]$entry.sizeBytes -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) { throw 'guest input artifact is invalid' } }",
            "if (Test-Path -LiteralPath $target) { $item = Get-Item -LiteralPath $target -Force; if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or -not [Linq.Enumerable]::SequenceEqual([IO.File]::ReadAllBytes($target), $expected)) { throw 'guest input manifest conflicts' }; [Console]::Out.Write('guest input manifest reused'); exit 0 }",
            "$temporary = Join-Path $root '.manifest.part'",
            "if (Test-Path -LiteralPath $temporary) { $item = Get-Item -LiteralPath $temporary -Force; if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'guest input manifest partial conflicts' }; Remove-Item -LiteralPath $temporary -Force -ErrorAction Stop }",
            "[IO.File]::WriteAllBytes($temporary, $expected)",
            "if (Test-Path -LiteralPath $target) { throw 'guest input manifest conflicts' }; [IO.File]::Move($temporary, $target); $item = Get-Item -LiteralPath $target -Force; if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or -not [Linq.Enumerable]::SequenceEqual([IO.File]::ReadAllBytes($target), $expected)) { throw 'guest input manifest publication failed' }; Assert-SafeDirectory 'C:\\ProgramData'; Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask'; Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask\\inputs'; Assert-SafeDirectory $root",
            "[Console]::Out.Write('guest input ready')",
        )
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
