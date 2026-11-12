import unittest

from crypto_super_radar.order_flow import (
    VenueOrderFlow,
    build_multi_venue_order_flow_report,
    summarize_liquidation_events,
)


class VenueOrderFlowTests(unittest.TestCase):
    def make_venue(self, name="binance", connected_at=0):
        venue = VenueOrderFlow(
            name, window_ms=100_000, min_coverage_pct=80,
            stale_after_ms=10_000, future_skew_ms=1_000,
        )
        venue.connect(connected_at)
        return venue

    def add_trade(self, venue, trade_id, event_time, received, side="BUY", quantity=1.0):
        return venue.ingest_trade(
            trade_id=trade_id,
            event_time_ms=event_time,
            received_at_ms=received,
            price=100.0,
            base_quantity=quantity,
            aggressor_side=side,
            source="public-trades",
        )

    def test_duplicate_trade_id_does_not_change_cvd(self):
        venue = self.make_venue()
        self.assertTrue(self.add_trade(venue, "t1", 90_000, 90_000)["accepted"])
        self.assertFalse(self.add_trade(venue, "t1", 90_000, 90_001)["accepted"])
        snapshot = venue.snapshot(95_000)
        self.assertEqual(snapshot["event_count"], 1)
        self.assertEqual(snapshot["cvd_base"], 1.0)
        self.assertEqual(snapshot["duplicate_count"], 1)

    def test_event_time_orders_out_of_order_arrivals(self):
        venue = self.make_venue()
        self.add_trade(venue, "late", 92_000, 95_000, "SELL", 2.0)
        self.add_trade(venue, "early", 91_000, 96_000, "BUY", 1.0)
        snapshot = venue.snapshot(96_000)
        self.assertEqual(snapshot["first_event_time_ms"], 91_000)
        self.assertEqual(snapshot["last_event_time_ms"], 92_000)
        self.assertEqual(snapshot["cvd_base"], -1.0)

    def test_warming_up_window_does_not_emit_usable_cvd(self):
        venue = self.make_venue(connected_at=80_000)
        self.add_trade(venue, "t1", 90_000, 90_000)
        snapshot = venue.snapshot(95_000)
        self.assertEqual(snapshot["status"], "WARMING_UP")
        self.assertIsNone(snapshot["cvd_base"])
        self.assertEqual(snapshot["observed_cvd_base"], 1.0)

    def test_stale_venue_cannot_be_hidden_by_live_venue(self):
        live = self.make_venue("binance", 0)
        stale = self.make_venue("bybit", 0)
        self.add_trade(live, "a", 90_000, 90_000)
        stale.heartbeat(70_000)
        report = build_multi_venue_order_flow_report(
            {"binance": live, "bybit": stale}, now_ms=95_000
        )
        self.assertEqual(report["venues"]["binance"]["status"], "LIVE")
        self.assertEqual(report["venues"]["bybit"]["status"], "STALE")
        self.assertEqual(report["directional_alignment"], "UNKNOWN")
        self.assertIsNone(report["aggregate_cvd"])

    def test_reconnect_does_not_reuse_events_from_uncovered_gap(self):
        venue = self.make_venue(connected_at=0)
        self.add_trade(venue, "before-gap", 70_000, 70_000)
        venue.disconnect()
        venue.connect(80_000)
        self.add_trade(venue, "after-reconnect", 90_000, 90_000, "SELL", 2.0)

        snapshot = venue.snapshot(95_000)

        self.assertEqual(snapshot["status"], "WARMING_UP")
        self.assertEqual(snapshot["event_count"], 1)
        self.assertIsNone(snapshot["cvd_base"])
        self.assertEqual(snapshot["observed_cvd_base"], -2.0)

    def test_pre_connection_backfill_is_not_misrepresented_as_live_flow(self):
        venue = self.make_venue(connected_at=80_000)
        result = self.add_trade(venue, "old-backfill", 70_000, 90_000)

        self.assertEqual(result["reason"], "EVENT_PRECEDES_CURRENT_CONNECTION")
        self.assertEqual(venue.snapshot(95_000)["event_count"], 0)

    def test_invalid_aggressor_side_freezes_venue(self):
        venue = self.make_venue()
        result = venue.ingest_trade(
            trade_id="bad", event_time_ms=90_000, received_at_ms=90_000,
            price=100, base_quantity=1, aggressor_side="UNKNOWN",
            source="public-trades",
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(venue.snapshot(95_000)["status"], "FROZEN")

    def test_future_event_timestamp_freezes_venue(self):
        venue = self.make_venue()
        result = self.add_trade(venue, "future", 95_000, 90_000)
        self.assertEqual(result["reason"], "FUTURE_EVENT_TIMESTAMP")
        self.assertEqual(venue.snapshot(95_000)["status"], "FROZEN")

    def test_unclassified_contract_quantity_cannot_be_ingested_as_base(self):
        venue = self.make_venue()
        result = venue.ingest_trade(
            trade_id="contract", event_time_ms=90_000, received_at_ms=90_000,
            price=100, base_quantity=None, aggressor_side="BUY",
            source="linear-contracts",
        )
        self.assertEqual(result["reason"], "UNKNOWN_OR_INVALID_BASE_QUANTITY")
        self.assertEqual(venue.snapshot(95_000)["status"], "FROZEN")


class LiquidationEvidenceTests(unittest.TestCase):
    def test_estimated_large_print_is_not_counted_as_confirmed_liquidation(self):
        report = summarize_liquidation_events([
            {
                "venue": "hyperliquid", "event_id": "liq-1", "event_time_ms": 1_000,
                "notional_usd": 10_000, "evidence_class": "CONFIRMED_LIQUIDATION",
                "source": "liquidation-feed",
            },
            {
                "venue": "binance", "event_id": "trade-1", "event_time_ms": 1_100,
                "notional_usd": 50_000, "evidence_class": "ESTIMATED_LARGE_PRINT",
                "source": "large-trade-proxy",
            },
        ])
        self.assertEqual(report["confirmed_liquidation_count"], 1)
        self.assertEqual(report["confirmed_liquidation_notional_usd"], 10_000)
        self.assertEqual(report["estimated_large_print_count"], 1)
        self.assertEqual(report["estimated_large_print_notional_usd"], 50_000)

    def test_liquidation_events_are_deduplicated_per_venue(self):
        event = {
            "venue": "hyperliquid", "event_id": "liq-1", "event_time_ms": 1_000,
            "notional_usd": 10_000, "evidence_class": "CONFIRMED_LIQUIDATION",
            "source": "liquidation-feed",
        }
        report = summarize_liquidation_events([event, event])
        self.assertEqual(report["confirmed_liquidation_count"], 1)
        self.assertEqual(report["duplicate_rows"], 1)


if __name__ == "__main__":
    unittest.main()
