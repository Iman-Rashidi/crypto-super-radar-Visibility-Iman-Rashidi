"""Unit tests for the read-only live provider acceptance runner."""
from __future__ import annotations

import unittest

import requests
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from scripts.live_provider_acceptance import (
    AcceptanceRunner,
    classify_observation,
    epoch_datetime,
    parse_feed_datetime,
    positive_number,
    positive_or_zero,
    relative_difference_pct,
    DEFAULT_ACCEPTANCE_SYMBOLS,
    run_watchlist,
)


class TimestampAndThresholdTests(unittest.TestCase):
    def test_epoch_seconds_and_milliseconds_are_supported(self):
        seconds = 1_800_000_000
        self.assertEqual(epoch_datetime(seconds), epoch_datetime(seconds * 1000))
        self.assertIsNone(epoch_datetime(True))
        self.assertIsNone(epoch_datetime(float("nan")))
        self.assertIsNone(epoch_datetime(-1))

    def test_feed_rfc2822_and_iso_timestamps_are_supported(self):
        rfc = parse_feed_datetime("Mon, 09 Nov 2026 12:00:00 GMT")
        iso = parse_feed_datetime("2026-11-09T12:00:00Z")
        self.assertEqual(rfc, iso)
        self.assertIsNone(parse_feed_datetime("not-a-date"))

    def test_numeric_normalizers_reject_boolean_and_non_finite_values(self):
        self.assertIsNone(positive_number(True))
        self.assertIsNone(positive_number(float("inf")))
        self.assertEqual(positive_or_zero(0), 0.0)
        self.assertIsNone(positive_or_zero(-1))

    def test_relative_price_difference_uses_symmetric_denominator(self):
        self.assertAlmostEqual(relative_difference_pct(100, 105), 4.8780487805)
        self.assertEqual(relative_difference_pct(0, 0), 0.0)

    def test_classification_distinguishes_stale_missing_and_future_data(self):
        now = datetime.now(timezone.utc)
        args = {
            "latency_ms": 100,
            "received_at": now,
            "max_latency_ms": 1000,
            "max_age_seconds": 120,
        }
        self.assertEqual(classify_observation(
            **args, source_timestamp=now - timedelta(seconds=30)
        )[0], "PASS")
        self.assertEqual(classify_observation(
            **args, source_timestamp=now - timedelta(seconds=600)
        )[0], "FAIL")
        self.assertEqual(classify_observation(
            **args, source_timestamp=None
        )[0], "INCONCLUSIVE")
        self.assertEqual(classify_observation(
            **args, source_timestamp=now + timedelta(seconds=120)
        )[0], "FAIL")
        self.assertEqual(classify_observation(
            **{**args, "latency_ms": 2000}, source_timestamp=now
        )[0], "FAIL")


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.last_call = None

    def get(self, url, **kwargs):
        self.last_call = (url, kwargs)
        return FakeResponse(self.payload)


class FakeBlockedResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        raise requests.HTTPError(
            f"{self.status_code} blocked",
            response=self,
        )


class FakeBlockedSession:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}

    def get(self, url, **kwargs):
        return FakeBlockedResponse(self.status_code)


class FakeLimiter:
    def wait(self, url):
        return 0.0


class RunnerTests(unittest.TestCase):
    def test_symbol_validation_blocks_url_or_parameter_injection(self):
        with self.assertRaises(ValueError):
            AcceptanceRunner(symbol="BTCUSDT&foo=bar")
        with self.assertRaises(ValueError):
            AcceptanceRunner(symbol="BTC/USDT")

    def test_binance_spot_probe_records_price_timestamp_and_latency(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        session = FakeSession({
            "symbol": "BTCUSDT",
            "lastPrice": "65000.5",
            "quoteVolume": "123456789",
            "closeTime": now_ms,
        })
        runner = AcceptanceRunner(session=session, limiter=FakeLimiter())
        result = runner.check_binance_spot()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["details"]["price"], 65000.5)
        self.assertEqual(result["provider"], "Binance Spot")
        self.assertIsNotNone(result["source_timestamp_utc"])
        self.assertIsInstance(result["latency_ms"], float)
        self.assertNotIn("raw", result["details"])

    def test_invalid_spot_payload_is_fail_not_unknown(self):
        session = FakeSession({"symbol": "BTCUSDT", "lastPrice": "0", "quoteVolume": "10", "closeTime": 1})
        runner = AcceptanceRunner(session=session, limiter=FakeLimiter())
        result = runner.check_binance_spot()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("invalid", result["reason"])

    def test_missing_provider_timestamp_is_inconclusive(self):
        session = FakeSession({
            "symbol": "BTCUSDT",
            "lastPrice": "65000",
            "quoteVolume": "123456",
            "closeTime": None,
        })
        runner = AcceptanceRunner(session=session, limiter=FakeLimiter())
        result = runner.check_binance_spot()
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertIsNone(result["source_timestamp_utc"])

    def test_derived_spot_check_is_inconclusive_without_both_venues(self):
        runner = AcceptanceRunner(limiter=FakeLimiter())
        result = runner.check_spot_price_agreement()
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertIn("unavailable until both providers", result["reason"])

    def test_region_or_access_restriction_is_inconclusive_not_provider_failure(self):
        for status_code in (403, 429, 451):
            runner = AcceptanceRunner(
                session=FakeBlockedSession(status_code),
                limiter=FakeLimiter(),
            )
            result = runner.check_binance_spot()
            self.assertEqual(result["status"], "INCONCLUSIVE")
            self.assertIn(f"HTTP {status_code}", result["reason"])

    def test_missing_rss_endpoint_is_inconclusive_not_provider_failure(self):
        runner = AcceptanceRunner(
            session=FakeBlockedSession(404),
            limiter=FakeLimiter(),
        )
        result = runner.check_rss(
            "social_rss", "https://www.reddit.com/r/CryptoCurrency/.rss"
        )
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertIn("HTTP 404", result["reason"])

    def test_missing_perpetual_venues_make_derived_checks_inconclusive(self):
        runner = AcceptanceRunner(limiter=FakeLimiter())
        runner.check_perpetual_agreement()
        derived = [item for item in runner.results]
        self.assertEqual(len(derived), 3)
        self.assertTrue(all(item["status"] == "INCONCLUSIVE" for item in derived))

    def test_germany_spot_provider_snapshot_passes_cross_venue_price_check(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        runner = AcceptanceRunner(limiter=FakeLimiter())
        runner._values["binance_spot"] = {"price": 75228.0}
        runner._values["bybit_spot"] = {"price": 75221.4}
        runner.results.extend([
            {"check": "binance_spot", "source_timestamp_utc": now},
            {"check": "bybit_spot", "source_timestamp_utc": now},
        ])
        result = runner.check_spot_price_agreement()
        self.assertEqual(result["status"], "PASS")
        self.assertLess(result["details"]["difference_pct"], 0.01)

    def test_germany_perpetual_snapshot_passes_price_funding_and_interval_checks(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        runner = AcceptanceRunner(limiter=FakeLimiter())
        runner._values["binance_perpetual"] = {
            "mark_price": 75194.32082609,
            "funding_rate": 0.0001,
            "funding_interval_hours": 8.0,
        }
        runner._values["bybit_perpetual"] = {
            "mark_price": 75191.2,
            "funding_rate": 0.00007689,
            "funding_interval_hours": 8.0,
        }
        runner.results.extend([
            {"check": "binance_perpetual", "source_timestamp_utc": now},
            {"check": "bybit_perpetual", "source_timestamp_utc": now},
        ])
        runner.check_perpetual_agreement()
        derived = runner.results[-3:]
        self.assertEqual([item["status"] for item in derived], ["PASS", "PASS", "PASS"])
        self.assertLess(derived[0]["details"]["difference_pct"], 0.01)
        self.assertEqual(derived[1]["details"]["binance_interval_hours"], 8.0)
        self.assertLess(derived[2]["details"]["absolute_difference"], 0.0002)


    def test_default_watchlist_runs_market_checks_for_all_five_symbols_and_shared_checks_once(self):
        calls = []

        class FakeRunner:
            def __init__(self, *, symbol, **kwargs):
                self.symbol = symbol
                self.results = []

            def _record(self, name):
                calls.append((self.symbol, name))
                self.results.append({"check": name, "status": "PASS", "symbol": self.symbol})

            def check_binance_spot(self): self._record("binance_spot")
            def check_bybit_spot(self): self._record("bybit_spot")
            def check_spot_price_agreement(self): self._record("spot_price_agreement")
            def check_binance_perpetual(self): self._record("binance_perpetual")
            def check_bybit_perpetual(self): self._record("bybit_perpetual")
            def check_perpetual_agreement(self):
                self._record("perpetual_price_agreement")
                self._record("funding_interval_agreement")
                self._record("funding_rate_agreement")
            def check_evm_rpc(self, name, url, chain_id): self._record(name)
            def check_solana_rpc(self): self._record("solana_rpc")
            def check_fear_greed(self): self._record("fear_greed")
            def check_rss(self, name, url): self._record(name)

        with patch("scripts.live_provider_acceptance.AcceptanceRunner", FakeRunner):
            report = run_watchlist(
                session=object(),
                limiter=FakeLimiter(),
            )

        self.assertEqual(report["symbols"], list(DEFAULT_ACCEPTANCE_SYMBOLS))
        self.assertEqual(set(report["symbol_reports"]), set(DEFAULT_ACCEPTANCE_SYMBOLS))
        for symbol in DEFAULT_ACCEPTANCE_SYMBOLS:
            self.assertEqual(report["symbol_reports"][symbol]["check_count"], 8)
        shared_names = [name for symbol, name in calls if name in {
            "ethereum_rpc", "base_rpc", "solana_rpc", "fear_greed", "news_rss", "social_rss"
        }]
        self.assertCountEqual(shared_names, [
            "ethereum_rpc", "base_rpc", "solana_rpc", "fear_greed", "news_rss", "social_rss"
        ])
        self.assertEqual(report["overall_status"], "PASS")
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertFalse(report["safety"]["production_or_live_account_accessed"])

    def test_watchlist_rejects_empty_duplicate_and_injected_symbols(self):
        for symbols in ((), ("BTCUSDT", "BTCUSDT"), ("BTCUSDT&x=1",)):
            with self.subTest(symbols=symbols):
                with self.assertRaises(ValueError):
                    run_watchlist(symbols, session=object(), limiter=FakeLimiter())

    def test_cross_venue_price_divergence_is_a_hard_failure(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        runner = AcceptanceRunner(limiter=FakeLimiter())
        runner._values["binance_spot"] = {"price": 100.0}
        runner._values["bybit_spot"] = {"price": 120.0}
        runner.results.extend([
            {"check": "binance_spot", "source_timestamp_utc": now},
            {"check": "bybit_spot", "source_timestamp_utc": now},
        ])
        result = runner.check_spot_price_agreement()
        self.assertEqual(result["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
