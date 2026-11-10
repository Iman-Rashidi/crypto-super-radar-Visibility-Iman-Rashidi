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
    def __init__(self, cursor: Any):
        self._cursor = cursor

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


class _ConnectionAdapter:
    """Small DB-API adapter for sqlite3-style rows and transaction context managers."""

    def __init__(self, connection: Any):
        self._connection = connection

    def execute(self, sql: str, parameters: Any = ()):
        return _CursorAdapter(self._connection.execute(sql, parameters))

    def cursor(self, *args, **kwargs):
        return _CursorAdapter(self._connection.cursor(*args, **kwargs))

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
