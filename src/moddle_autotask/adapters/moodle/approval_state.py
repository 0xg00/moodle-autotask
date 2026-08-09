"""Durable human decisions for exact Moodle notification revisions."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from moddle_autotask.domain.models import Digest, ExecutionMode, LabHandle

from .path_safety import assert_no_indirection
from .state import NotificationEvent, _event_from_json, _validate_identity


class ApprovalStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalButtons:
    approve: str
    ignore: str
    details: str


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


_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32}$")
_SCHEMA_VERSION = "3"
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
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                        (_SCHEMA_VERSION,),
                    )
                    connection.execute(
                        "INSERT INTO telegram_cursor(singleton, next_update_id) VALUES (1, 0)"
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
                        connection.execute(
                            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                            (_SCHEMA_VERSION,),
                        )
                    elif version != (_SCHEMA_VERSION,) or not _valid_schema(
                        connection, _SCHEMA_VERSION
                    ):
                        raise ApprovalStateError("approval state schema is corrupt")
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
        payload = json.dumps(
            {"reportMarkdown": report_markdown, "succeeded": succeeded, "summary": summary},
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

    def pending_execution_notification(self) -> ExecutionNotification | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT r.payload, o.payload FROM execution_outbox o JOIN requests r "
                    "ON r.event_id = o.event_id WHERE o.delivered_at IS NULL "
                    "ORDER BY o.created_at, o.event_id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                event = _event_from_json(str(row[0]))
                payload = json.loads(str(row[1]))
                if not isinstance(payload, dict) or set(payload) != {
                    "reportMarkdown",
                    "succeeded",
                    "summary",
                }:
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
                return ExecutionNotification(event, succeeded, summary, report)
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


def _new_token() -> str:
    token = secrets.token_urlsafe(24)
    if not _TOKEN.fullmatch(token):
        raise ApprovalStateError("could not create approval callback")
    return token


def _now(value: int | None) -> int:
    moment = int(time.time()) if value is None else value
    if not isinstance(moment, int) or isinstance(moment, bool) or moment < 0:
        raise ApprovalStateError("approval timestamp is invalid")
    return moment


def _positive_id(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value < 2**63:
        raise ApprovalStateError(f"Telegram {name} identity is invalid")


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
    if version in {"2", _SCHEMA_VERSION}:
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
    if version == _SCHEMA_VERSION:
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
    actual = {
        (kind, name, table, sql if isinstance(sql, str) else None)
        for kind, name, table, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','index','view','trigger') AND name != 'sqlite_sequence'"
        )
    }
    cursor = connection.execute("SELECT singleton, next_update_id FROM telegram_cursor").fetchall()
    return (
        actual == expected
        and len(cursor) == 1
        and cursor[0][0] == 1
        and isinstance(cursor[0][1], int)
        and cursor[0][1] >= 0
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


def _assert_safe_path(path: Path) -> None:
    try:
        assert_no_indirection(path)
    except ValueError as error:
        raise ApprovalStateError("approval state path is unsafe") from error
