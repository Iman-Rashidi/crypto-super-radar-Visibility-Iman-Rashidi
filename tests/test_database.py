"""Offline tests for the local/Turso database adapter."""
from __future__ import annotations

import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

from crypto_super_radar.database import (
    _ConnectionAdapter,
    connect_database,
    storage_backend,
)


class _FakeRemoteCursor:
    def __init__(self, connection, cursor):
        self.connection = connection
        self.cursor = cursor

    def execute(self, sql, parameters=()):
        self.connection._maybe_fail()
        return self.cursor.execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.connection._maybe_fail()
        return self.cursor.executemany(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class _FakeRemoteConnection:
    def __init__(self):
        self.raw = sqlite3.connect(":memory:")
        self.failures_remaining = 0
        self.execute_calls = 0
        self.cursor_execute_calls = 0

    @property
    def in_transaction(self):
        return self.raw.in_transaction

    def _maybe_fail(self):
        self.execute_calls += 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise sqlite3.OperationalError("HTTP status 404: stream not found")

    def execute(self, sql, parameters=()):
        self._maybe_fail()
        return self.raw.execute(sql, parameters)

    def cursor(self, *args, **kwargs):
        return _FakeRemoteCursor(self, self.raw.cursor(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self.raw, name)


class DatabaseAdapterTests(unittest.TestCase):
    def test_idle_turso_stream_is_retried_once_outside_transactions(self):
        remote = _FakeRemoteConnection()
        remote.failures_remaining = 1
        db = _ConnectionAdapter(remote)

        row = db.execute("SELECT 7 AS value").fetchone()

        self.assertEqual(row["value"], 7)
        self.assertEqual(remote.execute_calls, 2)
        db.close()

    def test_cursor_execute_recovers_from_expired_idle_stream(self):
        remote = _FakeRemoteConnection()
        remote.failures_remaining = 1
        db = _ConnectionAdapter(remote)

        cursor = db.cursor()
        cursor.execute("SELECT 9 AS value")
        row = cursor.fetchone()

        self.assertEqual(row["value"], 9)
        self.assertEqual(remote.execute_calls, 2)
        db.close()

    def test_expired_stream_is_not_retried_inside_transaction(self):
        remote = _FakeRemoteConnection()
        db = _ConnectionAdapter(remote)
        remote.raw.execute("BEGIN")
        remote.failures_remaining = 1

        with self.assertRaisesRegex(sqlite3.OperationalError, "stream not found"):
            db.execute("SELECT 1")

        self.assertEqual(remote.execute_calls, 1)
        db.rollback()
        db.close()

    def test_other_http_errors_are_not_retried(self):
        class UnauthorizedRemote(_FakeRemoteConnection):
            def _maybe_fail(self):
                self.execute_calls += 1
                raise sqlite3.OperationalError("HTTP status 401: unauthorized")

        remote = UnauthorizedRemote()
        db = _ConnectionAdapter(remote)

        with self.assertRaisesRegex(sqlite3.OperationalError, "401"):
            db.execute("SELECT 1")

        self.assertEqual(remote.execute_calls, 1)
        db.close()

    def test_hybrid_rows_support_name_index_and_dict_access(self):
        raw = sqlite3.connect(":memory:")
        db = _ConnectionAdapter(raw)
        db.execute("CREATE TABLE sample (id INTEGER, name TEXT)")
        with db:
            db.execute("INSERT INTO sample VALUES (?, ?)", (7, "radar"))
        row = db.execute("SELECT id, name FROM sample").fetchone()
        self.assertEqual(row["name"], "radar")
        self.assertEqual(row[0], 7)
        self.assertEqual(dict(row), {"id": 7, "name": "radar"})
        db.close()

    def test_local_sqlite_remains_the_default(self):
        env = {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
            "CSR_DB_PATH": ":memory:",
        }
        with patch.dict(os.environ, env, clear=False):
            db = connect_database(":memory:")
            self.assertEqual(storage_backend(), "LOCAL_SQLITE")
            self.assertEqual(db.execute("SELECT 1").fetchone()[0], 1)
            db.close()

    def test_remote_driver_is_selected_when_both_credentials_exist(self):
        def fake_connect(url, auth_token):
            self.assertEqual(url, "turso://test-db.turso.io")
            self.assertEqual(auth_token, "test-token")
            return sqlite3.connect(":memory:")

        fake_module = types.SimpleNamespace(connect=fake_connect)
        env = {
            "TURSO_DATABASE_URL": "turso://test-db.turso.io",
            "TURSO_AUTH_TOKEN": "test-token",
        }
        with patch.dict(os.environ, env, clear=False), patch.dict(
            sys.modules, {"turso_serverless": fake_module}
        ):
            db = connect_database("data/test.sqlite3")
            self.assertEqual(storage_backend(), "TURSO_CLOUD_SQLITE")
            db.execute("CREATE TABLE sample (value INTEGER)")
            with db:
                db.execute("INSERT INTO sample VALUES (9)")
            self.assertEqual(db.execute("SELECT value FROM sample").fetchone()["value"], 9)
            db.close()

    def test_partial_turso_credentials_fail_closed(self):
        env = {"TURSO_DATABASE_URL": "turso://test-db.turso.io", "TURSO_AUTH_TOKEN": ""}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(RuntimeError, "Set both"):
                connect_database("data/test.sqlite3")


if __name__ == "__main__":
    unittest.main()
