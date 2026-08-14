"""Durable human decisions for exact Moodle notification revisions."""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from moddle_autotask.adapters.aws import central_protocol, lab_protocol
from moddle_autotask.domain.models import Digest, ExecutionMode, LabHandle

from .path_safety import assert_no_indirection
from .state import NotificationEvent, _event_from_json, _event_id, _validate_identity

if TYPE_CHECKING:
    from moddle_autotask.adapters.aws.retention import PreparedTombstone


class ApprovalStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalButtons:
    approve: str
    ignore: str
    details: str


@dataclass(frozen=True, slots=True)
class SubmissionButtons:
    submit: str
    decline: str
    details: str
    requires_statement: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    action: str
    result: str
    event: NotificationEvent


@dataclass(frozen=True, slots=True)
class WorkItem:
    event: NotificationEvent
    selected_mode: ExecutionMode
    specification_digest: Digest
    provision_key: str
    status: str
    lab_handle: LabHandle | None
    attempts: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class WorkClaim:
    item: WorkItem
    lease_token: str


@dataclass(frozen=True, slots=True)
class ExecutionNotification:
    event: NotificationEvent
    succeeded: bool
    summary: str
    report_markdown: str
    provenance: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RetentionRecord:
    """Read-only evidence that a completed execution may enter local retention planning."""

    event_id: str
    task_key: str
    revision_digest: str
    selected_mode: ExecutionMode
    work_status: str
    succeeded: bool
    outbox_created_at: int
    delivered_at: int | None
    scratch_eligible_at: int
    evidence_eligible_at: int | None
    central_job_ids: tuple[str, ...]
    bundle_digest: str | None
    execution_family: str = "central"
    barrier_ids: tuple[str, ...] = ()
    dispatch_id: str | None = None
    dispatch_digest: str | None = None
    result_digests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetentionCompletionReceipt:
    tombstone_id: str
    completed_at: int
    event_id: str
    target_phase: str


@dataclass(frozen=True, slots=True)
class RetentionReconciliationPage:
    cursor: tuple[int, str, str]
    receipts: tuple[RetentionCompletionReceipt, ...]
    lab_phase_ids: tuple[str, ...] = ()
    dispatch_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionReconciliationResult:
    """The one durable reconciliation state observed by a controller cycle."""

    state: Literal["page", "wrapped", "empty_initial"]
    page: RetentionReconciliationPage | None = None

    def __post_init__(self) -> None:
        if self.state == "page" and self.page is not None and self.page.receipts:
            return
        if self.state in {"wrapped", "empty_initial"} and self.page is None:
            return
        raise ValueError("retention reconciliation result is invalid")


@dataclass(frozen=True, slots=True)
class SubmissionManifest:
    event: NotificationEvent
    manifest_digest: str
    filename: str
    report_markdown: str
    report_digest: str

    @property
    def submission_statement_digest(self) -> str | None:
        if not self.event.requires_submission_statement:
            return None
        return _statement_digest(
            self.event.submission_statement, self.event.submission_statement_format
        )

    @property
    def submission_statement_plain(self) -> str | None:
        if not self.event.requires_submission_statement:
            return None
        return _plain_statement(
            self.event.submission_statement, self.event.submission_statement_format
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifacts": [
                {
                    "filename": self.filename,
                    "sha256": self.report_digest,
                    "sizeBytes": len(self.report_markdown.encode("utf-8")),
                }
            ],
            "assignmentId": self.event.assignment_id,
            "submissionDrafts": self.event.submission_drafts,
            "requireSubmissionStatement": self.event.requires_submission_statement,
            "submissionStatement": self.event.submission_statement,
            "submissionStatementFormat": self.event.submission_statement_format,
            "submissionStatementDigest": self.submission_statement_digest,
            "submissionStatementPlain": self.submission_statement_plain,
            "manifestDigest": self.manifest_digest,
            "reportDigest": self.report_digest,
            "reportMarkdown": self.report_markdown,
            "revisionDigest": self.event.revision_digest,
            "taskKey": self.event.task_key,
        }


@dataclass(frozen=True, slots=True)
class SubmissionClaim:
    manifest: SubmissionManifest
    lease_token: str
    phase: str
    draft_item_id: int | None


@dataclass(frozen=True, slots=True)
class SubmissionNotification:
    manifest: SubmissionManifest
    status: str
    reference: str | None


_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32}$")
_SCHEMA_VERSION = "7"
_PREVIOUS_SCHEMA_VERSION = "6"
_METADATA_SQL = "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_REQUESTS_SQL = (
    "CREATE TABLE requests ("
    "event_id TEXT PRIMARY KEY NOT NULL, task_key TEXT NOT NULL, "
    "revision_digest TEXT NOT NULL, payload TEXT NOT NULL, "
    "delivery_state TEXT NOT NULL CHECK (delivery_state IN ('prepared','notified')), "
    "decision TEXT NOT NULL CHECK (decision IN ('pending','approved','ignored')), "
    "decided_by INTEGER, decided_at INTEGER, chat_id INTEGER, message_id INTEGER, "
    "created_at INTEGER NOT NULL, "
    "UNIQUE(task_key, revision_digest), "
    "CHECK ((decision = 'pending') = (decided_by IS NULL AND decided_at IS NULL)), "
    "CHECK ((delivery_state = 'notified') = (chat_id IS NOT NULL AND message_id IS NOT NULL)))"
)
_CALLBACKS_SQL = (
    "CREATE TABLE callbacks ("
    "token TEXT PRIMARY KEY NOT NULL, event_id TEXT NOT NULL, "
    "action TEXT NOT NULL CHECK (action IN ('approve','ignore','details')), "
    "UNIQUE(event_id, action), FOREIGN KEY(event_id) REFERENCES requests(event_id))"
)
_CURSOR_SQL = (
    "CREATE TABLE telegram_cursor ("
    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
    "next_update_id INTEGER NOT NULL CHECK (next_update_id >= 0))"
)
_WORK_SQL_V2 = (
    "CREATE TABLE work_items ("
    "event_id TEXT PRIMARY KEY NOT NULL, "
    "selected_mode TEXT NOT NULL CHECK (selected_mode IN ('central','in_guest','hybrid')), "
    "specification_digest TEXT NOT NULL, provision_key TEXT NOT NULL UNIQUE, "
    "status TEXT NOT NULL CHECK "
    "(status IN ('pending','lab_pending','ready','failed','cleaned')), "
    "lab_handle TEXT, attempts INTEGER NOT NULL DEFAULT 0 "
    "CHECK (attempts >= 0 AND attempts <= 1000000), available_at INTEGER NOT NULL, "
    "lease_owner TEXT, lease_token TEXT, lease_expires_at INTEGER, error_code TEXT, "
    "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
    "FOREIGN KEY(event_id) REFERENCES requests(event_id), "
    "CHECK ((lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR "
    "(lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)), "
    "CHECK (status != 'lab_pending' OR lab_handle IS NOT NULL), "
    "CHECK (lab_handle IS NULL OR selected_mode != 'central'), "
    "CHECK (status != 'ready' OR selected_mode = 'central' OR lab_handle IS NOT NULL))"
)
_WORK_SQL = _WORK_SQL_V2.replace(
    "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, ",
    "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, cleanup_due_at INTEGER, ",
)
_OUTBOX_SQL = (
    "CREATE TABLE execution_outbox (event_id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL, "
    "delivered_at INTEGER, created_at INTEGER NOT NULL, "
    "FOREIGN KEY(event_id) REFERENCES work_items(event_id), "
    "CHECK (delivered_at IS NULL OR delivered_at >= created_at))"
)
_WORK_CLAIMABLE_INDEX_SQL = (
    "CREATE INDEX work_claimable_idx ON work_items(status, available_at, lease_expires_at)"
)
_OUTBOX_PENDING_INDEX_SQL = (
    "CREATE INDEX execution_outbox_pending_idx ON execution_outbox(delivered_at, created_at)"
)
_RETENTION_COMPLETIONS_SQL = (
    "CREATE TABLE retention_completions ("
    "event_id TEXT NOT NULL, target_phase TEXT NOT NULL CHECK (target_phase IN ('scratch','evidence')), "
    "tombstone_id TEXT NOT NULL CHECK (length(tombstone_id) = 64 AND tombstone_id NOT GLOB '*[^0-9a-f]*'), "
    "completed_at INTEGER NOT NULL CHECK (completed_at >= 0), "
    "PRIMARY KEY(event_id, target_phase), FOREIGN KEY(event_id) REFERENCES requests(event_id))"
)
_RETENTION_COMPLETIONS_INDEX_SQL = (
    "CREATE INDEX retention_completions_completed_idx ON retention_completions(completed_at, event_id)"
)
_RETENTION_RECONCILIATION_CURSOR_SQL = (
    "CREATE TABLE retention_reconciliation_cursor ("
    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
    "completed_at INTEGER NOT NULL CHECK (completed_at >= -1), "
    "event_id TEXT NOT NULL, "
    "target_phase TEXT NOT NULL CHECK (target_phase IN ('','scratch','evidence')), "
    "CHECK ((completed_at = -1 AND event_id = '' AND target_phase = '') OR "
    "(completed_at >= 0 AND event_id != '' AND target_phase IN ('scratch','evidence')))"
    ")"
)
_SUBMISSIONS_SQL_V4 = (
    "CREATE TABLE submissions (event_id TEXT PRIMARY KEY NOT NULL, manifest_digest TEXT NOT NULL, "
    "payload TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN "
    "('awaiting_approval','approved','declined','uploading','saving','submitted','failed')), "
    "decided_by INTEGER, decided_at INTEGER, lease_token TEXT, lease_expires_at INTEGER, "
    "draft_item_id INTEGER, receipt_reference TEXT, error_code TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
    "FOREIGN KEY(event_id) REFERENCES requests(event_id), "
    "CHECK ((lease_token IS NULL AND lease_expires_at IS NULL) OR "
    "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)), "
    "CHECK ((status IN ('uploading','saving')) = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)), "
    "CHECK (status != 'saving' OR draft_item_id IS NOT NULL), "
    "CHECK (draft_item_id IS NULL OR draft_item_id > 0))"
)
_SUBMISSIONS_SQL = _SUBMISSIONS_SQL_V4.replace(
    "('awaiting_approval','approved','declined','uploading','saving','submitted','failed')",
    "('awaiting_approval','approved','declined','uploading','saving','finalizing','submitted','failed')",
)
_SUBMISSIONS_SQL = _SUBMISSIONS_SQL.replace(
    "CHECK ((status IN ('uploading','saving')) = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))",
    "CHECK ((status IN ('uploading','saving','finalizing')) = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))",
)
_SUBMISSIONS_SQL = _SUBMISSIONS_SQL.replace(
    "draft_item_id INTEGER, receipt_reference TEXT, error_code TEXT",
    "draft_item_id INTEGER, receipt_reference TEXT, receipt_payload TEXT, error_code TEXT",
)
_SUBMISSION_CALLBACKS_SQL = (
    "CREATE TABLE submission_callbacks (token TEXT PRIMARY KEY NOT NULL, event_id TEXT NOT NULL, "
    "action TEXT NOT NULL CHECK (action IN ('submit','decline','details')), "
    "UNIQUE(event_id, action), FOREIGN KEY(event_id) REFERENCES submissions(event_id))"
)
_SUBMISSION_OUTBOX_SQL = (
    "CREATE TABLE submission_outbox (event_id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL, "
    "delivered_at INTEGER, created_at INTEGER NOT NULL, FOREIGN KEY(event_id) REFERENCES submissions(event_id), "
    "CHECK (delivered_at IS NULL OR delivered_at >= created_at))"
)
_SUBMISSION_OUTBOX_PENDING_INDEX_SQL = (
    "CREATE INDEX submission_outbox_pending_idx ON submission_outbox(delivered_at, created_at)"
)
_LEASE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_WORK_ATTEMPTS = 20
_LAB_TTL_SECONDS = 7200


class ApprovalState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        _assert_safe_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(self.path)
        try:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if not tables:
                    connection.execute(_METADATA_SQL)
                    connection.execute(_REQUESTS_SQL)
                    connection.execute(_CALLBACKS_SQL)
                    connection.execute(_CURSOR_SQL)
                    connection.execute(_WORK_SQL)
                    connection.execute(_WORK_CLAIMABLE_INDEX_SQL)
                    connection.execute(_OUTBOX_SQL)
                    connection.execute(_OUTBOX_PENDING_INDEX_SQL)
                    connection.execute(_RETENTION_COMPLETIONS_SQL)
                    connection.execute(_RETENTION_COMPLETIONS_INDEX_SQL)
                    connection.execute(_RETENTION_RECONCILIATION_CURSOR_SQL)
                    _create_submission_schema(connection)
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                        (_SCHEMA_VERSION,),
                    )
                    connection.execute(
                        "INSERT INTO telegram_cursor(singleton, next_update_id) VALUES (1, 0)"
                    )
                    connection.execute(
                        "INSERT INTO retention_reconciliation_cursor "
                        "(singleton, completed_at, event_id, target_phase) VALUES (1, -1, '', '')"
                    )
                else:
                    version = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'schema_version'"
                    ).fetchone()
                    if version == ("1",):
                        if not _valid_schema(connection, "1"):
                            raise ApprovalStateError("approval state schema is corrupt")
                        connection.execute(_WORK_SQL)
                        connection.execute(_WORK_CLAIMABLE_INDEX_SQL)
                        connection.execute(_OUTBOX_SQL)
                        connection.execute(_OUTBOX_PENDING_INDEX_SQL)
                        _create_submission_schema(connection)
                        connection.execute(
                            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                            (_SCHEMA_VERSION,),
                        )
                        for payload, decided_at in connection.execute(
                            "SELECT payload, decided_at FROM requests WHERE decision = 'approved'"
                        ).fetchall():
                            if not isinstance(payload, str) or not isinstance(decided_at, int):
                                raise ApprovalStateError("approval state schema is corrupt")
                            _enqueue_work(connection, _event_from_json(payload), decided_at)
                    elif version == ("2",):
                        if not _valid_schema(connection, "2"):
                            raise ApprovalStateError("approval state schema is corrupt")
                        connection.execute("DROP INDEX work_claimable_idx")
                        connection.execute("ALTER TABLE work_items RENAME TO work_items_v2")
                        connection.execute(_WORK_SQL)
                        connection.execute(
                            "INSERT INTO work_items(event_id, selected_mode, specification_digest, "
                            "provision_key, status, lab_handle, attempts, available_at, lease_owner, "  # noqa: E501
                            "lease_token, lease_expires_at, error_code, created_at, updated_at) "
                            "SELECT event_id, selected_mode, specification_digest, provision_key, status, "  # noqa: E501
                            "lab_handle, attempts, available_at, lease_owner, lease_token, lease_expires_at, "  # noqa: E501
                            "error_code, created_at, updated_at FROM work_items_v2"
                        )
                        connection.execute("DROP TABLE work_items_v2")
                        connection.execute(_WORK_CLAIMABLE_INDEX_SQL)
                        connection.execute(_OUTBOX_SQL)
                        connection.execute(_OUTBOX_PENDING_INDEX_SQL)
                        _create_submission_schema(connection)
                        connection.execute(
                            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                            (_SCHEMA_VERSION,),
                        )
                    elif version == ("3",):
                        if not _valid_schema(connection, "3"):
                            raise ApprovalStateError("approval state schema is corrupt")
                        _create_submission_schema(connection)
                        connection.execute(
                            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                            (_SCHEMA_VERSION,),
                        )
                    elif version == ("4",):
                        if not _valid_schema(connection, "4"):
                            raise ApprovalStateError("approval state schema is corrupt")
                        _migrate_submission_schema_v4(connection)
                        connection.execute(
                            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                            (_SCHEMA_VERSION,),
                        )
                        connection.execute(_RETENTION_COMPLETIONS_SQL)
                        connection.execute(_RETENTION_COMPLETIONS_INDEX_SQL)
                    elif version == ("5",):
                        if not _valid_schema(connection, "5"):
                            raise ApprovalStateError("approval state schema is corrupt")
                        connection.execute(_RETENTION_COMPLETIONS_SQL)
                        connection.execute(_RETENTION_COMPLETIONS_INDEX_SQL)
                        connection.execute(
                            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                            (_SCHEMA_VERSION,),
                        )
                    elif version == (_PREVIOUS_SCHEMA_VERSION,):
                        if not _valid_schema(connection, _PREVIOUS_SCHEMA_VERSION):
                            raise ApprovalStateError("approval state schema is corrupt")
                        connection.execute(_RETENTION_RECONCILIATION_CURSOR_SQL)
                        connection.execute(
                            "INSERT INTO retention_reconciliation_cursor "
                            "(singleton, completed_at, event_id, target_phase) VALUES (1, -1, '', '')"
                        )
                        connection.execute(
                            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                            (_SCHEMA_VERSION,),
                        )
                    elif version != (_SCHEMA_VERSION,) or not _valid_schema(
                        connection, _SCHEMA_VERSION
                    ):
                        raise ApprovalStateError("approval state schema is corrupt")
                    if version in {("1",), ("2",), ("3",)}:
                        connection.execute(_RETENTION_COMPLETIONS_SQL)
                        connection.execute(_RETENTION_COMPLETIONS_INDEX_SQL)
                    if version in {("1",), ("2",), ("3",), ("4",), ("5",)}:
                        connection.execute(_RETENTION_RECONCILIATION_CURSOR_SQL)
                        connection.execute(
                            "INSERT INTO retention_reconciliation_cursor "
                            "(singleton, completed_at, event_id, target_phase) VALUES (1, -1, '', '')"
                        )
                connection.execute("COMMIT")
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                connection.close()
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except ApprovalStateError:
            raise
        except (OSError, ValueError, sqlite3.Error) as error:
            raise ApprovalStateError("could not initialize approval state") from error

    def prepare(self, event: NotificationEvent, now: int | None = None) -> ApprovalButtons:
        if not isinstance(event, NotificationEvent):
            raise ApprovalStateError("approval notification is invalid")
        moment = _now(now)
        payload = json.dumps(
            event.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT task_key, revision_digest, payload FROM requests WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO requests(event_id, task_key, revision_digest, payload, "
                        "delivery_state, decision, created_at) "
                        "VALUES (?, ?, ?, ?, 'prepared', 'pending', ?)",
                        (
                            event.event_id,
                            event.task_key,
                            event.revision_digest,
                            payload,
                            moment,
                        ),
                    )
                    for action in ("approve", "ignore", "details"):
                        connection.execute(
                            "INSERT INTO callbacks(token, event_id, action) VALUES (?, ?, ?)",
                            (_new_token(), event.event_id, action),
                        )
                elif row != (event.task_key, event.revision_digest, payload):
                    raise ApprovalStateError("stored approval request is corrupt")
                tokens = dict(
                    connection.execute(
                        "SELECT action, token FROM callbacks WHERE event_id = ?",
                        (event.event_id,),
                    ).fetchall()
                )
                if set(tokens) != {"approve", "ignore", "details"} or any(
                    not isinstance(token, str) or not _TOKEN.fullmatch(token)
                    for token in tokens.values()
                ):
                    raise ApprovalStateError("stored approval callbacks are corrupt")
                connection.execute("COMMIT")
                return ApprovalButtons(tokens["approve"], tokens["ignore"], tokens["details"])
        except ApprovalStateError:
            raise
        except sqlite3.IntegrityError as error:
            raise ApprovalStateError("approval request identity conflicts") from error
        except sqlite3.Error as error:
            raise ApprovalStateError("could not prepare approval request") from error

    def mark_notified(self, event: NotificationEvent, chat_id: int, message_id: int) -> None:
        _positive_id(chat_id, "chat")
        _positive_id(message_id, "message")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT task_key, revision_digest, payload FROM requests WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                payload = json.dumps(
                    event.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                if row != (event.task_key, event.revision_digest, payload):
                    raise ApprovalStateError("approval request does not match notification")
                connection.execute(
                    "UPDATE requests SET delivery_state = 'notified', chat_id = ?, message_id = ? "
                    "WHERE event_id = ?",
                    (chat_id, message_id, event.event_id),
                )
                connection.execute("COMMIT")
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not record approval notification") from error

    def resolve(
        self, token: str, user_id: int, allowed_user_id: int, now: int | None = None
    ) -> ApprovalOutcome:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ApprovalStateError("approval callback is invalid")
        _positive_id(user_id, "user")
        _positive_id(allowed_user_id, "allowed user")
        if user_id != allowed_user_id:
            raise ApprovalStateError("approval callback is unauthorized")
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT c.action, r.payload, r.decision FROM callbacks c "
                    "JOIN requests r ON r.event_id = c.event_id WHERE c.token = ?",
                    (token,),
                ).fetchone()
                if row is None:
                    raise ApprovalStateError("approval callback is unknown")
                action, payload, decision = row
                event = _event_from_json(payload)
                if action == "details":
                    result = "details"
                else:
                    requested = "approved" if action == "approve" else "ignored"
                    if decision == "pending":
                        connection.execute(
                            "UPDATE requests SET decision = ?, decided_by = ?, decided_at = ? "
                            "WHERE event_id = ? AND decision = 'pending'",
                            (requested, user_id, moment, event.event_id),
                        )
                        if requested == "approved":
                            _enqueue_work(connection, event, moment)
                        result = requested
                    elif decision == requested:
                        if requested == "approved":
                            _enqueue_work(connection, event, moment)
                        result = f"already_{requested}"
                    else:
                        result = "conflict"
                connection.execute("COMMIT")
                return ApprovalOutcome(action, result, event)
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not apply approval callback") from error

    def decision(self, task_key: str, revision_digest: str) -> str | None:
        _validate_identity(task_key, revision_digest)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT decision FROM requests WHERE task_key = ? AND revision_digest = ?",
                    (task_key, revision_digest),
                ).fetchone()
                return None if row is None else str(row[0])
        except sqlite3.Error as error:
            raise ApprovalStateError("could not read approval decision") from error

    def next_update_id(self) -> int:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT next_update_id FROM telegram_cursor WHERE singleton = 1"
                ).fetchone()
                if row is None or not isinstance(row[0], int) or row[0] < 0:
                    raise ApprovalStateError("approval update cursor is corrupt")
                return row[0]
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not read approval update cursor") from error

    def advance_update_id(self, processed_update_id: int) -> int:
        if (
            not isinstance(processed_update_id, int)
            or isinstance(processed_update_id, bool)
            or processed_update_id < 0
        ):
            raise ApprovalStateError("Telegram update identity is invalid")
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE telegram_cursor SET next_update_id = "
                    "MAX(next_update_id, ?) WHERE singleton = 1",
                    (processed_update_id + 1,),
                )
                row = connection.execute(
                    "SELECT next_update_id FROM telegram_cursor WHERE singleton = 1"
                ).fetchone()
                if row is None or not isinstance(row[0], int) or row[0] < 0:
                    raise ApprovalStateError("approval update cursor is corrupt")
                return row[0]
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not advance approval update cursor") from error

    def claim_work(
        self, owner: str, lease_seconds: int, now: int | None = None
    ) -> WorkClaim | None:
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise ApprovalStateError("work lease owner is invalid")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 6 <= lease_seconds <= 3600
        ):
            raise ApprovalStateError("work lease duration is invalid")
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT w.event_id, w.selected_mode, w.specification_digest, "
                    "w.provision_key, w.status, w.lab_handle, w.attempts, w.error_code, "
                    "r.payload "
                    "FROM work_items w JOIN requests r ON r.event_id = w.event_id "
                    "WHERE (w.status IN ('pending','lab_pending') OR "
                    "(w.status = 'failed' AND w.lab_handle IS NOT NULL AND "
                    "(w.cleanup_due_at IS NULL OR w.cleanup_due_at <= ?)) OR "
                    "(w.status = 'ready' AND "
                    "(w.selected_mode = 'central' OR (w.lab_handle IS NOT NULL AND "
                    "(w.error_code IS NULL OR w.cleanup_due_at IS NULL OR "
                    "w.cleanup_due_at <= ?))))) "
                    "AND w.available_at <= ? "
                    "AND (w.lease_expires_at IS NULL OR w.lease_expires_at <= ?) "
                    "ORDER BY CASE w.status WHEN 'failed' THEN 0 WHEN 'ready' THEN 1 "
                    "WHEN 'lab_pending' THEN 2 ELSE 3 END, "
                    "w.created_at, w.event_id",
                    (moment, moment, moment, moment),
                ).fetchall()
                selected: tuple[object, ...] | None = None
                selected_event: NotificationEvent | None = None
                selected_attempts: int | None = None
                for row in rows:
                    event = _event_from_json(str(row[8]))
                    stored_attempts = _stored_attempts(row[6])
                    candidate = _work_item(row, event, attempts=stored_attempts)
                    if (
                        candidate.selected_mode is not ExecutionMode.CENTRAL
                        and candidate.status == "pending"
                    ):
                        active = connection.execute(
                            "SELECT 1 FROM work_items WHERE event_id != ? "
                            "AND ((lab_handle IS NOT NULL AND status != 'cleaned') "
                            "OR (selected_mode != 'central' AND status = 'pending' "
                            "AND lease_expires_at > ?)) LIMIT 1",
                            (row[0], moment),
                        ).fetchone()
                        if active is not None:
                            continue
                    selected = row
                    selected_event = event
                    selected_attempts = stored_attempts
                    break
                if selected is None or selected_event is None or selected_attempts is None:
                    connection.execute("COMMIT")
                    return None
                token = secrets.token_urlsafe(32)
                if not _LEASE_TOKEN.fullmatch(token):
                    raise ApprovalStateError("could not create work lease")
                updated = connection.execute(
                    "UPDATE work_items SET attempts = attempts + 1, lease_owner = ?, "
                    "lease_token = ?, lease_expires_at = ?, updated_at = ? "
                    "WHERE event_id = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                    (owner, token, moment + lease_seconds, moment, selected[0], moment),
                ).rowcount
                if updated != 1:
                    raise ApprovalStateError("could not acquire work lease")
                connection.execute("COMMIT")
                item = _work_item(selected, selected_event, attempts=selected_attempts + 1)
                return WorkClaim(item, token)
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not claim approved work") from error

    def record_lab(self, claim: WorkClaim, handle: LabHandle, now: int | None = None) -> bool:
        if claim.item.status != "pending" or claim.item.selected_mode is ExecutionMode.CENTRAL:
            raise ApprovalStateError("work claim cannot record a lab")
        return self._finish_claim(
            claim,
            "status = 'lab_pending', lab_handle = ?, available_at = ?, error_code = NULL",
            (handle.value, _now(now)),
            "record provisioned lab",
            now,
        )

    def mark_ready(
        self, claim: WorkClaim, now: int | None = None, *, for_execution: bool = False
    ) -> bool:
        if claim.item.status not in {"pending", "lab_pending"}:
            raise ApprovalStateError("work claim cannot become ready")
        if claim.item.status == "pending" and claim.item.selected_mode is not ExecutionMode.CENTRAL:
            raise ApprovalStateError("non-central work requires a lab")
        moment = _now(now)
        available_at = moment
        error_code = "agent_ready" if for_execution else None
        if claim.item.lab_handle is not None and not for_execution:
            available_at += _LAB_TTL_SECONDS
        return self._finish_claim(
            claim,
            "status = 'ready', available_at = ?, error_code = ?",
            (available_at, error_code),
            "mark work ready",
            now,
        )

    def mark_execution_complete(self, claim: WorkClaim, now: int | None = None) -> bool:
        if claim.item.status != "ready":
            raise ApprovalStateError("work claim cannot complete execution")
        moment = _now(now)
        if claim.item.lab_handle is None:
            assignment = "status = 'cleaned', available_at = ?, error_code = NULL"
            values: tuple[object, ...] = (moment,)
        else:
            assignment = "available_at = ?, error_code = 'execution_complete'"
            values = (moment + _LAB_TTL_SECONDS,)
        return self._finish_claim(claim, assignment, values, "complete execution", now)

    def complete_execution(
        self,
        claim: WorkClaim,
        *,
        succeeded: bool,
        summary: str,
        report_markdown: str,
        provenance: dict[str, object] | None = None,
        now: int | None = None,
    ) -> bool:
        if claim.item.status != "ready" or not isinstance(succeeded, bool):
            raise ApprovalStateError("work claim cannot complete execution")
        if (
            not isinstance(summary, str)
            or not isinstance(report_markdown, str)
            or len(summary.encode("utf-8")) > 16_384
            or len(report_markdown.encode("utf-8")) > 2 * 1024 * 1024
        ):
            raise ApprovalStateError("execution completion is invalid")
        moment = _now(now)
        central = claim.item.selected_mode is ExecutionMode.CENTRAL
        if (
            (central and succeeded and provenance is None)
            or (not central and provenance is None)
            or (provenance is not None and not isinstance(provenance, dict))
        ):
            raise ApprovalStateError("execution provenance is invalid")
        if provenance is not None:
            _validate_execution_provenance(provenance)
            _validate_execution_provenance_binding(provenance, claim.item)
            _validate_execution_provenance_outcome(provenance, succeeded)
        payload_value: dict[str, object] = {
            "reportMarkdown": report_markdown,
            "succeeded": succeeded,
            "summary": summary,
        }
        if provenance is not None:
            payload_value["kind"] = "moodle-execution-outcome-v2"
            payload_value["provenance"] = provenance
        payload = json.dumps(
            payload_value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if claim.item.lab_handle is None:
            assignment = "status = 'cleaned', available_at = ?, error_code = NULL"
            values: tuple[object, ...] = (moment,)
        elif succeeded:
            assignment, values = (
                "available_at = ?, error_code = 'execution_complete', cleanup_due_at = ?",
                (moment + _LAB_TTL_SECONDS, moment + _LAB_TTL_SECONDS),
            )
        else:
            assignment, values = (
                "status = 'failed', available_at = ?, error_code = 'agent_failed', cleanup_due_at = ?",  # noqa: E501
                (moment, moment),
            )
        if not isinstance(claim, WorkClaim) or not _LEASE_TOKEN.fullmatch(claim.lease_token):
            raise ApprovalStateError("work claim is invalid")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    f"UPDATE work_items SET {assignment}, lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE event_id = ? AND lease_token = ? "  # noqa: E501
                    "AND lease_expires_at > ?",
                    (*values, moment, claim.item.event.event_id, claim.lease_token, moment),
                ).rowcount
                if updated == 1:
                    connection.execute(
                        "INSERT INTO execution_outbox(event_id, payload, delivered_at, created_at) "
                        "VALUES (?, ?, NULL, ?)",
                        (claim.item.event.event_id, payload, moment),
                    )
                connection.execute("COMMIT")
                return bool(updated)
        except sqlite3.IntegrityError as error:
            raise ApprovalStateError("execution completion is corrupt") from error
        except sqlite3.Error as error:
            raise ApprovalStateError("could not persist execution completion") from error

    def retention_records(
        self, now: int, scratch_ttl: int, evidence_ttl: int, limit: int
    ) -> tuple[RetentionRecord, ...]:
        """Return only durable completion/outbox facts; this method never mutates state.

        Local collectors use these records to decide whether a separately
        durable tombstone may be prepared.  In particular, delivery age is
        never inferred from the execution timestamp.
        """
        moment = _now(now)
        if type(scratch_ttl) is not int or not 1 <= scratch_ttl <= 90 * 24 * 3600:
            raise ValueError("scratch retention TTL is invalid")
        if type(evidence_ttl) is not int or not 1 <= evidence_ttl <= 90 * 24 * 3600:
            raise ValueError("evidence retention TTL is invalid")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("retention record limit is invalid")
        try:
            with self._connect() as connection:
                if not _valid_schema(connection, _SCHEMA_VERSION):
                    raise ApprovalStateError("approval state schema is corrupt")
                joined = (
                    " FROM execution_outbox o JOIN work_items w ON w.event_id = o.event_id "
                    "JOIN requests r ON r.event_id = o.event_id "
                )
                impossible = connection.execute(
                    "SELECT 1" + joined
                    + "WHERE (w.selected_mode = 'central' AND w.status != 'cleaned') "
                    "OR (w.selected_mode != 'central' AND w.status IN ('pending','lab_pending')) "
                    "LIMIT 1"
                ).fetchone()
                malformed = connection.execute(
                    "SELECT 1" + joined
                    + "WHERE w.status = 'cleaned' AND ("
                    "json_valid(o.payload) != 1 OR "
                    "CASE WHEN json_valid(o.payload) THEN "
                    "COALESCE(json_type(o.payload, '$.succeeded'), 'missing') "
                    "ELSE 'invalid' END NOT IN ('true','false') OR "
                    "CASE WHEN json_valid(o.payload) THEN "
                    "COALESCE(json_type(o.payload, '$.summary'), 'missing') "
                    "ELSE 'invalid' END != 'text' OR "
                    "CASE WHEN json_valid(o.payload) THEN "
                    "COALESCE(json_type(o.payload, '$.reportMarkdown'), 'missing') "
                    "ELSE 'invalid' END != 'text' OR "
                    "CASE WHEN json_valid(o.payload) THEN CASE WHEN "
                    "json_type(o.payload, '$.succeeded') = 'false' THEN CASE WHEN "
                    "json_type(o.payload, '$.provenance') = 'object' THEN 0 ELSE CASE WHEN "
                    "(SELECT COUNT(*) FROM json_each(o.payload)) != 3 OR "
                    "(SELECT COUNT(DISTINCT key) FROM json_each(o.payload)) != 3 OR "
                    "(SELECT COUNT(*) FROM json_each(o.payload) "
                    "WHERE key IN ('succeeded','summary','reportMarkdown')) != 3 "
                    "THEN 1 ELSE 0 END END ELSE 0 END ELSE 1 END = 1) LIMIT 1"
                ).fetchone()
                if impossible is not None or malformed is not None:
                    raise ApprovalStateError("retention record is corrupt")
                rows = connection.execute(
                    "SELECT r.event_id, r.task_key, r.revision_digest, r.payload, "
                    "w.selected_mode, w.status, o.payload, o.created_at, o.delivered_at, "
                    "w.specification_digest"
                    + joined
                    + "WHERE w.status = 'cleaned' "
                    "AND json_valid(o.payload) = 1 "
                    "AND ((w.selected_mode = 'central' AND ("
                    "json_type(o.payload, '$.succeeded') = 'true' "
                    "OR json_type(o.payload, '$.provenance') = 'object')) "
                    "OR (w.selected_mode != 'central' "
                    "AND json_type(o.payload, '$.provenance') = 'object')) "
                    "AND NOT EXISTS (SELECT 1 FROM retention_completions c "
                    "WHERE c.event_id = o.event_id AND c.target_phase = 'evidence') "
                    "AND (NOT EXISTS (SELECT 1 FROM retention_completions c "
                    "WHERE c.event_id = o.event_id AND c.target_phase = 'scratch') "
                    "OR (o.delivered_at IS NOT NULL AND o.delivered_at + ? <= ? "
                    "AND json_type(o.payload, '$.provenance.artifactBundleDigest') = 'text')) "
                    "ORDER BY o.created_at, o.event_id LIMIT ?",
                    (evidence_ttl, moment, limit),
                ).fetchall()
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not read retention records") from error
        return tuple(_retention_record(row, moment, scratch_ttl, evidence_ttl) for row in rows)

    def record_retention_completions(
        self, completed: tuple[PreparedTombstone, ...], *, completed_at: int
    ) -> bool:
        """Atomically record exact terminal filesystem receipts without pruning audit rows."""
        from moddle_autotask.adapters.aws.retention import PreparedTombstone

        if (
            type(completed_at) is not int
            or completed_at < 0
            or not isinstance(completed, tuple)
            or not completed
            or len(completed) > 10_000
        ):
            raise ApprovalStateError("retention completion is invalid")
        seen: set[tuple[str, str]] = set()
        for item in completed:
            if (
                not isinstance(item, PreparedTombstone)
                or item.target_phase not in {"scratch", "evidence"}
                or not isinstance(item.tombstone_id, str)
                or _DIGEST.fullmatch(item.tombstone_id) is None
                or item.event_id != _event_id(item.task_key, item.revision_digest)
                or (item.target_phase == "scratch") != bool(item.job_ids)
                or (item.target_phase == "evidence") != (item.bundle_digest is not None)
                or (item.event_id, item.target_phase) in seen
            ):
                raise ApprovalStateError("retention completion is invalid")
            seen.add((item.event_id, item.target_phase))
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                inserted = False
                if not _valid_schema(connection, _SCHEMA_VERSION):
                    raise ApprovalStateError("approval state schema is corrupt")
                for item in completed:
                    outbox = connection.execute(
                        "SELECT 1 FROM execution_outbox WHERE event_id = ?", (item.event_id,)
                    ).fetchone()
                    if outbox is None:
                        raise ApprovalStateError("retention completion is invalid")
                    row = connection.execute(
                        "SELECT tombstone_id FROM retention_completions "
                        "WHERE event_id = ? AND target_phase = ?",
                        (item.event_id, item.target_phase),
                    ).fetchone()
                    if row is None:
                        connection.execute(
                            "INSERT INTO retention_completions(event_id, target_phase, tombstone_id, completed_at) "
                            "VALUES (?, ?, ?, ?)",
                            (item.event_id, item.target_phase, item.tombstone_id, completed_at),
                        )
                        inserted = True
                    elif row[0] != item.tombstone_id:
                        raise ApprovalStateError("retention completion conflicts")
                connection.execute("COMMIT")
                return inserted
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not record retention completion") from error

    def retention_reconciliation_page(self, *, limit: int) -> RetentionReconciliationResult:
        """Read one durable completion-ledger page, wrapping only after its end."""
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("retention completion scan limit is invalid")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not _valid_schema(connection, _SCHEMA_VERSION):
                    raise ApprovalStateError("approval state schema is corrupt")
                cursor = _retention_reconciliation_cursor(connection)
                rows = connection.execute(
                    "SELECT tombstone_id, completed_at, event_id, target_phase "
                    "FROM retention_completions WHERE completed_at > ? "
                    "OR (completed_at = ? AND (event_id > ? "
                    "OR (event_id = ? AND target_phase > ?))) "
                    "ORDER BY completed_at, event_id, target_phase LIMIT ?",
                    (cursor[0], cursor[0], cursor[1], cursor[1], cursor[2], limit),
                ).fetchall()
                wrapped = not rows and cursor != (-1, "", "")
                if wrapped:
                    connection.execute(
                        "UPDATE retention_reconciliation_cursor SET completed_at = -1, "
                        "event_id = '', target_phase = '' WHERE singleton = 1"
                    )
                connection.execute("COMMIT")
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not read retention completions") from error
        receipts = tuple(_retention_completion_receipt(row) for row in rows)
        if any(
            current <= previous
            for previous, current in zip(
                ((cursor[0], cursor[1], cursor[2]), *(item_key(item) for item in receipts)),
                (item_key(item) for item in receipts),
                strict=False,
            )
        ):
            raise ApprovalStateError("retention completion is corrupt")
        if receipts:
            return RetentionReconciliationResult(
                "page", RetentionReconciliationPage(cursor, receipts)
            )
        if wrapped:
            return RetentionReconciliationResult("wrapped")
        return RetentionReconciliationResult("empty_initial")

    def advance_retention_reconciliation(self, page: RetentionReconciliationPage) -> None:
        """Persist the exact validated page boundary; an interrupted validation repeats it."""
        if not isinstance(page, RetentionReconciliationPage) or not page.receipts:
            raise ApprovalStateError("retention reconciliation page is invalid")
        receipts = page.receipts
        if any(
            not isinstance(item, RetentionCompletionReceipt)
            or item_key(item) <= page.cursor
            for item in receipts
        ) or any(
            item_key(current) <= item_key(previous)
            for previous, current in zip(receipts, receipts[1:], strict=False)
        ):
            raise ApprovalStateError("retention reconciliation page is invalid")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not _valid_schema(connection, _SCHEMA_VERSION):
                    raise ApprovalStateError("approval state schema is corrupt")
                if _retention_reconciliation_cursor(connection) != page.cursor:
                    raise ApprovalStateError("retention reconciliation cursor changed")
                last = receipts[-1]
                connection.execute(
                    "UPDATE retention_reconciliation_cursor SET completed_at = ?, event_id = ?, "
                    "target_phase = ? WHERE singleton = 1",
                    item_key(last),
                )
                connection.execute("COMMIT")
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not advance retention reconciliation") from error

    def pending_execution_notification(self) -> ExecutionNotification | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT r.payload, o.payload, w.selected_mode FROM execution_outbox o JOIN requests r "
                    "ON r.event_id = o.event_id JOIN work_items w ON w.event_id = o.event_id WHERE o.delivered_at IS NULL "
                    "ORDER BY o.created_at, o.event_id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                event = _event_from_json(str(row[0]))
                payload = json.loads(str(row[1]))
                if not isinstance(payload, dict):
                    raise ApprovalStateError("stored execution completion is corrupt")
                is_v2 = payload.get("kind") == "moodle-execution-outcome-v2"
                if (str(row[2]) == "central" and bool(payload.get("succeeded")) and not is_v2) or (
                    str(row[2]) != "central" and not is_v2
                ):
                    raise ApprovalStateError("stored execution completion is corrupt")
                expected = (
                    {"kind", "provenance", "reportMarkdown", "succeeded", "summary"}
                    if is_v2
                    else {"reportMarkdown", "succeeded", "summary"}
                )
                if set(payload) != expected:
                    raise ApprovalStateError("stored execution completion is corrupt")
                succeeded, summary, report = (
                    payload["succeeded"],
                    payload["summary"],
                    payload["reportMarkdown"],
                )
                if (
                    not isinstance(succeeded, bool)
                    or not isinstance(summary, str)
                    or not isinstance(report, str)
                ):
                    raise ApprovalStateError("stored execution completion is corrupt")
                provenance = payload.get("provenance") if is_v2 else None
                if provenance is not None and not isinstance(provenance, dict):
                    raise ApprovalStateError("stored execution completion is corrupt")
                if provenance is not None:
                    _validate_execution_provenance(provenance)
                    _validate_execution_provenance_outcome(provenance, succeeded)
                return ExecutionNotification(event, succeeded, summary, report, provenance)
        except (ValueError, sqlite3.Error) as error:
            raise ApprovalStateError("could not read execution completion") from error

    def mark_execution_notification_delivered(
        self, notification: ExecutionNotification, now: int | None = None
    ) -> bool:
        if not isinstance(notification, ExecutionNotification):
            raise ApprovalStateError("execution completion is invalid")
        try:
            with self._connect() as connection:
                return bool(
                    connection.execute(
                        "UPDATE execution_outbox SET delivered_at = ? WHERE event_id = ? AND delivered_at IS NULL",  # noqa: E501
                        (_now(now), notification.event.event_id),
                    ).rowcount
                )
        except sqlite3.Error as error:
            raise ApprovalStateError("could not record execution notification") from error

    def prepare_submission(
        self,
        event: NotificationEvent,
        summary: str,
        report_markdown: str,
        now: int | None = None,
    ) -> tuple[SubmissionManifest, SubmissionButtons]:
        if not isinstance(event, NotificationEvent) or event.assignment_id is None:
            raise ApprovalStateError("submission manifest has no exact assignment identity")
        if not event.submission_drafts and event.requires_submission_statement:
            raise ApprovalStateError("submission policy is not supported")
        if not isinstance(summary, str) or not isinstance(report_markdown, str):
            raise ApprovalStateError("submission manifest is invalid")
        manifest = _submission_manifest(event, report_markdown)
        payload = json.dumps(
            manifest.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT manifest_digest, payload FROM submissions WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO submissions(event_id, manifest_digest, payload, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'awaiting_approval', ?, ?)",
                        (event.event_id, manifest.manifest_digest, payload, moment, moment),
                    )
                    for action in ("submit", "decline", "details"):
                        connection.execute(
                            "INSERT INTO submission_callbacks(token, event_id, action) VALUES (?, ?, ?)",
                            (_new_token(), event.event_id, action),
                        )
                elif row != (manifest.manifest_digest, payload):
                    raise ApprovalStateError("stored submission manifest conflicts")
                tokens = dict(
                    connection.execute(
                        "SELECT action, token FROM submission_callbacks WHERE event_id = ?",
                        (event.event_id,),
                    ).fetchall()
                )
                if set(tokens) != {"submit", "decline", "details"} or any(
                    not isinstance(token, str) or not _TOKEN.fullmatch(token)
                    for token in tokens.values()
                ):
                    raise ApprovalStateError("stored submission callbacks are corrupt")
                connection.execute("COMMIT")
                return manifest, SubmissionButtons(
                    tokens["submit"],
                    tokens["decline"],
                    tokens["details"],
                    event.requires_submission_statement,
                )
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not prepare submission approval") from error

    def resolve_submission(
        self, token: str, user_id: int, allowed_user_id: int, now: int | None = None
    ) -> tuple[str, str, SubmissionManifest]:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ApprovalStateError("submission callback is invalid")
        _positive_id(user_id, "user")
        _positive_id(allowed_user_id, "allowed user")
        if user_id != allowed_user_id:
            raise ApprovalStateError("submission callback is unauthorized")
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT c.action, s.payload, s.manifest_digest, s.status, r.payload "
                    "FROM submission_callbacks c JOIN submissions s ON s.event_id = c.event_id "
                    "JOIN requests r ON r.event_id = s.event_id WHERE c.token = ?",
                    (token,),
                ).fetchone()
                if row is None:
                    raise ApprovalStateError("submission callback is unknown")
                action, payload, digest, status, event_payload = row
                manifest = _manifest_from_json(
                    str(payload), str(digest), _event_from_json(str(event_payload))
                )
                if action == "details":
                    result = "details"
                else:
                    requested = "approved" if action == "submit" else "declined"
                    if status == "awaiting_approval":
                        updated = connection.execute(
                            "UPDATE submissions SET status = ?, decided_by = ?, decided_at = ?, updated_at = ? "
                            "WHERE event_id = ? AND status = 'awaiting_approval'",
                            (requested, user_id, moment, moment, manifest.event.event_id),
                        ).rowcount
                        if updated != 1:
                            raise ApprovalStateError("could not apply submission callback")
                        result = requested
                    elif status == requested:
                        result = f"already_{requested}"
                    else:
                        result = "conflict"
                connection.execute("COMMIT")
                return str(action), result, manifest
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not apply submission callback") from error

    def claim_submission(
        self, owner: str, lease_seconds: int, now: int | None = None
    ) -> SubmissionClaim | None:
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise ApprovalStateError("submission lease owner is invalid")
        if not isinstance(lease_seconds, int) or not 6 <= lease_seconds <= 3600:
            raise ApprovalStateError("submission lease duration is invalid")
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # An upload whose item id was never recorded cannot have reached the
                # assignment, so it is safe to offer it again.  A saved draft might
                # have reached Moodle: claim it solely for remote verification.
                connection.execute(
                    "UPDATE submissions SET status = 'approved', lease_token = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE status = 'uploading' "
                    "AND lease_expires_at <= ?",
                    (moment, moment),
                )
                stale = connection.execute(
                    "SELECT s.event_id, s.payload, s.manifest_digest, r.payload FROM submissions s "
                    "JOIN requests r ON r.event_id = s.event_id WHERE s.status IN ('saving','finalizing') "
                    "AND s.lease_expires_at <= ?",
                    (moment,),
                ).fetchall()
                for _event_id, payload, digest, event_payload in stale:
                    _manifest_from_json(
                        str(payload), str(digest), _event_from_json(str(event_payload))
                    )
                row = connection.execute(
                    "SELECT s.payload, s.manifest_digest, s.status, s.draft_item_id, r.payload "
                    "FROM submissions s JOIN requests r ON r.event_id = s.event_id "
                    "WHERE s.status = 'approved' OR (s.status IN ('saving','finalizing') AND s.lease_expires_at <= ?) "
                    "ORDER BY CASE s.status WHEN 'saving' THEN 0 WHEN 'finalizing' THEN 1 ELSE 2 END, s.created_at, s.event_id LIMIT 1",
                    (moment,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                manifest = _manifest_from_json(
                    str(row[0]), str(row[1]), _event_from_json(str(row[4]))
                )
                phase = str(row[2])
                draft_item_id = row[3]
                if phase in {"saving", "finalizing"} and (
                    not isinstance(draft_item_id, int) or draft_item_id <= 0
                ):
                    raise ApprovalStateError("stored submission attempt is corrupt")
                token = secrets.token_urlsafe(32)
                if not _LEASE_TOKEN.fullmatch(token):
                    raise ApprovalStateError("could not create submission lease")
                updated = connection.execute(
                    "UPDATE submissions SET status = CASE WHEN status = 'approved' THEN 'uploading' ELSE status END, "
                    "lease_token = ?, lease_expires_at = ?, updated_at = ? WHERE event_id = ? "
                    "AND (status = 'approved' OR (status IN ('saving','finalizing') AND lease_expires_at <= ?))",
                    (token, moment + lease_seconds, moment, manifest.event.event_id, moment),
                ).rowcount
                if updated != 1:
                    raise ApprovalStateError("could not acquire submission lease")
                connection.execute("COMMIT")
                return SubmissionClaim(
                    manifest,
                    token,
                    phase if phase in {"saving", "finalizing"} else "uploading",
                    draft_item_id,
                )
        except ApprovalStateError:
            raise
        except sqlite3.Error as error:
            raise ApprovalStateError("could not claim approved submission") from error

    def complete_submission(
        self, claim: SubmissionClaim, reference: str, now: int | None = None
    ) -> bool:
        return self._finish_submission(claim, "submitted", reference, None, now)

    def record_submission_draft(
        self, claim: SubmissionClaim, item_id: int, now: int | None = None
    ) -> SubmissionClaim | None:
        if (
            not isinstance(claim, SubmissionClaim)
            or claim.phase != "uploading"
            or not _LEASE_TOKEN.fullmatch(claim.lease_token)
            or not isinstance(item_id, int)
            or isinstance(item_id, bool)
            or item_id <= 0
        ):
            raise ApprovalStateError("submission draft is invalid")
        moment = _now(now)
        try:
            with self._connect() as connection:
                updated = connection.execute(
                    "UPDATE submissions SET status = 'saving', draft_item_id = ?, updated_at = ? "
                    "WHERE event_id = ? AND status = 'uploading' AND lease_token = ? "
                    "AND lease_expires_at > ?",
                    (item_id, moment, claim.manifest.event.event_id, claim.lease_token, moment),
                ).rowcount
                if updated != 1:
                    return None
                return SubmissionClaim(claim.manifest, claim.lease_token, "saving", item_id)
        except sqlite3.Error as error:
            raise ApprovalStateError("could not persist submission draft") from error

    def fail_submission(
        self, claim: SubmissionClaim, error_code: str, now: int | None = None
    ) -> bool:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
            raise ApprovalStateError("submission error code is invalid")
        return self._finish_submission(claim, "failed", None, error_code, now)

    def record_submission_finalizing(
        self, claim: SubmissionClaim, now: int | None = None
    ) -> SubmissionClaim | None:
        if (
            not isinstance(claim, SubmissionClaim)
            or claim.phase != "saving"
            or claim.draft_item_id is None
        ):
            raise ApprovalStateError("submission finalization is invalid")
        moment = _now(now)
        try:
            with self._connect() as connection:
                updated = connection.execute(
                    "UPDATE submissions SET status = 'finalizing', updated_at = ? WHERE event_id = ? "
                    "AND status = 'saving' AND lease_token = ? AND lease_expires_at > ?",
                    (moment, claim.manifest.event.event_id, claim.lease_token, moment),
                ).rowcount
                if updated != 1:
                    return None
                return SubmissionClaim(
                    claim.manifest, claim.lease_token, "finalizing", claim.draft_item_id
                )
        except sqlite3.Error as error:
            raise ApprovalStateError("could not persist submission finalization") from error

    def pending_submission_notification(self) -> SubmissionNotification | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT s.payload, s.manifest_digest, o.payload, r.payload FROM submission_outbox o "
                    "JOIN submissions s ON s.event_id = o.event_id "
                    "JOIN requests r ON r.event_id = s.event_id WHERE o.delivered_at IS NULL "
                    "ORDER BY o.created_at, o.event_id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                manifest = _manifest_from_json(
                    str(row[0]), str(row[1]), _event_from_json(str(row[3]))
                )
                payload = json.loads(str(row[2]))
                if not isinstance(payload, dict) or set(payload) != {"reference", "status"}:
                    raise ApprovalStateError("stored submission completion is corrupt")
                status, reference = payload["status"], payload["reference"]
                if status not in {"submitted", "failed"} or (
                    reference is not None and not isinstance(reference, str)
                ):
                    raise ApprovalStateError("stored submission completion is corrupt")
                return SubmissionNotification(manifest, status, reference)
        except (ValueError, sqlite3.Error) as error:
            raise ApprovalStateError("could not read submission completion") from error

    def mark_submission_notification_delivered(
        self, notification: SubmissionNotification, now: int | None = None
    ) -> bool:
        if not isinstance(notification, SubmissionNotification):
            raise ApprovalStateError("submission completion is invalid")
        try:
            with self._connect() as connection:
                return bool(
                    connection.execute(
                        "UPDATE submission_outbox SET delivered_at = ? WHERE event_id = ? "
                        "AND delivered_at IS NULL",
                        (_now(now), notification.manifest.event.event_id),
                    ).rowcount
                )
        except sqlite3.Error as error:
            raise ApprovalStateError("could not record submission notification") from error

    def fail_execution(self, claim: WorkClaim, error_code: str, now: int | None = None) -> bool:
        if claim.item.status != "ready" or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ApprovalStateError("work claim cannot fail execution")
        return self._finish_claim(
            claim,
            "status = 'failed', available_at = ?, error_code = ?",
            (_now(now), error_code),
            "fail execution",
            now,
        )

    def mark_cleaned(self, claim: WorkClaim, now: int | None = None) -> bool:
        if claim.item.status not in {"ready", "failed"} or claim.item.lab_handle is None:
            raise ApprovalStateError("work claim cannot become cleaned")
        return self._finish_claim(
            claim,
            "status = 'cleaned', available_at = ?, error_code = NULL",
            (_now(now),),
            "mark work cleaned",
            now,
        )

    def retry_work(
        self,
        claim: WorkClaim,
        error_code: str,
        delay_seconds: int,
        now: int | None = None,
        *,
        exhaustible: bool = True,
    ) -> bool:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ApprovalStateError("work error code is invalid")
        if (
            not isinstance(delay_seconds, int)
            or isinstance(delay_seconds, bool)
            or not 1 <= delay_seconds <= 3600
        ):
            raise ApprovalStateError("work retry delay is invalid")
        if not isinstance(exhaustible, bool):
            raise ApprovalStateError("work retry policy is invalid")
        moment = _now(now)
        if exhaustible and claim.item.attempts >= _MAX_WORK_ATTEMPTS:
            assignment = "status = 'failed', available_at = ?, error_code = ?"
        else:
            assignment = "available_at = ?, error_code = ?"
        return self._finish_claim(
            claim,
            assignment,
            (moment + delay_seconds, error_code),
            "defer approved work",
            now,
        )

    def fail_work(self, claim: WorkClaim, error_code: str, now: int | None = None) -> bool:
        if claim.item.status != "pending" or claim.item.lab_handle is not None:
            raise ApprovalStateError("work claim cannot fail before provisioning")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ApprovalStateError("work error code is invalid")
        return self._finish_claim(
            claim,
            "status = 'failed', available_at = ?, error_code = ?",
            (_now(now), error_code),
            "fail approved work",
            now,
        )

    def work_status(self, task_key: str, revision_digest: str) -> WorkItem | None:
        _validate_identity(task_key, revision_digest)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT w.event_id, w.selected_mode, w.specification_digest, "
                    "w.provision_key, w.status, w.lab_handle, w.attempts, w.error_code, "
                    "r.payload "
                    "FROM work_items w JOIN requests r ON r.event_id = w.event_id "
                    "WHERE r.task_key = ? AND r.revision_digest = ?",
                    (task_key, revision_digest),
                ).fetchone()
                if row is None:
                    return None
                return _work_item(
                    row,
                    _event_from_json(str(row[8])),
                    attempts=_stored_attempts(row[6]),
                )
        except (ValueError, sqlite3.Error) as error:
            raise ApprovalStateError("could not read approved work") from error

    def _finish_claim(
        self,
        claim: WorkClaim,
        assignment: str,
        values: tuple[object, ...],
        action: str,
        now: int | None,
    ) -> bool:
        if not isinstance(claim, WorkClaim) or not _LEASE_TOKEN.fullmatch(claim.lease_token):
            raise ApprovalStateError("work claim is invalid")
        moment = _now(now)
        try:
            with self._connect() as connection:
                updated = connection.execute(
                    f"UPDATE work_items SET {assignment}, lease_owner = NULL, "
                    "lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                    "WHERE event_id = ? AND lease_token = ? AND lease_expires_at > ?",
                    (*values, moment, claim.item.event.event_id, claim.lease_token, moment),
                ).rowcount
                return bool(updated)
        except sqlite3.Error as error:
            raise ApprovalStateError(f"could not {action}") from error

    def _finish_submission(
        self,
        claim: SubmissionClaim,
        status: str,
        reference: str | None,
        error_code: str | None,
        now: int | None,
    ) -> bool:
        if (
            not isinstance(claim, SubmissionClaim)
            or not _LEASE_TOKEN.fullmatch(claim.lease_token)
            or status not in {"submitted", "failed"}
            or (status == "submitted" and (not isinstance(reference, str) or not reference))
        ):
            raise ApprovalStateError("submission completion is invalid")
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                approval = connection.execute(
                    "SELECT decided_by, decided_at FROM submissions WHERE event_id = ?",
                    (claim.manifest.event.event_id,),
                ).fetchone()
                if (
                    approval is None
                    or not isinstance(approval[0], int)
                    or not isinstance(approval[1], int)
                ):
                    raise ApprovalStateError("stored submission approval is corrupt")
                receipt_payload: dict[str, object]
                if status == "submitted":
                    receipt_payload = {
                        "approvedAt": approval[1],
                        "approvedBy": approval[0],
                        "manifestDigest": claim.manifest.manifest_digest,
                        "reference": reference,
                        "submittedAt": moment,
                    }
                else:
                    receipt_payload = {"errorCode": error_code, "failedAt": moment}
                updated = connection.execute(
                    "UPDATE submissions SET status = ?, lease_token = NULL, lease_expires_at = NULL, "
                    "receipt_reference = ?, receipt_payload = ?, error_code = ?, updated_at = ? WHERE event_id = ? "
                    "AND status IN ('uploading','saving','finalizing') AND lease_token = ? AND lease_expires_at > ?",
                    (
                        status,
                        reference,
                        json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")),
                        error_code,
                        moment,
                        claim.manifest.event.event_id,
                        claim.lease_token,
                        moment,
                    ),
                ).rowcount
                if updated:
                    _enqueue_submission_notification(
                        connection, claim.manifest, status, reference, moment
                    )
                connection.execute("COMMIT")
                return bool(updated)
        except sqlite3.Error as error:
            raise ApprovalStateError("could not persist submission completion") from error


def _new_token() -> str:
    token = secrets.token_urlsafe(24)
    if not _TOKEN.fullmatch(token):
        raise ApprovalStateError("could not create approval callback")
    return token


class _StatementText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")


def _plain_statement(statement: str, statement_format: int) -> str:
    if not isinstance(statement, str) or not isinstance(statement_format, int):
        raise ApprovalStateError("submission statement is invalid")
    if statement_format == 1:  # Moodle FORMAT_HTML.
        parser = _StatementText()
        parser.feed(statement)
        parser.close()
        text = "".join(parser.parts)
    else:
        text = statement
    return "\n".join(
        line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ).strip()


def _statement_digest(statement: str, statement_format: int) -> str:
    if (
        not isinstance(statement, str)
        or not isinstance(statement_format, int)
        or statement_format < 0
    ):
        raise ApprovalStateError("submission statement is invalid")
    return sha256(
        b"moodle-submission-statement-v1\0"
        + str(statement_format).encode("ascii")
        + b"\0"
        + statement.encode("utf-8")
    ).hexdigest()


def _submission_manifest(event: NotificationEvent, report_markdown: str) -> SubmissionManifest:
    if event.assignment_id is None:
        raise ApprovalStateError("submission manifest has no exact assignment identity")
    encoded = report_markdown.encode("utf-8")
    if not encoded or len(encoded) > 2 * 1024 * 1024:
        raise ApprovalStateError("submission report is invalid")
    report_digest = sha256(encoded).hexdigest()
    filename = f"autotask-{event.revision_digest.removeprefix('moodle-assignment-v1:')[:16]}.md"
    candidate = {
        "artifacts": [{"filename": filename, "sha256": report_digest, "sizeBytes": len(encoded)}],
        "assignmentId": event.assignment_id,
        "submissionDrafts": event.submission_drafts,
        "requireSubmissionStatement": event.requires_submission_statement,
        "submissionStatement": event.submission_statement,
        "submissionStatementFormat": event.submission_statement_format,
        "submissionStatementDigest": _statement_digest(
            event.submission_statement, event.submission_statement_format
        )
        if event.requires_submission_statement
        else None,
        "submissionStatementPlain": _plain_statement(
            event.submission_statement, event.submission_statement_format
        )
        if event.requires_submission_statement
        else None,
        "reportDigest": report_digest,
        "reportMarkdown": report_markdown,
        "revisionDigest": event.revision_digest,
        "taskKey": event.task_key,
    }
    digest = sha256(
        json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    return SubmissionManifest(event, digest, filename, report_markdown, report_digest)


def _manifest_from_json(payload: str, digest: str, event: NotificationEvent) -> SubmissionManifest:
    try:
        raw = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ApprovalStateError("stored submission manifest is corrupt") from error
    legacy_keys = {
        "artifacts",
        "assignmentId",
        "submissionDrafts",
        "requireSubmissionStatement",
        "manifestDigest",
        "reportDigest",
        "reportMarkdown",
        "revisionDigest",
        "taskKey",
    }
    statement_keys = legacy_keys | {
        "submissionStatement",
        "submissionStatementFormat",
        "submissionStatementDigest",
        "submissionStatementPlain",
    }
    if not isinstance(raw, dict) or set(raw) not in (legacy_keys, statement_keys):
        raise ApprovalStateError("stored submission manifest is corrupt")
    assignment_id = raw["assignmentId"]
    submission_drafts = raw["submissionDrafts"]
    requires_submission_statement = raw["requireSubmissionStatement"]
    artifacts = raw["artifacts"]
    report = raw["reportMarkdown"]
    report_digest = raw["reportDigest"]
    if (
        not isinstance(assignment_id, int)
        or isinstance(assignment_id, bool)
        or assignment_id <= 0
        or not isinstance(submission_drafts, bool)
        or not isinstance(requires_submission_statement, bool)
        or not isinstance(artifacts, list)
        or len(artifacts) != 1
        or not isinstance(artifacts[0], dict)
        or set(artifacts[0]) != {"filename", "sha256", "sizeBytes"}
        or not isinstance(report, str)
        or not isinstance(report_digest, str)
        or not _DIGEST.fullmatch(report_digest)
    ):
        raise ApprovalStateError("stored submission manifest is corrupt")
    if (
        not isinstance(event, NotificationEvent)
        or event.assignment_id != assignment_id
        or event.submission_drafts != submission_drafts
        or event.requires_submission_statement != requires_submission_statement
        or event.task_key != raw["taskKey"]
        or event.revision_digest != raw["revisionDigest"]
    ):
        raise ApprovalStateError("stored submission manifest is corrupt")
    expected_report = sha256(report.encode("utf-8")).hexdigest()
    if expected_report != report_digest:
        raise ApprovalStateError("stored submission manifest is corrupt")
    candidate = {
        "artifacts": artifacts,
        "assignmentId": assignment_id,
        "submissionDrafts": submission_drafts,
        "requireSubmissionStatement": requires_submission_statement,
        "reportDigest": report_digest,
        "reportMarkdown": report,
        "revisionDigest": event.revision_digest,
        "taskKey": event.task_key,
    }
    if set(raw) == statement_keys:
        expected_statement_digest = (
            _statement_digest(event.submission_statement, event.submission_statement_format)
            if requires_submission_statement
            else None
        )
        expected_plain = (
            _plain_statement(event.submission_statement, event.submission_statement_format)
            if requires_submission_statement
            else None
        )
        if (
            raw["submissionStatement"] != event.submission_statement
            or raw["submissionStatementFormat"] != event.submission_statement_format
            or raw["submissionStatementDigest"] != expected_statement_digest
            or raw["submissionStatementPlain"] != expected_plain
        ):
            raise ApprovalStateError("stored submission manifest is corrupt")
        candidate.update(
            {
                "submissionStatement": event.submission_statement,
                "submissionStatementFormat": event.submission_statement_format,
                "submissionStatementDigest": expected_statement_digest,
                "submissionStatementPlain": expected_plain,
            }
        )
    expected_manifest = sha256(
        json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    if raw["manifestDigest"] != digest or digest != expected_manifest:
        raise ApprovalStateError("stored submission manifest is corrupt")
    filename = artifacts[0]["filename"]
    size = artifacts[0]["sizeBytes"]
    if (
        not isinstance(filename, str)
        or filename
        != f"autotask-{event.revision_digest.removeprefix('moodle-assignment-v1:')[:16]}.md"
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size != len(report.encode("utf-8"))
        or artifacts[0]["sha256"] != report_digest
    ):
        raise ApprovalStateError("stored submission manifest is corrupt")
    return SubmissionManifest(event, digest, filename, report, report_digest)


def _enqueue_submission_notification(
    connection: sqlite3.Connection,
    manifest: SubmissionManifest,
    status: str,
    reference: str | None,
    moment: int,
) -> None:
    payload = json.dumps(
        {"reference": reference, "status": status},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    connection.execute(
        "INSERT OR IGNORE INTO submission_outbox(event_id, payload, delivered_at, created_at) "
        "VALUES (?, ?, NULL, ?)",
        (manifest.event.event_id, payload, moment),
    )


def _now(value: int | None) -> int:
    moment = int(time.time()) if value is None else value
    if not isinstance(moment, int) or isinstance(moment, bool) or moment < 0:
        raise ApprovalStateError("approval timestamp is invalid")
    return moment


def _positive_id(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value < 2**63:
        raise ApprovalStateError(f"Telegram {name} identity is invalid")


def _create_submission_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_SUBMISSIONS_SQL)
    connection.execute(_SUBMISSION_CALLBACKS_SQL)
    connection.execute(_SUBMISSION_OUTBOX_SQL)
    connection.execute(_SUBMISSION_OUTBOX_PENDING_INDEX_SQL)


def _migrate_submission_schema_v4(connection: sqlite3.Connection) -> None:
    """Rebuild the CHECK-constrained table without interpreting stored decisions.

    A v4 ``saving`` row is deliberately preserved: recovery still verifies it
    before any mutation.  Any malformed v4 manifest is rejected by the normal
    loader before it can be claimed.
    """
    connection.execute("DROP INDEX submission_outbox_pending_idx")
    connection.execute("ALTER TABLE submission_callbacks RENAME TO submission_callbacks_v4")
    connection.execute("ALTER TABLE submission_outbox RENAME TO submission_outbox_v4")
    connection.execute("ALTER TABLE submissions RENAME TO submissions_v4")
    _create_submission_schema(connection)
    connection.execute(
        "INSERT INTO submissions(event_id, manifest_digest, payload, status, decided_by, "
        "decided_at, lease_token, lease_expires_at, draft_item_id, receipt_reference, error_code, "
        "created_at, updated_at) SELECT event_id, manifest_digest, payload, status, decided_by, "
        "decided_at, lease_token, lease_expires_at, draft_item_id, receipt_reference, error_code, "
        "created_at, updated_at FROM submissions_v4"
    )
    connection.execute(
        "INSERT INTO submission_callbacks SELECT token, event_id, action FROM submission_callbacks_v4"
    )
    connection.execute(
        "INSERT INTO submission_outbox SELECT event_id, payload, delivered_at, created_at FROM submission_outbox_v4"
    )
    connection.execute("DROP TABLE submission_callbacks_v4")
    connection.execute("DROP TABLE submission_outbox_v4")
    connection.execute("DROP TABLE submissions_v4")


def _valid_schema(connection: sqlite3.Connection, version: str) -> bool:
    rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    if rows != [("schema_version", version)]:
        return False
    expected = {
        ("table", "metadata", "metadata", _METADATA_SQL),
        ("table", "requests", "requests", _REQUESTS_SQL),
        ("table", "callbacks", "callbacks", _CALLBACKS_SQL),
        ("table", "telegram_cursor", "telegram_cursor", _CURSOR_SQL),
        ("index", "sqlite_autoindex_metadata_1", "metadata", None),
        ("index", "sqlite_autoindex_requests_1", "requests", None),
        ("index", "sqlite_autoindex_requests_2", "requests", None),
        ("index", "sqlite_autoindex_callbacks_1", "callbacks", None),
        ("index", "sqlite_autoindex_callbacks_2", "callbacks", None),
    }
    if version in {"2", "3", "4", "5", _PREVIOUS_SCHEMA_VERSION, _SCHEMA_VERSION}:
        expected.update(
            {
                (
                    "table",
                    "work_items",
                    "work_items",
                    _WORK_SQL_V2 if version == "2" else _WORK_SQL,
                ),
                ("index", "sqlite_autoindex_work_items_1", "work_items", None),
                ("index", "sqlite_autoindex_work_items_2", "work_items", None),
                (
                    "index",
                    "work_claimable_idx",
                    "work_items",
                    _WORK_CLAIMABLE_INDEX_SQL,
                ),
            }
        )
    if version in {"3", "4", "5", _PREVIOUS_SCHEMA_VERSION, _SCHEMA_VERSION}:
        expected.update(
            {
                ("table", "execution_outbox", "execution_outbox", _OUTBOX_SQL),
                ("index", "sqlite_autoindex_execution_outbox_1", "execution_outbox", None),
                (
                    "index",
                    "execution_outbox_pending_idx",
                    "execution_outbox",
                    _OUTBOX_PENDING_INDEX_SQL,
                ),
            }
        )
    if version in {"4", "5", _PREVIOUS_SCHEMA_VERSION, _SCHEMA_VERSION}:
        expected.update(
            {
                (
                    "table",
                    "submissions",
                    "submissions",
                    _SUBMISSIONS_SQL_V4 if version == "4" else _SUBMISSIONS_SQL,
                ),
                (
                    "table",
                    "submission_callbacks",
                    "submission_callbacks",
                    _SUBMISSION_CALLBACKS_SQL,
                ),
                ("table", "submission_outbox", "submission_outbox", _SUBMISSION_OUTBOX_SQL),
                ("index", "sqlite_autoindex_submissions_1", "submissions", None),
                (
                    "index",
                    "sqlite_autoindex_submission_callbacks_1",
                    "submission_callbacks",
                    None,
                ),
                (
                    "index",
                    "sqlite_autoindex_submission_callbacks_2",
                    "submission_callbacks",
                    None,
                ),
                (
                    "index",
                    "sqlite_autoindex_submission_outbox_1",
                    "submission_outbox",
                    None,
                ),
                (
                    "index",
                    "submission_outbox_pending_idx",
                    "submission_outbox",
                    _SUBMISSION_OUTBOX_PENDING_INDEX_SQL,
                ),
            }
        )
    if version in {_PREVIOUS_SCHEMA_VERSION, _SCHEMA_VERSION}:
        expected.update(
            {
                (
                    "table",
                    "retention_completions",
                    "retention_completions",
                    _RETENTION_COMPLETIONS_SQL,
                ),
                (
                    "index",
                    "sqlite_autoindex_retention_completions_1",
                    "retention_completions",
                    None,
                ),
                (
                    "index",
                    "retention_completions_completed_idx",
                    "retention_completions",
                    _RETENTION_COMPLETIONS_INDEX_SQL,
                ),
            }
        )
    if version == _SCHEMA_VERSION:
        expected.add(
            (
                "table",
                "retention_reconciliation_cursor",
                "retention_reconciliation_cursor",
                _RETENTION_RECONCILIATION_CURSOR_SQL,
            )
        )
    actual = {
        (kind, name, table, sql if isinstance(sql, str) else None)
        for kind, name, table, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','index','view','trigger') AND name != 'sqlite_sequence'"
        )
    }
    if actual != expected:
        return False
    cursor = connection.execute("SELECT singleton, next_update_id FROM telegram_cursor").fetchall()
    reconciliation_cursor = connection.execute(
        "SELECT singleton, completed_at, event_id, target_phase "
        "FROM retention_reconciliation_cursor"
    ).fetchall() if version == _SCHEMA_VERSION else [()]
    reconciliation_cursor_valid = False
    if len(reconciliation_cursor) == 1:
        reconciliation_row = cast(tuple[object, ...], reconciliation_cursor[0])
        if len(reconciliation_row) == 4:
            reconciliation_cursor_valid = (
                reconciliation_row[0] == 1
                and type(reconciliation_row[1]) is int
                and reconciliation_row[1] >= 0
                and isinstance(reconciliation_row[2], str)
                and reconciliation_row[2] != ""
                and reconciliation_row[3] in {"scratch", "evidence"}
            )
    return (
        len(cursor) == 1
        and cursor[0][0] == 1
        and isinstance(cursor[0][1], int)
        and cursor[0][1] >= 0
        and (
            version != _SCHEMA_VERSION
            or reconciliation_cursor == [(1, -1, "", "")]
            or reconciliation_cursor_valid
        )
        and not connection.execute("PRAGMA foreign_key_check").fetchone()
    )


def _enqueue_work(connection: sqlite3.Connection, event: NotificationEvent, moment: int) -> None:
    mode = _select_mode(event)
    specification_digest = Digest.of_json(event.as_dict()).value
    provision_key = _provision_key(event, specification_digest)
    connection.execute(
        "INSERT OR IGNORE INTO work_items(event_id, selected_mode, specification_digest, "
        "provision_key, status, attempts, available_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
        (
            event.event_id,
            mode.value,
            specification_digest,
            provision_key,
            moment,
            moment,
            moment,
        ),
    )


def _select_mode(event: NotificationEvent) -> ExecutionMode:
    filenames = tuple(attachment.filename.lower() for attachment in event.attachments)
    if any(name.endswith((".ova", ".ovf", ".vdi", ".vmdk", ".vhd", ".vhdx")) for name in filenames):
        return ExecutionMode.IN_GUEST
    if any(attachment.is_lab_artifact for attachment in event.attachments):
        return ExecutionMode.HYBRID
    searchable = f"{event.course_name} {event.course_shortname} {event.assignment_title}".lower()
    lab_words = (
        "laboratori",
        "laboratorio",
        "pràctica",
        "práctica",
        "practica",
        "virtual",
    )
    if any(word in searchable for word in lab_words):
        return ExecutionMode.HYBRID
    return ExecutionMode.CENTRAL


def _work_item(row: tuple[object, ...], event: NotificationEvent, attempts: int) -> WorkItem:
    try:
        mode = ExecutionMode(str(row[1]))
        digest = Digest(str(row[2]))
        provision_key = str(row[3])
        status = str(row[4])
        raw_handle = row[5]
        handle = None if raw_handle is None else LabHandle(str(raw_handle))
        error_code = row[7]
    except (TypeError, ValueError) as error:
        raise ApprovalStateError("stored approved work is corrupt") from error
    expected_digest = Digest.of_json(event.as_dict())
    if (
        row[0] != event.event_id
        or mode is ExecutionMode.AUTO
        or mode is not _select_mode(event)
        or digest != expected_digest
        or provision_key != _provision_key(event, expected_digest.value)
        or not _DIGEST.fullmatch(provision_key)
        or status not in {"pending", "lab_pending", "ready", "failed", "cleaned"}
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
        or (
            error_code is not None
            and (
                not isinstance(error_code, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None
            )
        )
        or (status == "lab_pending" and handle is None)
        or (handle is not None and mode is ExecutionMode.CENTRAL)
        or (status == "ready" and mode is not ExecutionMode.CENTRAL and handle is None)
    ):
        raise ApprovalStateError("stored approved work is corrupt")
    return WorkItem(event, mode, digest, provision_key, status, handle, attempts, error_code)


def _stored_attempts(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ApprovalStateError("stored approved work is corrupt")
    return value


def _provision_key(event: NotificationEvent, specification_digest: str) -> str:
    return sha256(
        f"moodle-work-provision-v1\0{event.event_id}\0{specification_digest}".encode()
    ).hexdigest()


def _validate_execution_provenance(value: dict[str, object]) -> None:
    """Strict decoder: execution outcomes never accept caller-defined metadata."""
    if value.get("kind") == lab_protocol.LAB_PROVENANCE_KIND:
        try:
            lab_protocol.validate_provenance(value)
        except lab_protocol.LabProtocolError as error:
            raise ApprovalStateError("execution provenance is invalid") from error
        return
    if value.get("kind") == "moodle-central-provenance-v3":
        try:
            central_protocol.validate_terminal_provenance(value)
        except central_protocol.CentralProtocolError as error:
            raise ApprovalStateError("execution provenance is invalid") from error
        return
    required = {
        "kind",
        "roles",
        "jobIds",
        "selectedMode",
        "specificationDigest",
        "preparedInputManifestDigest",
        "plannerJobId",
        "executorJobId",
        "reviewerJobId",
        "planDigest",
        "plannerResultDigest",
        "executorResultDigest",
        "artifactManifestDigest",
        "artifactBundleDigest",
        "reviewerResultDigest",
        "reviewerAccepted",
        "bundleLocator",
        "artifactManifest",
    }
    if set(value) != required or value.get("kind") != "moodle-central-provenance-v2":
        raise ApprovalStateError("execution provenance is invalid")
    if value.get("roles") != ["central_planner", "central_executor", "central_reviewer"]:
        raise ApprovalStateError("execution provenance is invalid")
    job_ids = value.get("jobIds")
    if (
        not isinstance(job_ids, list)
        or len(job_ids) != 3
        or not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in job_ids)
    ):
        raise ApprovalStateError("execution provenance is invalid")
    if len(set(job_ids)) != 3 or job_ids != [
        value.get("plannerJobId"),
        value.get("executorJobId"),
        value.get("reviewerJobId"),
    ]:
        raise ApprovalStateError("execution provenance is invalid")
    if value.get("selectedMode") != "central" or value.get("reviewerAccepted") is not True:
        raise ApprovalStateError("execution provenance is invalid")
    digest_fields = required - {
        "kind",
        "roles",
        "jobIds",
        "selectedMode",
        "reviewerAccepted",
        "bundleLocator",
        "artifactManifest",
    }
    if not all(
        isinstance(value.get(field), str) and _DIGEST.fullmatch(str(value[field]))
        for field in digest_fields
    ):
        raise ApprovalStateError("execution provenance is invalid")
    bundle = value.get("bundleLocator")
    if bundle != f"bundles/{value['artifactBundleDigest']}.zip":
        raise ApprovalStateError("execution provenance is invalid")
    manifest = value.get("artifactManifest")
    if not isinstance(manifest, dict):
        raise ApprovalStateError("execution provenance is invalid")
    _validate_outcome_manifest(manifest)
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if sha256(canonical).hexdigest() != value["artifactManifestDigest"]:
        raise ApprovalStateError("execution provenance is invalid")


def _validate_execution_provenance_binding(
    provenance: dict[str, object], item: WorkItem
) -> None:
    expected: dict[str, object] = {
        "selectedMode": item.selected_mode.value,
        "specificationDigest": item.specification_digest.value,
        "taskKey": item.event.task_key,
        "revisionDigest": item.event.revision_digest,
        "eventId": item.event.event_id,
    }
    if any(
        key in provenance and provenance.get(key) != expected_value
        for key, expected_value in expected.items()
    ):
        raise ApprovalStateError("execution provenance is invalid")


def _validate_execution_provenance_outcome(
    provenance: dict[str, object], succeeded: bool
) -> None:
    kind = provenance.get("kind")
    if kind == "moodle-central-provenance-v2":
        matches = succeeded
    elif kind == "moodle-central-provenance-v3":
        matches = not succeeded
    elif kind == lab_protocol.LAB_PROVENANCE_KIND:
        matches = (provenance.get("terminalStatus") == "succeeded") is succeeded
    else:
        matches = False
    if not matches:
        raise ApprovalStateError("execution provenance is invalid")


def _retention_reconciliation_cursor(connection: sqlite3.Connection) -> tuple[int, str, str]:
    rows = connection.execute(
        "SELECT completed_at, event_id, target_phase FROM retention_reconciliation_cursor "
        "WHERE singleton = 1"
    ).fetchall()
    if len(rows) != 1:
        raise ApprovalStateError("retention reconciliation cursor is corrupt")
    row = rows[0]
    if len(row) != 3:
        raise ApprovalStateError("retention reconciliation cursor is corrupt")
    completed_at, event_id, target_phase = row
    if (
        type(completed_at) is not int
        or not isinstance(event_id, str)
        or target_phase not in {"", "scratch", "evidence"}
        or (
            (completed_at == -1 and (event_id != "" or target_phase != ""))
            or (completed_at >= 0 and (not event_id or target_phase == ""))
            or completed_at < -1
        )
    ):
        raise ApprovalStateError("retention reconciliation cursor is corrupt")
    return completed_at, event_id, target_phase


def _retention_completion_receipt(row: tuple[object, ...]) -> RetentionCompletionReceipt:
    if len(row) != 4:
        raise ApprovalStateError("retention completion is corrupt")
    tombstone_id, completed_at, event_id, target_phase = row
    if (
        not isinstance(tombstone_id, str)
        or _DIGEST.fullmatch(tombstone_id) is None
        or type(completed_at) is not int
        or completed_at < 0
        or not isinstance(event_id, str)
        or not event_id.startswith("moodle-notification-event-v1:")
        or _DIGEST.fullmatch(event_id.removeprefix("moodle-notification-event-v1:")) is None
        or target_phase not in {"scratch", "evidence"}
    ):
        raise ApprovalStateError("retention completion is corrupt")
    return RetentionCompletionReceipt(
        tombstone_id, completed_at, event_id, target_phase
    )


def item_key(receipt: RetentionCompletionReceipt) -> tuple[int, str, str]:
    return receipt.completed_at, receipt.event_id, receipt.target_phase


def _retention_record(
    row: tuple[object, ...], now: int, scratch_ttl: int, evidence_ttl: int
) -> RetentionRecord:
    if len(row) != 10:
        raise ApprovalStateError("retention record is corrupt")
    (
        event_id,
        task_key,
        revision_digest,
        event_payload,
        mode,
        status,
        payload,
        created,
        delivered,
        specification_digest,
    ) = row
    if (
        not isinstance(event_id, str)
        or not isinstance(task_key, str)
        or not isinstance(revision_digest, str)
        or not isinstance(event_payload, str)
        or mode not in {item.value for item in ExecutionMode}
        or status not in {"pending", "lab_pending", "ready", "failed", "cleaned"}
        or not isinstance(payload, str)
        or not isinstance(specification_digest, str)
        or _DIGEST.fullmatch(specification_digest) is None
        or type(created) is not int
        or created < 0
        or (delivered is not None and (type(delivered) is not int or delivered < created))
    ):
        raise ApprovalStateError("retention record is corrupt")
    event = _event_from_json(event_payload)
    if (event.event_id, event.task_key, event.revision_digest) != (event_id, task_key, revision_digest):
        raise ApprovalStateError("retention record is corrupt")
    try:
        outcome = json.loads(payload, object_pairs_hook=_retention_json_object)
    except json.JSONDecodeError as error:
        raise ApprovalStateError("retention record is corrupt") from error
    if not isinstance(outcome, dict) or any(not isinstance(key, str) for key in outcome):
        raise ApprovalStateError("retention record is corrupt")
    required = {"succeeded", "summary", "reportMarkdown"}
    if not required <= set(outcome) or not isinstance(outcome.get("succeeded"), bool):
        raise ApprovalStateError("retention record is corrupt")
    succeeded = cast(bool, outcome["succeeded"])
    job_ids: tuple[str, ...] = ()
    bundle_digest: str | None = None
    execution_family = "central"
    barrier_ids: tuple[str, ...] = ()
    dispatch_id: str | None = None
    dispatch_digest: str | None = None
    result_digests: tuple[str, ...] = ()
    provenance: dict[str, object] | None = None
    if mode == ExecutionMode.CENTRAL.value and succeeded:
        if set(outcome) != required | {"kind", "provenance"}:
            raise ApprovalStateError("retention record is corrupt")
        if outcome.get("kind") != "moodle-execution-outcome-v2" or not isinstance(
            outcome.get("provenance"), dict
        ):
            raise ApprovalStateError("retention record is corrupt")
        provenance = cast(dict[str, object], outcome["provenance"])
        _validate_execution_provenance(provenance)
        if provenance.get("specificationDigest") != specification_digest:
            raise ApprovalStateError("retention record is corrupt")
        job_ids = tuple(cast(list[str], provenance["jobIds"]))
        result_digests = (
            cast(str, provenance["plannerResultDigest"]),
            cast(str, provenance["executorResultDigest"]),
            cast(str, provenance["reviewerResultDigest"]),
        )
        bundle_digest = cast(str, provenance["artifactBundleDigest"])
    elif mode == ExecutionMode.CENTRAL.value and not succeeded:
        if set(outcome) == required:
            pass
        elif (
            set(outcome) == required | {"kind", "provenance"}
            and outcome.get("kind") == "moodle-execution-outcome-v2"
            and isinstance(outcome.get("provenance"), dict)
        ):
            provenance = cast(dict[str, object], outcome["provenance"])
            _validate_execution_provenance(provenance)
            if provenance.get("kind") != "moodle-central-provenance-v3":
                raise ApprovalStateError("retention record is corrupt")
            if (
                provenance.get("eventId") != event_id
                or provenance.get("taskKey") != task_key
                or provenance.get("revisionDigest") != revision_digest
                or provenance.get("specificationDigest") != specification_digest
            ):
                raise ApprovalStateError("retention record is corrupt")
            job_ids = tuple(cast(list[str], provenance["jobIds"]))
            result_digests = tuple(cast(list[str], provenance["resultDigests"]))
            bundle = provenance.get("artifactBundleDigest")
            bundle_digest = cast(str | None, bundle)
        else:
            raise ApprovalStateError("retention record is corrupt")
    elif mode in {ExecutionMode.IN_GUEST.value, ExecutionMode.HYBRID.value}:
        if (
            set(outcome) != required | {"kind", "provenance"}
            or outcome.get("kind") != "moodle-execution-outcome-v2"
            or not isinstance(outcome.get("provenance"), dict)
        ):
            raise ApprovalStateError("retention record is corrupt")
        provenance = cast(dict[str, object], outcome["provenance"])
        _validate_execution_provenance(provenance)
        if (
            provenance.get("kind") != lab_protocol.LAB_PROVENANCE_KIND
            or provenance.get("selectedMode") != mode
            or provenance.get("taskKey") != task_key
            or provenance.get("revisionDigest") != revision_digest
            or provenance.get("specificationDigest") != specification_digest
        ):
            raise ApprovalStateError("retention record is corrupt")
        execution_family = "lab"
        job_ids = tuple(cast(list[str], provenance["jobIds"]))
        result_digests = tuple(cast(list[str], provenance["resultDigests"]))
        barrier_ids = tuple(cast(list[str], provenance["barrierIds"]))
        dispatch = provenance.get("dispatch")
        if dispatch is not None:
            dispatch_record = cast(dict[str, object], dispatch)
            dispatch_id = cast(str, dispatch_record["dispatchId"])
            dispatch_digest = cast(str, dispatch_record["dispatchDigest"])
    elif set(outcome) != required:
        raise ApprovalStateError("retention record is corrupt")
    valid_completion = (
        (mode == ExecutionMode.CENTRAL.value and status == "cleaned")
        or (
            mode in {ExecutionMode.IN_GUEST.value, ExecutionMode.HYBRID.value}
            and status in ({"ready", "cleaned"} if succeeded else {"failed", "cleaned"})
        )
    )
    if not valid_completion:
        raise ApprovalStateError("retention record is corrupt")
    if provenance is not None:
        _validate_execution_provenance_outcome(provenance, succeeded)
    scratch_at = created + scratch_ttl
    evidence_at = None if delivered is None else delivered + evidence_ttl
    return RetentionRecord(
        event_id,
        task_key,
        revision_digest,
        ExecutionMode(mode),
        status,
        succeeded,
        created,
        delivered,
        scratch_at,
        evidence_at,
        job_ids,
        bundle_digest,
        execution_family,
        barrier_ids,
        dispatch_id,
        dispatch_digest,
        result_digests,
    )


def _retention_json_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if not isinstance(key, str) or key in value:
            raise ApprovalStateError("retention record is corrupt")
        value[key] = item
    return value


def _validate_outcome_manifest(manifest: dict[str, object]) -> None:
    if (
        set(manifest) != {"kind", "files", "totals"}
        or manifest.get("kind") != "artifact-manifest-v1"
    ):
        raise ApprovalStateError("execution provenance is invalid")
    files, totals = manifest.get("files"), manifest.get("totals")
    if (
        not isinstance(files, list)
        or not 1 <= len(files) <= 64
        or not isinstance(totals, dict)
        or set(totals) != {"files", "bytes"}
    ):
        raise ApprovalStateError("execution provenance is invalid")
    previous = b""
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ApprovalStateError("execution provenance is invalid")
        path, size, digest = item.get("path"), item.get("size"), item.get("sha256")
        parts = path.split("/") if isinstance(path, str) else []
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or len(path.encode("utf-8")) > 240
            or not 1 <= len(parts) <= 8
            or any(not part or part in {".", ".."} for part in parts)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            raise ApprovalStateError("execution provenance is invalid")
        encoded = path.encode("utf-8")
        key = unicodedata.normalize("NFC", path).casefold()
        if encoded <= previous or key in seen:
            raise ApprovalStateError("execution provenance is invalid")
        previous = encoded
        seen.add(key)
        total += size
    if total > 1_900_000 or totals != {"files": len(files), "bytes": total}:
        raise ApprovalStateError("execution provenance is invalid")


def _assert_safe_path(path: Path) -> None:
    try:
        assert_no_indirection(path)
    except ValueError as error:
        raise ApprovalStateError("approval state path is unsafe") from error
