from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.wallet_event_store import WalletEventStore


def event(wallet="WalletCaseSensitive111111111111111111111", **overrides):
    row = {
        "chain": "solana", "tx_signature": "signature-1",
        "wallet_address": wallet, "token_address": "TokenCaseSensitive1111111111111111111111",
        "event_type": "TRANSFER_IN", "event_index": 0,
        "event_time_ms": 1_700_000_001_000, "received_at_ms": 1_700_000_001_010,
        "slot": 10, "token_delta": 2.5, "quote_delta": None,
        "quote_symbol": None, "source": "public_solana_json_rpc",
        "evidence_status": "OBSERVED",
    }
    row.update(overrides)
    return row


class WalletEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = WalletEventStore(self.db)

    def tearDown(self):
        self.db.close()

    def test_same_transaction_can_be_attributed_to_multiple_wallets(self):
        result = self.store.append_many([
            event(wallet="WalletOne111111111111111111111111111"),
            event(wallet="WalletTwo111111111111111111111111111"),
        ])
        self.assertEqual(result, {"inserted": 2, "duplicates": 0})

    def test_replay_is_idempotent_per_wallet_and_event_index(self):
        row = event()
        self.assertEqual(self.store.append_many([row]), {"inserted": 1, "duplicates": 0})
        self.assertEqual(self.store.append_many([row]), {"inserted": 0, "duplicates": 1})

    def test_evm_addresses_are_normalized_but_solana_addresses_are_case_sensitive(self):
        base = event(chain="base", wallet_address="0x" + "A" * 40, token_address="0x" + "B" * 40)
        self.store.append_many([base])
        alt = dict(base, wallet_address="0x" + "a" * 40, token_address="0x" + "b" * 40)
        self.assertEqual(self.store.append_many([alt])["duplicates"], 1)
        first = event(wallet_address="WalletAa111111111111111111111111111111")
        second = dict(first, wallet_address="walletAa111111111111111111111111111111")
        self.store.append_many([first, second])
        self.assertEqual(len(self.store.wallet_history(
            chain="solana", wallet_address="WalletAa111111111111111111111111111111"
        )), 1)
        self.assertEqual(len(self.store.wallet_history(
            chain="solana", wallet_address="walletAa111111111111111111111111111111"
        )), 1)

    def test_counterparty_address_is_persisted_and_retrieved(self):
        row = event(counterparty_address="WalletCounterparty111111111111111111111")
        self.store.append_many([row])
        saved = self.store.token_events(chain="solana", token_address=row["token_address"])
        self.assertEqual(saved[0]["counterparty_address"], row["counterparty_address"])

    def test_invalid_batch_is_rejected_before_any_write(self):
        with self.assertRaisesRegex(ValueError, "event_index"):
            self.store.append_many([event(), event(event_index=-1)])
        self.assertEqual(self.store.wallet_history(
            chain="solana", wallet_address="WalletCaseSensitive111111111111111111111"
        ), [])

    def test_non_finite_delta_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "token_delta"):
            self.store.append_many([event(token_delta=float("nan"))])

    def test_event_history_is_sorted_by_event_time_then_slot_then_index(self):
        self.store.append_many([
            event(tx_signature="b", event_index=2, event_time_ms=1_700_000_001_001, slot=11),
            event(tx_signature="a", event_index=1, event_time_ms=1_700_000_001_000, slot=12),
        ])
        rows = self.store.wallet_history(
            chain="solana", wallet_address="WalletCaseSensitive111111111111111111111"
        )
        self.assertEqual([row["tx_signature"] for row in rows], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
