"""Offline tests for the permanent €100 Gem Hunter watchlist ledger."""
import sqlite3
import unittest
from unittest.mock import Mock

from crypto_super_radar.gem_hunter_watchlist import (
    ensure_watchlist_schema, record_watchlist_candidates, refresh_watchlist_prices,
    watchlist_snapshot, is_watchlist_eligible, send_watchlist_profit_alerts,
)
from crypto_super_radar.telegram_notifier import TARGET_CHAT_ID

class WatchlistLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        ensure_watchlist_schema(self.db)

    def tearDown(self):
        self.db.close()

    def candidate(self, **overrides):
        row = {"chain":"solana","token_address":"MintABC","token_name":"Alpha",
               "token_symbol":"ALP","price_usd":0.01,"category_fa":"میم‌کوین",
               "gem_hunter_v3_status":"TIER_2_EARLY_ALPHA",
               "pump_phase_fa":"🟢 فاز ۱: شروع پامپ (Early Ignition)",
               "risk_score":55,"security_evidence":{"status":"PASS"}}
        row.update(overrides)
        return row

    def telegram_client(self, *, send_responses=3):
        class Response:
            def __init__(self, payload):
                self.payload = payload
            def raise_for_status(self):
                return None
            def json(self):
                return self.payload

        client = Mock()
        verified_chat = {"ok": True, "result": {
            "id": TARGET_CHAT_ID, "username": "Germany004", "type": "private"
        }}
        client.post.side_effect = [Response(verified_chat)] + [
            Response({"ok": True, "result": {"message_id": index}})
            for index in range(send_responses)
        ]
        return client

    def test_first_detection_name_timestamp_and_initial_price_are_immutable(self):
        record_watchlist_candidates(self.db, [self.candidate()])
        first = dict(self.db.execute("SELECT * FROM gem_hunter_watchlist_ledger").fetchone())
        record_watchlist_candidates(self.db, [self.candidate(token_name="Renamed", token_symbol="NEW", price_usd=0.02)])
        second = dict(self.db.execute("SELECT * FROM gem_hunter_watchlist_ledger").fetchone())
        self.assertEqual(second["first_detected_utc"], first["first_detected_utc"])
        self.assertEqual(second["initial_price_usd"], 0.01)
        self.assertEqual(second["token_name"], "Alpha")
        self.assertEqual(second["token_symbol"], "ALP")
        self.assertEqual(second["detection_count"], 2)

    def test_eur_100_current_and_peak_profit_calculation(self):
        record_watchlist_candidates(self.db, [self.candidate()])
        class FakeResponse:
            def raise_for_status(self): pass
            def json(self):
                return {"pairs":[{"chainId":"solana","baseToken":{"address":"MintABC"},
                                  "priceUsd":"0.025","liquidity":{"usd":100000}}]}
        client = Mock()
        client.get.return_value = FakeResponse()
        result = refresh_watchlist_prices(self.db, session=client)
        self.assertEqual(result["prices_updated"], 1)
        item = watchlist_snapshot(self.db)["items"][0]
        self.assertAlmostEqual(item["current_change_pct"], 150.0)
        self.assertAlmostEqual(item["eur_100_current_value"], 250.0)
        self.assertAlmostEqual(item["eur_100_net_profit_eur"], 150.0)
        self.assertAlmostEqual(item["peak_change_pct"], 150.0)
        self.assertAlmostEqual(item["eur_100_peak_profit_eur"], 150.0)

    def test_early_alpha_requires_rising_phase_and_52_percent_buy_share(self):
        self.assertTrue(is_watchlist_eligible(self.candidate(qualification_status="QUALIFIED_EARLY_WAVE", risk_score=55,
            security_evidence={"status":"PASS"}, gem_hunter_v3_status="EARLY_ALPHA_REVIEW",
            pump_phase_fa="🟢 فاز ۱: شروع پامپ (Early Ignition)", buy_volume_share_pct=52)))
        self.assertFalse(is_watchlist_eligible(self.candidate(gem_hunter_v3_status="EARLY_ALPHA_REVIEW",
            pump_phase_fa="🔴 فاز ۳: سقف فرسودگی / خطر دامپ", buy_volume_share_pct=90)))
        self.assertFalse(is_watchlist_eligible(self.candidate(gem_hunter_v3_status="EARLY_ALPHA_REVIEW",
            pump_phase_fa="🟡 فاز ۲: میانه موج صعودی", buy_volume_share_pct=51.99)))


    def test_profit_milestones_are_sent_once_and_deduplicated_per_level(self):
        record_watchlist_candidates(self.db, [self.candidate()])
        with self.db:
            self.db.execute(
                "UPDATE gem_hunter_watchlist_ledger SET latest_price_usd=0.02 WHERE token_id='solana:MintABC'"
            )
        client = self.telegram_client(send_responses=3)

        first = send_watchlist_profit_alerts(
            self.db, session=client, bot_token="bot-token", chat_id="12345"
        )
        second = send_watchlist_profit_alerts(
            self.db, session=client, bot_token="bot-token", chat_id="12345"
        )

        self.assertEqual(first, {"status": "OK", "sent": 3, "duplicates": 0, "errors": 0})
        self.assertEqual(second, {"status": "OK", "sent": 0, "duplicates": 3, "errors": 0})
        self.assertEqual(client.post.call_count, 4)  # one getUpdates resolution + three sends
        messages = [call.kwargs["json"]["text"] for call in client.post.call_args_list[1:]]
        self.assertIn("+25%", messages[0])
        self.assertIn("+50%", messages[1])
        self.assertIn("+100%", messages[2])
        self.assertEqual(client.post.call_args_list[0].kwargs["json"], {"chat_id": TARGET_CHAT_ID})
        self.assertEqual(client.post.call_args_list[1].kwargs["json"]["chat_id"], TARGET_CHAT_ID)
        self.assertTrue(all("€200.00" in message and "+€100.00" in message for message in messages))
        self.assertTrue(all("گزارش سود واچ‌لیست رادار" in message for message in messages))

    def test_profit_alert_does_not_send_below_25_percent(self):
        record_watchlist_candidates(self.db, [self.candidate()])
        with self.db:
            self.db.execute(
                "UPDATE gem_hunter_watchlist_ledger SET latest_price_usd=0.012 WHERE token_id='solana:MintABC'"
            )
        client = Mock()
        result = send_watchlist_profit_alerts(
            self.db, session=client, bot_token="bot-token", chat_id="12345"
        )
        self.assertEqual(result["sent"], 0)
        client.post.assert_not_called()

    def test_failed_profit_alert_delivery_is_not_retried_or_marked_sent(self):
        record_watchlist_candidates(self.db, [self.candidate()])
        with self.db:
            self.db.execute(
                "UPDATE gem_hunter_watchlist_ledger SET latest_price_usd=0.013 WHERE token_id='solana:MintABC'"
            )
        failed = Mock()
        class Response:
            def raise_for_status(self): return None
            def json(self):
                return {"ok": True, "result": {
                    "id": TARGET_CHAT_ID, "username": "Germany004", "type": "private"
                }}
        failed.post.side_effect = [Response(), RuntimeError("temporary Telegram failure")]
        result = send_watchlist_profit_alerts(
            self.db, session=failed, bot_token="bot-token", chat_id="12345"
        )
        self.assertEqual(result["errors"], 1)

        self.assertIsNone(self.db.execute(
            "SELECT sent_at_utc FROM gem_hunter_watchlist_profit_alerts WHERE token_id='solana:MintABC' AND threshold_pct=25"
        ).fetchone()[0])
        retry_client = Mock()
        retry = send_watchlist_profit_alerts(
            self.db, session=retry_client, bot_token="bot-token", chat_id="12345"
        )
        self.assertEqual(retry["sent"], 0)
        self.assertEqual(retry["duplicates"], 1)
        retry_client.post.assert_not_called()


    def test_phase_three_risk_95_never_enters_eur_100_watchlist(self):
        item=self.candidate(pump_phase_fa="🔴 فاز ۳: سقف فرسودگی / خطر دامپ",risk_score=95,
                            qualification_status="WATCH_ONLY_HIGH_RISK")
        self.assertFalse(is_watchlist_eligible(item))
        self.assertEqual(record_watchlist_candidates(self.db,[item]),0)

if __name__ == "__main__":
    unittest.main()
