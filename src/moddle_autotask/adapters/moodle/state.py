"""Safe at-least-once acknowledgement state."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .path_safety import assert_no_indirection


class MoodleStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    task_key: str
    revision_digest: str


_TASK_KEY = re.compile(r"^moodle-task-v1:[0-9a-f]{64}$")
_REVISION = re.compile(r"^moodle-assignment-v1:[0-9a-f]{64}$")


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
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS metadata "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS acknowledgements ("
                    "task_key TEXT NOT NULL, revision_digest TEXT NOT NULL, "
                    "acknowledged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "PRIMARY KEY (task_key, revision_digest))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', '1')"
                )
                if connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone() != ("1",):
                    raise MoodleStateError("Moodle state schema version is unsupported")
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except sqlite3.Error as error:
            raise MoodleStateError("could not initialize Moodle state") from error

    def status(self, task_key: str, revision_digest: str) -> str | None:
        _validate_identity(task_key, revision_digest)
        try:
            with self._connect() as connection:
                exact = connection.execute(
                    "SELECT 1 FROM acknowledgements WHERE task_key = ? AND revision_digest = ?",
                    (task_key, revision_digest),
                ).fetchone()
                if exact:
                    return None
                prior = connection.execute(
                    "SELECT 1 FROM acknowledgements WHERE task_key = ? LIMIT 1", (task_key,)
                ).fetchone()
                return "UPDATED" if prior else "NEW"
        except sqlite3.Error as error:
            raise MoodleStateError("could not read Moodle state") from error

    def acknowledge(self, task_key: str, revision_digest: str) -> Acknowledgement:
        _validate_identity(task_key, revision_digest)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO acknowledgements(task_key, revision_digest) "
                    "VALUES (?, ?)",
                    (task_key, revision_digest),
                )
                connection.execute("COMMIT")
        except sqlite3.Error as error:
            raise MoodleStateError("could not write Moodle state") from error
        return Acknowledgement(task_key, revision_digest)


def _validate_identity(task_key: str, revision_digest: str) -> None:
    if not _TASK_KEY.fullmatch(task_key) or not _REVISION.fullmatch(revision_digest):
        raise MoodleStateError("Moodle acknowledgement identity is invalid")


def _assert_safe_path(path: Path) -> None:
    try:
        assert_no_indirection(path)
    except ValueError as error:
        raise MoodleStateError("Moodle state path is unsafe") from error
