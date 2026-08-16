from __future__ import annotations

import hashlib
import io
import os
import socket
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from moddle_autotask.adapters.aws import agent_cli
from moddle_autotask.adapters.aws.agent_cli import (
    CodexSpoolRunner,
    _BundlePublicationBusy,
    _collect_artifact_bundle,
    _validate_bundle,
)
from moddle_autotask.adapters.aws.agent_spool import (
    AgentSpoolError,
    ExecutionProgress,
    FileAgentBroker,
    _canonical,
)
from moddle_autotask.adapters.aws.completion import TelegramExecutionNotifier
from moddle_autotask.adapters.aws.labs import JsonCommandRunner
from moddle_autotask.adapters.moodle.state import MoodleState, NotificationDraft, NotificationEvent
from moddle_autotask.adapters.moodle.telegram import TelegramConfig


def _manifest(path: str, data: bytes) -> dict[str, object]:
    return {
        "kind": "artifact-manifest-v1",
        "files": [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
        "totals": {"files": 1, "bytes": len(data)},
    }


def test_collector_is_deterministic_and_reuses_exact_bundle(tmp_path: Path) -> None:
    outputs, bundles = tmp_path / "outputs", tmp_path / "bundles"
    outputs.mkdir()
    bundles.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    first = _collect_artifact_bundle(outputs, ["report.md"], bundles)
    second = _collect_artifact_bundle(outputs, ["report.md"], bundles)
    assert first == second
    manifest, digest = first
    assert list(bundles.glob("*.zip")) == [bundles / f"{digest}.zip"]
    _validate_bundle(bundles / f"{digest}.zip", manifest, digest)


@pytest.mark.parametrize("path", ["outputs/report.md", "OUTPUTS/report.md"])
def test_collector_rejects_output_root_prefixed_expectation(
    tmp_path: Path, path: str
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "report.md").write_text("report", encoding="utf-8")

    with pytest.raises(AgentSpoolError, match="expected artifact path"):
        _collect_artifact_bundle(outputs, [path], tmp_path / "bundles")


def test_collector_persists_new_bundle_before_removing_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs, bundles = tmp_path / "outputs", tmp_path / "bundles"
    outputs.mkdir()
    bundles.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    events: list[tuple[str, str]] = []
    real_link = os.link
    real_chmod = os.chmod
    real_sync_file = agent_cli._fsync_bundle_file
    real_sync_directory = agent_cli._fsync_directory
    real_unlink = Path.unlink

    def record_link(
        source: Path | str, target: Path | str, *args: object, **kwargs: object
    ) -> None:
        del args, kwargs
        events.append(("link", Path(target).name))
        real_link(source, target)

    def record_chmod(path: Path | str, mode: int, *args: object, **kwargs: object) -> None:
        del args, kwargs
        if Path(path).suffix == ".zip":
            events.append(("chmod", Path(path).name))
        real_chmod(path, mode)

    def record_sync_file(path: Path, links: int) -> None:
        events.append((f"file-{links}", path.name))
        real_sync_file(path, links)

    def record_sync_directory(path: Path) -> None:
        events.append(("directory", path.name))
        real_sync_directory(path)

    def record_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(".bundle-") and path.exists():
            events.append(("unlink", path.name))
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(os, "link", record_link)
    monkeypatch.setattr(os, "chmod", record_chmod)
    monkeypatch.setattr(agent_cli, "_fsync_bundle_file", record_sync_file)
    monkeypatch.setattr(agent_cli, "_fsync_directory", record_sync_directory)
    monkeypatch.setattr(Path, "unlink", record_unlink)

    _manifest, digest = _collect_artifact_bundle(outputs, ["report.md"], bundles)

    target = f"{digest}.zip"
    temp_sync = next(index for index, event in enumerate(events) if event[0] == "file-1")
    linked = events.index(("link", target))
    chmodded = events.index(("chmod", target))
    target_sync = events.index(("file-2", target))
    first_directory = events.index(("directory", bundles.name))
    temporary_unlink = next(index for index, event in enumerate(events) if event[0] == "unlink")
    second_directory = next(
        index
        for index, event in enumerate(events)
        if event == ("directory", bundles.name) and index > temporary_unlink
    )
    assert (
        temp_sync
        < linked
        < chmodded
        < target_sync
        < first_directory
        < temporary_unlink
        < second_directory
    )


def test_collector_recovery_persists_post_link_target_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    seed = tmp_path / "seed"
    _manifest, digest = _collect_artifact_bundle(outputs, ["report.md"], seed)
    raw = (seed / f"{digest}.zip").read_bytes()
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    temporary = bundles / (".bundle-" + "d" * 32 + ".zip")
    temporary.write_bytes(raw)
    target = bundles / f"{digest}.zip"
    try:
        os.link(temporary, target)
    except OSError:
        pytest.skip("hardlinks unavailable")
    events: list[tuple[str, str]] = []
    real_chmod = os.chmod
    real_sync_file = agent_cli._fsync_bundle_file
    real_sync_directory = agent_cli._fsync_directory
    real_unlink = Path.unlink

    def record_chmod(path: Path | str, mode: int, *args: object, **kwargs: object) -> None:
        del args, kwargs
        if Path(path) == target:
            events.append(("chmod", target.name))
        real_chmod(path, mode)

    def record_sync_file(path: Path, links: int) -> None:
        events.append((f"file-{links}", path.name))
        real_sync_file(path, links)

    def record_sync_directory(path: Path) -> None:
        events.append(("directory", path.name))
        real_sync_directory(path)

    def record_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == temporary and path.exists():
            events.append(("unlink", path.name))
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(os, "chmod", record_chmod)
    monkeypatch.setattr(agent_cli, "_fsync_bundle_file", record_sync_file)
    monkeypatch.setattr(agent_cli, "_fsync_directory", record_sync_directory)
    monkeypatch.setattr(Path, "unlink", record_unlink)

    assert _collect_artifact_bundle(outputs, ["report.md"], bundles)[1] == digest
    chmodded = events.index(("chmod", target.name))
    target_sync = events.index(("file-2", target.name))
    first_directory = events.index(("directory", bundles.name))
    unlinked = events.index(("unlink", temporary.name))
    second_directory = next(
        index
        for index, event in enumerate(events)
        if event == ("directory", bundles.name) and index > unlinked
    )
    assert chmodded < target_sync < first_directory < unlinked < second_directory


def test_collector_reuse_syncs_directory_after_explicit_temporary_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs, bundles = tmp_path / "outputs", tmp_path / "bundles"
    outputs.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    _manifest, digest = _collect_artifact_bundle(outputs, ["report.md"], bundles)
    events: list[tuple[str, str]] = []
    real_sync_file = agent_cli._fsync_bundle_file
    real_sync_directory = agent_cli._fsync_directory
    real_unlink = Path.unlink

    def record_sync_file(path: Path, links: int) -> None:
        events.append((f"file-{links}", path.name))
        real_sync_file(path, links)

    def record_sync_directory(path: Path) -> None:
        events.append(("directory", path.name))
        real_sync_directory(path)

    def record_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(".bundle-") and path.exists():
            events.append(("unlink", path.name))
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(agent_cli, "_fsync_bundle_file", record_sync_file)
    monkeypatch.setattr(agent_cli, "_fsync_directory", record_sync_directory)
    monkeypatch.setattr(Path, "unlink", record_unlink)

    assert _collect_artifact_bundle(outputs, ["report.md"], bundles)[1] == digest
    target = f"{digest}.zip"
    target_sync = events.index(("file-1", target))
    first_directory = events.index(("directory", bundles.name))
    temporary_unlink = next(index for index, event in enumerate(events) if event[0] == "unlink")
    second_directory = next(
        index
        for index, event in enumerate(events)
        if event == ("directory", bundles.name) and index > temporary_unlink
    )
    assert target_sync < first_directory < temporary_unlink < second_directory


def test_collector_pre_link_recovery_syncs_directory_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs, bundles = tmp_path / "outputs", tmp_path / "bundles"
    outputs.mkdir()
    bundles.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    temporary = bundles / (".bundle-" + "e" * 32 + ".zip")
    temporary.write_bytes(b"pre-link crash")
    events: list[tuple[str, str]] = []
    real_sync_directory = agent_cli._fsync_directory
    real_unlink = Path.unlink

    def record_sync_directory(path: Path) -> None:
        events.append(("directory", path.name))
        real_sync_directory(path)

    def record_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == temporary and path.exists():
            events.append(("unlink", path.name))
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(agent_cli, "_fsync_directory", record_sync_directory)
    monkeypatch.setattr(Path, "unlink", record_unlink)

    _collect_artifact_bundle(outputs, ["report.md"], bundles)
    assert events.index(("unlink", temporary.name)) < events.index(("directory", bundles.name))


def test_result_publication_recovers_exact_pre_and_post_link_temporaries(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    job_id = "a" * 64
    pre_link = results / (f".{job_id}.json." + "b" * 32 + ".tmp")
    pre_link.write_bytes(b'{"result":"pre"}')
    agent_cli._recover_result_temporaries(results)
    assert not pre_link.exists()

    post_link = results / (f".{job_id}.json." + "c" * 32 + ".tmp")
    post_link.write_bytes(b'{"result":"post"}')
    target = results / f"{job_id}.json"
    try:
        os.link(post_link, target)
    except OSError:
        pytest.skip("hardlinks unavailable")
    agent_cli._recover_result_temporaries(results)
    assert not post_link.exists()
    assert target.lstat().st_nlink == 1


@pytest.mark.parametrize(
    "path", ["../x", "/x", "x\\y", "C:/x", "", "a/./b", "a/../b", "a\x00b", "a/b/c/d/e/f/g/h/i"]
)
def test_collector_rejects_unsafe_expected_paths(tmp_path: Path, path: str) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    with pytest.raises(AgentSpoolError):
        _collect_artifact_bundle(outputs, [path], tmp_path / "bundles")


def test_collector_rejects_exact_set_and_normalized_collision(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "a").write_bytes(b"x")
    with pytest.raises(AgentSpoolError, match="differs"):
        _collect_artifact_bundle(outputs, ["b"], tmp_path / "bundles")
    with pytest.raises(AgentSpoolError, match="collide"):
        _collect_artifact_bundle(outputs, ["A", "a"], tmp_path / "bundles")


def test_collector_rejects_hardlink_and_symlink(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    source = outputs / "a"
    source.write_bytes(b"x")
    try:
        os.link(source, outputs / "b")
    except OSError:
        pytest.skip("hardlinks unavailable")
    with pytest.raises(AgentSpoolError):
        _collect_artifact_bundle(outputs, ["a", "b"], tmp_path / "bundles")
    (outputs / "b").unlink()
    try:
        (outputs / "b").symlink_to(source)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(AgentSpoolError):
        _collect_artifact_bundle(outputs, ["a", "b"], tmp_path / "bundles")


@dataclass
class _Transport:
    fail_zip: bool = False
    documents: list[str] = field(default_factory=list)

    def send_message(self, chat_id: int, text: str, buttons: object = None) -> int:
        del chat_id, text, buttons
        return 1

    def send_document(self, chat_id: int, filename: str, content: bytes, caption: str) -> int:
        del chat_id, content, caption
        self.documents.append(filename)
        if self.fail_zip and filename.endswith(".zip"):
            raise RuntimeError("zip send failed")
        return 1


def _event(tmp_path: Path) -> NotificationEvent:
    state = MoodleState(tmp_path / "moodle.sqlite3")
    event = state.enqueue(
        NotificationDraft(
            "moodle-task-v1:" + "a" * 64,
            "moodle-assignment-v1:" + "b" * 64,
            "C",
            "CS",
            "T",
            0,
            0,
            0,
            0,
            1,
            (),
        ),
        now=1,
    )
    assert event is not None
    return event


def test_notifier_sends_verified_report_then_zip_and_rejects_tamper(tmp_path: Path) -> None:
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    data = b"evidence"
    manifest = _manifest("report.md", data)
    import zipfile

    temporary = bundles / "x.zip"
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("report.md", data)
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    path = bundles / f"{digest}.zip"
    temporary.rename(path)
    provenance: dict[str, object] = {
        "artifactBundleDigest": digest,
        "bundleLocator": f"bundles/{digest}.zip",
        "artifactManifest": manifest,
        "artifactManifestDigest": hashlib.sha256(_canonical(manifest)).hexdigest(),
    }
    transport = _Transport()
    notifier = TelegramExecutionNotifier(
        TelegramConfig("123456:abcdefghijklmnopqrstuvwxyzABCDE", 1, 1), transport, bundles
    )
    event = _event(tmp_path)
    notifier.notify(
        event, ExecutionProgress("succeeded", "done", "# Informe\nEvidence", provenance)
    )
    assert transport.documents[-1] == f"{digest}.zip"
    path.write_bytes(b"tampered")
    with pytest.raises(RuntimeError):
        notifier.notify(
            event, ExecutionProgress("succeeded", "done", "# Informe\nEvidence", provenance)
        )


def test_collector_rejects_bundle_root_indirection_and_recovers_stale_lock(tmp_path: Path) -> None:
    outputs, outside, bundles = tmp_path / "outputs", tmp_path / "outside", tmp_path / "bundles"
    outputs.mkdir()
    outside.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    try:
        bundles.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(AgentSpoolError, match="bundle directory"):
        _collect_artifact_bundle(outputs, ["report.md"], bundles)
    bundles.unlink()
    bundles.mkdir()
    (bundles / ".publish.lock").touch()
    _collect_artifact_bundle(outputs, ["report.md"], bundles)


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX flock contention")
def test_collector_os_lock_contention_is_transient_and_recovers(tmp_path: Path) -> None:
    outputs, bundles = tmp_path / "outputs", tmp_path / "bundles"
    outputs.mkdir()
    bundles.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,sys,time; f=open(sys.argv[1], 'a+b'); "
                "f.write(b'\\0'); f.flush(); fcntl.flock(f, fcntl.LOCK_EX); "
                "print('locked', flush=True); time.sleep(30)"
            ),
            str(bundles / ".publish.lock"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(_BundlePublicationBusy):
            _collect_artifact_bundle(outputs, ["report.md"], bundles)
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    _collect_artifact_bundle(outputs, ["report.md"], bundles)


def test_collector_quota_is_projected_and_existing_bundle_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs, bundles = tmp_path / "outputs", tmp_path / "bundles"
    outputs.mkdir()
    (outputs / "report.md").write_bytes(b"one")
    _manifest, digest = _collect_artifact_bundle(outputs, ["report.md"], bundles)
    published = bundles / f"{digest}.zip"
    size = published.stat().st_size
    monkeypatch.setattr(agent_cli, "_MAX_BUNDLE_TOTAL", size)
    # Replaying a known target does not need capacity for another copy.
    assert _collect_artifact_bundle(outputs, ["report.md"], bundles)[1] == digest
    (bundles / ".orphan").write_bytes(b"x")
    with pytest.raises(AgentSpoolError, match="quota"):
        _collect_artifact_bundle(outputs, ["report.md"], bundles)
    (bundles / ".orphan").unlink()
    (outputs / "report.md").write_bytes(b"two")
    with pytest.raises(AgentSpoolError, match="quota"):
        _collect_artifact_bundle(outputs, ["report.md"], bundles)
    assert list(bundles.glob("*.zip")) == [published]


def test_collector_recovers_pre_link_temporary_before_quota_and_preserves_unrelated(
    tmp_path: Path,
) -> None:
    outputs, bundles = tmp_path / "outputs", tmp_path / "bundles"
    outputs.mkdir()
    bundles.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    abandoned = bundles / (".bundle-" + "a" * 32 + ".zip")
    abandoned.write_bytes(b"pre-link crash")
    unrelated = bundles / ".bundle-not-ours.zip"
    unrelated.write_bytes(b"preserve")

    _manifest, digest = _collect_artifact_bundle(outputs, ["report.md"], bundles)

    assert not abandoned.exists()
    assert unrelated.read_bytes() == b"preserve"
    assert (bundles / f"{digest}.zip").is_file()


def test_collector_recovers_post_link_temporary_and_reuses_target(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    seed = tmp_path / "seed"
    _manifest, digest = _collect_artifact_bundle(outputs, ["report.md"], seed)
    raw = (seed / f"{digest}.zip").read_bytes()

    bundles = tmp_path / "bundles"
    bundles.mkdir()
    abandoned = bundles / (".bundle-" + "b" * 32 + ".zip")
    abandoned.write_bytes(raw)
    target = bundles / f"{digest}.zip"
    try:
        os.link(abandoned, target)
    except OSError:
        pytest.skip("hardlinks unavailable")
    assert abandoned.lstat().st_nlink == target.lstat().st_nlink == 2

    assert _collect_artifact_bundle(outputs, ["report.md"], bundles)[1] == digest
    assert not abandoned.exists()
    assert target.lstat().st_nlink == 1
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o640


def test_collector_refuses_unsafe_exact_temporary_without_deleting_it(tmp_path: Path) -> None:
    outputs, bundles = tmp_path / "outputs", tmp_path / "bundles"
    outputs.mkdir()
    bundles.mkdir()
    (outputs / "report.md").write_bytes(b"verified\n")
    unsafe = bundles / (".bundle-" + "c" * 32 + ".zip")
    unsafe.mkdir()

    with pytest.raises(AgentSpoolError, match="temporary"):
        _collect_artifact_bundle(outputs, ["report.md"], bundles)
    assert unsafe.is_dir()


def test_collector_rejects_fifo_and_socket_on_posix(tmp_path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "mkfifo"):
        pytest.skip("POSIX file types only")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    fifo = outputs / "artifact"
    os.mkfifo(fifo)
    with pytest.raises(AgentSpoolError):
        _collect_artifact_bundle(outputs, ["artifact"], tmp_path / "bundles")
    fifo.unlink()
    unix_socket = socket.socket(
        cast(socket.AddressFamily, getattr(socket, "AF_UNIX"))  # noqa: B009
    )
    try:
        unix_socket.bind(str(outputs / "artifact"))
        with pytest.raises(AgentSpoolError):
            _collect_artifact_bundle(outputs, ["artifact"], tmp_path / "bundles")
    finally:
        unix_socket.close()


def test_workspace_reset_refuses_nested_indirection_without_touching_target(tmp_path: Path) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep")
    nested = workspace / "nested"
    nested.mkdir()
    try:
        (nested / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(AgentSpoolError):
        CodexSpoolRunner._reset_central_workspace(workspace)
    assert sentinel.read_text() == "keep"
    assert workspace.exists()


def test_broker_validates_bundle_from_the_single_verified_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "results"
    bundles = results / "bundles"
    bundles.mkdir(parents=True)
    payload = b"evidence"
    manifest = _manifest("report.md", payload)
    raw_path = bundles / "raw.zip"
    with zipfile.ZipFile(raw_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("report.md", payload)
    raw = raw_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    raw_path.rename(bundles / f"{digest}.zip")
    broker = FileAgentBroker(
        tmp_path / "jobs", results, "eu-west-1", cast(JsonCommandRunner, object())
    )
    original = zipfile.ZipFile
    sources: list[object] = []

    def checked(source: Any, *args: Any, **kwargs: Any) -> zipfile.ZipFile:
        sources.append(source)
        assert isinstance(source, io.BytesIO)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", checked)
    broker._verify_bundle(manifest, digest)
    assert len(sources) == 1
