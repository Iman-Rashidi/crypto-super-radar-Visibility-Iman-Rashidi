"""Deterministic tests for WalletGraphAnalytics."""
import unittest

from crypto_super_radar.wallet_graph import WalletEvent, WalletGraphAnalytics


def _rows(wallet_count=10, *, chain="solana", token="mint-a", start=1_700_000_000):
    rows = []
    for index in range(wallet_count):
        rows.append({
            "chain": chain, "wallet": f"wallet-{index}", "token": token,
            "event_type": "buy", "timestamp": start + index * 137,
            "amount_usd": 80.0 + index * 17,
            "funding_source": f"funder-{index}",
            "funding_source_type": "private",
        })
    return rows


class WalletGraphAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.analytics = WalletGraphAnalytics()

    def test_insufficient_data_never_passes(self):
        result = self.analytics.analyze_token_distribution(
            _rows(1), chain="solana", token="mint-a"
        )
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_unrelated_scope_is_excluded(self):
        rows = _rows()
        rows += _rows(chain="base", token="mint-a")
        rows += _rows(chain="solana", token="mint-b")
        result = self.analytics.analyze_token_distribution(
            rows, chain="solana", token="mint-a"
        )
        self.assertEqual(result["unique_wallet_count"], 10)
        self.assertEqual(result["chain"], "solana")
        self.assertEqual(result["token"], "mint-a")

    def test_duplicate_events_are_removed(self):
        rows = _rows()
        rows.append(dict(rows[0]))
        result = self.analytics.analyze_token_distribution(
            rows, chain="solana", token="mint-a"
        )
        self.assertEqual(result["event_count"], 10)
        self.assertEqual(result["duplicate_events_removed"], 1)

    def test_public_cex_funder_alone_is_not_sybil_evidence(self):
        rows = _rows()
        for row in rows:
            row["funding_source"] = "exchange-hot-wallet"
            row["funding_source_type"] = "cex"
        result = self.analytics.analyze_token_distribution(
            rows, chain="solana", token="mint-a"
        )
        self.assertEqual(result["status"], "NO_ANOMALY_DETECTED")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_temporal_and_amount_pattern_is_flagged(self):
        rows = []
        for wallet_index in range(8):
            for event_index in range(2):
                rows.append({
                    "chain": "solana", "wallet": f"wallet-{wallet_index}",
                    "token": "mint-a", "event_type": "buy",
                    "timestamp": 1_700_000_000 + event_index * 300 + wallet_index,
                    "amount_usd": 125.0,
                    "funding_source": f"private-{wallet_index}",
                    "funding_source_type": "private",
                })
        result = self.analytics.analyze_token_distribution(
            rows, chain="solana", token="mint-a"
        )
        self.assertIn(result["status"], {"WATCHLIST", "SUSPICIOUS"})
        self.assertGreater(result["suspicious_pair_count"], 0)

    def test_deployer_wallets_are_counted_uniquely(self):
        rows = _rows()
        for row in rows[:2]:
            row["deployer_relation"] = True
        rows.extend([
            dict(rows[0], timestamp=1_700_001_000),
            dict(rows[0], timestamp=1_700_002_000),
        ])
        result = self.analytics.analyze_token_distribution(
            rows, chain="solana", token="mint-a"
        )
        self.assertEqual(result["deployer_linked_wallet_count"], 1)

    def test_invalid_amounts_are_rejected(self):
        for bad in (float("nan"), float("inf"), -1, True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    WalletEvent.from_mapping({
                        "chain": "solana", "wallet": "w", "token": "t",
                        "event_type": "buy", "timestamp": 1, "amount_usd": bad,
                    })

    def test_missing_or_insufficient_evidence_never_authorizes_trade(self):
        result = self.analytics.analyze_token_distribution(
            _rows(), chain="solana", token="mint-a"
        )
        self.assertEqual(result["status"], "NO_ANOMALY_DETECTED")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_shared_private_funder_without_temporal_corroboration_is_not_enough(self):
        rows = _rows()
        for index, row in enumerate(rows):
            row["funding_source"] = "same-private-source"
            row["funding_source_type"] = "private"
            row["timestamp"] = 1_700_000_000 + index * 600
        result = self.analytics.analyze_token_distribution(
            rows, chain="solana", token="mint-a"
        )
        self.assertEqual(result["status"], "NO_ANOMALY_DETECTED")
        self.assertEqual(result["suspicious_pair_count"], 0)

    def test_invalid_rows_make_analysis_incomplete_not_clean(self):
        rows = _rows()
        rows.append({
            "chain": "solana", "wallet": "bad-wallet", "token": "mint-a",
            "event_type": "buy", "timestamp": -10, "amount_usd": 10,
        })
        result = self.analytics.analyze_token_distribution(
            rows, chain="solana", token="mint-a"
        )
        self.assertEqual(result["status"], "ANALYSIS_INCOMPLETE")
        self.assertEqual(result["invalid_event_count"], 1)
        self.assertEqual(result["trade_authorization"], "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
