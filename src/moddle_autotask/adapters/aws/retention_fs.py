"""Crash-safe, owner-split filesystem retention protocol.

This module is deliberately a callable engine.  Service scheduling and host layout
installation are separate concerns.  Every pathname below is derived from a fixed
root plus a validated digest; it never performs an age or orphan sweep.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from . import central_protocol, lab_protocol
from .retention import (
    AgentRetentionAck,
    CommittedTombstone,
    PreparedTombstone,
    RetentionError,
    decode_ack,
    decode_committed,
    decode_prepared,
)
from .storage_quota import (
    StorageCapacityError,
    StorageDemand,
    StorageEnvelopeError,
    StorageLimit,
    admit_owner_write,
    measure_tree_no_follow,
    storage_admission_lock,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_JSON = re.compile(r"^[0-9a-f]{64}\.json$")
_MAX_METADATA = 16_384
_MAX_CENTRAL_JSON = 2_000_000
_MAX_SCAN_ENTRIES = 10_000
_MIB = 1024 * 1024


def _barrier_ids(prepared: PreparedTombstone) -> tuple[str, ...]:
    return prepared.barrier_ids if prepared.execution_family == "lab" else prepared.job_ids


def _current_uid() -> int:
    return getattr(os, "getuid", lambda: 0)()


def _current_gid() -> int:
    return getattr(os, "getgid", lambda: 0)()


class RetentionFilesystemError(RuntimeError):
    """The durable retention protocol encountered unsafe or conflicting state."""


class RetentionBarrierError(RetentionFilesystemError):
    """A durable tombstone barrier prevents new work from being published or run."""


class RetentionCapacityError(RetentionFilesystemError):
    """A transient retention-metadata capacity admission refusal."""


@dataclass(frozen=True, slots=True)
class RetentionStoragePolicy:
    """Fixed soft/hard envelopes for durable Phase B metadata only."""

    controller_private_hard: StorageLimit = StorageLimit(256 * _MIB, 131_072)
    controller_private_soft: StorageLimit = StorageLimit(128 * _MIB, 65_536)
    shared_hard: StorageLimit = StorageLimit(512 * _MIB, 262_144)
    shared_soft: StorageLimit = StorageLimit(256 * _MIB, 131_072)
    agent_private_hard: StorageLimit = StorageLimit(512 * _MIB, 262_144)
    agent_private_soft: StorageLimit = StorageLimit(256 * _MIB, 131_072)
    acknowledgements_hard: StorageLimit = StorageLimit(256 * _MIB, 131_072)
    acknowledgements_soft: StorageLimit = StorageLimit(128 * _MIB, 65_536)


@dataclass(frozen=True, slots=True)
class RetentionRoots:
    """Explicit roots; callers, not this engine, choose production layout."""

    controller_private: Path
    shared_jobs: Path
    agent_private: Path
    agent_results: Path
    agent_workspaces: Path
    agent_bundles: Path


@dataclass(frozen=True, slots=True)
class RetentionOwnership:
    """Expected owners and modes, injectable for root/two-UID integration tests."""

    controller_uid: int = field(default_factory=_current_uid)
    agent_uid: int = field(default_factory=_current_uid)
    controller_gid: int = field(default_factory=_current_gid)
    agent_gid: int = field(default_factory=_current_gid)
    controller_anchor_directory_mode: int = 0o750
    private_directory_mode: int = 0o700
    shared_directory_mode: int = 0o2750
    private_file_mode: int = 0o600
    shared_file_mode: int = 0o640
    private_lock_mode: int = 0o600
    shared_lock_mode: int = 0o640
    target_directory_mode: int | None = None
    target_file_mode: int | None = None
    bundle_validator: Callable[[Path, str], bool] | None = None


@dataclass(frozen=True, slots=True)
class _ProtocolNode:
    uid: int
    gid: int
    mode: int
    directory: bool


class RetentionFilesystem:
    """Controller preparation/ack and agent consumption in one offline engine."""

    def __init__(
        self,
        roots: RetentionRoots,
        ownership: RetentionOwnership | None = None,
        storage_policy: RetentionStoragePolicy | None = None,
    ) -> None:
        self.roots = roots
        self.ownership = ownership or RetentionOwnership()
        self.storage_policy = storage_policy or RetentionStoragePolicy()

    def _node(
        self, uid: int, *, shared: bool, directory: bool, lock: bool = False
    ) -> _ProtocolNode:
        if lock:
            mode = self.ownership.shared_lock_mode if shared else self.ownership.private_lock_mode
        elif directory:
            mode = (
                self.ownership.shared_directory_mode
                if shared
                else self.ownership.private_directory_mode
            )
        else:
            mode = self.ownership.shared_file_mode if shared else self.ownership.private_file_mode
        if shared:
            gid = (
                self.ownership.agent_gid
                if uid == self.ownership.controller_uid
                else self.ownership.controller_gid
            )
        else:
            gid = (
                self.ownership.controller_gid
                if uid == self.ownership.controller_uid
                else self.ownership.agent_gid
            )
        return _ProtocolNode(uid, gid, mode, directory)

    @property
    def _controller_private_dir(self) -> _ProtocolNode:
        return self._node(self.ownership.controller_uid, shared=False, directory=True)

    @property
    def _controller_anchor_dir(self) -> _ProtocolNode:
        return _ProtocolNode(
            self.ownership.controller_uid,
            self.ownership.controller_gid,
            self.ownership.controller_anchor_directory_mode,
            True,
        )

    @property
    def _controller_private_file(self) -> _ProtocolNode:
        return self._node(self.ownership.controller_uid, shared=False, directory=False)

    @property
    def _shared_controller_dir(self) -> _ProtocolNode:
        return self._node(self.ownership.controller_uid, shared=True, directory=True)

    @property
    def _shared_controller_file(self) -> _ProtocolNode:
        return self._node(self.ownership.controller_uid, shared=True, directory=False)

    @property
    def _agent_private_dir(self) -> _ProtocolNode:
        return self._node(self.ownership.agent_uid, shared=False, directory=True)

    @property
    def _agent_private_file(self) -> _ProtocolNode:
        return self._node(self.ownership.agent_uid, shared=False, directory=False)

    @property
    def _shared_agent_dir(self) -> _ProtocolNode:
        return self._node(self.ownership.agent_uid, shared=True, directory=True)

    @property
    def _shared_agent_file(self) -> _ProtocolNode:
        return self._node(self.ownership.agent_uid, shared=True, directory=False)

    @property
    def _prepared(self) -> Path:
        return self.roots.controller_private / "retention" / "prepared"

    @property
    def _completed(self) -> Path:
        return self.roots.controller_private / "retention" / "completed"

    @property
    def _deleting(self) -> Path:
        return self.roots.controller_private / "retention" / "deleting"

    @property
    def _shared(self) -> Path:
        return self.roots.shared_jobs / ".retention"

    @property
    def _committed(self) -> Path:
        return self._shared / "committed"

    @property
    def _controller_barriers(self) -> Path:
        return self._shared / "barriers"

    @property
    def _controller_locks(self) -> Path:
        return self._shared / "locks"

    @property
    def _agent(self) -> Path:
        return self.roots.agent_private / "retention"

    @property
    def _intents(self) -> Path:
        return self._agent / "intents"

    @property
    def _agent_barriers(self) -> Path:
        return self._agent / "barriers"

    @property
    def _agent_locks(self) -> Path:
        return self._agent / "locks"

    @property
    def _trash(self) -> Path:
        return self._agent / "trash"

    @property
    def _acks(self) -> Path:
        return self.roots.agent_results / ".retention" / "acks"

    def _quota_spec(self, owner: str) -> tuple[Path, _ProtocolNode, StorageLimit, StorageLimit]:
        policy = self.storage_policy
        if owner == "controller":
            return (
                self.roots.controller_private / "retention",
                self._controller_private_dir,
                policy.controller_private_hard,
                policy.controller_private_soft,
            )
        if owner == "shared":
            return self._shared, self._shared_controller_dir, policy.shared_hard, policy.shared_soft
        if owner == "agent":
            return (
                self._agent,
                self._agent_private_dir,
                policy.agent_private_hard,
                policy.agent_private_soft,
            )
        if owner == "acks":
            return (
                self.roots.agent_results / ".retention",
                self._shared_agent_dir,
                policy.acknowledgements_hard,
                policy.acknowledgements_soft,
            )
        raise ValueError("retention quota owner is invalid")

    @contextlib.contextmanager
    def _metadata_capacity(
        self, owner: str, publications: tuple[tuple[Path, bytes, _ProtocolNode], ...]
    ) -> Iterator[None]:
        """Hold one metadata-root lock and admit an all-or-nothing publication group."""
        root, node, hard, _soft = self._quota_spec(owner)
        try:
            with storage_admission_lock(root):
                # Recover an exact linked stage before measuring it.  A linked
                # stage has already allocated its inode and is not new capacity.
                for path, raw, file_node in publications:
                    _recover_exact_publication(path, raw, file_node)
                self._validate_quota_ledger(owner)
                demand = StorageDemand(0, 0)
                for path, raw, file_node in publications:
                    demand += _publication_peak_demand(root, path, raw, file_node)
                admit_owner_write(
                    root,
                    demand,
                    hard,
                    expected_uid=node.uid,
                    expected_gid=node.gid,
                    root_headroom=False,
                )
                yield
        except StorageCapacityError as error:
            raise RetentionCapacityError("retention metadata capacity is closed") from error
        except StorageEnvelopeError as error:
            raise RetentionFilesystemError("retention metadata ledger is unsafe") from error

    def _admit_new_chain(self, owners: tuple[str, ...] = ("controller", "shared")) -> None:
        """Refuse a fresh chain if any owner metadata ledger has reached soft closure."""
        for owner in owners:
            root, node, _hard, soft = self._quota_spec(owner)
            if owner == "agent" and not _exists_no_follow(root):
                _validate_protocol_node(self.roots.agent_private, self._agent_private_dir)
                continue
            try:
                with storage_admission_lock(root):
                    self._validate_quota_ledger(owner)
                    current = measure_tree_no_follow(
                        root, expected_uid=node.uid, expected_gid=node.gid
                    )
            except StorageEnvelopeError as error:
                raise RetentionFilesystemError("retention metadata ledger is unsafe") from error
            if (
                current.allocated_bytes >= soft.max_allocated_bytes
                or current.nodes >= soft.max_nodes
            ):
                raise RetentionCapacityError("retention metadata admission is closed")

    def _validate_quota_ledger(self, owner: str) -> None:
        """Reject names/layouts the generic no-follow meter cannot classify."""
        if owner == "controller":
            _validate_exact_directory_names(
                self.roots.controller_private / "retention",
                {
                    "prepared": self._controller_private_dir,
                    "completed": self._controller_private_dir,
                    "deleting": self._controller_private_dir,
                },
            )
            _validate_metadata_ledger(
                self._prepared, self._controller_private_file, decode_prepared
            )
            _validate_metadata_ledger(self._completed, self._controller_private_file, decode_ack)
            _validate_deleting_ledger(self._deleting, self._controller_private_file)
            return
        if owner == "shared":
            _validate_exact_directory_names(
                self._shared,
                {
                    "committed": self._shared_controller_dir,
                    "barriers": self._shared_controller_dir,
                    "locks": self._shared_controller_dir,
                },
            )
            _validate_metadata_ledger(
                self._committed, self._shared_controller_file, decode_committed
            )
            _validate_metadata_ledger(
                self._controller_barriers,
                self._shared_controller_file,
                decode_committed,
                _committed_job_ids,
            )
            _validate_lock_ledger(
                self._controller_locks,
                self._node(self.ownership.controller_uid, shared=True, directory=False, lock=True),
            )
            return
        if owner == "agent":
            _validate_exact_directory_names(
                self._agent,
                {
                    "intents": self._agent_private_dir,
                    "barriers": self._agent_private_dir,
                    "locks": self._agent_private_dir,
                    "trash": self._agent_private_dir,
                },
            )
            _validate_metadata_ledger(self._intents, self._agent_private_file, decode_committed)
            _validate_metadata_ledger(
                self._agent_barriers,
                self._agent_private_file,
                decode_committed,
                _committed_job_ids,
            )
            _validate_lock_ledger(
                self._agent_locks,
                self._node(self.ownership.agent_uid, shared=False, directory=False, lock=True),
            )
            _validate_trash_ledger(self._trash, self.ownership)
            return
        if owner == "acks":
            _validate_exact_directory_names(
                self.roots.agent_results / ".retention", {"acks": self._shared_agent_dir}
            )
            _validate_metadata_ledger(self._acks, self._shared_agent_file, decode_ack)
            return
        raise ValueError("retention quota owner is invalid")

    def prepare(self, prepared: PreparedTombstone) -> None:
        """Durably create one immutable controller-private preparation record."""
        raw = prepared.as_json()
        if decode_prepared(raw) != prepared:
            raise RetentionFilesystemError("prepared tombstone is invalid")
        _validate_dirs(
            self.roots.shared_jobs,
            self._shared_controller_dir,
            (self._committed, self._shared_controller_dir),
            (self._controller_barriers, self._shared_controller_dir),
            (self._controller_locks, self._shared_controller_dir),
        )
        _ensure_dirs(
            self.roots.controller_private,
            self._controller_anchor_dir,
            (self._prepared, self._controller_private_dir),
            (self._completed, self._controller_private_dir),
            (self._deleting, self._controller_private_dir),
        )
        receipt = self._completed / f"{prepared.tombstone_id}.json"
        if _exists_no_follow(receipt):
            _read_ack(receipt, prepared.tombstone_id, self._controller_private_file)
            return
        prepared_path = self._prepared / f"{prepared.tombstone_id}.json"
        stage = prepared_path.with_name(
            f".retention-stage-{prepared_path.name}-{hashlib.sha256(raw).hexdigest()}"
        )
        # This canonical controller-private directory inode serializes fresh
        # chain admission across the controller before any prepared record is
        # durable.  It adds no path/node and precedes every metadata-root lock.
        with storage_admission_lock(self._prepared):
            if not _exists_no_follow(prepared_path) and not _exists_no_follow(stage):
                self._admit_new_chain()
            with self._metadata_capacity(
                "controller", ((prepared_path, raw, self._controller_private_file),)
            ):
                _publish_immutable(prepared_path, raw, self._controller_private_file)

    def commit(self, prepared: PreparedTombstone, *, committed_at: int) -> CommittedTombstone:
        """Publish barriers before the immutable shared committed tombstone."""
        self.prepare(prepared)
        committed = CommittedTombstone(prepared, committed_at)
        raw = committed.as_json()
        if decode_committed(raw) != committed:
            raise RetentionFilesystemError("committed tombstone is invalid")
        receipt = self._completed / f"{prepared.tombstone_id}.json"
        if _exists_no_follow(receipt):
            ack = _read_ack(receipt, prepared.tombstone_id, self._controller_private_file)
            if ack.committed_at != committed_at:
                raise RetentionFilesystemError("completed retention receipt conflicts")
            return committed
        lock_context = (
            _job_locks(
                self._controller_locks,
                _barrier_ids(prepared),
                self._shared_controller_dir,
                self._node(self.ownership.controller_uid, shared=True, directory=False, lock=True),
            )
            if prepared.target_phase == "scratch"
            else contextlib.nullcontext()
        )
        with lock_context:
            publications = tuple(
                (self._controller_barriers / f"{job_id}.json", raw, self._shared_controller_file)
                for job_id in _barrier_ids(prepared)
            ) + (
                (
                    self._committed / f"{prepared.tombstone_id}.json",
                    raw,
                    self._shared_controller_file,
                ),
            )
            with self._metadata_capacity("shared", publications):
                for path, publication_raw, node in publications:
                    _publish_immutable(path, publication_raw, node)
        return committed

    def agent_consume(
        self, tombstone_id: str, *, acknowledged_at: int, now: int
    ) -> AgentRetentionAck:
        """Delete only agent-owned exact targets, then publish a canonical ack."""
        _require_digest(tombstone_id)
        if type(now) is not int or type(acknowledged_at) is not int or now < 0:
            raise RetentionFilesystemError("retention acknowledgement time is invalid")
        self._validate_shared_controller_state()
        self._validate_shared_agent_state()
        committed = _read_committed(
            self._committed / f"{tombstone_id}.json", tombstone_id, self._shared_controller_file
        )
        prepared = committed.prepared
        if prepared.target_phase not in {"scratch", "evidence"}:
            raise RetentionFilesystemError("retention target phase is invalid")
        if (
            now < committed.committed_at
            or now < prepared.eligible_at
            or acknowledged_at < committed.committed_at
        ):
            raise RetentionFilesystemError("retention targets are not yet eligible")
        _ensure_dirs(
            self.roots.agent_private,
            self._agent_private_dir,
            (self._intents, self._agent_private_dir),
            (self._agent_barriers, self._agent_private_dir),
            (self._agent_locks, self._agent_private_dir),
            (self._trash, self._agent_private_dir),
        )
        lock_context = (
            _job_locks(
                self._agent_locks,
                _barrier_ids(prepared),
                self._agent_private_dir,
                self._node(self.ownership.agent_uid, shared=False, directory=False, lock=True),
            )
            if prepared.target_phase == "scratch"
            else contextlib.nullcontext()
        )
        with lock_context:
            with _publication_locks(
                self.roots.agent_results, self.roots.agent_bundles, self.ownership
            ):
                for job_id in _barrier_ids(prepared):
                    _require_barrier(
                        self._controller_barriers / f"{job_id}.json",
                        committed,
                        self._shared_controller_file,
                    )
                intent = self._intents / f"{tombstone_id}.json"
                retry = _exists_no_follow(intent)
                if retry and _read_regular(intent, self._agent_private_file) != committed.as_json():
                    raise RetentionFilesystemError("retention metadata conflicts")
                if not retry:
                    # The controller cannot traverse the agent's 0700 private
                    # root.  The agent therefore performs its owner-local half
                    # of fresh-chain admission before any private publication.
                    self._admit_new_chain(("agent", "acks"))
                if prepared.target_phase == "scratch":
                    if prepared.execution_family == "lab":
                        self._preflight_lab_scratch(committed, allow_missing=retry)
                    else:
                        self._preflight_scratch(committed, allow_missing=retry)
                else:
                    self._preflight_evidence(prepared, allow_missing=retry)
                publications = tuple(
                    (
                        self._agent_barriers / f"{job_id}.json",
                        committed.as_json(),
                        self._agent_private_file,
                    )
                    for job_id in _barrier_ids(prepared)
                ) + ((intent, committed.as_json(), self._agent_private_file),)
                with self._metadata_capacity("agent", publications):
                    for path, publication_raw, node in publications:
                        _publish_immutable(path, publication_raw, node)
            # These are the same lock pathnames used by result/bundle publishers;
            # job locks are intentionally acquired first.
            with _publication_locks(
                self.roots.agent_results, self.roots.agent_bundles, self.ownership
            ):
                if prepared.target_phase == "evidence":
                    if not isinstance(prepared.bundle_digest, str):
                        raise RetentionFilesystemError("retention evidence target is invalid")
                    bundle = self.roots.agent_bundles / f"{prepared.bundle_digest}.zip"
                    if not _exists_no_follow(bundle):
                        if not retry:
                            raise RetentionFilesystemError("retention evidence bundle is missing")
                    else:
                        self._validate_bundle(bundle, prepared.bundle_digest)
                if prepared.target_phase == "scratch":
                    _validate_protocol_node(self.roots.agent_workspaces, self._agent_private_dir)
                for workspace in (
                    self.roots.agent_workspaces / job_id for job_id in prepared.job_ids
                ):
                    self._remove_workspace(workspace)
                for result in (
                    self.roots.agent_results / f"{job_id}.json" for job_id in prepared.job_ids
                ):
                    _unlink_regular(result)
                if prepared.target_phase == "evidence":
                    _unlink_regular(self.roots.agent_bundles / f"{prepared.bundle_digest}.zip")
                _sync_parents(
                    self.roots.agent_results,
                    self.roots.agent_bundles,
                    *((self.roots.agent_workspaces,) if prepared.target_phase == "scratch" else ()),
                )
                ack = AgentRetentionAck(tombstone_id, committed.committed_at, acknowledged_at)
                ack_publication = (
                    self._acks / f"{tombstone_id}.json",
                    ack.as_json(),
                    self._shared_agent_file,
                )
                with self._metadata_capacity("acks", (ack_publication,)):
                    _publish_immutable(*ack_publication)
                return ack

    def _preflight_scratch(self, committed: CommittedTombstone, *, allow_missing: bool) -> None:
        """Verify immutable controller provenance and every surviving agent target."""
        prepared = committed.prepared
        _validate_protocol_node(self.roots.agent_workspaces, self._agent_private_dir)
        jobs: list[dict[str, object]] = []
        results: list[dict[str, object] | None] = []
        expected_trash = {f"{job_id}.trash" for job_id in prepared.job_ids}
        _validate_protocol_node(self._trash, self._agent_private_dir)
        try:
            trash_entries = list(self._trash.iterdir())
        except OSError as error:
            raise RetentionFilesystemError("retention trash is unsafe") from error
        if {entry.name for entry in trash_entries} - expected_trash:
            raise RetentionFilesystemError("retention trash is unsafe")
        for job_id in prepared.job_ids:
            job_directory = self.roots.shared_jobs / job_id
            jobs.append(_read_central_job_tree(job_directory, self.ownership))
            result_path = self.roots.agent_results / f"{job_id}.json"
            if not _exists_no_follow(result_path):
                if not allow_missing:
                    raise RetentionFilesystemError("central retention result is missing")
                results.append(None)
            else:
                results.append(
                    _read_central_json(
                        result_path,
                        self.ownership.agent_uid,
                        gid=self.ownership.controller_gid,
                        mode=self.ownership.shared_file_mode,
                    )
                )
        try:
            present_results = [result for result in results if result is not None]
            digest_fields = {
                "central_planner": "plannerResultDigest",
                "central_executor": "executorResultDigest",
                "central_reviewer": "reviewerResultDigest",
            }
            for index, result in enumerate(results):
                if result is not None:
                    digest_field = digest_fields.get(cast(str, result.get("role")))
                    if (
                        digest_field is None
                        or result.get(digest_field) != prepared.result_digests[index]
                    ):
                        raise RetentionFilesystemError(
                            "central scratch result provenance digest is invalid"
                        )
            if len(present_results) == len(results):
                budget_prefix = len(jobs) in {1, 2} and all(
                    bool(result.get("succeeded")) for result in present_results
                )
                terminal = (
                    len(jobs) != 3
                    or not all(bool(result.get("succeeded")) for result in present_results)
                    or present_results[-1].get("accepted") is False
                )
                if budget_prefix:
                    central_protocol.terminal_provenance(
                        jobs,
                        present_results,
                        terminal_role=central_protocol.CENTRAL_ROLES[len(jobs)],
                        terminal_status="budget_error",
                    )
                    chain = tuple(jobs)
                elif terminal:
                    chain, _ = central_protocol.validate_central_terminal_chain(
                        jobs,
                        present_results,
                        expected_job_ids=prepared.job_ids,
                        expected_event_id=prepared.event_id,
                        expected_task_key=prepared.task_key,
                        expected_revision_digest=prepared.revision_digest,
                    )
                else:
                    chain = central_protocol.validate_central_job_chain(
                        jobs,
                        expected_job_ids=cast(tuple[str, str, str], prepared.job_ids),
                        expected_event_id=prepared.event_id,
                        expected_task_key=prepared.task_key,
                        expected_revision_digest=prepared.revision_digest,
                    )
                    central_protocol.validate_central_result_chain(
                        jobs,
                        present_results,
                        expected_job_ids=cast(tuple[str, str, str], prepared.job_ids),
                        expected_event_id=prepared.event_id,
                        expected_task_key=prepared.task_key,
                        expected_revision_digest=prepared.revision_digest,
                    )
            else:
                chain = central_protocol.validate_central_job_prefix(
                    jobs,
                    expected_job_ids=prepared.job_ids,
                    expected_event_id=prepared.event_id,
                    expected_task_key=prepared.task_key,
                    expected_revision_digest=prepared.revision_digest,
                )
                for job, result in zip(chain, results, strict=True):
                    if result is not None:
                        central_protocol.validate_central_result_context(job, result)
            for job, result in zip(chain, results, strict=True):
                if (
                    job["role"] == "central_executor"
                    and result is not None
                    and result.get("succeeded") is True
                ):
                    digest = result.get("artifactBundleDigest")
                    if not isinstance(digest, str):
                        raise RetentionFilesystemError("central scratch provenance is invalid")
                    self._validate_bundle(self.roots.agent_bundles / f"{digest}.zip", digest)
                workspace = self.roots.agent_workspaces / cast(str, job["jobId"])
                trash = self._trash / f"{workspace.name}.trash"
                if _exists_no_follow(workspace) and _exists_no_follow(trash):
                    raise RetentionFilesystemError("retention trash conflicts with workspace")
                if result is None and _exists_no_follow(workspace):
                    raise RetentionFilesystemError("central retention result is missing")
                if not _exists_no_follow(workspace):
                    if _exists_no_follow(trash):
                        if not allow_missing:
                            raise RetentionFilesystemError("central retention workspace is missing")
                        _validate_recoverable_trash(trash, job, result, self.ownership)
                    if not allow_missing:
                        raise RetentionFilesystemError("central retention workspace is missing")
                    continue
                _validate_central_workspace(workspace, job, result, self.ownership)
        except central_protocol.CentralProtocolError as error:
            raise RetentionFilesystemError("central scratch provenance is invalid") from error

    def _preflight_evidence(self, prepared: PreparedTombstone, *, allow_missing: bool) -> None:
        """Require the exact retained bundle before an evidence intent is durable."""
        digest = prepared.bundle_digest
        if not isinstance(digest, str):
            raise RetentionFilesystemError("retention evidence target is invalid")
        bundle = self.roots.agent_bundles / f"{digest}.zip"
        if not _exists_no_follow(bundle):
            if not allow_missing:
                raise RetentionFilesystemError("retention evidence bundle is missing")
            return
        self._validate_bundle(bundle, digest)

    def _preflight_lab_scratch(
        self, committed: CommittedTombstone, *, allow_missing: bool
    ) -> None:
        """Validate the exact lab prefix, dispatch, results, and partial workspaces."""
        prepared = committed.prepared
        if prepared.execution_family != "lab" or not prepared.job_ids:
            raise RetentionFilesystemError("lab scratch provenance is invalid")
        _validate_protocol_node(self.roots.agent_workspaces, self._agent_private_dir)
        jobs: list[dict[str, object]] = []
        results: list[dict[str, object]] = []
        expected_trash = {f"{job_id}.trash" for job_id in prepared.job_ids}
        _validate_protocol_node(self._trash, self._agent_private_dir)
        try:
            trash_entries = list(self._trash.iterdir())
        except OSError as error:
            raise RetentionFilesystemError("retention trash is unsafe") from error
        if {entry.name for entry in trash_entries} - expected_trash:
            raise RetentionFilesystemError("retention trash is unsafe")
        for job_id in prepared.job_ids:
            job = _read_lab_job_tree(self.roots.shared_jobs / job_id, self.ownership)
            result_path = self.roots.agent_results / f"{job_id}.json"
            if not _exists_no_follow(result_path):
                if allow_missing:
                    # Missing results are accepted only after the durable intent;
                    # every surviving workspace still receives exact validation.
                    result = None
                else:
                    raise RetentionFilesystemError("lab retention result is missing")
            else:
                result = _read_central_json(
                    result_path,
                    self.ownership.agent_uid,
                    gid=self.ownership.controller_gid,
                    mode=self.ownership.shared_file_mode,
                )
            jobs.append(job)
            if result is not None:
                if lab_protocol.canonical_digest(result) != prepared.result_digests[len(jobs) - 1]:
                    raise RetentionFilesystemError("lab retention result digest is invalid")
                results.append(result)
        try:
            if len(results) == len(jobs):
                lab_protocol.validate_chain(
                    list(jobs), list(results), expected_job_ids=prepared.job_ids
                )
            else:
                for index, job in enumerate(jobs):
                    lab_protocol.validate_job(job, prepared.job_ids[index])
                    result_path = self.roots.agent_results / f"{prepared.job_ids[index]}.json"
                    if _exists_no_follow(result_path):
                        result = _read_central_json(
                            result_path,
                            self.ownership.agent_uid,
                            gid=self.ownership.controller_gid,
                            mode=self.ownership.shared_file_mode,
                        )
                        lab_protocol.validate_result(
                            result,
                            prepared.job_ids[index],
                            cast(str, job["phase"]),
                        )
            plan_result_path = self.roots.agent_results / f"{prepared.job_ids[0]}.json"
            plan_result = (
                _read_central_json(
                    plan_result_path,
                    self.ownership.agent_uid,
                    gid=self.ownership.controller_gid,
                    mode=self.ownership.shared_file_mode,
                )
                if _exists_no_follow(plan_result_path)
                else None
            )
            if prepared.dispatch_id is not None:
                if plan_result is None and not allow_missing:
                    raise RetentionFilesystemError("lab dispatch plan result is missing")
                dispatch = _read_lab_dispatch(
                    self.roots.shared_jobs
                    / "dispatches"
                    / f"{prepared.dispatch_id}.json",
                    self.ownership,
                )
                lab_protocol.validate_dispatch(
                    dispatch,
                    expected_report_id=prepared.dispatch_id,
                    expected_plan_digest=(
                        lab_protocol.canonical_digest(plan_result)
                        if plan_result is not None
                        else cast(str, dispatch.get("planDigest"))
                    ),
                )
                if lab_protocol.canonical_digest(dispatch) != prepared.dispatch_digest:
                    raise RetentionFilesystemError("lab dispatch provenance is invalid")
            for index, job in enumerate(jobs):
                workspace = self.roots.agent_workspaces / prepared.job_ids[index]
                trash = self._trash / f"{prepared.job_ids[index]}.trash"
                result_path = self.roots.agent_results / f"{prepared.job_ids[index]}.json"
                result = (
                    _read_central_json(
                        result_path,
                        self.ownership.agent_uid,
                        gid=self.ownership.controller_gid,
                        mode=self.ownership.shared_file_mode,
                    )
                    if _exists_no_follow(result_path)
                    else None
                )
                if _exists_no_follow(workspace) and _exists_no_follow(trash):
                    raise RetentionFilesystemError("retention trash conflicts with workspace")
                if _exists_no_follow(workspace):
                    _validate_lab_workspace(workspace, job, result, self.ownership)
                elif _exists_no_follow(trash):
                    if not allow_missing:
                        raise RetentionFilesystemError("lab retention workspace is missing")
                    _validate_lab_workspace(trash, job, result, self.ownership, partial=True)
                elif not allow_missing and result is not None and result.get("succeeded") is True:
                    raise RetentionFilesystemError("lab retention workspace is missing")
        except lab_protocol.LabProtocolError as error:
            raise RetentionFilesystemError("lab scratch provenance is invalid") from error

    def _validate_bundle(self, bundle: Path, digest: str) -> None:
        try:
            _validate_protocol_node(
                bundle,
                _ProtocolNode(
                    self.ownership.agent_uid,
                    self.ownership.controller_gid,
                    self.ownership.shared_file_mode,
                    False,
                ),
            )
        except RetentionFilesystemError as error:
            raise RetentionFilesystemError("bundle provenance is invalid") from error
        if not _bundle_matches(bundle, digest, self.ownership.bundle_validator):
            raise RetentionFilesystemError("bundle provenance is invalid")

    def controller_consume_ack(self, tombstone_id: str) -> AgentRetentionAck:
        """Delete only controller-owned exact jobs after an agent's valid acknowledgement."""
        _require_digest(tombstone_id)
        receipt = self._completed / f"{tombstone_id}.json"
        if _exists_no_follow(receipt):
            try:
                ack = _read_ack(receipt, tombstone_id, self._controller_private_file)
            except RetentionFilesystemError:
                # A crash after link(2) leaves the known final receipt hard-linked
                # to its deterministic stage.  Decode that exact inode only; the
                # terminal receipt directory is intentionally never enumerated.
                try:
                    ack = decode_ack(_read_scan_stage(receipt, self._controller_private_file))
                except RetentionError as error:
                    raise RetentionFilesystemError(
                        "completed retention receipt is invalid"
                    ) from error
                if ack.tombstone_id != tombstone_id:
                    raise RetentionFilesystemError(
                        "completed retention receipt is invalid"
                    ) from None
                with self._metadata_capacity(
                    "controller", ((receipt, ack.as_json(), self._controller_private_file),)
                ):
                    _publish_immutable(receipt, ack.as_json(), self._controller_private_file)
                ack = _read_ack(receipt, tombstone_id, self._controller_private_file)
            # A crash after receipt publication may leave active metadata behind.
            self._cleanup_controller_metadata(tombstone_id, ack, committed=None)
            return ack
        self._validate_shared_controller_state()
        committed = _read_committed(
            self._committed / f"{tombstone_id}.json", tombstone_id, self._shared_controller_file
        )
        ack_path = self._acks / f"{tombstone_id}.json"
        self._validate_shared_agent_state()
        ack = _read_ack(ack_path, tombstone_id, self._shared_agent_file)
        if ack.committed_at != committed.committed_at:
            raise RetentionFilesystemError("acknowledgement does not match committed tombstone")
        if committed.prepared.target_phase not in {"scratch", "evidence"}:
            raise RetentionFilesystemError("retention target phase is invalid")
        lock_context = (
            _job_locks(
                self._controller_locks,
                _barrier_ids(committed.prepared),
                self._shared_controller_dir,
                self._node(self.ownership.controller_uid, shared=True, directory=False, lock=True),
            )
            if committed.prepared.target_phase == "scratch"
            else contextlib.nullcontext()
        )
        with lock_context:
            # Re-read under lock so an acknowledged record cannot race a replaced file.
            ack = _read_ack(ack_path, tombstone_id, self._shared_agent_file)
            if ack.committed_at != committed.committed_at:
                raise RetentionFilesystemError("acknowledgement does not match committed tombstone")
            if committed.prepared.target_phase == "scratch":
                marker = self._deleting / f"{tombstone_id}.json"
                marker_exists = _exists_no_follow(marker)
                jobs = self._controller_jobs_for_deletion(committed, ack, marker, marker_exists)
                dispatch = self._controller_dispatch_for_deletion(
                    committed, marker_exists=marker_exists
                )
            else:
                marker = None
                marker_exists = True
                jobs = []
                dispatch = None
            receipt_publication = (
                self._completed / f"{tombstone_id}.json",
                ack.as_json(),
                self._controller_private_file,
            )
            publications: tuple[tuple[Path, bytes, _ProtocolNode], ...] = (receipt_publication,)
            if marker is not None and not marker_exists:
                publications = (
                    (
                        marker,
                        _deleting_marker_raw(
                            committed, ack, {"jobs": jobs, "dispatch": dispatch}
                        ),
                        self._controller_private_file,
                    ),
                    receipt_publication,
                )
            with self._metadata_capacity("controller", publications):
                if marker is not None and not marker_exists:
                    _controller_delete_fault("before-marker")
                    _publish_immutable(*publications[0])
                    _controller_delete_fault("after-marker")
                if marker is not None:
                    for index, job_id in enumerate(committed.prepared.job_ids, start=1):
                        job = self.roots.shared_jobs / job_id
                        if _exists_no_follow(job):
                            _remove_tree(job)
                            _fsync_dir(self.roots.shared_jobs)
                        _controller_delete_fault(f"delete-{index}")
                    if (
                        committed.prepared.execution_family == "lab"
                        and committed.prepared.dispatch_id
                    ):
                        dispatch_path = (
                            self.roots.shared_jobs
                            / "dispatches"
                            / f"{committed.prepared.dispatch_id}.json"
                        )
                        _unlink_regular(dispatch_path)
                        _fsync_dir(dispatch_path.parent)
                        _controller_delete_fault("delete-dispatch")
                # Receipt before cleanup makes replay safe and lets the planner suppress this ID.
                _publish_immutable(*receipt_publication)
                _controller_delete_fault("after-receipt")
            self._cleanup_controller_metadata(tombstone_id, ack, committed=committed)
        return ack

    def _controller_jobs_for_deletion(
        self,
        committed: CommittedTombstone,
        ack: AgentRetentionAck,
        marker: Path,
        marker_exists: bool,
    ) -> list[dict[str, object]]:
        """Validate every live job; only a marker authorizes an absent one on replay."""
        jobs: list[dict[str, object]] = []
        for job_id in committed.prepared.job_ids:
            path = self.roots.shared_jobs / job_id
            if not _exists_no_follow(path):
                if not marker_exists:
                    raise RetentionFilesystemError("controller retention job is missing")
                continue
            job = (
                _read_lab_job_tree(path, self.ownership)
                if committed.prepared.execution_family == "lab"
                else _read_central_job_tree(path, self.ownership)
            )
            if job.get("jobId") != job_id:
                raise RetentionFilesystemError("controller scratch provenance is invalid")
            jobs.append(job)
        try:
            if committed.prepared.execution_family == "lab":
                if marker_exists:
                    _read_deleting_marker(marker, committed, ack, self._controller_private_file)
                    for job in jobs:
                        lab_protocol.validate_job(job, cast(str, job["jobId"]))
                else:
                    if len(jobs) != len(committed.prepared.job_ids):
                        raise RetentionFilesystemError("controller retention job is missing")
                    # Result files are agent-owned and already removed after the
                    # acknowledgement.  Exact per-job validation plus the lab
                    # tombstone's immutable identity is the controller boundary.
                    for job, job_id in zip(
                        jobs, committed.prepared.job_ids, strict=True
                    ):
                        lab_protocol.validate_job(job, job_id)
                return jobs
            if marker_exists:
                _read_deleting_marker(marker, committed, ack, self._controller_private_file)
                for job in jobs:
                    central_protocol.validate_central_job(
                        job, cast(str, job["jobId"])
                    )
            else:
                if len(jobs) != len(committed.prepared.job_ids):
                    raise RetentionFilesystemError("controller retention job is missing")
                chain = central_protocol.validate_central_job_prefix(
                    jobs,
                    expected_job_ids=committed.prepared.job_ids,
                    expected_event_id=committed.prepared.event_id,
                    expected_task_key=committed.prepared.task_key,
                    expected_revision_digest=committed.prepared.revision_digest,
                )
                return list(chain)
        except central_protocol.CentralProtocolError as error:
            raise RetentionFilesystemError("controller scratch provenance is invalid") from error
        return jobs

    def _controller_dispatch_for_deletion(
        self, committed: CommittedTombstone, *, marker_exists: bool
    ) -> dict[str, object] | None:
        prepared = committed.prepared
        if prepared.execution_family != "lab" or prepared.dispatch_id is None:
            return None
        path = self.roots.shared_jobs / "dispatches" / f"{prepared.dispatch_id}.json"
        if not _exists_no_follow(path):
            if marker_exists:
                return None
            raise RetentionFilesystemError("controller retention dispatch is missing")
        record = _read_lab_dispatch(path, self.ownership)
        try:
            lab_protocol.validate_dispatch(
                record,
                expected_report_id=prepared.dispatch_id,
                expected_plan_digest=cast(str, record.get("planDigest")),
            )
        except lab_protocol.LabProtocolError as error:
            raise RetentionFilesystemError("controller retention dispatch is invalid") from error
        if lab_protocol.canonical_digest(record) != prepared.dispatch_digest:
            raise RetentionFilesystemError("controller retention dispatch is invalid")
        return record

    def _cleanup_controller_metadata(
        self,
        tombstone_id: str,
        ack: AgentRetentionAck,
        *,
        committed: CommittedTombstone | None,
    ) -> None:
        marker = self._deleting / f"{tombstone_id}.json"
        if _exists_no_follow(marker):
            _read_deleting_marker(marker, committed, ack, self._controller_private_file)
        if committed is not None and _exists_no_follow(self._committed / f"{tombstone_id}.json"):
            if _read_committed(
                self._committed / f"{tombstone_id}.json", tombstone_id, self._shared_controller_file
            ) != committed:
                raise RetentionFilesystemError("committed retention metadata conflicts")
        ack_path = self._acks / f"{tombstone_id}.json"
        if _exists_no_follow(ack_path) and _read_ack(
            ack_path, tombstone_id, self._shared_agent_file
        ) != ack:
            raise RetentionFilesystemError("acknowledgement does not match completed receipt")
        _unlink_regular(self._prepared / f"{tombstone_id}.json")
        _unlink_regular(self._committed / f"{tombstone_id}.json")
        _unlink_regular(marker)
        # The controller has no write authority to the agent-owned shared ack
        # directory (agent:controller 2750).  The completed receipt is terminal;
        # retain the exact immutable ack rather than weakening that ownership split.
        _sync_parents(self._prepared, self._committed, self._deleting)

    def is_completed(self, prepared: PreparedTombstone | str) -> bool:
        """Probe one exact canonical terminal receipt; never trust its filename alone."""
        tombstone_id = prepared if isinstance(prepared, str) else prepared.tombstone_id
        _require_digest(tombstone_id)
        receipt = self._completed / f"{tombstone_id}.json"
        if not _exists_no_follow(receipt):
            return False
        _read_ack(receipt, tombstone_id, self._controller_private_file)
        return True

    def actionable_acks(
        self, *, limit: int = 1, scan_limit: int = _MAX_SCAN_ENTRIES
    ) -> tuple[str, ...]:
        """Return non-terminal shared acknowledgements in deterministic order.

        Discovery is deliberately stricter than filename matching: every entry is
        owned, mode-checked and canonically decoded.  A completed receipt is
        filtered before the action limit so old terminal acknowledgements cannot
        starve newer work.
        """
        _validate_scan_limit(limit, scan_limit)
        self._validate_shared_agent_state()
        _ensure_dirs(
            self.roots.controller_private,
            self._controller_anchor_dir,
            (self._prepared, self._controller_private_dir),
            (self._completed, self._controller_private_dir),
            (self._deleting, self._controller_private_dir),
        )
        committed = cast(
            tuple[CommittedTombstone, ...],
            _scan_metadata_directory(
                self._committed, self._shared_controller_file, decode_committed, scan_limit
            ),
        )
        actionable: list[str] = []
        for record in committed:
            tombstone_id = record.prepared.tombstone_id
            acknowledgement = self._acks / f"{tombstone_id}.json"
            if not _exists_no_follow(acknowledgement):
                continue
            _read_ack(acknowledgement, tombstone_id, self._shared_agent_file)
            receipt = self._completed / f"{tombstone_id}.json"
            if _exists_no_follow(receipt):
                try:
                    _read_ack(receipt, tombstone_id, self._controller_private_file)
                except RetentionFilesystemError:
                    # Return a linked response-loss receipt to its known-ID
                    # recovery path without scanning terminal collections.
                    pass
                else:
                    continue
            self._recover_discovered_committed(record)
            actionable.append(tombstone_id)
            if len(actionable) == limit:
                break
        return tuple(actionable)

    def actionable_committed(
        self, *, limit: int = 1, scan_limit: int = _MAX_SCAN_ENTRIES
    ) -> tuple[str, ...]:
        """Return shared tombstones still needing agent consumption.

        Existing canonical acknowledgements are terminal for the agent and are
        excluded before the action limit.  Staging entries are accepted only if
        they are the exact deterministic publication names for their payload.
        """
        _validate_scan_limit(limit, scan_limit)
        self._validate_shared_controller_state()
        self._validate_shared_agent_state()
        committed = cast(
            tuple[CommittedTombstone, ...],
            _scan_metadata_directory(
                self._committed, self._shared_controller_file, decode_committed, scan_limit
            ),
        )
        actionable: list[str] = []
        for record in committed:
            tombstone_id = record.prepared.tombstone_id
            acknowledgement = self._acks / f"{tombstone_id}.json"
            if _exists_no_follow(acknowledgement):
                _read_ack(acknowledgement, tombstone_id, self._shared_agent_file)
                continue
            self._recover_discovered_committed(record)
            actionable.append(tombstone_id)
            if len(actionable) == limit:
                break
        return tuple(actionable)

    # Explicit role names keep call sites self-documenting while preserving the
    # concise generic APIs for tests and offline tools.
    controller_actionable_acks = actionable_acks
    agent_actionable_committed = actionable_committed

    def _recover_discovered_committed(self, committed: CommittedTombstone) -> None:
        """Finalize the selected shared publication before handing out its ID."""
        publication = (
            self._committed / f"{committed.prepared.tombstone_id}.json",
            committed.as_json(),
            self._shared_controller_file,
        )
        with self._metadata_capacity("shared", (publication,)):
            _publish_immutable(*publication)

    def _validate_shared_controller_state(self) -> None:
        _validate_dirs(
            self.roots.shared_jobs,
            self._shared_controller_dir,
            (self._shared, self._shared_controller_dir),
            (self._committed, self._shared_controller_dir),
            (self._controller_barriers, self._shared_controller_dir),
            (self._controller_locks, self._shared_controller_dir),
        )

    def _validate_shared_agent_state(self) -> None:
        _validate_dirs(
            self.roots.agent_results,
            self._shared_agent_dir,
            (self.roots.agent_results / ".retention", self._shared_agent_dir),
            (self._acks, self._shared_agent_dir),
        )

    def _remove_workspace(self, workspace: Path) -> None:
        trash = self._trash / f"{workspace.name}.trash"
        if not _exists_no_follow(workspace):
            if _exists_no_follow(trash):
                _remove_tree(trash)
                _fsync_dir(self._trash)
            return
        if _exists_no_follow(trash):
            raise RetentionFilesystemError("existing retention trash blocks workspace cleanup")
        workspace.rename(trash)
        _fsync_dir(workspace.parent)
        _fsync_dir(self._trash)
        _remove_tree(trash)
        _fsync_dir(self._trash)


def controller_job_barred(retention_root: Path, job_id: str) -> bool:
    """Read-only publication barrier probe used by the controller broker."""
    _require_digest(job_id)
    return _exists_no_follow(retention_root / "barriers" / f"{job_id}.json")


def agent_job_barred(agent_private: Path | None, job_id: str) -> bool:
    """Read-only execution barrier probe; absent optional roots preserve v1/v2 behaviour."""
    if agent_private is None:
        return False
    _require_digest(job_id)
    return _exists_no_follow(agent_private / "retention" / "barriers" / f"{job_id}.json")


@contextlib.contextmanager
def retention_job_lock(root: Path | None, job_id: str) -> Iterator[None]:
    """Kernel lock shared with protocol users; no root means legacy no-op."""
    if root is None:
        yield
        return
    _require_digest(job_id)
    uid = _current_uid()
    try:
        metadata = root.lstat()
    except OSError as error:
        raise RetentionFilesystemError("retention protocol state is unsafe") from error
    gid = metadata.st_gid
    # A controller may own a setgid shared root without being a member of its
    # shared group.  Ownership plus the fixed root mode is the authority here.
    if metadata.st_uid != uid:
        raise RetentionFilesystemError("retention protocol state is unsafe")
    if root.name == ".retention":
        root_node = _ProtocolNode(uid, gid, 0o2750, True)
        directory = root_node
        lock = _ProtocolNode(uid, gid, 0o640, False)
        lock_root = root / "locks"
    else:
        root_node = _ProtocolNode(uid, gid, 0o700, True)
        directory = root_node
        lock = _ProtocolNode(uid, gid, 0o600, False)
        lock_root = root / "retention" / "locks"
    # Explicit roots are installed/provisioned before use; a missing or
    # tampered root is not an invitation to create a look-alike tree.
    _validate_protocol_node(root, root_node)
    _ensure_dirs(root, root_node, (lock_root, directory))
    with _job_locks(lock_root, (job_id,), directory, lock):
        yield


def _require_digest(value: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise RetentionFilesystemError("retention identifier is invalid")


def _ensure_dirs(
    anchor: Path, anchor_node: _ProtocolNode, *directories: tuple[Path, _ProtocolNode]
) -> None:
    """Create and verify only declared retention roots and descendants."""
    _ensure_dir(anchor, anchor_node)
    for directory, node in directories:
        try:
            relative = directory.relative_to(anchor)
        except ValueError as error:
            raise RetentionFilesystemError("retention directory escapes its anchor") from error
        current = anchor
        for part in relative.parts:
            current = current / part
            _ensure_dir(current, node)


def _validate_dirs(
    anchor: Path, anchor_node: _ProtocolNode, *directories: tuple[Path, _ProtocolNode]
) -> None:
    _validate_protocol_node(anchor, anchor_node)
    for directory, node in directories:
        try:
            relative = directory.relative_to(anchor)
        except ValueError as error:
            raise RetentionFilesystemError("retention directory escapes its anchor") from error
        current = anchor
        for part in relative.parts:
            current = current / part
            _validate_protocol_node(current, node)


def _ensure_dir(path: Path, node: _ProtocolNode) -> None:
    if not _exists_no_follow(path):
        try:
            path.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise RetentionFilesystemError("could not create retention directory") from error
        _set_protocol_identity(path, node)
        _fsync_dir(path.parent)
    _validate_protocol_node(path, node)
    _fsync_dir(path)


def _set_protocol_identity(path: Path, node: _ProtocolNode, descriptor: int | None = None) -> None:
    if os.name == "nt":
        return
    current_uid = os.getuid()  # type: ignore[attr-defined, unused-ignore]
    current = os.fstat(descriptor) if descriptor is not None else path.lstat()
    if (
        current.st_uid == node.uid
        and current.st_gid == node.gid
        and stat.S_IMODE(current.st_mode) == node.mode
    ):
        return
    if current.st_uid == node.uid and current.st_gid == node.gid:
        try:
            if descriptor is None:
                os.chmod(path, node.mode)
            else:
                os.fchmod(descriptor, node.mode)  # type: ignore[attr-defined, unused-ignore]
        except OSError as error:
            raise RetentionFilesystemError("could not set retention identity") from error
        return
    allowed_gids = {*os.getgroups(), os.getgid()}  # type: ignore[attr-defined, unused-ignore]
    if os.geteuid() != 0 and (  # type: ignore[attr-defined, unused-ignore]
        node.uid != current_uid or node.gid not in allowed_gids
    ):
        raise RetentionFilesystemError("not authorized to set retention identity")
    try:
        if descriptor is None:
            os.chown(path, node.uid, node.gid)  # type: ignore[attr-defined, unused-ignore]
            os.chmod(path, node.mode)
        else:
            os.fchown(descriptor, node.uid, node.gid)  # type: ignore[attr-defined, unused-ignore]
            os.fchmod(descriptor, node.mode)  # type: ignore[attr-defined, unused-ignore]
    except OSError as error:
        raise RetentionFilesystemError("could not set retention identity") from error


def _validate_protocol_node(path: Path, node: _ProtocolNode) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RetentionFilesystemError("retention protocol state is unsafe") from error
    expected_type = stat.S_ISDIR if node.directory else stat.S_ISREG
    if (
        path.is_symlink()
        or not expected_type(metadata.st_mode)
        or metadata.st_uid != node.uid
        or metadata.st_gid != node.gid
        or stat.S_IMODE(metadata.st_mode) != node.mode
        or (not node.directory and metadata.st_nlink != 1)
    ):
        raise RetentionFilesystemError("retention protocol state is unsafe")
    return metadata


def _publish_immutable(path: Path, raw: bytes, node: _ProtocolNode) -> None:
    """Publish immutable metadata through one deterministic sibling staging inode."""
    if len(raw) > _MAX_METADATA:
        raise RetentionFilesystemError("retention metadata is unsafe")
    digest = hashlib.sha256(raw).hexdigest()
    stage = path.with_name(f".retention-stage-{path.name}-{digest}")
    _reject_unknown_staging(path.parent, stage)
    final_exists = _exists_no_follow(path)
    stage_exists = _exists_no_follow(stage)
    if final_exists and stage_exists:
        _recover_linked_staging(path, stage, raw, node)
        return
    if final_exists:
        if _read_regular(path, node) != raw:
            raise RetentionFilesystemError("retention metadata conflicts")
        return
    if stage_exists:
        _read_staging(stage, raw, node, links=1)
    else:
        _publication_fault("create")
        descriptor = -1
        try:
            descriptor = os.open(
                stage,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                node.mode,
            )
            written = 0
            while written < len(raw):
                count = os.write(descriptor, raw[written:])
                if count <= 0:
                    raise OSError("short retention metadata write")
                written += count
            _publication_fault("write")
            _set_protocol_identity(stage, node, descriptor)
            os.fsync(descriptor)
            _publication_fault("fsync")
        except OSError as error:
            raise RetentionFilesystemError("could not stage retention metadata") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        _read_staging(stage, raw, node, links=1)
    try:
        os.link(stage, path, follow_symlinks=False)
        _publication_fault("link")
    except FileExistsError:
        if not _exists_no_follow(path):
            raise RetentionFilesystemError("retention metadata publication raced") from None
        _recover_linked_staging(path, stage, raw, node)
        return
    except OSError as error:
        raise RetentionFilesystemError("could not publish retention metadata") from error
    _publication_fault("post-link")
    _recover_linked_staging(path, stage, raw, node)


def _publication_fault(phase: str) -> None:
    """A narrow test seam for crashes at durable metadata transition boundaries."""
    del phase


def _controller_delete_fault(phase: str) -> None:
    """A narrow test seam for controller deletion crash boundaries."""
    del phase


def _deleting_marker_raw(
    committed: CommittedTombstone, ack: AgentRetentionAck, jobs: object
) -> bytes:
    """Bind a controller deletion attempt to its exact validated chain and agent ack."""
    return central_protocol.canonical_json(
        {
            "kind": "retention-controller-deleting-v1",
            "tombstoneId": committed.prepared.tombstone_id,
            "committed": json.loads(committed.as_json()),
            "ack": json.loads(ack.as_json()),
            "jobChainDigest": central_protocol.canonical_digest(jobs),
        }
    )


def _read_deleting_marker(
    path: Path,
    committed: CommittedTombstone | None,
    ack: AgentRetentionAck,
    node: _ProtocolNode,
) -> None:
    try:
        raw = _read_regular(path, node)
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"kind", "tombstoneId", "committed", "ack", "jobChainDigest"}
            or value.get("kind") != "retention-controller-deleting-v1"
            or value.get("tombstoneId") != ack.tombstone_id
            or not isinstance(value.get("committed"), dict)
            or not isinstance(value.get("ack"), dict)
            or not isinstance(value.get("jobChainDigest"), str)
            or _DIGEST.fullmatch(cast(str, value.get("jobChainDigest"))) is None
            or central_protocol.canonical_json(value) != raw
        ):
            raise RetentionFilesystemError("controller deletion marker is invalid")
        marker_committed = decode_committed(central_protocol.canonical_json(value["committed"]))
        marker_ack = decode_ack(central_protocol.canonical_json(value["ack"]))
    except (OSError, RetentionError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RetentionFilesystemError("controller deletion marker is invalid") from error
    if (
        marker_ack != ack
        or marker_committed.prepared.tombstone_id != ack.tombstone_id
        or (committed is not None and marker_committed != committed)
    ):
        raise RetentionFilesystemError("controller deletion marker conflicts")


def _recover_linked_staging(path: Path, stage: Path, raw: bytes, node: _ProtocolNode) -> None:
    """Accept only a final name hard-linked to the deterministic exact staging inode."""
    _read_staging(stage, raw, node, links=2)
    final = _read_staging(path, raw, node, links=2)
    staged = stage.lstat()
    if (staged.st_dev, staged.st_ino) != (final.st_dev, final.st_ino):
        raise RetentionFilesystemError("retention metadata publication conflicts")
    try:
        _fsync_dir(path.parent)
        _publication_fault("dir-fsync")
        stage.unlink()
        _publication_fault("unlink")
        _fsync_dir(path.parent)
        _publication_fault("dir-fsync")
    except OSError as error:
        raise RetentionFilesystemError("could not finalize retention metadata") from error
    if _read_regular(path, node) != raw:
        raise RetentionFilesystemError("retention metadata conflicts")


def _read_staging(path: Path, raw: bytes, node: _ProtocolNode, *, links: int) -> os.stat_result:
    """Read an exact staging/final inode while allowing the expected hard-link count."""
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != links
            or before.st_uid != node.uid
            or before.st_gid != node.gid
            or stat.S_IMODE(before.st_mode) != node.mode
            or before.st_size != len(raw)
        ):
            raise RetentionFilesystemError("retention metadata staging is unsafe")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_nlink,
                before.st_size,
            ):
                raise RetentionFilesystemError("retention metadata staging changed while reading")
            data = os.read(descriptor, _MAX_METADATA + 1)
        finally:
            os.close(descriptor)
        after = path.lstat()
        if (
            data != raw
            or (after.st_dev, after.st_ino, after.st_nlink, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_nlink, before.st_size, before.st_mtime_ns)
        ):
            raise RetentionFilesystemError("retention metadata staging changed while reading")
        return before
    except OSError as error:
        raise RetentionFilesystemError("retention metadata staging is unsafe") from error


def _reject_unknown_staging(parent: Path, expected: Path) -> None:
    try:
        candidates = [
            entry for entry in parent.iterdir() if entry.name.startswith(".retention-stage-")
        ]
    except OSError as error:
        raise RetentionFilesystemError("retention metadata staging is unsafe") from error
    if any(candidate != expected for candidate in candidates):
        raise RetentionFilesystemError("retention metadata staging is unsafe")


def _recover_exact_publication(path: Path, raw: bytes, node: _ProtocolNode) -> None:
    """Finalize only the deterministic linked stage for this exact publication."""
    digest = hashlib.sha256(raw).hexdigest()
    stage = path.with_name(f".retention-stage-{path.name}-{digest}")
    if _exists_no_follow(path) and _exists_no_follow(stage):
        _recover_linked_staging(path, stage, raw, node)


def _publication_peak_demand(
    root: Path, path: Path, raw: bytes, node: _ProtocolNode
) -> StorageDemand:
    """Return the conservative physical peak for one immutable publication."""
    digest = hashlib.sha256(raw).hexdigest()
    stage = path.with_name(f".retention-stage-{path.name}-{digest}")
    if _exists_no_follow(path):
        if _read_regular(path, node) != raw:
            raise RetentionFilesystemError("retention metadata conflicts")
        return StorageDemand(0, 0)
    if _exists_no_follow(stage):
        _read_staging(stage, raw, node, links=1)
        # The stage inode already owns the bytes, but link(2) briefly adds a node.
        return StorageDemand(0, 1)
    statvfs = cast(Callable[[Path], os.statvfs_result] | None, getattr(os, "statvfs", None))
    if statvfs is None:
        raise RetentionCapacityError("retention metadata capacity is unavailable")
    try:
        filesystem = statvfs(root)
    except OSError as error:
        raise RetentionCapacityError("retention metadata capacity is unavailable") from error
    block = filesystem.f_frsize or filesystem.f_bsize
    if block <= 0:
        raise RetentionCapacityError("retention metadata capacity is unavailable")
    allocated = ((len(raw) + block - 1) // block) * block
    # Creation adds the stage and link(2) briefly adds the final name.  Counting
    # that peak prevents transient hard-cap breaches during crash-safe publish.
    return StorageDemand(allocated, 2)


def _validate_exact_directory_names(
    root: Path, expected: dict[str, _ProtocolNode]
) -> None:
    try:
        entries = {entry.name: entry for entry in root.iterdir()}
    except OSError as error:
        raise RetentionFilesystemError("retention metadata ledger is unsafe") from error
    if set(entries) != set(expected):
        raise RetentionFilesystemError("retention metadata ledger is unsafe")
    for name, node in expected.items():
        _validate_protocol_node(entries[name], node)


def _validate_metadata_ledger(
    directory: Path,
    node: _ProtocolNode,
    decoder: Callable[[bytes], object],
    identifiers: Callable[[object], tuple[str, ...]] | None = None,
) -> None:
    """Validate final and deterministic stage names before generic tree metering."""
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise RetentionFilesystemError("retention metadata ledger is unsafe") from error
    staged: dict[str, bytes] = {}
    for entry in entries:
        if _JSON.fullmatch(entry.name) is not None:
            continue
        if not entry.name.startswith(".retention-stage-"):
            _validate_protocol_node(entry, node)
            raise RetentionFilesystemError("retention metadata ledger is unsafe")
        raw = _read_scan_stage(entry, node)
        value = _decode_ledger_metadata(raw, decoder)
        accepted = identifiers(value) if identifiers is not None else (_ledger_metadata_id(value),)
        matching = [
            identifier
            for identifier in accepted
            if entry.name == f".retention-stage-{identifier}.json-{hashlib.sha256(raw).hexdigest()}"
        ]
        if len(matching) != 1 or f"{matching[0]}.json" in staged:
            raise RetentionFilesystemError("retention metadata staging is unsafe")
        staged[f"{matching[0]}.json"] = raw
    for entry in entries:
        if _JSON.fullmatch(entry.name) is None:
            _validate_protocol_node(entry, node)
            continue
        staged_raw = staged.get(entry.name)
        if staged_raw is None:
            raw = _read_regular(entry, node)
        else:
            raw = staged_raw
            _read_staging(entry, raw, node, links=2)
        value = _decode_ledger_metadata(raw, decoder)
        accepted = identifiers(value) if identifiers is not None else (_ledger_metadata_id(value),)
        if entry.name not in {f"{identifier}.json" for identifier in accepted}:
            raise RetentionFilesystemError("retention metadata identity is invalid")


def _decode_ledger_metadata(raw: bytes, decoder: Callable[[bytes], object]) -> object:
    try:
        return decoder(raw)
    except RetentionError as error:
        raise RetentionFilesystemError("retention metadata is invalid") from error


def _ledger_metadata_id(value: object) -> str:
    if isinstance(value, PreparedTombstone):
        return value.tombstone_id
    if isinstance(value, CommittedTombstone):
        return value.prepared.tombstone_id
    if isinstance(value, AgentRetentionAck):
        return value.tombstone_id
    raise RetentionFilesystemError("retention metadata is invalid")


def _committed_job_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, CommittedTombstone):
        raise RetentionFilesystemError("retention metadata is invalid")
    return value.prepared.job_ids


def _validate_deleting_ledger(directory: Path, node: _ProtocolNode) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise RetentionFilesystemError("retention metadata ledger is unsafe") from error
    for entry in entries:
        if _JSON.fullmatch(entry.name) is None:
            raise RetentionFilesystemError("retention metadata ledger is unsafe")
        raw = _read_regular(entry, node)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RetentionFilesystemError("retention metadata ledger is unsafe") from error
        identifier = entry.name[:-5]
        if (
            not isinstance(value, dict)
            or value.get("kind") != "retention-controller-deleting-v1"
            or value.get("tombstoneId") != identifier
            or _DIGEST.fullmatch(identifier) is None
            or central_protocol.canonical_json(value) != raw
        ):
            raise RetentionFilesystemError("retention metadata ledger is unsafe")


def _validate_lock_ledger(directory: Path, node: _ProtocolNode) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise RetentionFilesystemError("retention metadata ledger is unsafe") from error
    for entry in entries:
        if not entry.name.endswith(".lock") or _DIGEST.fullmatch(entry.name[:-5]) is None:
            raise RetentionFilesystemError("retention metadata ledger is unsafe")
        _validate_protocol_node(entry, node)


def _validate_trash_ledger(directory: Path, ownership: RetentionOwnership) -> None:
    _validate_protocol_node(
        directory,
        _ProtocolNode(
            ownership.agent_uid,
            ownership.agent_gid,
            ownership.private_directory_mode,
            True,
        ),
    )
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise RetentionFilesystemError("retention metadata ledger is unsafe") from error
    for entry in entries:
        if not entry.name.endswith(".trash") or _DIGEST.fullmatch(entry.name[:-6]) is None:
            raise RetentionFilesystemError("retention metadata ledger is unsafe")
        _validate_safe_trash_tree(entry, ownership.agent_uid)


def _validate_safe_trash_tree(path: Path, uid: int) -> None:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RetentionFilesystemError("retention metadata ledger is unsafe")
        for entry in path.iterdir():
            entry_metadata = entry.lstat()
            if (
                entry.is_symlink()
                or entry_metadata.st_uid != uid
                or (not stat.S_ISDIR(entry_metadata.st_mode) and entry_metadata.st_nlink != 1)
                or stat.S_IMODE(entry_metadata.st_mode) & 0o022
            ):
                raise RetentionFilesystemError("retention metadata ledger is unsafe")
            if stat.S_ISDIR(entry_metadata.st_mode):
                _validate_safe_trash_tree(entry, uid)
            elif not stat.S_ISREG(entry_metadata.st_mode):
                raise RetentionFilesystemError("retention metadata ledger is unsafe")
    except OSError as error:
        raise RetentionFilesystemError("retention metadata ledger is unsafe") from error


def _validate_scan_limit(limit: int, scan_limit: int) -> None:
    if (
        type(limit) is not int
        or type(scan_limit) is not int
        or not 1 <= limit <= _MAX_SCAN_ENTRIES
        or not limit <= scan_limit <= _MAX_SCAN_ENTRIES
    ):
        raise ValueError("retention scan limit is invalid")


def _scan_metadata_directory(
    directory: Path,
    node: _ProtocolNode,
    decoder: Callable[[bytes], AgentRetentionAck | CommittedTombstone],
    scan_limit: int,
) -> tuple[AgentRetentionAck | CommittedTombstone, ...]:
    """Validate a bounded protocol directory without trusting entry names."""
    try:
        entries: dict[str, Path] = {}
        physical = 0
        for entry in directory.iterdir():
            physical += 1
            if physical > 2 * scan_limit:
                raise RetentionFilesystemError("retention metadata scan exceeds its limit")
            if entry.name in entries:
                raise RetentionFilesystemError("retention metadata scan is unsafe")
            entries[entry.name] = entry
    except OSError as error:
        raise RetentionFilesystemError("retention metadata scan is unsafe") from error
    # A crash-safe publication can occupy its deterministic stage and final
    # name simultaneously.  Bound logical records, not those two names.
    staged: dict[str, bytes] = {}
    for name in sorted(entries):
        entry = entries[name]
        if _JSON.fullmatch(name) is not None:
            continue
        if not name.startswith(".retention-stage-"):
            raise RetentionFilesystemError("retention metadata scan is unsafe")
        raw = _read_scan_stage(entry, node)
        value = _decode_scanned_metadata(raw, decoder)
        tombstone_id = _metadata_tombstone_id(value)
        expected = f".retention-stage-{tombstone_id}.json-{hashlib.sha256(raw).hexdigest()}"
        final_name = f"{tombstone_id}.json"
        if name != expected or final_name in staged:
            raise RetentionFilesystemError("retention metadata staging is unsafe")
        staged[final_name] = raw
    values: list[AgentRetentionAck | CommittedTombstone] = []
    for name in sorted(entries):
        entry = entries[name]
        if _JSON.fullmatch(name) is None:
            continue
        staged_raw = staged.get(name)
        if staged_raw is None:
            raw = _read_regular(entry, node)
        else:
            raw = staged_raw
            _read_staging(entry, raw, node, links=2)
        value = _decode_scanned_metadata(raw, decoder)
        if name != f"{_metadata_tombstone_id(value)}.json":
            raise RetentionFilesystemError("retention metadata identity is invalid")
        values.append(value)
    if len(values) > scan_limit:
        raise RetentionFilesystemError("retention metadata scan exceeds its limit")
    return tuple(values)


def _decode_scanned_metadata(
    raw: bytes, decoder: Callable[[bytes], AgentRetentionAck | CommittedTombstone]
) -> AgentRetentionAck | CommittedTombstone:
    try:
        return decoder(raw)
    except RetentionError as error:
        raise RetentionFilesystemError("retention metadata is invalid") from error


def _metadata_tombstone_id(value: AgentRetentionAck | CommittedTombstone) -> str:
    if isinstance(value, AgentRetentionAck):
        return value.tombstone_id
    return value.prepared.tombstone_id


def _read_scan_stage(path: Path, node: _ProtocolNode) -> bytes:
    """Read an in-flight deterministic stage with no-follow identity checks."""
    descriptor: int | None = None
    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in {1, 2}
            or before.st_uid != node.uid
            or before.st_gid != node.gid
            or stat.S_IMODE(before.st_mode) != node.mode
            or before.st_size > _MAX_METADATA
        ):
            raise RetentionFilesystemError("retention metadata staging is unsafe")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size)
            != (before.st_dev, before.st_ino, before.st_nlink, before.st_size)
        ):
            raise RetentionFilesystemError("retention metadata staging changed while reading")
        raw = os.read(descriptor, _MAX_METADATA + 1)
        after = path.lstat()
        if len(raw) != before.st_size or (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise RetentionFilesystemError("retention metadata staging changed while reading")
        return raw
    except OSError as error:
        raise RetentionFilesystemError("retention metadata staging is unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular(path: Path, node: _ProtocolNode) -> bytes:
    try:
        metadata = _validate_protocol_node(path, node)
        if metadata.st_size > _MAX_METADATA:
            raise RetentionFilesystemError("retention metadata is unsafe")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != node.uid
                or opened.st_gid != node.gid
                or stat.S_IMODE(opened.st_mode) != node.mode
                or opened.st_size > _MAX_METADATA
            ):
                raise RetentionFilesystemError("retention metadata is unsafe")
            raw = os.read(descriptor, _MAX_METADATA + 1)
        finally:
            os.close(descriptor)
        after = _validate_protocol_node(path, node)
        if (
            len(raw) != metadata.st_size
            or (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
                after.st_size,
            )
            != (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_mtime_ns,
                opened.st_size,
            )
            != (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
        ):
            raise RetentionFilesystemError("retention metadata changed while reading")
        return raw
    except OSError as error:
        raise RetentionFilesystemError("retention metadata is unsafe") from error


def _read_committed(path: Path, tombstone_id: str, node: _ProtocolNode) -> CommittedTombstone:
    try:
        value = decode_committed(_read_regular(path, node))
    except RetentionError as error:
        raise RetentionFilesystemError("committed retention metadata is invalid") from error
    if value.prepared.tombstone_id != tombstone_id or path.name != f"{tombstone_id}.json":
        raise RetentionFilesystemError("committed retention metadata identity is invalid")
    return value


def _read_ack(path: Path, tombstone_id: str, node: _ProtocolNode) -> AgentRetentionAck:
    try:
        value = decode_ack(_read_regular(path, node))
    except RetentionError as error:
        raise RetentionFilesystemError("retention acknowledgement is invalid") from error
    if value.tombstone_id != tombstone_id or path.name != f"{tombstone_id}.json":
        raise RetentionFilesystemError("retention acknowledgement identity is invalid")
    return value


def _require_barrier(path: Path, committed: CommittedTombstone, node: _ProtocolNode) -> None:
    try:
        value = decode_committed(_read_regular(path, node))
    except RetentionError as error:
        raise RetentionFilesystemError("retention barrier is invalid") from error
    if value != committed:
        raise RetentionFilesystemError("retention barrier is invalid")


@contextlib.contextmanager
def _job_locks(
    directory: Path,
    job_ids: tuple[str, ...],
    directory_node: _ProtocolNode,
    lock_node: _ProtocolNode,
) -> Iterator[None]:
    _validate_protocol_node(directory, directory_node)
    descriptors: list[int] = []
    try:
        for job_id in sorted(job_ids):
            _require_digest(job_id)
            path = directory / f"{job_id}.lock"
            descriptor = _open_protocol_lock(path, lock_node)
            if os.name != "nt":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)  # type: ignore[attr-defined, unused-ignore]
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            if os.name != "nt":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[attr-defined, unused-ignore]
            os.close(descriptor)


def _open_protocol_lock(path: Path, node: _ProtocolNode) -> int:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if _exists_no_follow(path):
        before = _validate_protocol_node(path, node)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise RetentionFilesystemError("retention lock is unsafe") from error
    else:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, node.mode)
        except FileExistsError:
            return _open_protocol_lock(path, node)
        except OSError as error:
            raise RetentionFilesystemError("could not create retention lock") from error
        try:
            _set_protocol_identity(path, node, descriptor)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        before = _validate_protocol_node(path, node)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != node.uid
            or opened.st_gid != node.gid
            or stat.S_IMODE(opened.st_mode) != node.mode
        ):
            raise RetentionFilesystemError("retention lock is unsafe")
        after = _validate_protocol_node(path, node)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_mtime_ns,
            opened.st_size,
            opened.st_nlink,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_size,
            before.st_nlink,
        ) or (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_size,
            after.st_nlink,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_size,
            before.st_nlink,
        ):
            raise RetentionFilesystemError("retention lock changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextlib.contextmanager
def _publication_locks(
    results: Path, bundles: Path, ownership: RetentionOwnership
) -> Iterator[None]:
    """Lock the established publisher inodes without trusting a pathname traversal."""
    roots = (
        (
            "bundles",
            bundles,
            ".publish.lock",
            _ProtocolNode(
                ownership.agent_uid,
                ownership.controller_gid,
                ownership.shared_directory_mode,
                True,
            ),
        ),
        (
            "results",
            results,
            ".results.publish.lock",
            _ProtocolNode(
                ownership.agent_uid,
                ownership.controller_gid,
                ownership.shared_directory_mode,
                True,
            ),
        ),
    )
    directories: list[tuple[Path, _ProtocolNode, int]] = []
    descriptors: list[int] = []
    try:
        for _name, root, _filename, node in roots:
            directories.append((root, node, _open_verified_directory(root, node)))
        for name, root, filename, node in roots:
            directory = next(descriptor for path, _node, descriptor in directories if path == root)
            descriptor = _open_publication_lock(directory, filename, node, name)
            if os.name != "nt":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)  # type: ignore[attr-defined, unused-ignore]
            descriptors.append(descriptor)
        for root, node, descriptor in directories:
            _verify_directory_descriptor(root, node, descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            if os.name != "nt":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[attr-defined, unused-ignore]
            os.close(descriptor)
        for _root, _node, descriptor in reversed(directories):
            os.close(descriptor)


def _open_verified_directory(path: Path, node: _ProtocolNode) -> int:
    """Open one provisioned root and bind its descriptor to its lstat inode."""
    _validate_protocol_node(path, node)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise RetentionFilesystemError("publication root is unsafe") from error
    try:
        _verify_directory_descriptor(path, node, descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_directory_descriptor(path: Path, node: _ProtocolNode, descriptor: int) -> None:
    opened = os.fstat(descriptor)
    current = _validate_protocol_node(path, node)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise RetentionFilesystemError("publication root changed while opening")


def _open_publication_lock(
    directory: int, filename: str, root: _ProtocolNode, label: str
) -> int:
    """Open or atomically create one canonical agent publisher lock by dir_fd."""
    lock = _ProtocolNode(root.uid, root.gid, 0o640, False)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(filename, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        try:
            descriptor = os.open(
                filename, flags | os.O_CREAT | os.O_EXCL, lock.mode, dir_fd=directory
            )
        except FileExistsError:
            return _open_publication_lock(directory, filename, root, label)
        except OSError as error:
            raise RetentionFilesystemError("could not create publication lock") from error
        try:
            # O_CREAT honours umask; correct the mode but never repair owner/group.
            fchmod = cast(Callable[[int, int], None] | None, getattr(os, "fchmod", None))
            if fchmod is None:
                raise RetentionFilesystemError("publication lock mode is unavailable")
            fchmod(descriptor, lock.mode)
            before = os.fstat(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
    except OSError as error:
        raise RetentionFilesystemError("publication lock is unsafe") from error
    else:
        try:
            descriptor = os.open(filename, flags, dir_fd=directory)
        except OSError as error:
            raise RetentionFilesystemError("publication lock is unsafe") from error
    try:
        opened = os.fstat(descriptor)
        after = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != lock.uid
            or before.st_gid != lock.gid
            or stat.S_IMODE(before.st_mode) != lock.mode
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != lock.uid
            or opened.st_gid != lock.gid
            or stat.S_IMODE(opened.st_mode) != lock.mode
            or (before.st_dev, before.st_ino, before.st_nlink)
            != (opened.st_dev, opened.st_ino, opened.st_nlink)
            or (after.st_dev, after.st_ino, after.st_nlink)
            != (opened.st_dev, opened.st_ino, opened.st_nlink)
        ):
            raise RetentionFilesystemError(f"{label} publication lock is unsafe")
        return descriptor
    except OSError as error:
        raise RetentionFilesystemError("publication lock is unsafe") from error
    except BaseException:
        os.close(descriptor)
        raise


def _exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _validate_agent_target(path: Path, ownership: RetentionOwnership, *, directory: bool) -> None:
    if not _exists_no_follow(path):
        return
    _validate_node(
        path,
        ownership.agent_uid,
        ownership.target_directory_mode if directory else ownership.target_file_mode,
        directory,
    )
    if directory:
        _validate_tree(path, ownership.agent_uid, ownership)


def _read_central_job_tree(path: Path, ownership: RetentionOwnership) -> dict[str, object]:
    _validate_exact_target(
        path,
        _ProtocolNode(
            ownership.controller_uid,
            ownership.agent_gid,
            ownership.shared_directory_mode,
            True,
        ),
    )
    _require_exact_entries(path, {"job.json", "inputs"})
    job = _read_central_json(
        path / "job.json",
        ownership.controller_uid,
        gid=ownership.agent_gid,
        mode=ownership.shared_file_mode,
    )
    inputs = job.get("preparedInputs")
    try:
        prepared_inputs = central_protocol.validate_prepared_inputs(inputs)
    except central_protocol.CentralProtocolError as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    expected = {Path(cast(str, item["path"])).name: item for item in prepared_inputs}
    _validate_exact_input_directory(
        path / "inputs",
        expected,
        ownership.controller_uid,
        gid=ownership.agent_gid,
        directory_mode=ownership.shared_directory_mode,
        file_mode=ownership.shared_file_mode,
    )
    return job


def _read_lab_job_tree(path: Path, ownership: RetentionOwnership) -> dict[str, object]:
    _validate_exact_target(
        path,
        _ProtocolNode(
            ownership.controller_uid,
            ownership.agent_gid,
            ownership.shared_directory_mode,
            True,
        ),
    )
    _require_exact_entries(path, {"job.json", "inputs"})
    job = _read_central_json(
        path / "job.json",
        ownership.controller_uid,
        gid=ownership.agent_gid,
        mode=ownership.shared_file_mode,
    )
    try:
        validated = lab_protocol.validate_job(job, path.name)
    except lab_protocol.LabProtocolError as error:
        raise RetentionFilesystemError("lab scratch provenance is invalid") from error
    attachments = cast(list[dict[str, object]], validated["attachments"])
    expected = {Path(cast(str, item["path"])).name: item for item in attachments}
    _validate_exact_input_directory(
        path / "inputs",
        expected,
        ownership.controller_uid,
        gid=ownership.agent_gid,
        directory_mode=ownership.shared_directory_mode,
        file_mode=ownership.shared_file_mode,
    )
    return validated


def _read_lab_dispatch(path: Path, ownership: RetentionOwnership) -> dict[str, object]:
    _validate_exact_target(
        path.parent,
        _ProtocolNode(
            ownership.controller_uid,
            ownership.agent_gid,
            ownership.shared_directory_mode,
            True,
        ),
    )
    return _read_central_json(
        path,
        ownership.controller_uid,
        gid=ownership.agent_gid,
        mode=ownership.shared_file_mode,
    )


def _read_central_json(
    path: Path, uid: int | None, *, gid: int | None = None, mode: int | None = None
) -> dict[str, object]:
    if gid is not None or mode is not None:
        _validate_exact_target(
            path, _ProtocolNode(cast(int, uid), cast(int, gid), cast(int, mode), False)
        )
    raw = _read_verified_regular(path, uid, _MAX_CENTRAL_JSON)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RetentionFilesystemError("central scratch provenance is invalid")
    if central_protocol.canonical_json(value) != raw:
        raise RetentionFilesystemError("central scratch provenance is invalid")
    return cast(dict[str, object], value)


def _validate_central_workspace(
    workspace: Path,
    job: dict[str, object],
    result: dict[str, object] | None,
    ownership: RetentionOwnership,
) -> None:
    uid = ownership.agent_uid
    _validate_exact_target(workspace, _ProtocolNode(uid, ownership.agent_gid, 0o700, True))
    try:
        contract = central_protocol.central_workspace_contract(job)
    except central_protocol.CentralProtocolError as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    root_entries = cast(list[str], contract["rootEntries"])
    _require_exact_entries(workspace, set(root_entries))
    prepared_inputs = cast(list[dict[str, object]], contract["preparedInputs"])
    expected_inputs = {Path(cast(str, item["path"])).name: item for item in prepared_inputs}
    _validate_exact_input_directory(workspace / "inputs", expected_inputs, uid)
    schema = cast(str, contract["resultSchemaJson"]).encode("utf-8")
    if _read_verified_regular(workspace / "result-schema.json", uid, _MAX_CENTRAL_JSON) != schema:
        raise RetentionFilesystemError("central scratch provenance is invalid")
    model = _read_workspace_model(workspace / "last-message.json", uid)
    try:
        role = cast(str, job["role"])
        central_protocol.validate_central_model_result(model, role)
        central_protocol.validate_central_model_context(job, model)
        if result is not None and (
            result.get("succeeded") is True or model.get("succeeded") is False
        ):
            central_protocol.validate_central_model_result_binding(job, model, result)
    except central_protocol.CentralProtocolError as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    if role == "central_executor" and result is not None and result.get("succeeded") is True:
        if result is None:
            raise RetentionFilesystemError("central scratch provenance is invalid")
        manifest = result.get("artifactManifest")
        try:
            central_protocol.validate_artifact_manifest(manifest)
        except central_protocol.CentralProtocolError as error:
            raise RetentionFilesystemError("central scratch provenance is invalid") from error
        files = cast(list[dict[str, object]], cast(dict[str, object], manifest)["files"])
        _validate_exact_outputs(workspace / "outputs", files, uid)


def _validate_lab_workspace(
    workspace: Path,
    job: dict[str, object],
    result: dict[str, object] | None,
    ownership: RetentionOwnership,
    *,
    partial: bool = False,
) -> None:
    """Validate a complete or capacity-interrupted lab workspace without following links."""
    uid = ownership.agent_uid
    _validate_exact_target(workspace, _ProtocolNode(uid, ownership.agent_gid, 0o700, True))
    try:
        entries = list(workspace.iterdir())
    except OSError as error:
        raise RetentionFilesystemError("lab scratch provenance is invalid") from error
    allowed = {"inputs", "result-schema.json", "last-message.json"}
    names = {entry.name for entry in entries}
    folded = {unicodedata.normalize("NFC", entry.name).casefold() for entry in entries}
    if not names <= allowed or len(folded) != len(entries):
        raise RetentionFilesystemError("lab scratch provenance is invalid")
    if result is not None and result.get("succeeded") is True and names != allowed:
        raise RetentionFilesystemError("lab scratch provenance is invalid")
    attachments = cast(list[dict[str, object]], job["attachments"])
    expected = {Path(cast(str, item["path"])).name: item for item in attachments}
    inputs = workspace / "inputs"
    if _exists_no_follow(inputs):
        if partial or result is None or result.get("succeeded") is not True:
            _validate_partial_lab_inputs(inputs, expected, uid)
        else:
            _validate_exact_input_directory(inputs, expected, uid)
    elif result is not None and result.get("succeeded") is True:
        raise RetentionFilesystemError("lab scratch provenance is invalid")
    schema = workspace / "result-schema.json"
    if _exists_no_follow(schema) and _read_verified_regular(
        schema, uid, _MAX_CENTRAL_JSON
    ) != lab_protocol.model_schema_json():
        raise RetentionFilesystemError("lab scratch provenance is invalid")
    model_path = workspace / "last-message.json"
    if _exists_no_follow(model_path):
        model = _read_workspace_model(model_path, uid)
        phase = cast(str, job["phase"])
        try:
            model_result = lab_protocol.validate_result(
                {
                    "kind": lab_protocol.LAB_RESULT_KIND,
                    "jobId": job["jobId"],
                    "phase": phase,
                    **model,
                },
                cast(str, job["jobId"]),
                phase,
            )
        except lab_protocol.LabProtocolError as error:
            raise RetentionFilesystemError("lab scratch provenance is invalid") from error
        if result is not None:
            expected_result = dict(model_result)
            if phase == "lab_report":
                context = cast(dict[str, object], job["context"])
                if context.get("labSucceeded") is False and expected_result["succeeded"] is True:
                    expected_result.update(
                        {
                            "succeeded": False,
                            "summary": "Lab execution failed",
                            "reportMarkdown": "",
                        }
                    )
            if expected_result != result:
                raise RetentionFilesystemError("lab scratch provenance is invalid")


def _validate_partial_lab_inputs(
    path: Path, expected: dict[str, dict[str, object]], uid: int | None
) -> None:
    _validate_node(path, uid, None, True)
    try:
        entries = list(path.iterdir())
    except OSError as error:
        raise RetentionFilesystemError("lab scratch provenance is invalid") from error
    names = {entry.name for entry in entries}
    if not names <= set(expected) or len(names) != len(entries):
        raise RetentionFilesystemError("lab scratch provenance is invalid")
    for name in names:
        item = expected[name]
        actual_size, actual_digest = _hash_verified_regular(path / name, uid)
        if actual_size != item["sizeBytes"] or actual_digest != item["sha256"]:
            raise RetentionFilesystemError("lab scratch provenance is invalid")


def _validate_recoverable_trash(
    trash: Path,
    job: dict[str, object],
    result: dict[str, object] | None,
    ownership: RetentionOwnership,
) -> None:
    """Allow only a partially deleted, named workspace left after a durable intent."""
    uid = ownership.agent_uid
    _validate_exact_target(trash, _ProtocolNode(uid, ownership.agent_gid, 0o700, True))
    _validate_tree(trash, uid, ownership)
    try:
        contract = central_protocol.central_workspace_contract(job)
    except central_protocol.CentralProtocolError as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    expected = set(cast(list[str], contract["rootEntries"]))
    try:
        entries = list(trash.iterdir())
    except OSError as error:
        raise RetentionFilesystemError("retention trash is unsafe") from error
    names = {entry.name for entry in entries}
    folded = {unicodedata.normalize("NFC", entry.name).casefold() for entry in entries}
    if not names <= expected or len(folded) != len(entries):
        raise RetentionFilesystemError("retention trash is unsafe")
    schema_path = trash / "result-schema.json"
    if _exists_no_follow(schema_path) and _read_verified_regular(
        schema_path, uid, _MAX_CENTRAL_JSON
    ) != cast(str, contract["resultSchemaJson"]).encode("utf-8"):
        raise RetentionFilesystemError("central scratch provenance is invalid")
    model_path = trash / "last-message.json"
    if _exists_no_follow(model_path):
        model = _read_workspace_model(model_path, uid)
        try:
            role = cast(str, job["role"])
            central_protocol.validate_central_model_result(model, role)
            central_protocol.validate_central_model_context(job, model)
            if result is not None and (
                result.get("succeeded") is True or model.get("succeeded") is False
            ):
                central_protocol.validate_central_model_result_binding(job, model, result)
        except central_protocol.CentralProtocolError as error:
            raise RetentionFilesystemError("central scratch provenance is invalid") from error


def _read_workspace_model(path: Path, uid: int | None) -> dict[str, object]:
    raw = _read_verified_regular(path, uid, _MAX_CENTRAL_JSON)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RetentionFilesystemError("central scratch provenance is invalid")
    return cast(dict[str, object], value)


def _require_exact_entries(path: Path, expected: set[str]) -> None:
    _validate_node(path, None, None, True)
    try:
        entries = list(path.iterdir())
    except OSError as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    names = {entry.name for entry in entries}
    folded = {unicodedata.normalize("NFC", entry.name).casefold() for entry in entries}
    if names != expected or len(folded) != len(entries):
        raise RetentionFilesystemError("central scratch provenance is invalid")


def _validate_exact_input_directory(
    path: Path,
    expected: dict[str, dict[str, object]],
    uid: int | None,
    *,
    gid: int | None = None,
    directory_mode: int | None = None,
    file_mode: int | None = None,
) -> None:
    if gid is not None or directory_mode is not None:
        _validate_exact_target(
            path, _ProtocolNode(cast(int, uid), cast(int, gid), cast(int, directory_mode), True)
        )
    _require_exact_entries(path, set(expected))
    for name, item in expected.items():
        size = item["sizeBytes"]
        digest = item["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or not isinstance(digest, str):
            raise RetentionFilesystemError("central scratch provenance is invalid")
        file = path / name
        if gid is not None or file_mode is not None:
            _validate_exact_target(
                file, _ProtocolNode(cast(int, uid), cast(int, gid), cast(int, file_mode), False)
            )
        actual_size, actual_digest = _hash_verified_regular(file, uid)
        if actual_size != size or actual_digest != digest:
            raise RetentionFilesystemError("central scratch provenance is invalid")


def _validate_exact_outputs(path: Path, files: list[dict[str, object]], uid: int | None) -> None:
    expected = {cast(str, item["path"]): item for item in files}
    actual: set[str] = set()
    directories: set[str] = set()
    _validate_node(path, uid, None, True)
    try:
        entries = list(path.rglob("*"))
    except OSError as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError as error:
            raise RetentionFilesystemError("central scratch provenance is invalid") from error
        relative = entry.relative_to(path).as_posix()
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(relative)
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RetentionFilesystemError("central scratch provenance is invalid")
        actual.add(relative)
    if actual != set(expected):
        raise RetentionFilesystemError("central scratch provenance is invalid")
    expected_directories = {
        parent.as_posix()
        for name in expected
        for parent in Path(name).parents
        if parent.as_posix() != "."
    }
    if directories != expected_directories:
        raise RetentionFilesystemError("central scratch provenance is invalid")
    all_names = actual | directories
    folded = {unicodedata.normalize("NFC", name).casefold() for name in all_names}
    if len(folded) != len(all_names):
        raise RetentionFilesystemError("central scratch provenance is invalid")
    for name, item in expected.items():
        size, digest = _hash_verified_regular(path / name, uid)
        if size != item.get("size") or digest != item.get("sha256"):
            raise RetentionFilesystemError("central scratch provenance is invalid")


def _read_verified_regular(path: Path, uid: int | None, limit: int) -> bytes:
    size, digest, raw = _read_verified_regular_parts(path, uid, limit)
    del size, digest
    return raw


def _hash_verified_regular(path: Path, uid: int | None) -> tuple[int, str]:
    size, digest, _raw = _read_verified_regular_parts(path, uid, None)
    return size, digest


def _read_verified_regular_parts(
    path: Path, uid: int | None, limit: int | None
) -> tuple[int, str, bytes]:
    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (uid is not None and before.st_uid != uid)
            or (limit is not None and before.st_size > limit)
        ):
            raise RetentionFilesystemError("central scratch provenance is invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_nlink) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_nlink,
        ):
            raise RetentionFilesystemError("central scratch provenance is invalid")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if limit is not None and total > limit:
                    raise RetentionFilesystemError("central scratch provenance is invalid")
                digest.update(chunk)
                if limit is not None:
                    chunks.append(chunk)
        after = path.lstat()
        if (
            total != before.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink)
        ):
            raise RetentionFilesystemError("central scratch provenance is invalid")
        return total, digest.hexdigest(), b"".join(chunks)
    except OSError as error:
        raise RetentionFilesystemError("central scratch provenance is invalid") from error
    finally:
        os.close(descriptor)


def _validate_controller_job(path: Path, ownership: RetentionOwnership) -> None:
    if not _exists_no_follow(path):
        return
    _validate_node(path, ownership.controller_uid, ownership.target_directory_mode, True)
    _validate_tree(path, ownership.controller_uid, ownership)


def _validate_node(path: Path, uid: int | None, mode: int | None, directory: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RetentionFilesystemError("retention target is unsafe") from error
    required_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        path.is_symlink()
        or not required_type(metadata.st_mode)
        or (not directory and metadata.st_nlink != 1)
    ):
        raise RetentionFilesystemError("retention target is unsafe")
    if uid is not None and metadata.st_uid != uid:
        raise RetentionFilesystemError("retention target owner is unsafe")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        raise RetentionFilesystemError("retention target mode is unsafe")


def _validate_exact_target(path: Path, node: _ProtocolNode) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RetentionFilesystemError("retention target is unsafe") from error
    required_type = stat.S_ISDIR if node.directory else stat.S_ISREG
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not required_type(metadata.st_mode)
        or metadata.st_uid != node.uid
        or metadata.st_gid != node.gid
        or stat.S_IMODE(metadata.st_mode) != node.mode
        or (not node.directory and metadata.st_nlink != 1)
    ):
        raise RetentionFilesystemError("retention target is unsafe")


def _validate_tree(path: Path, uid: int | None, ownership: RetentionOwnership) -> None:
    descriptor = _open_dir(path)
    try:
        _validate_tree_fd(descriptor, uid, ownership)
    finally:
        os.close(descriptor)


def _validate_tree_fd(descriptor: int, uid: int | None, ownership: RetentionOwnership) -> None:
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            if uid is not None and metadata.st_uid != uid:
                raise RetentionFilesystemError("retention tree entry is unsafe")
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                _validate_tree_fd(child, uid, ownership)
            finally:
                os.close(child)
        elif (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (uid is not None and metadata.st_uid != uid)
        ):
            raise RetentionFilesystemError("retention tree entry is unsafe")


def _open_dir(path: Path) -> int:
    try:
        return os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as error:
        raise RetentionFilesystemError("retention directory is unsafe") from error


def _remove_tree(path: Path) -> None:
    if not _exists_no_follow(path):
        return
    parent = _open_dir(path.parent)
    try:
        _remove_tree_fd(parent, path.name)
    finally:
        os.close(parent)


def _remove_tree_fd(parent: int, name: str) -> None:
    child = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
    )
    try:
        for entry in os.listdir(child):
            metadata = os.stat(entry, dir_fd=child, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                _remove_tree_fd(child, entry)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                os.unlink(entry, dir_fd=child)
            else:
                raise RetentionFilesystemError("retention tree changed while deleting")
        os.fsync(child)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)


def _unlink_regular(path: Path) -> None:
    if not _exists_no_follow(path):
        return
    _validate_node(path, None, None, False)
    try:
        path.unlink()
        _fsync_dir(path.parent)
    except OSError as error:
        raise RetentionFilesystemError("could not remove retention target") from error


def _bundle_matches(path: Path, digest: str, validator: Callable[[Path, str], bool] | None) -> bool:
    """Require the collector's content-addressed name and optional manifest verifier."""
    calculated = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                calculated.update(chunk)
    except OSError as error:
        raise RetentionFilesystemError("bundle provenance is invalid") from error
    return calculated.hexdigest() == digest and (validator is None or validator(path, digest))


def _sync_parents(*parents: Path) -> None:
    for parent in parents:
        if parent.exists():
            _fsync_dir(parent)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = _open_dir(path)
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise RetentionFilesystemError("could not sync retention directory") from error
    finally:
        os.close(descriptor)
