"""Offline tests for GeckoTerminal Pool trade evidence."""
from datetime import datetime, timedelta, timezone
import unittest

from crypto_super_radar.gem_hunter_pool_trades import analyze_pool_trade_evidence


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
TOKEN = "0x" + "1" * 40
POOL = "0x" + "2" * 40


def trade(kind, amount, index, *, wallet=None, minutes_ago=1):
    return {
        "kind": kind,
        "volume_in_usd": str(amount),
        "block_timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
        "tx_hash": "0x" + f"{index:064x}",
        "tx_from_address": wallet or ("0x" + f"{index + 10:040x}"),
    }


class GemHunterPoolTradeTests(unittest.TestCase):
    def analyze(self, rows, liquidity=100_000):
        return analyze_pool_trade_evidence(
            rows, chain="base", token_address=TOKEN,
            pool_address=POOL, liquidity_usd=liquidity, now=NOW,
        )

    def test_complete_trade_window_calculates_dollar_pressure_and_net_pool_flow(self):
        rows = [
            trade("buy", 1200, 1),
            trade("buy", 800, 2),
            trade("sell", 500, 3),
            trade("buy", 700, 4),
        ]
        result = self.analyze(rows)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["trade_count"], 4)
        self.assertAlmostEqual(result["buy_volume_usd"], 2700)
        self.assertAlmostEqual(result["sell_volume_usd"], 500)
        self.assertAlmostEqual(result["net_swap_flow_usd"], 2200)
        self.assertAlmostEqual(result["buy_volume_share_pct"], 2700 / 3200 * 100)
        self.assertEqual(result["buy_sell_pressure_status"], "UNKNOWN")

    def test_minimum_sample_and_share_threshold_classify_buy_pressure(self):
        rows = [trade("buy", 100, i, minutes_ago=i + 1) for i in range(24)]
        rows += [trade("sell", 100, i + 24, minutes_ago=i + 25) for i in range(6)]
        result = self.analyze(rows)
        self.assertEqual(result["buy_sell_pressure_status"], "PASS")
        rows = [trade("buy", 100, i, minutes_ago=i + 1) for i in range(10)]
        rows += [trade("sell", 100, i + 10, minutes_ago=i + 11) for i in range(20)]
        result = self.analyze(rows)
        self.assertEqual(result["buy_sell_pressure_status"], "FAIL")

    def test_300_row_provider_cap_is_partial_and_never_passes_pressure(self):
        rows = [trade("buy", 100, i, minutes_ago=i % 100 + 1) for i in range(300)]
        result = self.analyze(rows)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["coverage_status"], "PARTIAL")
        self.assertIn("PROVIDER_SAMPLE_CAPPED", result["reason_codes"])
        self.assertEqual(result["buy_sell_pressure_status"], "UNKNOWN")

    def test_bad_or_stale_rows_make_the_window_partial(self):
        rows = [trade("buy", 500, 1), trade("sell", 400, 2)]
        rows.append({
            "kind": "buy", "volume_in_usd": "NaN",
            "block_timestamp": (NOW - timedelta(minutes=2)).isoformat(),
            "tx_hash": "0x" + "a" * 64,
        })
        result = self.analyze(rows)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("TRADE_NOTIONAL", result["reason_codes"])
        self.assertEqual(result["buy_sell_pressure_status"], "UNKNOWN")

    def test_whale_threshold_uses_verified_liquidity_and_returns_attributed_events(self):
        rows = [
            trade("buy", 2500, 1, wallet="0x" + "a" * 40),
            trade("sell", 2100, 2, wallet="0x" + "b" * 40),
        ]
        result = self.analyze(rows, liquidity=100_000)
        self.assertEqual(result["whale_threshold_usd"], 2000)
        self.assertEqual(result["whale_buy_count"], 1)
        self.assertEqual(result["whale_sell_count"], 1)
        self.assertEqual(result["whale_events"][0]["event_type"], "SWAP_BUY")
        self.assertEqual(result["wallet_attribution_status"], "COMPLETE_FOR_RETURNED_SAMPLE")

    def test_repeated_round_trip_wallets_are_flagged_as_suspected_not_proven(self):
        rows = []
        for wallet_index in range(5):
            wallet = "0x" + f"{wallet_index + 1:040x}"
            for offset in range(6):
                rows.append(trade("buy" if offset % 2 == 0 else "sell",
                                  100 + offset, wallet_index * 6 + offset,
                                  wallet=wallet, minutes_ago=wallet_index * 6 + offset + 1))
        result = self.analyze(rows)
        self.assertEqual(result["wash_trading_signal"], "SUSPECTED")
        self.assertIn("CONCENTRATED_REPEAT_TRADER_PATTERN", result["reason_codes"])
        self.assertEqual(result["status"], "COMPLETE")

    def test_solana_wallet_case_is_not_normalized(self):
        rows = [
            trade("buy", 100, 1, wallet="AbCdEf12345678901234567890123456"),
            trade("sell", 100, 2, wallet="abcdef12345678901234567890123456"),
        ]
        result = analyze_pool_trade_evidence(
            rows, chain="solana", token_address="TokenAddress",
            pool_address="PoolAddress", liquidity_usd=50_000, now=NOW,
        )
        self.assertEqual(result["unique_trader_count"], 2)

    def test_naive_clock_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            analyze_pool_trade_evidence(
                [], chain="base", token_address=TOKEN,
                pool_address=POOL, liquidity_usd=100_000,
                now=datetime(2026, 11, 12, 12, 0),
            )


if __name__ == "__main__":
    unittest.main()
