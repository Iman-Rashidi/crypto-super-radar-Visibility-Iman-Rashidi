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


class DatabaseAdapterTests(unittest.TestCase):
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
            self.assertEqual(url, "libsql://test-db.turso.io")
            self.assertEqual(auth_token, "test-token")
            return sqlite3.connect(":memory:")

        fake_module = types.SimpleNamespace(connect=fake_connect)
        env = {
            "TURSO_DATABASE_URL": "libsql://test-db.turso.io",
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
        env = {"TURSO_DATABASE_URL": "libsql://test-db.turso.io", "TURSO_AUTH_TOKEN": ""}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(RuntimeError, "Set both"):
                connect_database("data/test.sqlite3")


if __name__ == "__main__":
    unittest.main()
