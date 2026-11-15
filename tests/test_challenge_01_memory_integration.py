from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.challenge_01_runner import (
    Challenge01DataError,
    Challenge01Runner,
    MIN_SAMPLES_PER_ROUND,
)
from crypto_super_radar.blofin_demo_spot_readonly import (
    BloFinDemoSpotReadOnly,
    DEMO_SPOT_REST_URL,
)


def candle(ts: int, opening: str, high: str, low: str, closing: str, confirm: str = "1"):
    return [str(ts), opening, high, low, closing, "10", "10", "10", confirm]


class FakeSpotClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_candles(self, inst_id, *, bar, limit, after=None):
        self.calls.append({"inst_id": inst_id, "bar": bar, "limit": limit, "after": after})
        return self.rows


class Challenge01MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "challenge01.sqlite3")
        self.now_ms = 1_800_000_000_000
        self.rows = [
            candle(self.now_ms - 60 * 60 * 1000 - index * 60 * 60 * 1000,
                   "100", "105", "95", "101" if index % 3 == 0 else ("99" if index % 3 == 1 else "100"))
            for index in range(MIN_SAMPLES_PER_ROUND + 5)
        ]

    def tearDown(self):
        self.tempdir.cleanup()

    def connection_factory(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def runner(self, rows=None):
        return Challenge01Runner(
            client=FakeSpotClient(rows if rows is not None else self.rows),
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            now_ms=lambda: self.now_ms,
        )

    def test_real_memory_write_close_reopen_and_verify_100_candles(self):
        result = self.runner().run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "BASELINE_RECORDED")
        self.assertEqual(result["sample_count"], 100)
        self.assertTrue(result["memory_recovery_verified"])
        self.assertEqual(result["memory_records_verified"], 100)
        self.assertEqual(result["memory_integrity"], "PASS")
        self.assertFalse(result["orders_submitted"])

    def test_open_latest_candle_is_excluded_and_completed_candles_are_used(self):
        rows = [candle(self.now_ms - 5 * 60 * 1000, "100", "106", "99", "105", "0")] + self.rows
        result = self.runner(rows).run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "BASELINE_RECORDED")
        self.assertEqual(result["sample_count"], 100)

    def test_duplicate_timestamp_is_recorded_unknown_and_never_signals(self):
        rows = list(self.rows)
        rows[2] = list(rows[1])
        result = self.runner(rows).run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "DUPLICATE_CANDLE_TIMESTAMP")
        self.assertFalse(result["orders_submitted"])

    def test_reversed_timestamp_order_is_recorded_unknown(self):
        rows = list(reversed(self.rows))
        result = self.runner(rows).run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "CANDLE_ORDER_NOT_NEWEST_FIRST")

    def test_inconsistent_ohlc_is_rejected(self):
        rows = list(self.rows)
        rows[0] = candle(self.now_ms - 60 * 60 * 1000, "100", "99", "95", "101")
        result = self.runner(rows).run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "INCONSISTENT_HIGH")

    def test_stale_latest_completed_candle_is_rejected(self):
        stale_now = self.now_ms + 6 * 60 * 60 * 1000
        runner = Challenge01Runner(
            client=FakeSpotClient(self.rows),
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            now_ms=lambda: stale_now,
        )
        result = runner.run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "LATEST_COMPLETED_CANDLE_STALE")

    def test_round_requires_100_samples(self):
        with self.assertRaises(ValueError):
            self.runner().run_bot_only_round(round_number=1, sample_count=99)


class BloFinDemoSpotAdapterTests(unittest.TestCase):
    def test_public_market_data_uses_demo_spot_endpoint_without_auth(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"code": "0", "data": []}

        class Session:
            def __init__(self):
                self.call = None

            def get(self, url, *, params, timeout):
                self.call = (url, params, timeout)
                return Response()

        session = Session()
        client = BloFinDemoSpotReadOnly(session=session)
        self.assertEqual(client.get_candles("BTC-USDT", bar="1H", limit=120), [])
        url, params, _timeout = session.call
        self.assertEqual(url, f"{DEMO_SPOT_REST_URL}/api/v1/spot/market/candles")
        self.assertEqual(params["instType"], "SPOT")
        self.assertEqual(params["instId"], "BTC-USDT")
        self.assertEqual(params["bar"], "1H")


if __name__ == "__main__":
    unittest.main()
