"""Database connection factory: local SQLite by default, Turso Cloud when configured.

Turso credentials are server-side secrets. Never expose them to browser code.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _credentials() -> tuple[str, str]:
    url = os.getenv("TURSO_DATABASE_URL", "").strip()
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if bool(url) != bool(token):
        raise RuntimeError(
            "Turso storage is only partially configured. Set both "
            "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or remove both to use local SQLite."
        )
    return url, token


def storage_backend() -> str:
    """Return the configured persistence backend without exposing credentials."""
    url, token = _credentials()
    return "TURSO_CLOUD_SQLITE" if url and token else "LOCAL_SQLITE"


class _HybridRow(dict):
    """Mapping row that supports both sqlite3.Row access styles: row['name'] and row[0]."""

    def __init__(self, description: Any, values: Any):
        names = [column[0] for column in (description or ())]
        if isinstance(values, Mapping):
            ordered = [values.get(name) for name in names] if names else list(values.values())
        else:
            ordered = list(values)
        self._values = tuple(ordered)
        super().__init__((name, value) for name, value in zip(names, ordered))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class _CursorAdapter:
    def __init__(self, cursor: Any, connection: "_ConnectionAdapter | None" = None):
        self._cursor = cursor
        self._connection = connection

    def execute(self, sql: str, parameters: Any = ()):
        if self._connection is None:
            self._cursor.execute(sql, parameters)
        else:
            result = self._connection._execute_with_stream_recovery(
                self._cursor.execute, sql, parameters
            )
            if result is not None:
                self._cursor = result
        return self

    def executemany(self, sql: str, seq_of_parameters: Any):
        if self._connection is None:
            self._cursor.executemany(sql, seq_of_parameters)
        else:
            result = self._connection._execute_with_stream_recovery(
                self._cursor.executemany, sql, seq_of_parameters
            )
            if result is not None:
                self._cursor = result
        return self

    def _row(self, values: Any) -> Any:
        if values is None or isinstance(values, _HybridRow):
            return values
        description = getattr(self._cursor, "description", None)
        if isinstance(values, Mapping):
            return _HybridRow(description, values)
        if description:
            return _HybridRow(description, values)
        return values

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchmany(self, size: int = 1):
        return [self._row(row) for row in self._cursor.fetchmany(size)]

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield self._row(row)

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


def _is_expired_stream_error(error: BaseException) -> bool:
    """Return True only for Turso's explicit expired/unknown stream response."""
    message = str(error).lower()
    return (
        "404" in message
        and "stream" in message
        and ("not found" in message or "expired" in message)
    )


class _ConnectionAdapter:
    """Small DB-API adapter with safe recovery from idle Turso HTTP streams."""

    def __init__(self, connection: Any):
        self._connection = connection

    def _execute_with_stream_recovery(self, operation, *args, **kwargs):
        # Replaying a statement after a lost stream is safe only when no
        # transaction was open before the request. Turso resets an expired
        # stream (and rolls back its transaction) before returning HTTP 404.
        # In a transaction we must surface the failure to the caller instead
        # of replaying only part of a multi-statement unit of work.
        try:
            was_in_transaction = bool(self._connection.in_transaction)
        except Exception:
            was_in_transaction = True

        try:
            return operation(*args, **kwargs)
        except Exception as error:
            if was_in_transaction or not _is_expired_stream_error(error):
                raise
            # The turso_serverless session clears its stale baton on HTTP
            # errors. The same connection can therefore open a fresh stream
            # when the exact same, not-yet-executed statement is retried.
            return operation(*args, **kwargs)

    def execute(self, sql: str, parameters: Any = ()):
        cursor = self._execute_with_stream_recovery(
            self._connection.execute, sql, parameters
        )
        return _CursorAdapter(cursor, self)

    def cursor(self, *args, **kwargs):
        return _CursorAdapter(self._connection.cursor(*args, **kwargs), self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def connect_database(path: str | None = None):
    """Open Turso Cloud over HTTP when configured; otherwise retain local SQLite.

    Explicit ':memory:' paths always stay local so isolated tests remain isolated.
    """
    if path == ":memory:":
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    url, token = _credentials()
    if url and token:
        try:
            import turso_serverless
        except ImportError as exc:
            raise RuntimeError(
                "Turso credentials are configured but turso_serverless is not installed."
            ) from exc
        remote = turso_serverless.connect(url, auth_token=token)
        return _ConnectionAdapter(remote)

    db_path = path or os.getenv("CSR_DB_PATH", "data/crypto_super_radar.sqlite3")
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection
