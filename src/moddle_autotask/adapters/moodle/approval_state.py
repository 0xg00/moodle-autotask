"""Durable human decisions for exact Moodle notification revisions."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

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


_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32}$")
_SCHEMA_VERSION = "1"
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
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                        (_SCHEMA_VERSION,),
                    )
                    connection.execute(
                        "INSERT INTO telegram_cursor(singleton, next_update_id) VALUES (1, 0)"
                    )
                elif not _valid_schema(connection):
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
        except (OSError, sqlite3.Error) as error:
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
                        result = requested
                    elif decision == requested:
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


def _valid_schema(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    if rows != [("schema_version", _SCHEMA_VERSION)]:
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
    actual = {
        (kind, name, table, sql if isinstance(sql, str) else None)
        for kind, name, table, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','index','view','trigger') AND name != 'sqlite_sequence'"
        )
    }
    cursor = connection.execute(
        "SELECT singleton, next_update_id FROM telegram_cursor"
    ).fetchall()
    return (
        actual == expected
        and len(cursor) == 1
        and cursor[0][0] == 1
        and isinstance(cursor[0][1], int)
        and cursor[0][1] >= 0
        and not connection.execute("PRAGMA foreign_key_check").fetchone()
    )


def _assert_safe_path(path: Path) -> None:
    try:
        assert_no_indirection(path)
    except ValueError as error:
        raise ApprovalStateError("approval state path is unsafe") from error
