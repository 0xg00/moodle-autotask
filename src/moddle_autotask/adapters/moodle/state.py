"""Durable, same-database Moodle acknowledgement and notification outbox state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .path_safety import assert_no_indirection


class MoodleStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    task_key: str
    revision_digest: str


@dataclass(frozen=True, slots=True)
class NotificationAttachment:
    filename: str
    size_bytes: int
    mimetype: str | None
    is_lab_artifact: bool

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str) or not self.filename or len(self.filename) > 255:
            raise ValueError("notification attachment filename is invalid")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ValueError("notification attachment size is invalid")
        if self.mimetype is not None and (
            not isinstance(self.mimetype, str) or len(self.mimetype) > 255
        ):
            raise ValueError("notification attachment mimetype is invalid")
        if not isinstance(self.is_lab_artifact, bool):
            raise ValueError("notification attachment hint is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "mimetype": self.mimetype,
            "is_lab_artifact": self.is_lab_artifact,
        }


@dataclass(frozen=True, slots=True)
class NotificationDraft:
    task_key: str
    revision_digest: str
    course_name: str
    course_shortname: str
    assignment_title: str
    allows_submissions_from: int
    due_date: int
    cutoff_date: int
    grading_due_date: int
    time_modified: int
    attachments: tuple[NotificationAttachment, ...]
    assignment_id: int | None = None
    submission_drafts: bool = False
    requires_submission_statement: bool = False

    def __post_init__(self) -> None:
        _validate_identity(self.task_key, self.revision_digest)
        for text in (self.course_name, self.course_shortname, self.assignment_title):
            if not isinstance(text, str) or not text.strip() or len(text) > 4096:
                raise ValueError("notification text is invalid")
        for value in (
            self.allows_submissions_from,
            self.due_date,
            self.cutoff_date,
            self.grading_due_date,
            self.time_modified,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("notification date is invalid")
        if (
            not isinstance(self.attachments, tuple)
            or not all(isinstance(item, NotificationAttachment) for item in self.attachments)
            or len(self.attachments) > 1000
        ):
            raise ValueError("too many notification attachments")
        if self.assignment_id is not None and (
            not isinstance(self.assignment_id, int)
            or isinstance(self.assignment_id, bool)
            or self.assignment_id <= 0
        ):
            raise ValueError("notification assignment identity is invalid")
        if not isinstance(self.submission_drafts, bool):
            raise ValueError("notification submission draft policy is invalid")
        if not isinstance(self.requires_submission_statement, bool):
            raise ValueError("notification submission statement policy is invalid")


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_id: str
    kind: str
    status: str
    task_key: str
    revision_digest: str
    course_name: str
    course_shortname: str
    assignment_title: str
    allows_submissions_from: int
    due_date: int
    cutoff_date: int
    grading_due_date: int
    time_modified: int
    attachments: tuple[NotificationAttachment, ...]
    assignment_id: int | None = None
    submission_drafts: bool = False
    requires_submission_statement: bool = False

    def __post_init__(self) -> None:
        if not _EVENT_ID.fullmatch(self.event_id):
            raise ValueError("notification event identity is invalid")
        if self.kind != "moodle-notification-v1" or self.status not in {"NEW", "UPDATED"}:
            raise ValueError("notification event kind is invalid")
        _validate_identity(self.task_key, self.revision_digest)
        if self.event_id != _event_id(self.task_key, self.revision_digest):
            raise ValueError("notification event identity is invalid")
        NotificationDraft(
            self.task_key,
            self.revision_digest,
            self.course_name,
            self.course_shortname,
            self.assignment_title,
            self.allows_submissions_from,
            self.due_date,
            self.cutoff_date,
            self.grading_due_date,
            self.time_modified,
            self.attachments,
            self.assignment_id,
            self.submission_drafts,
            self.requires_submission_statement,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "event_id": self.event_id,
            "status": self.status,
            "task_key": self.task_key,
            "revision_digest": self.revision_digest,
            "course_name": self.course_name,
            "course_shortname": self.course_shortname,
            "assignment_title": self.assignment_title,
            "allows_submissions_from": self.allows_submissions_from,
            "due_date": self.due_date,
            "cutoff_date": self.cutoff_date,
            "grading_due_date": self.grading_due_date,
            "time_modified": self.time_modified,
            "attachments": [item.as_dict() for item in self.attachments],
            "assignment_id": self.assignment_id,
            "submission_drafts": self.submission_drafts,
            "requires_submission_statement": self.requires_submission_statement,
        }


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    event: NotificationEvent
    lease_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.event, NotificationEvent) or not _LEASE_TOKEN.fullmatch(
            self.lease_token
        ):
            raise ValueError("outbox claim is invalid")


_TASK_KEY = re.compile(r"^moodle-task-v1:[0-9a-f]{64}$")
_REVISION = re.compile(r"^moodle-assignment-v1:[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^moodle-notification-event-v1:[0-9a-f]{64}$")
_LEASE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SCHEMA_VERSION = "2"
_MAX_ATTEMPTS = 1_000_000
_SchemaObject = tuple[str, str, str, str | None]
_IndexSignature = tuple[str, int, str, int, tuple[str, ...]]


_METADATA_TABLE_SQL = "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_ACKNOWLEDGEMENTS_TABLE_SQL = (
    "CREATE TABLE acknowledgements ("
    "task_key TEXT NOT NULL, revision_digest TEXT NOT NULL, "
    "acknowledged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "PRIMARY KEY (task_key, revision_digest))"
)
_OUTBOX_TABLE_SQL = (
    "CREATE TABLE outbox ("
    "event_id TEXT PRIMARY KEY NOT NULL, task_key TEXT NOT NULL, "
    "revision_digest TEXT NOT NULL, payload TEXT NOT NULL, "
    "delivery_state TEXT NOT NULL CHECK (delivery_state IN "
    "('pending','leased','delivered')), "
    "attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0 AND attempts <= 1000000), "
    "available_at INTEGER NOT NULL, lease_owner TEXT, lease_token TEXT, "
    "lease_expires_at INTEGER, "
    "created_at INTEGER NOT NULL, delivered_at INTEGER, error_code TEXT "
    "CHECK (error_code IS NULL OR error_code IN ('sink_failed','ownership_lost')), "
    "UNIQUE(task_key, revision_digest), "
    "CHECK ((delivery_state = 'leased') = (lease_owner IS NOT NULL "
    "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)), "
    "CHECK ((delivery_state = 'delivered') = (delivered_at IS NOT NULL)))"
)
_OUTBOX_CLAIMABLE_INDEX_SQL = (
    "CREATE INDEX outbox_claimable_idx ON outbox("
    "delivery_state, available_at, lease_expires_at)"
)
_OUTBOX_TASK_INDEX_SQL = "CREATE INDEX outbox_task_idx ON outbox(task_key)"


class MoodleState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 5000")
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
                    self._create_v2(connection)
                else:
                    self._validate_metadata(connection)
                    version = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'schema_version'"
                    ).fetchone()[0]
                    if version == "1":
                        self._validate_v1(connection)
                        self._create_outbox(connection)
                        connection.execute(
                            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                            (_SCHEMA_VERSION,),
                        )
                    elif version == _SCHEMA_VERSION:
                        self._validate_v2(connection)
                    else:
                        raise MoodleStateError("Moodle state schema version is unsupported")
                connection.execute("COMMIT")
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                connection.close()
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except MoodleStateError:
            raise
        except (OSError, sqlite3.Error, IndexError) as error:
            raise MoodleStateError("could not initialize Moodle state") from error

    @staticmethod
    def _validate_metadata(connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT key, value FROM metadata").fetchall()
        if rows != [("schema_version", "1")] and rows != [("schema_version", _SCHEMA_VERSION)]:
            raise MoodleStateError("Moodle state schema is corrupt")

    @staticmethod
    def _validate_v1(connection: sqlite3.Connection) -> None:
        if not _valid_schema_objects(connection, "1"):
            raise MoodleStateError("Moodle state schema is corrupt")

    @staticmethod
    def _validate_v2(connection: sqlite3.Connection) -> None:
        if not _valid_schema_objects(connection, _SCHEMA_VERSION):
            raise MoodleStateError("Moodle state schema is corrupt")

    @staticmethod
    def _create_v2(connection: sqlite3.Connection) -> None:
        connection.execute(_METADATA_TABLE_SQL)
        connection.execute(_ACKNOWLEDGEMENTS_TABLE_SQL)
        MoodleState._create_outbox(connection)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)", (_SCHEMA_VERSION,)
        )

    @staticmethod
    def _create_outbox(connection: sqlite3.Connection) -> None:
        connection.execute(_OUTBOX_TABLE_SQL)
        connection.execute(_OUTBOX_CLAIMABLE_INDEX_SQL)
        connection.execute(_OUTBOX_TASK_INDEX_SQL)

    def status(self, task_key: str, revision_digest: str) -> str | None:
        _validate_identity(task_key, revision_digest)
        try:
            with self._connect() as connection:
                if _is_acknowledged(connection, task_key, revision_digest):
                    return None
                return "UPDATED" if _has_prior_ack(connection, task_key) else "NEW"
        except sqlite3.Error as error:
            raise MoodleStateError("could not read Moodle state") from error

    def acknowledge(self, task_key: str, revision_digest: str) -> Acknowledgement:
        """Operator assertion that exact downstream delivery already succeeded, never approval."""
        _validate_identity(task_key, revision_digest)
        now = int(time.time())
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO acknowledgements(task_key, revision_digest) "
                    "VALUES (?, ?)",
                    (task_key, revision_digest),
                )
                connection.execute(
                    "UPDATE outbox SET delivery_state = 'delivered', delivered_at = ?, "
                    "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
                    "error_code = NULL WHERE task_key = ? AND revision_digest = ? "
                    "AND delivery_state IN ('pending','leased')",
                    (now, task_key, revision_digest),
                )
                connection.execute("COMMIT")
        except sqlite3.Error as error:
            raise MoodleStateError("could not write Moodle state") from error
        return Acknowledgement(task_key, revision_digest)

    def enqueue(self, draft: NotificationDraft, now: int | None = None) -> NotificationEvent | None:
        """Atomically classify and persist an unseen revision, returning its durable event."""
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if _is_suppressed(connection, draft.task_key, draft.revision_digest):
                    connection.execute("COMMIT")
                    return None
                status = "UPDATED" if _has_prior(connection, draft.task_key) else "NEW"
                event = _event_from_draft(draft, status)
                connection.execute(
                    "INSERT INTO outbox(event_id, task_key, revision_digest, payload, "
                    "delivery_state, "
                    "attempts, available_at, created_at) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)",
                    (
                        event.event_id,
                        event.task_key,
                        event.revision_digest,
                        json.dumps(
                            event.as_dict(),
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        moment,
                        moment,
                    ),
                )
                connection.execute("COMMIT")
                return event
        except sqlite3.IntegrityError:
            # A competing writer inserted the exact unique identity first.
            return None
        except sqlite3.Error as error:
            raise MoodleStateError("could not enqueue Moodle notification") from error

    def claim(
        self, owner: str, limit: int, lease_seconds: int, now: int | None = None
    ) -> tuple[OutboxClaim, ...]:
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise MoodleStateError("outbox lease owner is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise MoodleStateError("outbox claim limit is invalid")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 6 <= lease_seconds <= 3600
        ):
            raise MoodleStateError("outbox lease duration is invalid")
        moment = _now(now)
        claims: list[OutboxClaim] = []
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT event_id, task_key, revision_digest, payload FROM outbox "
                    "WHERE attempts < ? AND ((delivery_state = 'pending' AND available_at <= ?) "
                    "OR (delivery_state = 'leased' AND lease_expires_at <= ?)) "
                    "ORDER BY created_at, event_id LIMIT ?",
                    (_MAX_ATTEMPTS, moment, moment, limit),
                ).fetchall()
                for event_id, task_key, revision_digest, payload in rows:
                    token = secrets.token_urlsafe(32)
                    updated = connection.execute(
                        "UPDATE outbox SET delivery_state = 'leased', attempts = attempts + 1, "
                        "lease_owner = ?, lease_token = ?, lease_expires_at = ?, error_code = NULL "
                        "WHERE event_id = ? AND ((delivery_state = 'pending' "
                        "AND available_at <= ?) OR "
                        "(delivery_state = 'leased' AND lease_expires_at <= ?)) AND attempts < ?",
                        (
                            owner,
                            token,
                            moment + lease_seconds,
                            event_id,
                            moment,
                            moment,
                            _MAX_ATTEMPTS,
                        ),
                    ).rowcount
                    if updated:
                        event = _event_from_json(payload)
                        if (
                            event.event_id != event_id
                            or event.task_key != task_key
                            or event.revision_digest != revision_digest
                        ):
                            raise MoodleStateError("stored Moodle notification payload is corrupt")
                        claims.append(OutboxClaim(event, token))
                connection.execute("COMMIT")
        except sqlite3.Error as error:
            raise MoodleStateError("could not claim Moodle notifications") from error
        return tuple(claims)

    def renew(self, claim: OutboxClaim, lease_seconds: int, now: int | None = None) -> bool:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 6 <= lease_seconds <= 3600
        ):
            raise MoodleStateError("outbox lease duration is invalid")
        moment = _now(now)
        return self._owned_update(
            "UPDATE outbox SET lease_expires_at = ? WHERE event_id = ? "
            "AND delivery_state = 'leased' "
            "AND lease_token = ? AND lease_expires_at > ?",
            (moment + lease_seconds, claim.event.event_id, claim.lease_token, moment),
            "renew Moodle notification lease",
        )

    def complete(self, claim: OutboxClaim, now: int | None = None) -> bool:
        """Atomically record successful sink delivery and its exact acknowledgement."""
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    "UPDATE outbox SET delivery_state = 'delivered', delivered_at = ?, "
                    "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
                    "error_code = NULL "
                    "WHERE event_id = ? AND delivery_state = 'leased' AND lease_token = ? "
                    "AND lease_expires_at > ?",
                    (moment, claim.event.event_id, claim.lease_token, moment),
                ).rowcount
                if updated:
                    connection.execute(
                        "INSERT OR IGNORE INTO acknowledgements(task_key, revision_digest) "
                        "VALUES (?, ?)",
                        (claim.event.task_key, claim.event.revision_digest),
                    )
                connection.execute("COMMIT")
                return bool(updated)
        except sqlite3.Error as error:
            raise MoodleStateError("could not complete Moodle notification") from error

    def fail(
        self,
        claim: OutboxClaim,
        retry_base_seconds: int,
        retry_max_seconds: int,
        error_code: str = "sink_failed",
        now: int | None = None,
    ) -> bool:
        if (
            not isinstance(retry_base_seconds, int)
            or isinstance(retry_base_seconds, bool)
            or not isinstance(retry_max_seconds, int)
            or isinstance(retry_max_seconds, bool)
            or not 1 <= retry_base_seconds <= retry_max_seconds <= 86400
        ):
            raise MoodleStateError("outbox retry duration is invalid")
        if error_code not in {"sink_failed", "ownership_lost"}:
            raise MoodleStateError("outbox error code is invalid")
        moment = _now(now)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT attempts FROM outbox WHERE event_id = ? AND delivery_state = 'leased' "
                    "AND lease_token = ? AND lease_expires_at > ?",
                    (claim.event.event_id, claim.lease_token, moment),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return False
                delay = min(retry_max_seconds, retry_base_seconds * (2 ** min(row[0] - 1, 20)))
                connection.execute(
                    "UPDATE outbox SET delivery_state = 'pending', available_at = ?, "
                    "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
                    "error_code = ? WHERE event_id = ?",
                    (moment + delay, error_code, claim.event.event_id),
                )
                connection.execute("COMMIT")
                return True
        except sqlite3.Error as error:
            raise MoodleStateError("could not defer Moodle notification") from error

    def _owned_update(self, sql: str, parameters: tuple[object, ...], action: str) -> bool:
        try:
            with self._connect() as connection:
                return bool(connection.execute(sql, parameters).rowcount)
        except sqlite3.Error as error:
            raise MoodleStateError(f"could not {action}") from error


def _schema_objects(
    connection: sqlite3.Connection,
) -> frozenset[_SchemaObject]:
    return frozenset(
        (kind, name, table, sql if isinstance(sql, str) else None)
        for kind, name, table, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'view', 'trigger')"
        )
    )


def _index_signatures(
    connection: sqlite3.Connection, table: str
) -> frozenset[_IndexSignature]:
    return frozenset(
        (
            row[1],
            row[2],
            row[3],
            row[4],
            tuple(item[2] for item in connection.execute(f"PRAGMA index_info({row[1]})")),
        )
        for row in connection.execute(f"PRAGMA index_list({table})")
    )


def _valid_schema_objects(connection: sqlite3.Connection, version: str) -> bool:
    expected: set[_SchemaObject] = {
        ("table", "metadata", "metadata", _METADATA_TABLE_SQL),
        (
            "table",
            "acknowledgements",
            "acknowledgements",
            _ACKNOWLEDGEMENTS_TABLE_SQL,
        ),
        ("index", "sqlite_autoindex_metadata_1", "metadata", None),
        ("index", "sqlite_autoindex_acknowledgements_1", "acknowledgements", None),
    }
    expected_indexes: dict[str, set[_IndexSignature]] = {
        "metadata": {
            ("sqlite_autoindex_metadata_1", 1, "pk", 0, ("key",)),
        },
        "acknowledgements": {
            ("sqlite_autoindex_acknowledgements_1", 1, "pk", 0, ("task_key", "revision_digest")),
        },
    }
    if version == _SCHEMA_VERSION:
        expected.update(
            {
                ("table", "outbox", "outbox", _OUTBOX_TABLE_SQL),
                (
                    "index",
                    "outbox_claimable_idx",
                    "outbox",
                    _OUTBOX_CLAIMABLE_INDEX_SQL,
                ),
                (
                    "index",
                    "outbox_task_idx",
                    "outbox",
                    _OUTBOX_TASK_INDEX_SQL,
                ),
                ("index", "sqlite_autoindex_outbox_1", "outbox", None),
                ("index", "sqlite_autoindex_outbox_2", "outbox", None),
            }
        )
        expected_indexes["outbox"] = {
            ("outbox_task_idx", 0, "c", 0, ("task_key",)),
            (
                "outbox_claimable_idx",
                0,
                "c",
                0,
                ("delivery_state", "available_at", "lease_expires_at"),
            ),
            ("sqlite_autoindex_outbox_2", 1, "u", 0, ("task_key", "revision_digest")),
            ("sqlite_autoindex_outbox_1", 1, "pk", 0, ("event_id",)),
        }
    return _schema_objects(connection) == frozenset(expected) and all(
        _index_signatures(connection, table) == frozenset(signatures)
        for table, signatures in expected_indexes.items()
    )


def _is_acknowledged(connection: sqlite3.Connection, task_key: str, revision_digest: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM acknowledgements WHERE task_key = ? AND revision_digest = ?",
            (task_key, revision_digest),
        ).fetchone()
    )


def _is_suppressed(connection: sqlite3.Connection, task_key: str, revision_digest: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM acknowledgements WHERE task_key = ? AND revision_digest = ? "
            "UNION ALL SELECT 1 FROM outbox WHERE task_key = ? AND revision_digest = ? LIMIT 1",
            (task_key, revision_digest, task_key, revision_digest),
        ).fetchone()
    )


def _has_prior(connection: sqlite3.Connection, task_key: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM acknowledgements WHERE task_key = ? UNION ALL "
            "SELECT 1 FROM outbox WHERE task_key = ? LIMIT 1",
            (task_key, task_key),
        ).fetchone()
    )


def _has_prior_ack(connection: sqlite3.Connection, task_key: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM acknowledgements WHERE task_key = ?", (task_key,)
        ).fetchone()
    )


def _event_from_draft(draft: NotificationDraft, status: str) -> NotificationEvent:
    if status not in {"NEW", "UPDATED"}:
        raise MoodleStateError("notification status is invalid")
    return NotificationEvent(
        _event_id(draft.task_key, draft.revision_digest),
        "moodle-notification-v1",
        status,
        draft.task_key,
        draft.revision_digest,
        draft.course_name,
        draft.course_shortname,
        draft.assignment_title,
        draft.allows_submissions_from,
        draft.due_date,
        draft.cutoff_date,
        draft.grading_due_date,
        draft.time_modified,
        draft.attachments,
        draft.assignment_id,
        draft.submission_drafts,
        draft.requires_submission_statement,
    )


def _event_id(task_key: str, revision_digest: str) -> str:
    identity = json.dumps(
        {
            "schema": "moodle-notification-event-v1",
            "task_key": task_key,
            "revision_digest": revision_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "moodle-notification-event-v1:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _event_from_json(payload: str) -> NotificationEvent:
    try:
        raw = json.loads(payload)
        required = {
            "kind",
            "event_id",
            "status",
            "task_key",
            "revision_digest",
            "course_name",
            "course_shortname",
            "assignment_title",
            "allows_submissions_from",
            "due_date",
            "cutoff_date",
            "grading_due_date",
            "time_modified",
            "attachments",
        }
        permitted_keys = {
            frozenset(required),
            frozenset(required | {"assignment_id"}),
            frozenset(required | {"assignment_id", "submission_drafts"}),
            frozenset(
                required
                | {"assignment_id", "submission_drafts", "requires_submission_statement"}
            ),
        }
        if not isinstance(raw, dict) or set(raw) not in permitted_keys:
            raise ValueError
        attachments = raw["attachments"]
        if not isinstance(attachments, list) or any(
            not isinstance(item, dict)
            or set(item) != {"filename", "size_bytes", "mimetype", "is_lab_artifact"}
            for item in attachments
        ):
            raise ValueError
        event = NotificationEvent(
            raw["event_id"],
            raw["kind"],
            raw["status"],
            raw["task_key"],
            raw["revision_digest"],
            raw["course_name"],
            raw["course_shortname"],
            raw["assignment_title"],
            raw["allows_submissions_from"],
            raw["due_date"],
            raw["cutoff_date"],
            raw["grading_due_date"],
            raw["time_modified"],
            tuple(NotificationAttachment(**item) for item in attachments),
            raw.get("assignment_id"),
            raw.get("submission_drafts", False),
            raw.get("requires_submission_statement", False),
        )
        if (
            not _EVENT_ID.fullmatch(event.event_id)
            or event.kind != "moodle-notification-v1"
            or event.status not in {"NEW", "UPDATED"}
        ):
            raise ValueError
        return event
    except (TypeError, ValueError, KeyError) as error:
        raise MoodleStateError("stored Moodle notification payload is corrupt") from error


def _now(value: int | None) -> int:
    moment = int(time.time()) if value is None else value
    if not isinstance(moment, int) or isinstance(moment, bool) or moment < 0:
        raise MoodleStateError("outbox timestamp is invalid")
    return moment


def _validate_identity(task_key: str, revision_digest: str) -> None:
    if not _TASK_KEY.fullmatch(task_key) or not _REVISION.fullmatch(revision_digest):
        raise MoodleStateError("Moodle acknowledgement identity is invalid")


def _assert_safe_path(path: Path) -> None:
    try:
        assert_no_indirection(path)
    except ValueError as error:
        raise MoodleStateError("Moodle state path is unsafe") from error
