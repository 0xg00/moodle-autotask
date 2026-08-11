"""Digest-bound exchange between the credentialed Codex user and the AWS worker."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import unicodedata
import zipfile
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
# Central results are embedded in subsequent immutable jobs.  Keep their
# envelope deliberately small so an accepted upstream result cannot produce a
# downstream job that exceeds the agent's maximum readable size.
_MAX_CENTRAL_RESULT_BYTES = 256 * 1024
_MAX_COMMANDS = 32
_MAX_COMMAND_BYTES = 24 * 1024
_DISPATCH_KIND = "moodle-lab-dispatch-v1"
_COMMAND_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CENTRAL_ROLES = ("central_planner", "central_executor", "central_reviewer")
_CENTRAL_JOB_KIND = "moodle-agent-job-v2"
_CENTRAL_RESULT_KIND = "moodle-agent-result-v2"


class AgentSpoolError(RuntimeError):
    pass


class _CentralJobBudgetExceeded(AgentSpoolError):
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
    provenance: dict[str, object] | None = None

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
            return self._step_central(event, prepared)
        if lab_handle is None:
            raise AgentSpoolError("lab execution requires a lab handle")
        transfer_digest = self._guest_transfer_digest(prepared)
        plan_id = self._ensure_job("lab_plan", event, prepared, None)
        plan = self._result(plan_id, "lab_plan")
        if plan is None:
            return ExecutionProgress("pending")
        if not _result_succeeded(plan):
            return ExecutionProgress("failed", _result_summary(plan))
        commands = _commands(plan)
        plan_digest = hashlib.sha256(_canonical(plan)).hexdigest()
        report_id = self._job_id(
            "lab_report",
            event,
            self._lab_context_digest(transfer_digest, plan_digest),
        )
        report_job = self.jobs_root / report_id
        if not report_job.exists() and not report_job.is_symlink():
            transcript = self._dispatch_or_resume(
                lab_executor, lab_handle, commands, plan_digest, report_id
            )
            if transcript is None:
                return ExecutionProgress("failed", "Lab dispatch state is unsafe")
            context: dict[str, object] = {
                "planDigest": plan_digest,
                "labSucceeded": transcript.succeeded,
                "transcript": transcript.output,
            }
            if transfer_digest:
                context["transferDigest"] = transfer_digest
            self._ensure_job(
                "lab_report",
                event,
                prepared,
                context,
            )
        report = self._result(report_id, "lab_report")
        if report is None:
            return ExecutionProgress("pending")
        return _final_progress(report)

    def _step_central(
        self, event: NotificationEvent, prepared: PreparedAssignment
    ) -> ExecutionProgress:
        """Advance exactly one stateless, digest-bound central role at a time."""
        try:
            planner_id = self._ensure_central_job("central_planner", event, prepared, {})
        except _CentralJobBudgetExceeded:
            return ExecutionProgress("failed", "Central job exceeds serialized size budget")
        planner = self._central_result(planner_id, "central_planner")
        if planner is None:
            return ExecutionProgress("pending")
        if not bool(planner["succeeded"]):
            return ExecutionProgress("failed", str(planner["summary"]))
        plan = cast(dict[str, object], planner["plan"])
        dependencies: dict[str, str] = {
            "plannerJobId": planner_id,
            "planDigest": cast(str, planner["planDigest"]),
            "plannerResultDigest": cast(str, planner["plannerResultDigest"]),
        }
        try:
            executor_id = self._ensure_central_job(
                "central_executor", event, prepared, dependencies, plan=plan
            )
        except _CentralJobBudgetExceeded:
            return ExecutionProgress("failed", "Central job exceeds serialized size budget")
        executor = self._central_result(executor_id, "central_executor")
        if executor is None:
            return ExecutionProgress("pending")
        if not bool(executor["succeeded"]):
            return ExecutionProgress("failed", str(executor["summary"]))
        self._verify_bundle(
            cast(dict[str, object], executor["artifactManifest"]),
            cast(str, executor["artifactBundleDigest"]),
        )
        expected_criteria = {
            cast(str, criterion["id"])
            for criterion in cast(list[dict[str, object]], plan["acceptanceCriteria"])
        }
        if set(cast(dict[str, object], executor["evidence"])) != expected_criteria:
            raise AgentSpoolError("executor criterion coverage is invalid")
        review_dependencies: dict[str, str] = {
            **dependencies,
            "executorJobId": executor_id,
            "executorResultDigest": cast(str, executor["executorResultDigest"]),
            "artifactManifestDigest": cast(str, executor["artifactManifestDigest"]),
            "artifactBundleDigest": cast(str, executor["artifactBundleDigest"]),
        }
        try:
            reviewer_id = self._ensure_central_job(
                "central_reviewer",
                event,
                prepared,
                review_dependencies,
                plan=plan,
                executor_result=executor,
            )
        except _CentralJobBudgetExceeded:
            return ExecutionProgress("failed", "Central job exceeds serialized size budget")
        reviewer = self._central_result(reviewer_id, "central_reviewer")
        if reviewer is None:
            return ExecutionProgress("pending")
        if not bool(reviewer["succeeded"]):
            return ExecutionProgress(
                "failed", str(reviewer["summary"]), str(reviewer["reportMarkdown"])
            )
        expected_criteria = {
            cast(str, criterion["id"])
            for criterion in cast(list[dict[str, object]], plan["acceptanceCriteria"])
        }
        if set(cast(dict[str, object], reviewer.get("decisions", {}))) != expected_criteria:
            raise AgentSpoolError("reviewer criterion coverage is invalid")
        expected_digests = {
            key: value for key, value in review_dependencies.items() if key.endswith("Digest")
        }
        if reviewer.get("dependencyDigests") != expected_digests:
            raise AgentSpoolError("reviewer dependency binding is invalid")
        if not bool(reviewer["accepted"]):
            return ExecutionProgress(
                "failed", str(reviewer["summary"]), str(reviewer["reportMarkdown"])
            )
        provenance = {
            "kind": "moodle-central-provenance-v2",
            "roles": list(_CENTRAL_ROLES),
            "jobIds": [planner_id, executor_id, reviewer_id],
            "plannerJobId": planner_id,
            "executorJobId": executor_id,
            "reviewerJobId": reviewer_id,
            "selectedMode": "central",
            "specificationDigest": prepared.specification_digest,
            "preparedInputManifestDigest": self._prepared_manifest_digest(event, prepared),
            **review_dependencies,
            # The manifest is verified wrapper data carried in executorResult;
            # it is not a string digest dependency in the reviewer job.
            "artifactManifest": executor["artifactManifest"],
            "reviewerResultDigest": reviewer["reviewerResultDigest"],
            "reviewerAccepted": True,
            "bundleLocator": executor["bundleLocator"],
        }
        return ExecutionProgress(
            "succeeded", str(reviewer["summary"]), str(reviewer["reportMarkdown"]), provenance
        )

    def _verify_bundle(self, manifest: dict[str, object], digest: str) -> None:
        path = self.results_root / "bundles" / f"{digest}.zip"
        raw = _read_regular(path, _MAX_RESULT_BYTES)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise AgentSpoolError("central artifact bundle digest is invalid")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise AgentSpoolError("central artifact manifest is invalid")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                expected_names = [item.get("path") for item in files if isinstance(item, dict)]
                if archive.namelist() != expected_names or len(expected_names) != len(files):
                    raise AgentSpoolError("central artifact bundle entries are invalid")
                for info, item in zip(archive.infolist(), files, strict=True):
                    if not isinstance(item, dict):
                        raise AgentSpoolError("central artifact manifest is invalid")
                    content = archive.read(info)
                    if (
                        info.is_dir()
                        or info.compress_type != zipfile.ZIP_STORED
                        or len(content) != item.get("size")
                        or hashlib.sha256(content).hexdigest() != item.get("sha256")
                    ):
                        raise AgentSpoolError("central artifact bundle contents are invalid")
        except (OSError, zipfile.BadZipFile) as error:
            raise AgentSpoolError("central artifact bundle is invalid") from error

    def _prepared_manifest_digest(
        self, event: NotificationEvent, prepared: PreparedAssignment
    ) -> str:
        return hashlib.sha256(
            _canonical(
                [
                    {
                        "filename": item.filename,
                        "sizeBytes": item.size_bytes,
                        "sha256": item.sha256,
                        "attachmentKey": item.attachment_key,
                        "path": f"inputs/{index:04d}-{item.filename}",
                    }
                    for index, item in enumerate(self._agent_artifacts(event, prepared))
                ]
            )
        ).hexdigest()

    def _ensure_central_job(
        self,
        role: str,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        dependencies: dict[str, str],
        *,
        plan: dict[str, object] | None = None,
        executor_result: dict[str, object] | None = None,
    ) -> str:
        if role not in _CENTRAL_ROLES or _DIGEST.fullmatch(prepared.specification_digest) is None:
            raise AgentSpoolError("central job authority is invalid")
        snapshot = {
            "courseName": prepared.course_name,
            "courseShortname": prepared.course_shortname,
            "title": prepared.title,
            "intro": prepared.intro,
        }
        body: dict[str, object] = {
            "kind": _CENTRAL_JOB_KIND,
            "role": role,
            "eventId": event.event_id,
            "taskKey": event.task_key,
            "revisionDigest": event.revision_digest,
            "selectedMode": "central",
            "specificationDigest": prepared.specification_digest,
            "preparedInputManifestDigest": self._prepared_manifest_digest(event, prepared),
            "assignmentSnapshot": snapshot,
            "preparedInputs": [
                {
                    "attachmentKey": a.attachment_key,
                    "filename": a.filename,
                    "sizeBytes": a.size_bytes,
                    "sha256": a.sha256,
                    "path": f"inputs/{i:04d}-{a.filename}",
                }
                for i, a in enumerate(self._agent_artifacts(event, prepared))
            ],
            "dependencies": dependencies,
        }
        if plan is not None:
            body["plan"] = plan
        if executor_result is not None:
            # The reviewer receives only verified wrapper data, never the workspace.
            body["executorResult"] = executor_result
        job_id = hashlib.sha256(_canonical(body)).hexdigest()
        payload = {"jobId": job_id, **body}
        if len(_canonical(payload)) > _MAX_RESULT_BYTES:
            raise _CentralJobBudgetExceeded("central job exceeds serialized size budget")
        self._publish_central_job(job_id, payload, event, prepared)
        return job_id

    def _publish_central_job(
        self,
        job_id: str,
        payload: dict[str, object],
        event: NotificationEvent,
        prepared: PreparedAssignment,
    ) -> None:
        encoded = _canonical(payload)
        if len(encoded) > _MAX_RESULT_BYTES:
            raise AgentSpoolError("central job exceeds serialized size budget")
        target = self.jobs_root / job_id
        self._safe_root(self.jobs_root)
        if target.exists() or target.is_symlink():
            assert_no_indirection(target)
            if (
                not target.is_dir()
                or _read_regular(target / "job.json", _MAX_RESULT_BYTES) != encoded
            ):
                raise AgentSpoolError("existing central job conflicts")
            return
        temporary = Path(tempfile.mkdtemp(prefix=f".{job_id}.", dir=self.jobs_root))
        try:
            # jobs_root is setgid to the controller/agent shared group.
            # Retain it on every directory so file group access does not
            # depend on the controller process's primary group.
            os.chmod(temporary, 0o2750)
            inputs = temporary / "inputs"
            inputs.mkdir(mode=0o2750)
            os.chmod(inputs, 0o2750)
            for index, artifact in enumerate(self._agent_artifacts(event, prepared)):
                self._download(artifact, inputs / f"{index:04d}-{artifact.filename}")
            _write_exclusive(temporary / "job.json", encoded, 0o640)
            _fsync_directory(inputs)
            _fsync_directory(temporary)
            try:
                temporary.rename(target)
                _fsync_directory(self.jobs_root)
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                if (
                    not target.is_dir()
                    or _read_regular(target / "job.json", _MAX_RESULT_BYTES) != encoded
                ):
                    raise AgentSpoolError("concurrent agent job conflicts") from None
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _central_result(self, job_id: str, role: str) -> dict[str, object] | None:
        path = self.results_root / f"{job_id}.json"
        self._safe_root(self.results_root)
        if not path.exists() and not path.is_symlink():
            return None
        try:
            result = cast(dict[str, object], json.loads(_read_regular(path, _MAX_RESULT_BYTES)))
        except (json.JSONDecodeError, TypeError) as error:
            raise AgentSpoolError("central result is invalid") from error
        if (
            result.get("kind") != _CENTRAL_RESULT_KIND
            or result.get("jobId") != job_id
            or result.get("role") != role
        ):
            raise AgentSpoolError("central result identity is invalid")
        _validate_central_result(result, role)
        return result

    def _dispatch_or_resume(
        self,
        lab_executor: LabCommandExecutor,
        handle: LabHandle,
        commands: tuple[str, ...],
        plan_digest: str,
        execution_key: str,
    ) -> LabTranscript | None:
        commands_digest = hashlib.sha256(_canonical(list(commands))).hexdigest()
        intent = self._dispatch_payload(execution_key, handle, plan_digest, commands_digest, None)
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
        return lab_executor.wait_powershell(handle, stored_command_id, execution_key=execution_key)

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
        transfer_digest = self._guest_transfer_digest(prepared)
        context_digest = transfer_digest or None
        if context is not None:
            plan_digest = context.get("planDigest")
            if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
                raise AgentSpoolError("agent report context is invalid")
            supplied_transfer = context.get("transferDigest")
            if transfer_digest and supplied_transfer != transfer_digest:
                raise AgentSpoolError("agent report transfer context is invalid")
            context_digest = self._lab_context_digest(transfer_digest, plan_digest)
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
        if transfer_digest:
            payload["guestInputTransfer"] = {
                "guestPaths": list(prepared.guest_input_paths),
                "transferDigest": transfer_digest,
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
            os.chmod(temporary, 0o2750)
            inputs = temporary / "inputs"
            inputs.mkdir(mode=0o2750)
            os.chmod(inputs, 0o2750)
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
    def _guest_transfer_digest(prepared: PreparedAssignment) -> str:
        digest = prepared.guest_input_transfer_digest
        if _DIGEST.fullmatch(digest) is None:
            raise AgentSpoolError("guest input transfer digest is invalid")
        root = f"C:\\ProgramData\\MoodleAutotask\\inputs\\{digest}\\"
        if any(
            not isinstance(path, str)
            or not path.startswith(root)
            or "\x00" in path
            or len(path.encode("utf-8")) > 512
            for path in prepared.guest_input_paths
        ):
            raise AgentSpoolError("guest input paths are invalid")
        return digest

    @staticmethod
    def _lab_context_digest(transfer_digest: str, plan_digest: str) -> str:
        if not transfer_digest:
            return plan_digest
        return hashlib.sha256(
            _canonical({"planDigest": plan_digest, "transferDigest": transfer_digest})
        ).hexdigest()

    @staticmethod
    def _job_id(phase: str, event: NotificationEvent, context_digest: str | None) -> str:
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
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


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
        initial = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or initial.st_size > limit
        ):
            raise AgentSpoolError("agent spool file is unsafe")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError) as error:
        raise AgentSpoolError("agent spool file is unsafe") from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise AgentSpoolError("agent spool file is unsafe")
            data = stream.read(limit + 1)
        assert_no_indirection(path)
        after = path.lstat()
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or len(data) != metadata.st_size
            or (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
        ):
            raise AgentSpoolError("agent spool file changed while reading")
        return data
    except (OSError, ValueError) as error:
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


def _central_digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _central_text(value: object, name: str, maximum: int = 2_000_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise AgentSpoolError(f"central result {name} is invalid")
    return value


def _safe_artifact_path(value: str) -> bool:
    if (
        not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or len(value.encode("utf-8")) > 240
    ):
        return False
    parts = value.split("/")
    return len(parts) <= 8 and all(part and part not in {".", ".."} for part in parts)


def _validate_artifact_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"kind", "files", "totals"}:
        raise AgentSpoolError("artifact manifest is invalid")
    files, totals = value.get("files"), value.get("totals")
    if (
        value.get("kind") != "artifact-manifest-v1"
        or not isinstance(files, list)
        or not 1 <= len(files) <= 64
    ):
        raise AgentSpoolError("artifact manifest is invalid")
    if not isinstance(totals, dict) or set(totals) != {"files", "bytes"}:
        raise AgentSpoolError("artifact manifest is invalid")
    previous = b""
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise AgentSpoolError("artifact manifest is invalid")
        path, size, digest = item.get("path"), item.get("size"), item.get("sha256")
        if (
            not isinstance(path, str)
            or not _safe_artifact_path(path)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            raise AgentSpoolError("artifact manifest is invalid")
        encoded = path.encode("utf-8")
        normalized = unicodedata.normalize("NFC", path).casefold()
        if encoded <= previous or normalized in seen:
            raise AgentSpoolError("artifact manifest order is invalid")
        previous = encoded
        seen.add(normalized)
        total += size
    if total > 1_900_000 or totals != {"files": len(files), "bytes": total}:
        raise AgentSpoolError("artifact manifest totals are invalid")
    return value


def _central_plan(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "steps",
        "acceptanceCriteria",
        "expectedArtifacts",
    }:
        raise AgentSpoolError("central plan is invalid")
    steps, criteria, artifacts = (
        value["steps"],
        value["acceptanceCriteria"],
        value["expectedArtifacts"],
    )
    if (
        not isinstance(steps, list)
        or not 1 <= len(steps) <= 64
        or not all(isinstance(x, str) and x.strip() for x in steps)
    ):
        raise AgentSpoolError("central plan steps are invalid")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 64:
        raise AgentSpoolError("central plan criteria are invalid")
    ids: set[str] = set()
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) != {"id", "text"}:
            raise AgentSpoolError("central plan criteria are invalid")
        identifier = _central_text(criterion.get("id"), "criterion id", 256)
        _central_text(criterion.get("text"), "criterion text", 16384)
        if identifier in ids:
            raise AgentSpoolError("central plan criterion IDs are not unique")
        ids.add(identifier)
    seen: set[str] = set()
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 64:
        raise AgentSpoolError("central expected artifacts are invalid")
    for item in artifacts:
        if not isinstance(item, str) or not _safe_artifact_path(item):
            raise AgentSpoolError("central expected artifact path is invalid")
        key = unicodedata.normalize("NFC", item).casefold()
        if key in seen:
            raise AgentSpoolError("central expected artifacts collide")
        seen.add(key)
    return value


def _validate_central_result(result: dict[str, object], role: str) -> None:
    try:
        if len(_canonical(result)) > _MAX_CENTRAL_RESULT_BYTES:
            raise AgentSpoolError("central result exceeds serialized size budget")
    except (TypeError, ValueError) as error:
        raise AgentSpoolError("central result is invalid") from error
    common = {"kind", "jobId", "role", "succeeded", "summary", "reportMarkdown"}
    if not isinstance(result.get("succeeded"), bool):
        raise AgentSpoolError("central result success flag is invalid")
    _central_text(result.get("summary"), "summary", 16_384)
    report = result.get("reportMarkdown")
    if not isinstance(report, str) or len(report.encode("utf-8")) > 2_000_000:
        raise AgentSpoolError("central result report is invalid")
    if result["succeeded"] and (not report.strip() or report.strip() == "# Informe"):
        raise AgentSpoolError("central report has no evidence")
    if not result["succeeded"]:
        digest_key = {
            "central_planner": "plannerResultDigest",
            "central_executor": "executorResultDigest",
            "central_reviewer": "reviewerResultDigest",
        }[role]
        if set(result) != common | {digest_key}:
            raise AgentSpoolError("central failure shape is invalid")
        unsigned = {k: v for k, v in result.items() if k != digest_key}
        if result.get(digest_key) != _central_digest(unsigned):
            raise AgentSpoolError("central failure digest is invalid")
        return
    if role == "central_planner":
        if set(result) != common | {"plan", "planDigest", "plannerResultDigest"}:
            raise AgentSpoolError("planner result shape is invalid")
        plan = _central_plan(result["plan"])
        if result["planDigest"] != _central_digest(plan):
            raise AgentSpoolError("planner plan digest is invalid")
        unsigned = {k: v for k, v in result.items() if k != "plannerResultDigest"}
        if result["plannerResultDigest"] != _central_digest(unsigned):
            raise AgentSpoolError("planner result digest is invalid")
    elif role == "central_executor":
        fields = {
            "evidence",
            "artifactManifest",
            "artifactManifestDigest",
            "artifactBundleDigest",
            "bundleLocator",
            "executorResultDigest",
        }
        if set(result) != common | fields or not isinstance(result["evidence"], dict):
            raise AgentSpoolError("executor result shape is invalid")
        if not all(
            isinstance(k, str) and isinstance(v, str) and v.strip()
            for k, v in result["evidence"].items()
        ):
            raise AgentSpoolError("executor evidence is invalid")
        _validate_artifact_manifest(result["artifactManifest"])
        if result["artifactManifestDigest"] != _central_digest(result["artifactManifest"]):
            raise AgentSpoolError("artifact manifest digest is invalid")
        if (
            not isinstance(result["artifactBundleDigest"], str)
            or _DIGEST.fullmatch(result["artifactBundleDigest"]) is None
            or result["bundleLocator"] != f"bundles/{result['artifactBundleDigest']}.zip"
        ):
            raise AgentSpoolError("artifact bundle is invalid")
        unsigned = {k: v for k, v in result.items() if k != "executorResultDigest"}
        if result["executorResultDigest"] != _central_digest(unsigned):
            raise AgentSpoolError("executor result digest is invalid")
    else:
        fields = {"accepted", "decisions", "findings", "dependencyDigests", "reviewerResultDigest"}
        if set(result) != common | fields or not isinstance(result["accepted"], bool):
            raise AgentSpoolError("reviewer result shape is invalid")
        if (
            not isinstance(result["decisions"], dict)
            or not result["decisions"]
            or not all(
                isinstance(k, str) and v in {"accepted", "rejected"}
                for k, v in result["decisions"].items()
            )
        ):
            raise AgentSpoolError("reviewer decisions are invalid")
        if (
            not isinstance(result["findings"], list)
            or len(result["findings"]) > 64
            or not all(isinstance(x, str) and len(x) <= 4096 for x in result["findings"])
        ):
            raise AgentSpoolError("reviewer findings are invalid")
        if not isinstance(result["dependencyDigests"], dict) or not all(
            isinstance(k, str) and isinstance(v, str) and _DIGEST.fullmatch(v)
            for k, v in result["dependencyDigests"].items()
        ):
            raise AgentSpoolError("reviewer dependency digests are invalid")
        if bool(result["accepted"]) != all(
            decision == "accepted" for decision in result["decisions"].values()
        ):
            raise AgentSpoolError("reviewer acceptance is incoherent")
        unsigned = {k: v for k, v in result.items() if k != "reviewerResultDigest"}
        if result["reviewerResultDigest"] != _central_digest(unsigned):
            raise AgentSpoolError("reviewer result digest is invalid")
