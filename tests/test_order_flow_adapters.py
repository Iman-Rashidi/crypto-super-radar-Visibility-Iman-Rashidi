from __future__ import annotations

import unittest

from crypto_super_radar.order_flow import VenueOrderFlow
from crypto_super_radar.order_flow_adapters import (
    OrderFlowAdapterError,
    ingest_binance_spot_message,
    ingest_bybit_spot_message,
    normalize_binance_spot_agg_trade,
    normalize_bybit_spot_public_trade_message,
)


class BinanceSpotTradeAdapterTests(unittest.TestCase):
    def test_buyer_maker_maps_to_sell_aggressor_and_uses_exchange_time(self):
        event = normalize_binance_spot_agg_trade(
            {
                "e": "aggTrade", "s": "BTCUSDT", "a": 123,
                "p": "65000.5", "q": "0.025", "T": 1_700_000_001_000,
                "E": 1_700_000_001_005, "m": True,
            },
            channel_type="spot",
            received_at_ms=1_700_000_001_010,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual(event["trade_id"], "123")
        self.assertEqual(event["event_time_ms"], 1_700_000_001_000)
        self.assertEqual(event["aggressor_side"], "SELL")
        self.assertEqual(event["base_quantity"], 0.025)
        self.assertEqual(event["source"], "binance_spot_aggTrade")

    def test_taker_buy_when_buyer_is_not_maker(self):
        event = normalize_binance_spot_agg_trade(
            {"e": "aggTrade", "s": "BTCUSDT", "a": 124, "p": "10", "q": "2", "T": 1000, "m": False},
            channel_type="spot",
            received_at_ms=1001,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual(event["aggressor_side"], "BUY")

    def test_combined_stream_wrapper_is_checked(self):
        venue = VenueOrderFlow("binance", window_ms=10_000, stale_after_ms=5_000)
        venue.connect(1_000)
        result = ingest_binance_spot_message(
            venue,
            {
                "stream": "btcusdt@aggTrade",
                "data": {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "10", "q": "1", "T": 1_500, "m": False},
            },
            channel_type="spot",
            received_at_ms=1_600,
            expected_symbol="BTCUSDT",
        )
        self.assertTrue(result["accepted"])
        snapshot = venue.snapshot(1_700)
        self.assertEqual(snapshot["event_count"], 1)
        self.assertEqual(snapshot["observed_cvd_base"], 1.0)
        self.assertIsNone(snapshot["cvd_base"])

    def test_malformed_binance_event_freezes_connected_venue(self):
        venue = VenueOrderFlow("binance", window_ms=10_000)
        venue.connect(1_000)
        result = ingest_binance_spot_message(
            venue,
            {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "10", "q": "1", "T": 1_500, "m": "true"},
            channel_type="spot",
            received_at_ms=1_600,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual(result["reason"], "INVALID_PROVIDER_PAYLOAD")
        self.assertEqual(venue.snapshot(1_700)["status"], "FROZEN")


    def test_non_spot_channel_is_rejected(self):
        with self.assertRaisesRegex(OrderFlowAdapterError, "SPOT_CHANNEL_REQUIRED"):
            normalize_binance_spot_agg_trade(
                {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "10", "q": "1", "T": 1000, "m": False},
                channel_type="linear",
                received_at_ms=1001,
                expected_symbol="BTCUSDT",
            )

    def test_symbol_mismatch_is_rejected(self):
        with self.assertRaisesRegex(OrderFlowAdapterError, "SYMBOL_MISMATCH"):
            normalize_binance_spot_agg_trade(
                {"e": "aggTrade", "s": "ETHUSDT", "a": 1, "p": "10", "q": "1", "T": 1000, "m": False},
                channel_type="spot",
                received_at_ms=1001,
                expected_symbol="BTCUSDT",
            )


class BybitSpotTradeAdapterTests(unittest.TestCase):
    def test_multi_trade_message_normalizes_taker_sides_and_base_size(self):
        events = normalize_bybit_spot_public_trade_message(
            {
                "topic": "publicTrade.BTCUSDT",
                "type": "snapshot",
                "ts": 1_700_000_001_020,
                "data": [
                    {"T": 1_700_000_001_000, "s": "BTCUSDT", "S": "Buy", "v": "0.01", "p": "65000", "i": "id-buy"},
                    {"T": 1_700_000_001_010, "s": "BTCUSDT", "S": "Sell", "v": "0.02", "p": "65001", "i": "id-sell"},
                ],
            },
            channel_type="spot",
            received_at_ms=1_700_000_001_030,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual([event["aggressor_side"] for event in events], ["BUY", "SELL"])
        self.assertEqual([event["base_quantity"] for event in events], [0.01, 0.02])
        self.assertEqual([event["trade_id"] for event in events], ["id-buy", "id-sell"])

    def test_ingestion_deduplicates_venue_native_trade_ids(self):
        venue = VenueOrderFlow("bybit", window_ms=10_000, stale_after_ms=5_000)
        venue.connect(1_000)
        message = {
            "topic": "publicTrade.BTCUSDT",
            "type": "snapshot",
            "data": [
                {"T": 1_500, "s": "BTCUSDT", "S": "Buy", "v": "0.01", "p": "65000", "i": "exec-1"},
                {"T": 1_500, "s": "BTCUSDT", "S": "Sell", "v": "0.02", "p": "65000", "i": "exec-2"},
            ],
        }
        first = ingest_bybit_spot_message(venue, message, channel_type="spot",
            received_at_ms=1_600, expected_symbol="BTCUSDT")
        second = ingest_bybit_spot_message(venue, message, channel_type="spot",
            received_at_ms=1_700, expected_symbol="BTCUSDT")
        self.assertEqual(first["accepted"], 2)
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(venue.snapshot(1_800)["event_count"], 2)

    def test_wrong_topic_freezes_venue(self):
        venue = VenueOrderFlow("bybit", window_ms=10_000)
        venue.connect(1_000)
        result = ingest_bybit_spot_message(
            venue,
            {"topic": "publicTrade.ETHUSDT", "type": "snapshot", "data": []},
            channel_type="spot",
            received_at_ms=1_600,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual(result["reason"], "INVALID_PROVIDER_PAYLOAD")
        self.assertEqual(venue.snapshot(1_700)["status"], "FROZEN")

    def test_missing_base_size_rejects_entire_batch_and_freezes_venue(self):
        venue = VenueOrderFlow("bybit", window_ms=10_000)
        venue.connect(1_000)
        result = ingest_bybit_spot_message(
            venue,
            {
                "topic": "publicTrade.BTCUSDT",
                "type": "snapshot",
                "data": [
                    {"T": 1_500, "s": "BTCUSDT", "S": "Buy", "p": "65000", "i": "exec-1"},
                ],
            },
            channel_type="spot",
            received_at_ms=1_600,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual(result["reason"], "INVALID_PROVIDER_PAYLOAD")
        self.assertEqual(venue.snapshot(1_700)["status"], "FROZEN")


    def test_non_spot_channel_is_rejected(self):
        with self.assertRaisesRegex(OrderFlowAdapterError, "SPOT_CHANNEL_REQUIRED"):
            normalize_bybit_spot_public_trade_message(
                {"topic": "publicTrade.BTCUSDT", "type": "snapshot", "data": []},
                channel_type="linear",
                received_at_ms=1001,
                expected_symbol="BTCUSDT",
            )


    def test_malformed_row_rejects_entire_batch_without_partial_ingestion(self):
        venue = VenueOrderFlow("bybit", window_ms=10_000)
        venue.connect(1_000)
        result = ingest_bybit_spot_message(
            venue,
            {
                "topic": "publicTrade.BTCUSDT",
                "type": "snapshot",
                "data": [
                    {"T": 1_500, "s": "BTCUSDT", "S": "Buy", "v": "0.01", "p": "65000", "i": "valid-1"},
                    {"T": 1_510, "s": "BTCUSDT", "S": "Sell", "v": "0.02", "p": "65000"},
                ],
            },
            channel_type="spot",
            received_at_ms=1_600,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual(result["reason"], "INVALID_PROVIDER_PAYLOAD")
        snapshot = venue.snapshot(1_700)
        self.assertEqual(snapshot["event_count"], 0)
        self.assertEqual(snapshot["status"], "FROZEN")

    def test_non_spot_channel_freezes_connected_venue(self):
        venue = VenueOrderFlow("binance", window_ms=10_000)
        venue.connect(1_000)
        result = ingest_binance_spot_message(
            venue,
            {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "10", "q": "1", "T": 1_500, "m": False},
            channel_type="linear",
            received_at_ms=1_600,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual(result["reason"], "INVALID_PROVIDER_PAYLOAD")
        self.assertEqual(venue.snapshot(1_700)["status"], "FROZEN")

    def test_adapter_does_not_invent_a_connection(self):
        venue = VenueOrderFlow("bybit", window_ms=10_000)
        result = ingest_bybit_spot_message(
            venue,
            {
                "topic": "publicTrade.BTCUSDT",
                "type": "snapshot",
                "data": [{"T": 1_500, "s": "BTCUSDT", "S": "Buy", "v": "0.01", "p": "65000", "i": "exec-1"}],
            },
            channel_type="spot",
            received_at_ms=1_600,
            expected_symbol="BTCUSDT",
        )
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(venue.snapshot(1_700)["status"], "DISCONNECTED")


if __name__ == "__main__":
    unittest.main()
