from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.solana_whales import SolanaWhaleScanner
from crypto_super_radar.wallet_event_store import WalletEventStore


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
    def post(self, *args, **kwargs):
        return Response(self.responses.pop(0))


class NoWaitLimiter:
    def wait(self, key):
        return None


class SolanaWalletPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = WalletEventStore(self.db)

    def tearDown(self):
        self.db.close()

    def test_scanner_preserves_base58_case_and_persists_all_wallets_as_transfers(self):
        source = "WalletSourceABC123456789ABCDEFGHJKLMNP"
        destination = "WalletDestXYZ123456789ABCDEFGHJKLMNP"
        mint = "TokenMintABC123456789ABCDEFGHJKLMNPQR"
        responses = [
            {"result": {"value": [{"address": "TokenAccount111111111111111111111111111"}]}},
            {"result": {"value": {"data": {"parsed": {"info": {"owner": source}}}}}},
            {"result": [{"signature": "TxSignatureExample123456789ABCDEFGHJKLMNP", "blockTime": 1_700_000_000, "err": None}]},
            {"result": {
                "slot": 12345,
                "meta": {
                    "preTokenBalances": [
                        {"mint": mint, "owner": source, "uiTokenAmount": {"uiAmount": 10.0}}
                    ],
                    "postTokenBalances": [
                        {"mint": mint, "owner": source, "uiTokenAmount": {"uiAmount": 7.0}},
                        {"mint": mint, "owner": destination, "uiTokenAmount": {"uiAmount": 3.0}}
                    ],
                }
            }},
        ]
        scanner = SolanaWhaleScanner(
            session=FakeSession(responses), limiter=NoWaitLimiter(),
            max_accounts=1, signatures_per_account=2,
            wallet_event_store=self.store,
        )
        result = scanner.scan_mint(mint, price_usd=2.0)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["wallet_event_persistence"]["inserted"], 2)
        self.assertEqual(result["wallet_events_observed"], 2)

        source_rows = self.store.wallet_history(chain="solana", wallet_address=source)
        destination_rows = self.store.wallet_history(chain="solana", wallet_address=destination)
        self.assertEqual(source_rows[0]["wallet_address"], source)
        self.assertEqual(destination_rows[0]["wallet_address"], destination)
        self.assertEqual(source_rows[0]["event_type"], "TRANSFER_OUT")
        self.assertEqual(destination_rows[0]["event_type"], "TRANSFER_IN")
        self.assertEqual(source_rows[0]["counterparty_address"], destination)
        self.assertEqual(destination_rows[0]["counterparty_address"], source)
        self.assertEqual(result["wallet_graph_events_loaded_from_history"], 2)
        self.assertEqual(result["wallet_graph_analysis"]["status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["wallet_graph_coverage"], "PARTIAL_RECENT_SAMPLE")
        self.assertEqual(source_rows[0]["token_delta"], -3.0)
        self.assertEqual(destination_rows[0]["token_delta"], 3.0)
        self.assertEqual(source_rows[0]["tx_signature"], destination_rows[0]["tx_signature"])
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_missing_block_time_is_not_persisted_as_a_fake_timestamp(self):
        mint = "TokenMintABC123456789ABCDEFGHJKLMNPQR"
        responses = [
            {"result": {"value": [{"address": "TokenAccount111111111111111111111111111"}]}},
            {"result": {"value": {"data": {"parsed": {"info": {"owner": "WalletOwner111111111111111111111111111"}}}}},
            {"result": [{"signature": "TxWithoutBlockTime", "blockTime": None, "err": None}]},
            {"result": {
                "slot": 123,
                "meta": {
                    "preTokenBalances": [{"mint": mint, "owner": "WalletOwner111111111111111111111111111", "uiTokenAmount": {"uiAmount": 1.0}}],
                    "postTokenBalances": []
                }
            }},
        ]
        scanner = SolanaWhaleScanner(
            session=FakeSession(responses), limiter=NoWaitLimiter(),
            max_accounts=1, wallet_event_store=self.store,
        )
        result = scanner.scan_mint(mint, price_usd=1.0)
        self.assertEqual(result["wallet_events_observed"], 0)
        self.assertEqual(result["wallet_event_timestamps_missing"], 1)
        self.assertEqual(self.store.wallet_history(
            chain="solana", wallet_address="WalletOwner111111111111111111111111111"
        ), [])


if __name__ == "__main__":
    unittest.main()
