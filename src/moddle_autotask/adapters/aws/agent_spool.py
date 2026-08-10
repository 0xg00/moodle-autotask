"""Digest-bound exchange between the credentialed Codex user and the AWS worker."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from moddle_autotask.adapters.moodle.path_safety import assert_no_indirection
from moddle_autotask.adapters.moodle.state import NotificationEvent
from moddle_autotask.domain.models import ExecutionMode, LabHandle

from .artifacts import PreparedArtifact, PreparedAssignment
from .labs import JsonCommandRunner, LabTranscript

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESULT_BYTES = 2 * 1024 * 1024
_MAX_COMMANDS = 32
_MAX_COMMAND_BYTES = 24 * 1024
_DISPATCH_KIND = "moodle-lab-dispatch-v1"
_COMMAND_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class AgentSpoolError(RuntimeError):
    pass


class LabCommandExecutor(Protocol):
    def dispatch_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> str: ...

    def wait_powershell(
        self, handle: LabHandle, command_id: str, *, execution_key: str
    ) -> LabTranscript: ...

    def run_powershell(
        self, handle: LabHandle, commands: tuple[str, ...], *, execution_key: str
    ) -> LabTranscript: ...


@dataclass(frozen=True, slots=True)
class ExecutionProgress:
    status: str
    summary: str = ""
    report_markdown: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"pending", "succeeded", "failed"}:
            raise ValueError("execution progress status is invalid")


class ExecutionBroker(Protocol):
    def step(
        self,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        mode: ExecutionMode,
        lab_handle: LabHandle | None,
        lab_executor: LabCommandExecutor,
    ) -> ExecutionProgress: ...


@dataclass(frozen=True, slots=True)
class FileAgentBroker:
    jobs_root: Path
    results_root: Path
    region: str
    runner: JsonCommandRunner

    def step(
        self,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        mode: ExecutionMode,
        lab_handle: LabHandle | None,
        lab_executor: LabCommandExecutor,
    ) -> ExecutionProgress:
        if (prepared.task_key, prepared.revision_digest) != (
            event.task_key,
            event.revision_digest,
        ):
            raise AgentSpoolError("prepared assignment does not match approval")
        if mode is ExecutionMode.CENTRAL:
            job_id = self._ensure_job("central", event, prepared, None)
            result = self._result(job_id, "central")
            if result is None:
                return ExecutionProgress("pending")
            return _final_progress(result)
        if lab_handle is None:
            raise AgentSpoolError("lab execution requires a lab handle")
        plan_id = self._ensure_job("lab_plan", event, prepared, None)
        plan = self._result(plan_id, "lab_plan")
        if plan is None:
            return ExecutionProgress("pending")
        if not _result_succeeded(plan):
            return ExecutionProgress("failed", _result_summary(plan))
        commands = _commands(plan)
        plan_digest = hashlib.sha256(_canonical(plan)).hexdigest()
        report_id = self._job_id("lab_report", event, plan_digest)
        report_job = self.jobs_root / report_id
        if not report_job.exists() and not report_job.is_symlink():
            transcript = self._dispatch_or_resume(
                lab_executor, lab_handle, commands, plan_digest, report_id
            )
            if transcript is None:
                return ExecutionProgress("failed", "Lab dispatch state is unsafe")
            self._ensure_job(
                "lab_report",
                event,
                prepared,
                {
                    "planDigest": plan_digest,
                    "labSucceeded": transcript.succeeded,
                    "transcript": transcript.output,
                },
            )
        report = self._result(report_id, "lab_report")
        if report is None:
            return ExecutionProgress("pending")
        return _final_progress(report)

    def _dispatch_or_resume(
        self,
        lab_executor: LabCommandExecutor,
        handle: LabHandle,
        commands: tuple[str, ...],
        plan_digest: str,
        execution_key: str,
    ) -> LabTranscript | None:
        commands_digest = hashlib.sha256(_canonical(list(commands))).hexdigest()
        intent = self._dispatch_payload(
            execution_key, handle, plan_digest, commands_digest, None
        )
        record = self._read_dispatch_record(execution_key, intent)
        if record is None:
            try:
                created = self._publish_dispatch_record(execution_key, intent)
            except AgentSpoolError:
                return None
            if not created:
                record = self._read_dispatch_record(execution_key, intent)
                if record is None:
                    return None
            else:
                command_id = lab_executor.dispatch_powershell(
                    handle, commands, execution_key=execution_key
                )
                dispatched = self._dispatch_payload(
                    execution_key, handle, plan_digest, commands_digest, command_id
                )
                try:
                    self._replace_dispatch_record(execution_key, intent, dispatched)
                except AgentSpoolError:
                    return None
                record = dispatched
        if record == intent:
            return None
        stored_command_id = record.get("commandId")
        if not isinstance(stored_command_id, str):
            return None
        return lab_executor.wait_powershell(
            handle, stored_command_id, execution_key=execution_key
        )

    @staticmethod
    def _dispatch_payload(
        execution_key: str,
        handle: LabHandle,
        plan_digest: str,
        commands_digest: str,
        command_id: str | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": _DISPATCH_KIND,
            "executionKey": execution_key,
            "labHandle": handle.value,
            "planDigest": plan_digest,
            "commandsDigest": commands_digest,
            "state": "intent" if command_id is None else "dispatched",
        }
        if command_id is not None:
            payload["commandId"] = command_id
        return payload

    def _dispatch_root(self) -> Path:
        root = self.jobs_root / "dispatches"
        self._safe_root(root)
        return root

    def _read_dispatch_record(
        self, execution_key: str, intent: dict[str, object]
    ) -> dict[str, object] | None:
        path = self._dispatch_root() / f"{execution_key}.json"
        if not path.exists() and not path.is_symlink():
            return None
        try:
            value = json.loads(_read_regular(path, 4096))
        except (AgentSpoolError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            return None
        record = cast(dict[str, object], value)
        expected = dict(intent)
        if record == expected:
            return record
        command_id = record.get("commandId")
        expected["state"] = "dispatched"
        expected["commandId"] = command_id
        if (
            record != expected
            or not isinstance(command_id, str)
            or _COMMAND_ID.fullmatch(command_id) is None
        ):
            return None
        return record

    def _publish_dispatch_record(self, execution_key: str, payload: dict[str, object]) -> bool:
        root = self._dispatch_root()
        path = root / f"{execution_key}.json"
        temporary = root / f".{execution_key}.{secrets.token_hex(16)}.tmp"
        try:
            _write_exclusive(temporary, _canonical(payload), 0o640)
            os.link(temporary, path)
            _fsync_directory(root)
            return True
        except FileExistsError:
            existing = self._read_dispatch_record(execution_key, payload)
            if existing != payload:
                raise AgentSpoolError("existing lab dispatch record is unsafe") from None
            return False
        except OSError as error:
            raise AgentSpoolError("could not publish lab dispatch record") from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _replace_dispatch_record(
        self, execution_key: str, previous: dict[str, object], payload: dict[str, object]
    ) -> None:
        root = self._dispatch_root()
        path = root / f"{execution_key}.json"
        if self._read_dispatch_record(execution_key, previous) != previous:
            raise AgentSpoolError("lab dispatch record changed unexpectedly")
        temporary = root / f".{execution_key}.{secrets.token_hex(16)}.tmp"
        try:
            _write_exclusive(temporary, _canonical(payload), 0o640)
            os.replace(temporary, path)
            _fsync_directory(root)
        except OSError as error:
            raise AgentSpoolError("could not update lab dispatch record") from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _ensure_job(
        self,
        phase: str,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        context: dict[str, object] | None,
    ) -> str:
        context_digest = None
        if context is not None:
            plan_digest = context.get("planDigest")
            if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
                raise AgentSpoolError("agent report context is invalid")
            context_digest = plan_digest
        job_id = self._job_id(phase, event, context_digest)
        target = self.jobs_root / job_id
        payload = {
            "kind": "moodle-agent-job-v1",
            "jobId": job_id,
            "phase": phase,
            "taskKey": event.task_key,
            "revisionDigest": event.revision_digest,
            "courseName": prepared.course_name,
            "courseShortname": prepared.course_shortname,
            "title": prepared.title,
            "intro": prepared.intro,
            "attachments": [
                {
                    "filename": artifact.filename,
                    "sizeBytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                    "path": f"inputs/{index:04d}-{artifact.filename}",
                }
                for index, artifact in enumerate(self._agent_artifacts(event, prepared))
            ],
            "context": context,
        }
        encoded = _canonical(payload)
        if len(encoded) > _MAX_RESULT_BYTES:
            raise AgentSpoolError("agent job is too large")
        self._safe_root(self.jobs_root)
        if target.exists() or target.is_symlink():
            assert_no_indirection(target)
            if not target.is_dir():
                raise AgentSpoolError("existing agent job directory is unsafe")
            existing = _read_regular(target / "job.json", _MAX_RESULT_BYTES)
            if existing != encoded:
                raise AgentSpoolError("existing agent job does not match exact revision")
            return job_id
        temporary = Path(tempfile.mkdtemp(prefix=f".{job_id}.", dir=self.jobs_root))
        try:
            os.chmod(temporary, 0o750)
            inputs = temporary / "inputs"
            inputs.mkdir(mode=0o750)
            os.chmod(inputs, 0o750)
            for index, artifact in enumerate(self._agent_artifacts(event, prepared)):
                destination = inputs / f"{index:04d}-{artifact.filename}"
                self._download(artifact, destination)
            _write_exclusive(temporary / "job.json", encoded, 0o640)
            _fsync_directory(inputs)
            _fsync_directory(temporary)
            try:
                temporary.rename(target)
                _fsync_directory(self.jobs_root)
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                assert_no_indirection(target)
                if not target.is_dir():
                    raise AgentSpoolError("concurrent agent job is unsafe") from None
                existing = _read_regular(target / "job.json", _MAX_RESULT_BYTES)
                if existing != encoded:
                    raise AgentSpoolError("concurrent agent job conflicts") from None
            return job_id
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, AgentSpoolError):
                raise
            raise AgentSpoolError("could not publish agent job") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _download(self, artifact: PreparedArtifact, destination: Path) -> None:
        self.runner.run_json(
            (
                "s3api",
                "get-object",
                "--region",
                self.region,
                "--bucket",
                artifact.bucket,
                "--key",
                artifact.object_key,
                str(destination),
            )
        )
        metadata = destination.lstat()
        if not stat.S_ISREG(metadata.st_mode) or destination.is_symlink():
            raise AgentSpoolError("downloaded agent input is unsafe")
        digest = _file_sha256(destination)
        if metadata.st_size != artifact.size_bytes or digest != artifact.sha256:
            raise AgentSpoolError("downloaded agent input failed integrity validation")
        os.chmod(destination, 0o640)
        _fsync_file(destination)

    def _result(self, job_id: str, phase: str) -> dict[str, object] | None:
        self._safe_root(self.results_root)
        path = self.results_root / f"{job_id}.json"
        if not path.exists() and not path.is_symlink():
            return None
        raw = _read_regular(path, _MAX_RESULT_BYTES)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AgentSpoolError("agent result is not valid JSON") from error
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise AgentSpoolError("agent result has an invalid shape")
        result = cast(dict[str, object], value)
        if set(result) != {
            "kind",
            "jobId",
            "phase",
            "succeeded",
            "summary",
            "reportMarkdown",
            "powershellCommands",
        }:
            raise AgentSpoolError("agent result has an invalid shape")
        if (
            result.get("kind") != "moodle-agent-result-v1"
            or result.get("jobId") != job_id
            or result.get("phase") != phase
        ):
            raise AgentSpoolError("agent result identity is invalid")
        succeeded = _result_succeeded(result)
        _result_summary(result)
        report = _report(result)
        commands = _commands(result)
        if phase == "lab_plan":
            if succeeded and not commands:
                raise AgentSpoolError("successful lab plan has no commands")
        else:
            if commands:
                raise AgentSpoolError("non-plan result contains lab commands")
            if succeeded and not report.strip():
                raise AgentSpoolError("successful agent result has no report")
        return result

    @staticmethod
    def _agent_artifacts(
        event: NotificationEvent, prepared: PreparedAssignment
    ) -> tuple[PreparedArtifact, ...]:
        if len(event.attachments) != len(prepared.artifacts):
            raise AgentSpoolError("prepared attachment count does not match approval")
        selected: list[PreparedArtifact] = []
        for advertised, artifact in zip(event.attachments, prepared.artifacts, strict=True):
            if (advertised.filename, advertised.size_bytes) != (
                artifact.filename,
                artifact.size_bytes,
            ):
                raise AgentSpoolError("prepared attachment metadata does not match approval")
            if not _safe_filename(artifact.filename):
                raise AgentSpoolError("prepared attachment filename is unsafe")
            if not advertised.is_lab_artifact:
                selected.append(artifact)
        return tuple(selected)

    @staticmethod
    def _job_id(
        phase: str, event: NotificationEvent, context_digest: str | None
    ) -> str:
        payload = {
            "contextDigest": context_digest,
            "phase": phase,
            "revisionDigest": event.revision_digest,
            "taskKey": event.task_key,
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()

    @staticmethod
    def _safe_root(root: Path) -> None:
        if not root.is_absolute():
            raise AgentSpoolError("agent spool root must be absolute")
        assert_no_indirection(root)
        root.mkdir(parents=True, exist_ok=True)
        assert_no_indirection(root)
        if not root.is_dir():
            raise AgentSpoolError("agent spool root is unsafe")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is None:
            os.chmod(path, mode)
        else:
            fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        assert_no_indirection(path)
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except (OSError, ValueError) as error:
        raise AgentSpoolError("agent spool file is unsafe") from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise AgentSpoolError("agent spool file is unsafe")
            data = stream.read(limit + 1)
        if len(data) != metadata.st_size:
            raise AgentSpoolError("agent spool file changed while reading")
        return data
    except OSError as error:
        raise AgentSpoolError("agent spool file is unsafe") from error


def _safe_filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
        and Path(value).name == value
    )


def _fsync_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise AgentSpoolError("could not sync agent input") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise AgentSpoolError("could not sync agent input") from error
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        raise AgentSpoolError("could not sync agent job") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise AgentSpoolError("could not sync agent job") from error
    finally:
        os.close(descriptor)


def _result_succeeded(result: dict[str, object]) -> bool:
    value = result.get("succeeded")
    if not isinstance(value, bool):
        raise AgentSpoolError("agent result success flag is invalid")
    return value


def _result_summary(result: dict[str, object]) -> str:
    value = result.get("summary")
    if not isinstance(value, str) or len(value) > 16_384:
        raise AgentSpoolError("agent result summary is invalid")
    return value


def _report(result: dict[str, object]) -> str:
    value = result.get("reportMarkdown")
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise AgentSpoolError("agent report is invalid")
    return value


def _commands(result: dict[str, object]) -> tuple[str, ...]:
    value = result.get("powershellCommands")
    if (
        not isinstance(value, list)
        or len(value) > _MAX_COMMANDS
        or not all(isinstance(command, str) and command.strip() for command in value)
    ):
        raise AgentSpoolError("agent PowerShell plan is invalid")
    commands = cast(list[str], value)
    if sum(len(command.encode("utf-8")) for command in commands) > _MAX_COMMAND_BYTES:
        raise AgentSpoolError("agent PowerShell plan is too large")
    return tuple(commands)


def _final_progress(result: dict[str, object]) -> ExecutionProgress:
    if not _result_succeeded(result):
        return ExecutionProgress("failed", _result_summary(result), _report(result))
    report = _report(result)
    if not report.strip():
        raise AgentSpoolError("successful agent result has no report")
    return ExecutionProgress("succeeded", _result_summary(result), report)
