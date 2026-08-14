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
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from moddle_autotask.adapters.moodle.path_safety import assert_no_indirection
from moddle_autotask.adapters.moodle.state import NotificationEvent
from moddle_autotask.domain.models import ExecutionMode, LabHandle

from . import central_protocol, lab_protocol
from .artifacts import PreparedArtifact, PreparedAssignment
from .labs import JsonCommandRunner, LabTranscript
from .retention_fs import RetentionBarrierError, controller_job_barred, retention_job_lock
from .storage_quota import (
    StorageCapacityError,
    StorageDemand,
    StorageEnvelopeError,
    StoragePolicy,
    admit_owner_write,
    storage_admission_lock,
    storage_demand_for_files,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESULT_BYTES = 2 * 1024 * 1024
_MAX_COMMANDS = 32
_MAX_COMMAND_BYTES = 24 * 1024
_DISPATCH_KIND = "moodle-lab-dispatch-v1"
_COMMAND_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MAX_CENTRAL_RESULT_BYTES = central_protocol.MAX_CENTRAL_RESULT_BYTES
_CENTRAL_ROLES = central_protocol.CENTRAL_ROLES
_CENTRAL_JOB_KIND = central_protocol.CENTRAL_JOB_KIND
_CENTRAL_RESULT_KIND = central_protocol.CENTRAL_RESULT_KIND
_STORAGE_POLICY = StoragePolicy()
_CENTRAL_JOB_TEMP = re.compile(r"^\.[0-9a-f]{64}\.[A-Za-z0-9_-]+$")


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
    controller_retention_root: Path | None = None

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
            return self._terminal_lab_progress(
                event,
                prepared,
                mode,
                (plan_id,),
                (plan,),
                (plan_id,),
                "failed",
            )
        commands = _commands(plan)
        plan_digest = hashlib.sha256(_canonical(plan)).hexdigest()
        report_id = self._job_id(
            "lab_report",
            event,
            self._lab_context_digest(transfer_digest, plan_digest),
        )
        report_job = self.jobs_root / report_id
        dispatch: dict[str, object] | None = None
        if not report_job.exists() and not report_job.is_symlink():
            try:
                transcript = self._dispatch_or_resume(
                    lab_executor, lab_handle, commands, plan_digest, report_id
                )
            except StorageCapacityError:
                return self._terminal_lab_progress(
                    event,
                    prepared,
                    mode,
                    (plan_id,),
                    (plan,),
                    (plan_id,),
                    "capacity_error",
                    summary="Lab dispatch storage capacity is exhausted",
                )
            if transcript is None:
                dispatch = self._read_exact_dispatch(report_id, lab_handle, plan_digest, commands)
                if dispatch is None:
                    return self._terminal_lab_progress(
                        event,
                        prepared,
                        mode,
                        (plan_id,),
                        (plan,),
                        (plan_id,),
                        "failed",
                        summary="Lab dispatch state is unsafe",
                    )
                return self._terminal_lab_progress(
                    event,
                    prepared,
                    mode,
                    (plan_id,),
                    (plan,),
                    (plan_id, report_id),
                    "dispatch_unknown",
                    dispatch=dispatch,
                    summary="Lab dispatch outcome is unknown",
                )
            context: dict[str, object] = {
                "planDigest": plan_digest,
                "labSucceeded": transcript.succeeded,
                "transcript": transcript.output,
            }
            if transfer_digest:
                context["transferDigest"] = transfer_digest
            try:
                self._ensure_job("lab_report", event, prepared, context)
            except StorageCapacityError:
                dispatch = self._read_exact_dispatch(
                    report_id, lab_handle, plan_digest, commands
                )
                if dispatch is None:
                    raise AgentSpoolError("lab dispatch state is unsafe") from None
                return self._terminal_lab_progress(
                    event,
                    prepared,
                    mode,
                    (plan_id,),
                    (plan,),
                    (plan_id, report_id),
                    "capacity_error",
                    dispatch=dispatch,
                    summary="Lab report job storage capacity is exhausted",
                )
        report = self._result(report_id, "lab_report")
        if report is None:
            return ExecutionProgress("pending")
        if dispatch is None:
            dispatch = self._read_exact_dispatch(report_id, lab_handle, plan_digest, commands)
        if dispatch is None:
            raise AgentSpoolError("lab report dispatch provenance is missing")
        succeeded = _result_succeeded(report)
        return self._terminal_lab_progress(
            event,
            prepared,
            mode,
            (plan_id, report_id),
            (plan, report),
            (plan_id, report_id),
            "succeeded" if succeeded else "failed",
            dispatch=dispatch,
            summary=_result_summary(report),
            report_markdown=_report(report),
        )

    def _terminal_lab_progress(
        self,
        event: NotificationEvent,
        prepared: PreparedAssignment,
        mode: ExecutionMode,
        job_ids: tuple[str, ...],
        results: tuple[dict[str, object], ...],
        barrier_ids: tuple[str, ...],
        terminal_status: str,
        *,
        dispatch: dict[str, object] | None = None,
        summary: str | None = None,
        report_markdown: str = "",
    ) -> ExecutionProgress:
        jobs = [self._lab_job(job_id) for job_id in job_ids]
        try:
            provenance = lab_protocol.build_provenance(
                jobs,
                list(results),
                selected_mode=mode.value,
                specification_digest=prepared.specification_digest,
                barrier_ids=barrier_ids,
                terminal_status=terminal_status,
                dispatch=dispatch,
            )
        except lab_protocol.LabProtocolError as error:
            raise AgentSpoolError("lab terminal provenance is invalid") from error
        succeeded = terminal_status == "succeeded"
        final_summary = summary if summary is not None else _result_summary(results[-1])
        return ExecutionProgress(
            "succeeded" if succeeded else "failed",
            final_summary,
            report_markdown,
            provenance,
        )

    def _lab_job(self, job_id: str) -> dict[str, object]:
        try:
            value = cast(
                dict[str, object],
                json.loads(_read_regular(self.jobs_root / job_id / "job.json", _MAX_RESULT_BYTES)),
            )
            return lab_protocol.validate_job(value, job_id)
        except (json.JSONDecodeError, TypeError, lab_protocol.LabProtocolError) as error:
            raise AgentSpoolError("lab durable job is invalid") from error

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
            return self._terminal_central_progress(
                (planner_id,), (planner,), str(planner["summary"]), ""
            )
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
            return self._terminal_central_progress(
                (planner_id,),
                (planner,),
                "Central job exceeds serialized size budget",
                "",
                terminal_role="central_executor",
                terminal_status="budget_error",
            )
        executor = self._central_result(executor_id, "central_executor")
        if executor is None:
            return ExecutionProgress("pending")
        if not bool(executor["succeeded"]):
            return self._terminal_central_progress(
                (planner_id, executor_id), (planner, executor), str(executor["summary"]), ""
            )
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
            return self._terminal_central_progress(
                (planner_id, executor_id),
                (planner, executor),
                "Central job exceeds serialized size budget",
                "",
                terminal_role="central_reviewer",
                terminal_status="budget_error",
            )
        reviewer = self._central_result(reviewer_id, "central_reviewer")
        if reviewer is None:
            return ExecutionProgress("pending")
        if not bool(reviewer["succeeded"]):
            return self._terminal_central_progress(
                (planner_id, executor_id, reviewer_id),
                (planner, executor, reviewer),
                str(reviewer["summary"]),
                str(reviewer["reportMarkdown"]),
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
            return self._terminal_central_progress(
                (planner_id, executor_id, reviewer_id),
                (planner, executor, reviewer),
                str(reviewer["summary"]),
                str(reviewer["reportMarkdown"]),
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

    def _terminal_central_progress(
        self,
        job_ids: tuple[str, ...],
        results: tuple[dict[str, object], ...],
        summary: str,
        report_markdown: str,
        *,
        terminal_role: str | None = None,
        terminal_status: str | None = None,
    ) -> ExecutionProgress:
        jobs = [self._central_job(job_id) for job_id in job_ids]
        try:
            provenance = central_protocol.terminal_provenance(
                jobs,
                list(results),
                terminal_role=terminal_role,
                terminal_status=terminal_status,
            )
        except central_protocol.CentralProtocolError as error:
            raise AgentSpoolError("central terminal provenance is invalid") from error
        return ExecutionProgress("failed", summary, report_markdown, provenance)

    def _central_job(self, job_id: str) -> dict[str, object]:
        try:
            value = cast(
                dict[str, object],
                json.loads(_read_regular(self.jobs_root / job_id / "job.json", _MAX_RESULT_BYTES)),
            )
            return central_protocol.validate_central_job(value, job_id)
        except (json.JSONDecodeError, TypeError, central_protocol.CentralProtocolError) as error:
            raise AgentSpoolError("central durable job is invalid") from error

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
        try:
            central_protocol.validate_central_job(payload, job_id)
        except central_protocol.CentralProtocolError as error:
            raise AgentSpoolError("central job envelope is invalid") from error
        self._publish_central_job(
            job_id, payload, event, prepared, admit_complete_chain=role == "central_planner"
        )
        return job_id

    def _publish_central_job(
        self,
        job_id: str,
        payload: dict[str, object],
        event: NotificationEvent,
        prepared: PreparedAssignment,
        *,
        admit_complete_chain: bool,
    ) -> None:
        encoded = _canonical(payload)
        if len(encoded) > _MAX_RESULT_BYTES:
            raise AgentSpoolError("central job exceeds serialized size budget")
        target = self.jobs_root / job_id
        self._safe_root(self.jobs_root)
        try:
            with retention_job_lock(self.controller_retention_root, job_id):
                if self.controller_retention_root is not None and controller_job_barred(
                    self.controller_retention_root, job_id
                ):
                    raise RetentionBarrierError("retention barrier refuses central job publication")
                # Keep this ordering: retention job lock then jobs storage lock.
                with storage_admission_lock(self.jobs_root):
                    _validate_jobs_storage_layout(self.jobs_root)
                    if target.exists() or target.is_symlink():
                        admit_owner_write(
                            self.jobs_root,
                            StorageDemand(0, 0),
                            _STORAGE_POLICY.jobs,
                            exclude=frozenset({".retention"}),
                        )
                        assert_no_indirection(target)
                        if (
                            not target.is_dir()
                            or _read_regular(target / "job.json", _MAX_RESULT_BYTES) != encoded
                        ):
                            raise AgentSpoolError("existing central job conflicts")
                        return
                    if admit_complete_chain:
                        admit_owner_write(
                            self.jobs_root,
                            _central_chain_storage_demand(
                                self.jobs_root, encoded, self._agent_artifacts(event, prepared)
                            ),
                            _STORAGE_POLICY.jobs,
                            exclude=frozenset({".retention"}),
                        )
                    else:
                        artifacts = self._agent_artifacts(event, prepared)
                        admit_owner_write(
                            self.jobs_root,
                            _job_storage_demand(self.jobs_root, encoded, artifacts),
                            _STORAGE_POLICY.jobs,
                            exclude=frozenset({".retention"}),
                        )
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
                        if self.controller_retention_root is not None and controller_job_barred(
                            self.controller_retention_root, job_id
                        ):
                            raise RetentionBarrierError(
                                "retention barrier refuses central job publication"
                            )
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
        except RetentionBarrierError as error:
            raise AgentSpoolError("retention barrier refuses central job publication") from error

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
        with retention_job_lock(self.controller_retention_root, execution_key):
            if self.controller_retention_root is not None and controller_job_barred(
                self.controller_retention_root, execution_key
            ):
                raise AgentSpoolError("retention barrier refuses lab dispatch")
            commands_digest = hashlib.sha256(_canonical(list(commands))).hexdigest()
            intent = self._dispatch_payload(
                execution_key, handle, plan_digest, commands_digest, None
            )
            record = self._read_dispatch_record(execution_key, intent)
            if record is None:
                created = self._publish_dispatch_record(execution_key, intent)
                if not created:
                    record = self._read_dispatch_record(execution_key, intent)
                    if record is None:
                        return None
                else:
                    root = self._dispatch_root()
                    placeholder = self._dispatch_payload(
                        execution_key,
                        handle,
                        plan_digest,
                        commands_digest,
                        "00000000-0000-0000-0000-000000000000",
                    )
                    with storage_admission_lock(self.jobs_root):
                        _validate_jobs_storage_layout(self.jobs_root)
                        _validate_dispatch_storage_layout(root)
                        admit_owner_write(
                            self.jobs_root,
                            storage_demand_for_files(
                                self.jobs_root, (len(_canonical(placeholder)),), 1
                            ),
                            _STORAGE_POLICY.jobs,
                            exclude=frozenset({".retention"}),
                        )
                        try:
                            command_id = lab_executor.dispatch_powershell(
                                handle, commands, execution_key=execution_key
                            )
                        except Exception:
                            # SendCommand may have reached AWS even when the client times out or
                            # cannot decode the response.  The durable intent is authoritative;
                            # classify the attempt as dispatch_unknown instead of retrying blind.
                            return None
                        if (
                            not isinstance(command_id, str)
                            or _COMMAND_ID.fullmatch(command_id) is None
                        ):
                            return None
                        dispatched = self._dispatch_payload(
                            execution_key, handle, plan_digest, commands_digest, command_id
                        )
                        try:
                            self._replace_dispatch_record_locked(
                                root, execution_key, intent, dispatched
                            )
                        except Exception:
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

    def _read_exact_dispatch(
        self,
        execution_key: str,
        handle: LabHandle,
        plan_digest: str,
        commands: tuple[str, ...],
    ) -> dict[str, object] | None:
        commands_digest = hashlib.sha256(_canonical(list(commands))).hexdigest()
        intent = self._dispatch_payload(
            execution_key, handle, plan_digest, commands_digest, None
        )
        record = self._read_dispatch_record(execution_key, intent)
        if record is None:
            return None
        try:
            return lab_protocol.validate_dispatch(
                record,
                expected_report_id=execution_key,
                expected_plan_digest=plan_digest,
            )
        except lab_protocol.LabProtocolError as error:
            raise AgentSpoolError("lab dispatch record is unsafe") from error

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
        try:
            self._safe_root(self.jobs_root)
            with storage_admission_lock(self.jobs_root):
                _validate_jobs_storage_layout(self.jobs_root)
                if not root.exists() and not root.is_symlink():
                    admit_owner_write(
                        self.jobs_root,
                        storage_demand_for_files(self.jobs_root, (), 1),
                        _STORAGE_POLICY.jobs,
                        exclude=frozenset({".retention"}),
                    )
                    root.mkdir(mode=0o2750)
                    os.chmod(root, 0o2750)
                    _fsync_directory(self.jobs_root)
                _validate_dispatch_storage_layout(root)
        except StorageCapacityError:
            raise
        except (OSError, StorageEnvelopeError) as error:
            raise AgentSpoolError("lab dispatch storage is unsafe") from error
        return root

    def _read_dispatch_record(
        self,
        execution_key: str,
        intent: dict[str, object],
        *,
        root: Path | None = None,
    ) -> dict[str, object] | None:
        path = (self._dispatch_root() if root is None else root) / f"{execution_key}.json"
        if not path.exists() and not path.is_symlink():
            return None
        try:
            value = json.loads(_read_regular(path, 4096))
        except (AgentSpoolError, json.JSONDecodeError) as error:
            raise AgentSpoolError("existing lab dispatch record is unsafe") from error
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise AgentSpoolError("existing lab dispatch record is unsafe")
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
            raise AgentSpoolError("existing lab dispatch record is unsafe")
        return record

    def _publish_dispatch_record(self, execution_key: str, payload: dict[str, object]) -> bool:
        root = self._dispatch_root()
        path = root / f"{execution_key}.json"
        temporary = root / f".{execution_key}.{secrets.token_hex(16)}.tmp"
        try:
            encoded = _canonical(payload)
            with storage_admission_lock(self.jobs_root):
                _validate_jobs_storage_layout(self.jobs_root)
                _validate_dispatch_storage_layout(root)
                admit_owner_write(
                    self.jobs_root,
                    storage_demand_for_files(self.jobs_root, (len(encoded),), 1),
                    _STORAGE_POLICY.jobs,
                    exclude=frozenset({".retention"}),
                )
                _write_exclusive(temporary, encoded, 0o640)
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
        encoded = _canonical(payload)
        with storage_admission_lock(self.jobs_root):
            _validate_jobs_storage_layout(self.jobs_root)
            _validate_dispatch_storage_layout(root)
            admit_owner_write(
                self.jobs_root,
                storage_demand_for_files(self.jobs_root, (len(encoded),), 1),
                _STORAGE_POLICY.jobs,
                exclude=frozenset({".retention"}),
            )
            self._replace_dispatch_record_locked(root, execution_key, previous, payload)

    def _replace_dispatch_record_locked(
        self,
        root: Path,
        execution_key: str,
        previous: dict[str, object],
        payload: dict[str, object],
    ) -> None:
        path = root / f"{execution_key}.json"
        if self._read_dispatch_record(execution_key, previous, root=root) != previous:
            raise AgentSpoolError("lab dispatch record changed unexpectedly")
        temporary = root / f".{execution_key}.{secrets.token_hex(16)}.tmp"
        try:
            _write_exclusive(temporary, _canonical(payload), 0o640)
            for attempt in range(3):
                try:
                    os.replace(temporary, path)
                    break
                except OSError as error:
                    if (
                        os.name != "nt"
                        or error.errno not in {errno.EACCES, errno.EPERM}
                        or attempt == 2
                    ):
                        raise
                    time.sleep(0.01)
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
        try:
            # Keep this order consistent with central publication and retention:
            # a per-job retention lock always precedes the shared jobs admission lock.
            with retention_job_lock(self.controller_retention_root, job_id):
                if self.controller_retention_root is not None and controller_job_barred(
                    self.controller_retention_root, job_id
                ):
                    raise AgentSpoolError("retention barrier refuses lab job publication")
                with storage_admission_lock(self.jobs_root):
                    _validate_jobs_storage_layout(self.jobs_root)
                    if target.exists() or target.is_symlink():
                        assert_no_indirection(target)
                        if not target.is_dir():
                            raise AgentSpoolError("existing agent job directory is unsafe")
                        existing = _read_regular(target / "job.json", _MAX_RESULT_BYTES)
                        if existing != encoded:
                            raise AgentSpoolError(
                                "existing agent job does not match exact revision"
                            )
                        return job_id
                    artifacts = self._agent_artifacts(event, prepared)
                    admit_owner_write(
                        self.jobs_root,
                        _job_storage_demand(self.jobs_root, encoded, artifacts),
                        _STORAGE_POLICY.jobs,
                        exclude=frozenset({".retention"}),
                    )
                    temporary = Path(tempfile.mkdtemp(prefix=f".{job_id}.", dir=self.jobs_root))
                    try:
                        os.chmod(temporary, 0o2750)
                        inputs = temporary / "inputs"
                        inputs.mkdir(mode=0o2750)
                        os.chmod(inputs, 0o2750)
                        for index, artifact in enumerate(artifacts):
                            destination = inputs / f"{index:04d}-{artifact.filename}"
                            self._download(artifact, destination)
                        _write_exclusive(temporary / "job.json", encoded, 0o640)
                        _fsync_directory(inputs)
                        _fsync_directory(temporary)
                        if self.controller_retention_root is not None and controller_job_barred(
                            self.controller_retention_root, job_id
                        ):
                            raise AgentSpoolError(
                                "retention barrier refuses lab job publication"
                            )
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
                    finally:
                        if temporary.exists():
                            shutil.rmtree(temporary, ignore_errors=True)
        except StorageCapacityError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, AgentSpoolError):
                raise
            raise AgentSpoolError("could not publish agent job") from error

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
    return central_protocol.canonical_json(value)


def _central_chain_storage_demand(
    root: Path, encoded: bytes, artifacts: tuple[PreparedArtifact, ...]
) -> StorageDemand:
    """Project planner, executor and reviewer before the first chain write."""
    # Each immutable job receives the same validated inputs.  The planner
    # result feeds the executor and reviewer, and the executor result feeds
    # the reviewer.  Reserve every result-sized expansion before publishing
    # the planner so later admitted writes cannot strand a partial chain.
    per_job_sizes = (len(encoded), *(item.size_bytes for item in artifacts))
    later_wrapper_sizes = (_MAX_CENTRAL_RESULT_BYTES,) * 3
    # job directory + inputs directory + job.json + every input, three times.
    per_job_nodes = 3 + len(artifacts)
    return storage_demand_for_files(
        root,
        (per_job_sizes * 3) + later_wrapper_sizes + (_central_dependency_envelope_bytes(),),
        3 * per_job_nodes,
    )


def _job_storage_demand(
    root: Path, encoded: bytes, artifacts: tuple[PreparedArtifact, ...]
) -> StorageDemand:
    """Project one immutable job directory and its inputs before download."""
    return storage_demand_for_files(
        root,
        (len(encoded), *(item.size_bytes for item in artifacts)),
        3 + len(artifacts),
    )


def _central_dependency_envelope_bytes() -> int:
    """Bound canonical dependency keys not represented by result expansions."""
    digest = "f" * 64
    return len(
        _canonical(
            {
                "dependencies": {
                    "plannerJobId": digest,
                    "planDigest": digest,
                    "plannerResultDigest": digest,
                    "executorJobId": digest,
                    "executorResultDigest": digest,
                    "artifactManifestDigest": digest,
                    "artifactBundleDigest": digest,
                },
                "executorResult": {},
                "plan": {},
            }
        )
    )


def _validate_jobs_storage_layout(root: Path) -> None:
    """Only known immutable jobs, retention state, and exact publish temps exist."""
    try:
        for entry in root.iterdir():
            if entry.name in {".retention", "dispatches"}:
                continue
            metadata = entry.lstat()
            if (
                entry.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or (
                    _DIGEST.fullmatch(entry.name) is None
                    and _CENTRAL_JOB_TEMP.fullmatch(entry.name) is None
                )
            ):
                raise StorageEnvelopeError("jobs storage layout is unsafe")
    except OSError as error:
        raise StorageEnvelopeError("jobs storage layout is unsafe") from error


def _validate_dispatch_storage_layout(root: Path) -> None:
    """Accept only canonical dispatch records and bounded publication temporaries."""
    try:
        metadata = root.lstat()
        if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise StorageEnvelopeError("dispatch storage layout is unsafe")
        for entry in root.iterdir():
            item = entry.lstat()
            final = re.fullmatch(r"[0-9a-f]{64}\.json", entry.name)
            temporary = re.fullmatch(r"\.[0-9a-f]{64}\.[0-9a-f]{32}\.tmp", entry.name)
            if (
                entry.is_symlink()
                or not stat.S_ISREG(item.st_mode)
                or item.st_nlink != 1
                or (final is None and temporary is None)
            ):
                raise StorageEnvelopeError("dispatch storage layout is unsafe")
    except OSError as error:
        raise StorageEnvelopeError("dispatch storage layout is unsafe") from error


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


def _validate_artifact_manifest(value: object) -> dict[str, object]:
    try:
        return central_protocol.validate_artifact_manifest(value)
    except central_protocol.CentralProtocolError as error:
        raise AgentSpoolError(str(error)) from None


def _central_plan(value: object) -> dict[str, object]:
    try:
        return central_protocol.central_plan(value)
    except central_protocol.CentralProtocolError as error:
        raise AgentSpoolError(str(error)) from None


def _validate_central_result(result: dict[str, object], role: str) -> None:
    try:
        central_protocol.validate_central_result(result, role)
    except central_protocol.CentralProtocolError as error:
        raise AgentSpoolError(str(error)) from None


def _central_digest(value: object) -> str:
    return central_protocol.canonical_digest(value)
