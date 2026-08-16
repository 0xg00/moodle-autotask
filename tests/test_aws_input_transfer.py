from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from moodle_autotask.adapters.aws import input_transfer
from moodle_autotask.adapters.aws.artifacts import PreparedArtifact, PreparedAssignment
from moodle_autotask.adapters.aws.input_transfer import (
    _MAX_COMMAND_SOURCE_BYTES,
    AwsGuestInputTransfer,
    GuestInputTransferError,
    _artifact_script,
    _manifest_script,
    _validate_presigned_url,
    canonical_guest_input_manifest,
)
from moodle_autotask.adapters.aws.labs import LabTranscript
from moodle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationAttachment,
    NotificationDraft,
    NotificationEvent,
)
from moodle_autotask.domain.models import LabHandle

# ruff: noqa: E501


def _event(tmp_path: Path, attachments: tuple[NotificationAttachment, ...]) -> NotificationEvent:
    event = MoodleState(tmp_path / "state.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + "a" * 64,
            "moodle-assignment-v1:" + "b" * 64,
            "Course",
            "C",
            "Task",
            0,
            1,
            0,
            0,
            1,
            attachments,
        ),
        now=1,
    )
    assert event is not None
    return event


def _prepared(event: NotificationEvent, names: tuple[str, ...]) -> PreparedAssignment:
    artifacts = tuple(
        PreparedArtifact(
            f"moodle-attachment-v1:{index:064x}",
            name,
            index + 2,
            hashlib.sha256(bytes([index]) * (index + 2)).hexdigest(),
            "private-bucket",
            f"assignments/{index}/{name}",
        )
        for index, name in enumerate(names)
    )
    return PreparedAssignment(
        event.task_key,
        event.revision_digest,
        artifacts,
        specification_digest="c" * 64,
    )


@dataclass
class _Presigner:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run_text(self, arguments: tuple[str, ...]) -> str:
        self.calls.append(arguments)
        location = arguments[2].removeprefix("s3://")
        bucket, key = location.split("/", 1)
        return (
            f"https://{bucket}.s3.eu-south-2.amazonaws.com/{key}?"
            "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
            "X-Amz-Credential=access%2F20260811%2Feu-south-2%2Fs3%2Faws4_request&"
            "X-Amz-Date=20260811T120000Z&X-Amz-Expires=300&X-Amz-SignedHeaders=host&"
            "X-Amz-Signature=" + "a" * 64 + "\n"
        )


@dataclass
class _Guest:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run_ephemeral_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> LabTranscript:
        assert handle == LabHandle("lab:test") and len(execution_key) == 64
        self.calls.append(commands)
        return LabTranscript(True, "ok")


def test_manifest_is_canonical_and_direct_import_exclusion_keeps_companion(
    tmp_path: Path,
) -> None:
    attachments = (
        NotificationAttachment("notes.txt", 2, "text/plain", False),
        NotificationAttachment("base.ova", 3, "application/octet-stream", True),
        NotificationAttachment("base.mf", 4, "text/plain", False),
    )
    event = _event(tmp_path, attachments)
    prepared = _prepared(event, tuple(x.filename for x in attachments))
    nested = canonical_guest_input_manifest(event, prepared)
    assert [item.filename for item in nested.artifacts] == ["notes.txt", "base.ova", "base.mf"]
    manifest = canonical_guest_input_manifest(
        event, prepared, excluded_attachment_keys=frozenset({prepared.artifacts[1].attachment_key})
    )

    payload = json.loads(manifest.manifest_bytes)
    assert [item["filename"] for item in payload["artifacts"]] == ["notes.txt", "base.mf"]
    assert payload["transferDigest"] == manifest.transfer_digest
    assert manifest.manifest_bytes == canonical_guest_input_manifest(
        event,
        prepared,
        excluded_attachment_keys=frozenset({prepared.artifacts[1].attachment_key}),
    ).manifest_bytes


def test_manifest_rejects_windows_casefold_collision_and_unsafe_names(tmp_path: Path) -> None:
    collision = _event(
        tmp_path,
        (
            NotificationAttachment("Readme.txt", 2, None, False),
            NotificationAttachment("README.TXT", 3, None, False),
        ),
    )
    with pytest.raises(GuestInputTransferError, match="collide"):
        canonical_guest_input_manifest(
            collision, _prepared(collision, ("Readme.txt", "README.TXT"))
        )

    unsafe = _event(tmp_path / "unsafe", (NotificationAttachment("..", 2, None, False),))
    with pytest.raises(GuestInputTransferError, match="unsafe"):
        canonical_guest_input_manifest(unsafe, _prepared(unsafe, ("..",)))


@pytest.mark.parametrize("filename", ("MANIFEST.JSON", ".MANIFEST.PART"))
def test_manifest_reserves_protocol_filenames_case_insensitively(tmp_path: Path, filename: str) -> None:
    event = _event(tmp_path, (NotificationAttachment(filename, 2, None, False),))
    with pytest.raises(GuestInputTransferError, match="protocol"):
        canonical_guest_input_manifest(event, _prepared(event, (filename,)))


def test_manifest_rejects_artifact_name_that_collides_with_protocol_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(input_transfer, "_execution_key", lambda transfer_digest, purpose: "f" * 64)
    names = ("alpha", ".alpha.ffffffffffffffff.part")
    event = _event(tmp_path, tuple(NotificationAttachment(name, index + 2, None, False) for index, name in enumerate(names)))
    with pytest.raises(GuestInputTransferError, match="protocol"):
        canonical_guest_input_manifest(event, _prepared(event, names))


def test_transfer_presigns_exact_objects_and_never_places_url_in_result(tmp_path: Path) -> None:
    event = _event(tmp_path, (NotificationAttachment("notes.txt", 2, None, False),))
    presigner, guest = _Presigner(), _Guest()
    ready = AwsGuestInputTransfer(presigner, "eu-south-2").ensure(
        event, _prepared(event, ("notes.txt",)), LabHandle("lab:test"), guest
    )

    assert presigner.calls[0][-2:] == ("--expires-in", "300")
    assert len(guest.calls) == 2  # one artifact, then manifest last
    assert "X-Amz-Signature=" not in guest.calls[0][0]
    assert "FromBase64String" in guest.calls[0][0]
    assert "X-Amz-Signature=" not in ready.transfer_digest
    assert "X-Amz-Signature=" not in "\n".join(guest.calls[1])
    assert guest.calls[0][0].find("-MaximumRedirection 0") >= 0
    assert "[IO.File]::Move" in guest.calls[0][0]
    assert "Assert-SafeDirectory 'C:\\ProgramData\\MoodleAutotask\\inputs'" in guest.calls[0][0]
    assert ".part" in guest.calls[0][0] and "directory contains unexpected entry" in guest.calls[0][0]


def test_empty_explicit_direct_import_input_is_ready_without_presign_or_ssm(tmp_path: Path) -> None:
    event = _event(tmp_path, (NotificationAttachment("base.ova", 2, None, True),))
    presigner, guest = _Presigner(), _Guest()
    prepared = _prepared(event, ("base.ova",))
    ready = AwsGuestInputTransfer(presigner, "eu-south-2").ensure(
        event,
        prepared,
        LabHandle("lab:test"),
        guest,
        excluded_attachment_keys=frozenset({prepared.artifacts[0].attachment_key}),
    )
    assert ready.guest_root is None and ready.guest_paths == ()
    assert not presigner.calls and not guest.calls


@pytest.mark.parametrize(
    "replacement",
    (
        "X-Amz-Algorithm=bad",
        "X-Amz-Expires=301",
        "X-Amz-SignedHeaders=x-host",
        "X-Amz-Signature=bad",
        "X-Amz-Credential=access%2F20260811%2Fus-east-1%2Fs3%2Faws4_request",
        "X-Amz-Credential=access%2F20260811%2Feu-south-2%2Fec2%2Faws4_request",
        "X-Amz-Date=invalid",
        "Unknown=value",
    ),
)
def test_presign_rejects_sigv4_tampering(tmp_path: Path, replacement: str) -> None:
    event = _event(tmp_path, (NotificationAttachment("notes.txt", 2, None, False),))
    artifact = canonical_guest_input_manifest(event, _prepared(event, ("notes.txt",))).artifacts[0]
    url = _Presigner().run_text(("s3", "presign", "s3://private-bucket/assignments/0/notes.txt"))
    key = replacement.split("=", 1)[0]
    parts = [replacement if part.startswith(key + "=") else part for part in url.split("?", 1)[1].split("&")]
    if not any(part.startswith(key + "=") for part in url.split("?", 1)[1].split("&")):
        parts.append(replacement)
    with pytest.raises(GuestInputTransferError):
        _validate_presigned_url(url.split("?", 1)[0] + "?" + "&".join(parts), artifact, "eu-south-2", 300)


def test_presign_accepts_session_token_but_rejects_duplicate_and_url_ambiguity(tmp_path: Path) -> None:
    event = _event(tmp_path, (NotificationAttachment("notes.txt", 2, None, False),))
    artifact = canonical_guest_input_manifest(event, _prepared(event, ("notes.txt",))).artifacts[0]
    url = _Presigner().run_text(("s3", "presign", "s3://private-bucket/assignments/0/notes.txt")).strip()
    _validate_presigned_url(url + "&X-Amz-Security-Token=session", artifact, "eu-south-2", 300)
    for invalid in (url + "&X-Amz-Signature=" + "b" * 64, url.replace("https://", "https://x@"), url + "#x"):
        with pytest.raises(GuestInputTransferError):
            _validate_presigned_url(invalid, artifact, "eu-south-2", 300)


def test_artifact_script_base64_transports_apostrophe_bearing_session_token(tmp_path: Path) -> None:
    event = _event(tmp_path, (NotificationAttachment("notes.txt", 2, None, False),))
    manifest = canonical_guest_input_manifest(event, _prepared(event, ("notes.txt",)))
    artifact = manifest.artifacts[0]
    payload = "x';$global:guestInputInjected=1;$url='"
    url = _Presigner().run_text(("s3", "presign", "s3://private-bucket/assignments/0/notes.txt")).strip()
    url += "&X-Amz-Security-Token=" + payload
    _validate_presigned_url(url, artifact, "eu-south-2", 300)

    script = _artifact_script(manifest, artifact, url)
    assert payload not in script
    assert "$global:guestInputInjected" not in script
    assert base64.b64encode(url.encode()).decode() in script
    assert "$url = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(" in script


def _wide_input(tmp_path: Path, count: int) -> tuple[NotificationEvent, PreparedAssignment]:
    names = tuple(f"{index:02d}-" + "a" * 125 for index in range(count))
    root = tmp_path / str(count)
    root.mkdir()
    event = _event(
        root,
        tuple(NotificationAttachment(name, index + 2, None, False) for index, name in enumerate(names)),
    )
    return event, _prepared(event, names)


def test_transfer_rejects_manifest_command_over_budget_before_presign_or_ssm(
    tmp_path: Path,
) -> None:
    event, prepared = _wide_input(tmp_path, 32)
    presigner, guest = _Presigner(), _Guest()
    with pytest.raises(GuestInputTransferError, match="command source"):
        AwsGuestInputTransfer(presigner, "eu-south-2").ensure(event, prepared, LabHandle("lab:test"), guest)
    assert not presigner.calls and not guest.calls


def test_transfer_accepts_largest_fitting_manifest_command_budget(tmp_path: Path) -> None:
    fitting: tuple[NotificationEvent, PreparedAssignment] | None = None
    for count in range(31, 0, -1):
        event, prepared = _wide_input(tmp_path, count)
        manifest = canonical_guest_input_manifest(event, prepared)
        if len(_manifest_script(manifest).encode()) <= _MAX_COMMAND_SOURCE_BYTES:
            fitting = (event, prepared)
            break
    assert fitting is not None
    event, prepared = fitting
    presigner, guest = _Presigner(), _Guest()
    ready = AwsGuestInputTransfer(presigner, "eu-south-2").ensure(event, prepared, LabHandle("lab:test"), guest)
    assert ready.transfer_digest and len(presigner.calls) == len(prepared.artifacts)
    assert len(guest.calls) == len(prepared.artifacts) + 1


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell.exe")


def _guest_script_at_root(script: str, root: Path) -> str:
    inputs = root / "ProgramData" / "MoodleAutotask" / "inputs"
    replacements = (
        (r"C:\ProgramData\MoodleAutotask\inputs", str(inputs)),
        (r"C:\ProgramData\MoodleAutotask", str(inputs.parent)),
        (r"C:\ProgramData", str(inputs.parent.parent)),
    )
    for source, target in replacements:
        # PowerShell single-quoted literals do not treat backslashes as escapes.
        script = script.replace(source, target)
    return script


def _run_ps(tmp_path: Path, source: str, first: bytes, second: bytes) -> subprocess.CompletedProcess[str]:
    executable = _powershell()
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    prelude = "\n".join(
        (
            "function Invoke-WebRequest {",
            " [CmdletBinding()] param([switch]$UseBasicParsing,[int]$MaximumRedirection,[Uri]$Uri,[string]$OutFile)",
            " if ($MaximumRedirection -ne 0) { throw 'redirect policy lost' }",
            f" if ($OutFile -like '*one.txt*') {{ [IO.File]::WriteAllBytes($OutFile,[Convert]::FromBase64String('{base64.b64encode(first).decode()}')) }} else {{ [IO.File]::WriteAllBytes($OutFile,[Convert]::FromBase64String('{base64.b64encode(second).decode()}')) }}",
            "}",
        )
    )
    path = tmp_path / "harness.ps1"
    path.write_text(prelude + "\n" + source, encoding="utf-8")
    return subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )


def test_generated_powershell_filesystem_contract(tmp_path: Path) -> None:
    attachments = (
        NotificationAttachment("one.txt", 2, None, False),
        NotificationAttachment("two.txt", 3, None, False),
    )
    event = _event(tmp_path, attachments)
    first, second = b"aa", b"bbb"
    prepared = PreparedAssignment(
        event.task_key,
        event.revision_digest,
        (
            PreparedArtifact("moodle-attachment-v1:" + "1" * 64, "one.txt", 2, hashlib.sha256(first).hexdigest(), "private-bucket", "assignments/one.txt"),
            PreparedArtifact("moodle-attachment-v1:" + "2" * 64, "two.txt", 3, hashlib.sha256(second).hexdigest(), "private-bucket", "assignments/two.txt"),
        ),
        specification_digest="c" * 64,
    )
    manifest = canonical_guest_input_manifest(event, prepared)
    root = tmp_path / "guest"
    scripts = [_guest_script_at_root(_artifact_script(manifest, item, "https://private-bucket.s3.eu-south-2.amazonaws.com/x?x"), root) for item in manifest.artifacts]
    future_part = root / "ProgramData" / "MoodleAutotask" / "inputs" / manifest.transfer_digest / f".{manifest.artifacts[1].filename}.{hashlib.sha256(f'moodle-guest-input-v1:{manifest.transfer_digest}:{manifest.artifacts[1].filename}'.encode()).hexdigest()[:16]}.part"
    future_part.parent.mkdir(parents=True)
    future_part.write_bytes(b"partial")
    assert _run_ps(tmp_path, scripts[0], first, second).returncode == 0
    assert future_part.exists()  # Future protocol temp is tolerated until its own command.
    assert _run_ps(tmp_path, scripts[1], first, second).returncode == 0
    final = _run_ps(tmp_path, _guest_script_at_root(_manifest_script(manifest), root), first, second)
    assert final.returncode == 0
    guest_root = future_part.parent
    assert (guest_root / "one.txt").read_bytes() == first
    assert (guest_root / "two.txt").read_bytes() == second
    assert (guest_root / "manifest.json").read_bytes() == manifest.manifest_bytes
    assert not list(guest_root.glob("*.part"))
    assert _run_ps(tmp_path, scripts[0], first, second).returncode == 0
    assert _run_ps(tmp_path, scripts[1], first, second).returncode == 0
    assert _run_ps(tmp_path, _guest_script_at_root(_manifest_script(manifest), root), first, second).returncode == 0
    assert not list(guest_root.glob("*.part"))

    unexpected_root = tmp_path / "unexpected"
    unexpected_guest = unexpected_root / "ProgramData" / "MoodleAutotask" / "inputs" / manifest.transfer_digest
    unexpected_guest.mkdir(parents=True)
    (unexpected_guest / "foreign.txt").write_bytes(b"foreign")
    assert _run_ps(
        tmp_path,
        _guest_script_at_root(
            _artifact_script(manifest, manifest.artifacts[0], "https://private-bucket.s3.eu-south-2.amazonaws.com/x?x"),
            unexpected_root,
        ),
        first,
        second,
    ).returncode != 0
    assert not (unexpected_guest / "one.txt").exists()

    remaining_part = guest_root / ".one.txt.remaining.part"
    remaining_part.write_bytes(b"partial")
    assert _run_ps(tmp_path, _guest_script_at_root(_manifest_script(manifest), root), first, second).returncode != 0
    assert (guest_root / "manifest.json").read_bytes() == manifest.manifest_bytes


def test_generated_powershell_rejects_reparse_ancestor(tmp_path: Path) -> None:
    attachments = (NotificationAttachment("one.txt", 2, None, False),)
    event = _event(tmp_path, attachments)
    artifact = PreparedArtifact(
        "moodle-attachment-v1:" + "1" * 64,
        "one.txt",
        2,
        hashlib.sha256(b"aa").hexdigest(),
        "private-bucket",
        "assignments/one.txt",
    )
    manifest = canonical_guest_input_manifest(
        event, PreparedAssignment(event.task_key, event.revision_digest, (artifact,), specification_digest="c" * 64)
    )
    root, outside = tmp_path / "reparse", tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    try:
        os.symlink(outside, root / "ProgramData", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    completed = _run_ps(
        tmp_path,
        _guest_script_at_root(
            _artifact_script(manifest, manifest.artifacts[0], "https://private-bucket.s3.eu-south-2.amazonaws.com/x?x"), root
        ),
        b"aa",
        b"",
    )
    assert completed.returncode != 0
    assert not list(outside.rglob("*"))
