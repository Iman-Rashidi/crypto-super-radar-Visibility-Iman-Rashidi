"""Deterministic tests for WalletGraphAnalytics."""
import unittest

from crypto_super_radar.wallet_graph import WalletEvent, WalletGraphAnalytics, WalletGraphConfig


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

    def test_single_direct_transfer_between_two_wallets_is_not_sybil_evidence(self):
        rows = _rows(wallet_count=2)
        rows = [
            {
                "chain": "solana", "wallet": "WalletSenderABC123",
                "token": "MintGraphABC123", "event_type": "transfer",
                "timestamp": 1_700_000_000, "amount_tokens": 5.0,
                "counterparty_wallet": "WalletReceiverXYZ789",
                "tx_hash": "SingleTransferSignature",
                "instruction_index": 0,
            },
            {
                "chain": "solana", "wallet": "WalletReceiverXYZ789",
                "token": "MintGraphABC123", "event_type": "transfer",
                "timestamp": 1_700_000_000, "amount_tokens": 5.0,
                "counterparty_wallet": "WalletSenderABC123",
                "tx_hash": "SingleTransferSignature",
                "instruction_index": 0,
            },
        ]
        result = WalletGraphAnalytics(min_wallets=2, min_events=2).analyze_token_distribution(
            rows, chain="solana", token="MintGraphABC123"
        )
        self.assertEqual(result["status"], "NO_ANOMALY_DETECTED")
        self.assertEqual(result["suspicious_pair_count"], 0)
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_repeated_direct_counterparty_transfers_are_explainable_graph_evidence(self):
        rows = []
        for index in range(3):
            signature = f"TransferSignature{index}"
            timestamp = 1_700_000_000 + index * 60
            for wallet, peer, delta in (
                ("WalletSenderABC123", "WalletReceiverXYZ789", -5.0),
                ("WalletReceiverXYZ789", "WalletSenderABC123", 5.0),
            ):
                rows.append({
                    "chain": "solana", "wallet": wallet,
                    "token": "MintGraphABC123", "event_type": "transfer",
                    "timestamp": timestamp, "amount_tokens": abs(delta),
                    "counterparty_wallet": peer, "tx_hash": signature,
                    "instruction_index": index,
                })
        result = WalletGraphAnalytics(min_wallets=2, min_events=2).analyze_token_distribution(
            rows, chain="solana", token="MintGraphABC123"
        )
        self.assertEqual(result["status"], "WATCHLIST")
        self.assertEqual(result["suspicious_pair_count"], 1)
        self.assertIn(
            "repeated_direct_counterparty_transfers",
            result["evidence"][0]["reasons"],
        )

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
        self.assertEqual(result["deployer_linked_wallet_count"], 2)

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


    def test_solana_base58_identity_preserves_case(self):
        event = WalletEvent.from_mapping({
            "chain": "solana", "wallet": "AbCdEf123", "token": "MintAbC123",
            "event_type": "buy", "timestamp": 1,
        })
        self.assertEqual(event.wallet, "AbCdEf123")
        self.assertEqual(event.token, "MintAbC123")

    def test_evm_addresses_are_normalized_case_insensitively(self):
        event = WalletEvent.from_mapping({
            "chain": "base", "wallet": "0xAbCd", "token": "0xDeF",
            "event_type": "buy", "timestamp": 1,
        })
        self.assertEqual(event.wallet, "0xabcd")
        self.assertEqual(event.token, "0xdef")

    def test_distinct_solana_instructions_in_one_signature_are_not_deduplicated(self):
        rows = _rows()
        rows[0].update({
            "signature": "5igNaTuReCaseSensitive",
            "instruction_index": 2,
            "inner_instruction_index": 0,
        })
        rows.append(dict(rows[0], inner_instruction_index=1))
        result = self.analytics.analyze_token_distribution(
            rows, chain="solana", token="mint-a"
        )
        self.assertEqual(result["event_count"], 11)
        self.assertEqual(result["duplicate_events_removed"], 0)

    def test_distinct_evm_logs_in_one_transaction_are_not_deduplicated(self):
        rows = _rows(chain="base", token="0xToken")
        rows[0].update({"tx_hash": "0xABCDEF", "log_index": 7, "block_hash": "0xBEEF"})
        rows.append(dict(rows[0], log_index=8))
        result = self.analytics.analyze_token_distribution(
            rows, chain="base", token="0xTOKEN"
        )
        self.assertEqual(result["event_count"], 11)
        self.assertEqual(result["duplicate_events_removed"], 0)

    def test_wallet_graph_config_is_injectable_and_validated(self):
        config = WalletGraphConfig(
            temporal_tolerance_seconds=45, min_wallets=4, min_events=8,
            max_candidate_pairs=500, max_events=2_000,
        )
        analytics = WalletGraphAnalytics(config=config)
        self.assertEqual(analytics.temporal_tolerance_seconds, 45)
        self.assertEqual(analytics.min_wallets, 4)
        with self.assertRaises(ValueError):
            WalletGraphConfig(min_wallets=1)

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
