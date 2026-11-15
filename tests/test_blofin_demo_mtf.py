from __future__ import annotations
import unittest
from crypto_super_radar.web_app import _blofin_mtf_execution_signal

M15 = 15 * 60 * 1000
H1 = 60 * 60 * 1000

def rows(interval, now, direction, count=100, incomplete=False):
    latest = now - interval - 60_000
    result = []
    for i in range(count):
        ts = latest - (count - 1 - i) * interval
        close = (100.0 + i * (0.30 if interval == M15 else 0.8)) if direction == "LONG" else (200.0 - i * (0.30 if interval == M15 else 0.8))
        op = close - 0.05 if direction == "LONG" else close + 0.05
        result.append([str(ts), str(op), str(close + 0.25), str(close - 0.25), str(close), "10", "10", "1000", "1"])
    if incomplete:
        result.append([str(latest + interval), "100", "101", "99", "100.5", "10", "10", "1000", "0"])
    return list(reversed(result))

class FakeCandles:
    def __init__(self, now, direction="LONG", stale=False):
        self.now, self.direction, self.stale, self.calls = now, direction, stale, []
    def get_candles(self, inst_id, *, bar="1m", limit=100):
        self.calls.append((inst_id, bar, limit))
        interval = M15 if bar == "15m" else H1
        data = rows(interval, self.now, self.direction, incomplete=(bar == "15m"))
        if bar == "15m" and self.stale:
            for row in data:
                row[0] = str(int(row[0]) - 3 * M15)
        return data

class BloFinMtfTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000_000

    def test_long_uses_completed_15m_and_1h_trend(self):
        client = FakeCandles(self.now)
        r = _blofin_mtf_execution_signal(client, desired_direction="LONG", now_ms=self.now)
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(r["signal_id"], f"BTC-USDT|15m|{r['signal_bar_open_ms']}")
        self.assertLess(r["stop_loss"], r["entry_price"])
        self.assertGreater(r["take_profit"], r["entry_price"])
        self.assertEqual([x[1] for x in client.calls], ["15m", "1H"])

    def test_short_levels_are_directionally_valid(self):
        r = _blofin_mtf_execution_signal(FakeCandles(self.now, "SHORT"), desired_direction="SHORT", now_ms=self.now)
        self.assertEqual(r["status"], "PASS")
        self.assertLess(r["take_profit"], r["entry_price"])
        self.assertGreater(r["stop_loss"], r["entry_price"])

    def test_daily_mismatch_blocks_entry(self):
        r = _blofin_mtf_execution_signal(FakeCandles(self.now, "LONG"), desired_direction="SHORT", now_ms=self.now)
        self.assertEqual(r["status"], "BLOCKED")
        self.assertEqual(r["hourly_direction"], "LONG")

    def test_stale_15m_blocks_entry(self):
        r = _blofin_mtf_execution_signal(FakeCandles(self.now, stale=True), desired_direction="LONG", now_ms=self.now)
        self.assertEqual(r["status"], "BLOCKED")
        self.assertIn("کندل ۱۵ دقیقه‌ای قدیمی", r["reason_fa"])

    def test_non_directional_signal_blocks_without_market_calls(self):
        client = FakeCandles(self.now)
        r = _blofin_mtf_execution_signal(client, desired_direction="NO_TRADE", now_ms=self.now)
        self.assertEqual(r["status"], "BLOCKED")
        self.assertEqual(client.calls, [])

    def test_easy_auto_direction_uses_live_15m_and_1h_signal_without_candle_pattern(self):
        client = FakeCandles(self.now, "LONG")
        r = _blofin_mtf_execution_signal(
            client, desired_direction=None, inst_id="BTC-USDT", now_ms=self.now,
        )
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(r["direction"], "LONG")
        self.assertFalse(r["candlestick_pattern_required"])
        self.assertEqual(r["signal_id"], f"BTC-USDT|15m|{r['signal_bar_open_ms']}")

    def test_human_selected_alt_contract_is_used_for_both_timeframes_and_signal_id(self):
        client = FakeCandles(self.now, "SHORT")
        r = _blofin_mtf_execution_signal(
            client, desired_direction=None, inst_id="ETH-USDT", now_ms=self.now,
        )
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(r["instrument_id"], "ETH-USDT")
        self.assertEqual(r["direction"], "SHORT")
        self.assertEqual(r["signal_id"], f"ETH-USDT|15m|{r['signal_bar_open_ms']}")
        self.assertEqual([call[0] for call in client.calls], ["ETH-USDT", "ETH-USDT"])


if __name__ == "__main__":
    unittest.main()
