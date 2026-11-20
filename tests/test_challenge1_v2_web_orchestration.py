from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from crypto_super_radar import challenge1_v2_orchestration as v2_orchestration
from crypto_super_radar import web_app


def make_v2_record(asof_ms: int) -> dict:
    asof = pd.Timestamp(asof_ms, unit="ms", tz="UTC")
    signal_open = asof - pd.Timedelta(minutes=15)
    h1_open = asof - pd.Timedelta(hours=1)
    h4_open = asof - pd.Timedelta(hours=4)
    symbol = "BTC-USDT"
    side = "LONG"
    signal_id = f"challenge1-v2:{symbol}:15m:{signal_open.isoformat()}:{side}"
    wave = {
        "status": "CONFIRMED",
        "pattern": "BULLISH",
        "phase": "WAVE_3_CONFIRMED",
        "confirmed_pivot_count": 3,
        "invalidation_price": 98.0,
        "timeframe": "1H",
        "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
        "expected_signal_direction": "LONG",
        "latest_closed_candle_open_utc": h1_open.isoformat(),
        "data_asof_utc": asof.isoformat(),
        "wave_alignment": "ALIGNED",
        "pivots": [
            {
                "index": 1, "kind": "LOW", "price": 95.0,
                "timestamp": (asof - pd.Timedelta(hours=8)).isoformat(),
                "confirmed_at": (asof - pd.Timedelta(hours=7)).isoformat(),
            },
            {
                "index": 2, "kind": "HIGH", "price": 110.0,
                "timestamp": (asof - pd.Timedelta(hours=6)).isoformat(),
                "confirmed_at": (asof - pd.Timedelta(hours=5)).isoformat(),
            },
            {
                "index": 3, "kind": "LOW", "price": 99.0,
                "timestamp": (asof - pd.Timedelta(hours=3)).isoformat(),
                "confirmed_at": (asof - pd.Timedelta(hours=2)).isoformat(),
            },
        ],
    }
    return {
        "signal": side,
        "signal_id": signal_id,
        "instrument_id": symbol,
        "execution_profile": "CHALLENGE_1_V2",
        "entry": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.4,
        "gross_rr": 2.4,
        "confidence_score": 85.0,
        "score_parts": {
            "h1_h4_alignment": 30,
            "adx_gt_28": 20,
            "value_zone_rejection": 25,
            "volume_gt_1_5x": 0,
            "btc_alignment": 10,
        },
        "regime": {
            "status": "TREND",
            "direction": side,
            "candle_open_utc": h1_open.isoformat(),
            "candle_open_utc_4h": h4_open.isoformat(),
            "aligned_4h": True,
        },
        "funding_rate": 0.0001,
        "funding_kind": "0",
        "funding_event_time_ms": asof_ms - 60 * 60_000,
        "funding_max_age_ms": v2_orchestration.FUNDING_MAX_AGE_MS,
        "spread_fraction": 0.0002,
        "signal_candle_open_utc": signal_open.isoformat(),
        "orders_submitted": False,
        "challenge1_v2_evidence": {
            "decision_engine_version": "CHALLENGE_1_V2_1H_4H_15M",
            "signal_id": signal_id,
            "instrument_id": symbol,
            "setup_timeframe": "15m",
            "context_timeframes": ["1H", "4H"],
            "higher_timeframes_used": True,
            "completed_candles_only": True,
            "signal_candle_open_utc": signal_open.isoformat(),
            "context_1h_candle_open_utc": h1_open.isoformat(),
            "context_4h_candle_open_utc": h4_open.isoformat(),
            "data_asof_utc": asof.isoformat(),
            "funding_rate": 0.0001,
            "funding_kind": "0",
            "funding_event_time_ms": asof_ms - 60 * 60_000,
            "funding_max_age_ms": v2_orchestration.FUNDING_MAX_AGE_MS,
            "elliott_wave": wave,
            "wave_alignment": "ALIGNED",
        },
    }


class FakeBloFinReadOnly:
    def __init__(self, *, now_ms: int | None = None, missing_bar: str | None = None,
                 funding_rows: list[dict] | None = None):
        self.now_ms = now_ms or int(time.time() * 1000)
        self.missing_bar = missing_bar
        self.funding_rows = funding_rows if funding_rows is not None else [{
            "fundingTime": self.now_ms - 60 * 60_000,
            "fundingRate": "0.0001",
            "fundingKind": "0",
        }]
        self.requested_bars: list[tuple[str, str]] = []

    def get_tradable_instrument(self, inst_id: str):
        return {
            "instId": inst_id, "apiPermission": "full", "state": "live",
            "contractType": "linear", "settleCurrency": "USDT",
            "contractValue": "0.001", "minSize": "0.1",
            "lotSize": "0.1", "tickSize": "0.1", "maxMarketSize": "100000",
        }

    def get_tickers(self, *, inst_id: str):
        return [{
            "instId": inst_id, "ts": str(self.now_ms), "last": "100",
            "bidPrice": "99.99", "askPrice": "100.01",
        }]

    def get_mark_price(self, *, inst_id: str):
        return [{"instId": inst_id, "ts": str(self.now_ms), "markPrice": "100"}]

    def get_candles(self, inst_id: str, *, bar: str, limit: int):
        self.requested_bars.append((inst_id, bar))
        if bar == self.missing_bar:
            return []
        interval = v2_orchestration.BAR_INTERVAL_MS[bar]
        count = 300 if bar in {"1H", "4H"} else 120
        last_open = self.now_ms - interval
        rows = []
        for i in range(count):
            timestamp = last_open - (count - 1 - i) * interval
            rows.append([
                str(timestamp), "100", "101", "99", "100",
                "1000", "100000", "0", "1",
            ])
        return list(reversed(rows))

    def get_funding_rate_history(self, *, inst_id: str, limit: int):
        return list(self.funding_rows)

    def get_futures_balance(self, *, product_type: str = "USDT-FUTURES"):
        return {"currency": "USDT", "equity": "1000"}

    def get_positions(self):
        return []

    def get_open_orders(self):
        return []


class FakeEventStore:
    def __init__(self):
        self.recorded = []

    def trade_budget_status(self):
        return {"unresolved_orders": 0}

    def record_challenge1_v2_decision(self, *, instrument_id: str, challenge_record):
        self.recorded.append((instrument_id, challenge_record))
        return "v2-experience-test"

    def close(self):
        return None


def approved_review(provider: str, model: str):
    return {
        "status": "APPROVED",
        "provider": provider,
        "model": model,
        "returned_model": model,
        "paper_entry_eligible": True,
        "request_attempted": True,
        "response_source": "LIVE_API",
        "response_validated": True,
        "model_verified": True,
        "expected_side": "LONG",
        "trader_proposal": "LONG",
        "elliott_wave_phase": "WAVE_3_CONFIRMED",
        "elliott_wave_direction": "BULLISH",
    }


class Challenge1V2MarketIngestionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeBloFinReadOnly()

    def test_v2_ingests_closed_1h_4h_15m_and_btc_15m_at_one_asof(self):
        asof_ms = self.client.now_ms
        expected = make_v2_record(asof_ms)
        captured = {}

        def analysis_stub(**kwargs):
            captured.update(kwargs)
            return expected

        with patch.object(v2_orchestration, "analyze_signal", side_effect=analysis_stub):
            record = v2_orchestration.analyze_challenge1_v2_market(
                self.client, "BTC-USDT", local_now_ms=asof_ms,
            )

        self.assertEqual(record["execution_profile"], "CHALLENGE_1_V2")
        self.assertEqual(record["signal"], "LONG")
        self.assertEqual(captured["asof"], pd.Timestamp(asof_ms, unit="ms", tz="UTC"))
        self.assertEqual(len(captured["frame_1h"]), 300)
        self.assertEqual(len(captured["frame_4h"]), 300)
        self.assertEqual(len(captured["frame_15m"]), 120)
        self.assertIs(captured["btc_15m"], captured["frame_15m"])
        self.assertEqual(captured["funding_kind"], "0")
        self.assertEqual(captured["funding_rate"], 0.0001)
        self.assertTrue(record["market_data_proof"]["common_asof"])
        self.assertTrue(record["market_data_proof"]["contract_specs_verified"])
        self.assertEqual(record["contract_specs"]["contractValue"], "0.001")
        self.assertEqual(
            {bar for _, bar in self.client.requested_bars},
            {"1H", "4H", "15m"},
        )

    def test_missing_4h_history_fails_closed_before_analysis(self):
        self.client.missing_bar = "4H"
        with patch.object(v2_orchestration, "analyze_signal") as analyze:
            with self.assertRaises(v2_orchestration.Challenge1V2DataBlocked):
                v2_orchestration.analyze_challenge1_v2_market(
                    self.client, "BTC-USDT", local_now_ms=self.client.now_ms,
                )
        analyze.assert_not_called()

    def test_stale_funding_fails_closed_before_analysis(self):
        self.client.funding_rows = [{
            "fundingTime": self.client.now_ms - v2_orchestration.FUNDING_MAX_AGE_MS - 1,
            "fundingRate": "0.0001", "fundingKind": "0",
        }]
        with patch.object(v2_orchestration, "analyze_signal") as analyze:
            with self.assertRaisesRegex(v2_orchestration.Challenge1V2DataBlocked, "stale"):
                v2_orchestration.analyze_challenge1_v2_market(
                    self.client, "BTC-USDT", local_now_ms=self.client.now_ms,
                )
        analyze.assert_not_called()

    def test_latest_non_regular_funding_never_falls_back_to_older_regular_event(self):
        self.client.funding_rows = [
            {"fundingTime": self.client.now_ms - 60 * 60_000, "fundingRate": "0.0001", "fundingKind": "0"},
            {"fundingTime": self.client.now_ms - 10 * 60_000, "fundingRate": "0.1", "fundingKind": "1"},
        ]
        with patch.object(v2_orchestration, "analyze_signal") as analyze:
            with self.assertRaisesRegex(v2_orchestration.Challenge1V2DataBlocked, "not regular"):
                v2_orchestration.analyze_challenge1_v2_market(
                    self.client, "BTC-USDT", local_now_ms=self.client.now_ms,
                )
        analyze.assert_not_called()


    def test_missing_1h_or_15m_history_fails_closed_before_analysis(self):
        for missing_bar in ("1H", "15m"):
            with self.subTest(missing_bar=missing_bar):
                client = FakeBloFinReadOnly(
                    now_ms=self.client.now_ms, missing_bar=missing_bar,
                )
                with patch.object(v2_orchestration, "analyze_signal") as analyze:
                    with self.assertRaisesRegex(
                        v2_orchestration.Challenge1V2DataBlocked, missing_bar,
                    ):
                        v2_orchestration.analyze_challenge1_v2_market(
                            client, "BTC-USDT", local_now_ms=client.now_ms,
                        )
                analyze.assert_not_called()

    def test_stale_required_timeframes_fail_closed_before_analysis(self):
        for stale_bar in ("1H", "4H", "15m"):
            with self.subTest(stale_bar=stale_bar):
                client = FakeBloFinReadOnly(now_ms=self.client.now_ms)
                get_candles = client.get_candles

                def stale_candles(inst_id: str, *, bar: str, limit: int):
                    rows = get_candles(inst_id, bar=bar, limit=limit)
                    if bar == stale_bar:
                        shift_ms = 2 * v2_orchestration.BAR_INTERVAL_MS[bar]
                        for row in rows:
                            row[0] = str(int(row[0]) - shift_ms)
                    return rows

                client.get_candles = stale_candles
                with patch.object(v2_orchestration, "analyze_signal") as analyze:
                    with self.assertRaisesRegex(
                        v2_orchestration.Challenge1V2DataBlocked, "stale",
                    ):
                        v2_orchestration.analyze_challenge1_v2_market(
                            client, "BTC-USDT", local_now_ms=client.now_ms,
                        )
                analyze.assert_not_called()


class Challenge1V2WebOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.job_id = "v2-web-orchestration-test"
        web_app.BLOFIN_EXECUTION_JOBS.pop(self.job_id, None)
        self.client = FakeBloFinReadOnly()
        self.store = FakeEventStore()
        self.record = make_v2_record(self.client.now_ms)
        self.contract = self.client.get_tradable_instrument("BTC-USDT")

    def tearDown(self):
        web_app.BLOFIN_EXECUTION_JOBS.pop(self.job_id, None)

    def test_end_to_end_v2_dry_run_reaches_two_reviewers_and_order_plan_without_order_write(self):
        review_one = approved_review("OpenRouter", "nemotron/test-model")
        review_two = approved_review("Groq", "llama/test-model")
        with patch.dict(os.environ, {"BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED": "true"}):
            with (
                patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"),
                patch.object(web_app, "BloFinDemoEventStore", return_value=self.store),
                patch.object(web_app, "BloFinDemoReadOnly", return_value=self.client),
                patch.object(web_app, "_equity_from_balance", return_value=1000.0),
                patch.object(web_app, "get_fresh_execution_quote", return_value={
                    "instrument_id": "BTC-USDT", "ticker_ts_ms": self.client.now_ms,
                    "mark_ts_ms": self.client.now_ms, "last": 100.0,
                    "bidPrice": 99.99, "askPrice": 100.01, "markPrice": 100.0,
                    "spread_fraction": 0.0002, "quote_age_ms": 0,
                }),
                patch.object(v2_orchestration, "analyze_signal", return_value=self.record) as engine,
                patch.object(web_app, "run_free_ai_second_opinion", return_value=review_one) as reviewer_one,
                patch.object(web_app, "run_independent_ai_supervisor", return_value=review_two) as reviewer_two,
                patch.object(web_app, "analyze_blofin_challenge1", side_effect=AssertionError("5m legacy fallback forbidden")),
            ):
                web_app._run_blofin_demo_execution_job(
                    self.job_id, "BTC-USDT", "CHALLENGE_1_V2",
                )

        job = web_app.BLOFIN_EXECUTION_JOBS[self.job_id]
        self.assertEqual(job["stage"], "V2_DRY_RUN_ORDER_PLAN_READY")
        self.assertEqual(job["result"]["status"], "DRY_RUN_ORDER_PLAN_READY")
        self.assertFalse(job["result"]["orders_submitted"])
        self.assertFalse(job["result"]["trade_executed"])
        self.assertEqual(job["result"]["execution_method"], "READ_ONLY_DRY_RUN_NO_ORDER_WRITE")
        self.assertEqual(job["result"]["order_plan"]["side"], "LONG")
        self.assertEqual(job["result"]["aggregate_risk_preview"]["max_concurrent_positions"], 4)
        self.assertEqual(len(job["ai_reviews"]), 2)
        self.assertEqual(
            {item["reviewer_id"] for item in job["ai_reviews"]},
            {"nemotron", "second_ai"},
        )
        self.assertEqual(
            {item["review"].get("reviewed_signal_id") for item in job["ai_reviews"]},
            {self.record["signal_id"]},
        )
        engine.assert_called_once()
        reviewer_one.assert_called_once()
        reviewer_two.assert_called_once()
        self.assertEqual(len(self.store.recorded), 1)

    def test_missing_4h_blocks_web_worker_without_calling_legacy_5m_analyzer(self):
        self.client.missing_bar = "4H"
        with patch.dict(os.environ, {"BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED": "true"}):
            with (
                patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"),
                patch.object(web_app, "BloFinDemoEventStore", return_value=self.store),
                patch.object(web_app, "BloFinDemoReadOnly", return_value=self.client),
                patch.object(v2_orchestration, "analyze_signal") as engine,
                patch.object(web_app, "analyze_blofin_challenge1", side_effect=AssertionError("5m fallback forbidden")) as legacy,
            ):
                web_app._run_blofin_demo_execution_job(
                    self.job_id, "BTC-USDT", "CHALLENGE_1_V2",
                )
        job = web_app.BLOFIN_EXECUTION_JOBS[self.job_id]
        self.assertEqual(job["stage"], "V2_BLOCKED")
        self.assertEqual(job["result"]["status"], "BLOCKED")
        self.assertFalse(job["result"]["orders_submitted"])
        engine.assert_not_called()
        legacy.assert_not_called()

    def test_v2_dispatch_is_separate_and_legacy_profile_is_not_routed_into_v2(self):
        with patch.object(web_app, "_run_blofin_demo_v2_readonly_job") as v2_worker:
            web_app._run_blofin_demo_execution_job(
                self.job_id, "BTC-USDT", "CHALLENGE_1_V2",
            )
        v2_worker.assert_called_once()
        self.assertEqual(v2_worker.call_args.args[:2], (self.job_id, "BTC-USDT"))
        v2_worker.reset_mock()
        with patch.object(web_app, "_blofin_demo_execution_readiness", return_value={
            "status": "BLOCKED", "durable_storage": False, "event_store_readable": False,
            "message_fa": "blocked for regression test",
        }):
            web_app._run_blofin_demo_execution_job(
                self.job_id, "BTC-USDT", "CHALLENGE_1",
            )
        v2_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
