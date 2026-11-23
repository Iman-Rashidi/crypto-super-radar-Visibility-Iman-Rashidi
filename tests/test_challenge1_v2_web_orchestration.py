from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from crypto_super_radar import challenge1_v2_orchestration as v2_orchestration
from crypto_super_radar import ai_supervisor
from crypto_super_radar import web_app
from crypto_super_radar.blofin_demo_executor import (
    BloFinDemoEventStore, _challenge1_two_reviewer_approval_ready,
)


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
            "instId": "BTC-USDT",
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
        interval = 5 * 60_000 if bar == "5m" else v2_orchestration.BAR_INTERVAL_MS[bar]
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


class FakeBloFinDemoTradeClient(FakeBloFinReadOnly):
    base_url = web_app.DEMO_REST_URL
    api_key = "fake-demo-key"
    api_secret = "fake-demo-secret"
    passphrase = "fake-demo-passphrase"

    def __init__(self, *, now_ms: int | None = None):
        super().__init__(now_ms=now_ms)
        self.calls = []
        self.order_args = None
        self.position_id = "v2-fake-position-1"

    def verify_trade_permission(self):
        self.calls.append("trade_permission")
        return {"readOnly": 0, "trade_permission": True}

    def check_clock_skew(self, *, inst_id):
        self.calls.append("clock")
        return {"offset_ms": 0}

    def get_position_mode(self):
        self.calls.append("position_mode")
        return {"positionMode": "net_mode"}

    def get_futures_balance(self, *, product_type="USDT-FUTURES"):
        return {"totalEquity": "1000", "equity": "1000"}

    def get_positions(self, *, inst_id=None):
        self.calls.append(("positions", inst_id))
        if self.order_args is None:
            return []
        filled = str(self.order_args["size"])
        signed = filled if self.order_args["side"] == "buy" else "-" + filled
        return [{
            "positionId": self.position_id,
            "instId": self.order_args["inst_id"],
            "positionSide": self.order_args["position_side"],
            "positions": signed,
        }]

    def get_open_orders(self, *, inst_id=None):
        self.calls.append(("open_orders", inst_id))
        return []

    def set_futures_leverage(self, inst_id, leverage, *, margin_mode="isolated", position_side=None):
        self.calls.append(("set_leverage", leverage, margin_mode, position_side))
        return {"code": "0", "data": [{"code": "0"}]}

    def place_futures_order(self, **kwargs):
        self.calls.append(("place_order", dict(kwargs)))
        self.order_args = dict(kwargs)
        return {"code": "0", "data": [{"code": "0", "orderId": "v2-fake-order-1"}]}

    def get_order_detail(self, inst_id, *, client_order_id):
        self.calls.append(("order_detail", client_order_id))
        return {
            "clientOrderId": client_order_id,
            "orderId": "v2-fake-order-1",
            "positionId": self.position_id,
            "instId": inst_id,
            "side": self.order_args["side"],
            "size": self.order_args["size"],
            "filledSize": self.order_args["size"],
            "averagePrice": "100",
            "state": "filled",
        }

    def get_open_tpsl_orders(self, *, inst_id=None, client_order_id=None, limit=100):
        self.calls.append(("tpsl", client_order_id))
        if self.order_args is None:
            return []
        return [{
            "tpslId": "v2-fake-tpsl-1",
            "clientOrderId": client_order_id,
            "positionId": self.position_id,
            "instId": inst_id,
            "positionSide": self.order_args["position_side"],
            "side": "sell" if self.order_args["side"] == "buy" else "buy",
            "size": self.order_args["size"],
            "actualSize": self.order_args["size"],
            "state": "live",
            "tpTriggerPrice": self.order_args["take_profit"],
            "tpTriggerPriceType": "mark",
            "slTriggerPrice": self.order_args["stop_loss"],
            "slTriggerPriceType": "mark",
        }]


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


    def test_v2_orchestration_attaches_live_dual_review_and_real_executor_gate_accepts_it(self):
        self.record = make_v2_record(self.client.now_ms)
        nemotron = approved_review("OpenRouter", "nemotron/test-model")
        groq = approved_review("Groq", "llama/test-model")
        with (
            patch.object(v2_orchestration, "analyze_signal", return_value=dict(self.record)),
            patch.object(ai_supervisor._ai, "run_free_ai_second_opinion", return_value=nemotron) as reviewer_one,
            patch.object(ai_supervisor._ai, "run_independent_ai_supervisor", return_value=groq) as reviewer_two,
        ):
            result = v2_orchestration.analyze_challenge1_v2_market(
                self.client, "BTC-USDT", local_now_ms=self.client.now_ms,
            )
        self.assertEqual(result["ai_consensus"]["status"], "APPROVED_BY_BOTH")
        self.assertEqual(result["ai_consensus"]["reviewed_signal_id"], result["signal_id"])
        self.assertEqual({row["reviewer_id"] for row in result["ai_reviews"]}, {"nemotron", "second_ai"})
        self.assertTrue(all(row["review"]["reviewed_signal_id"] == result["signal_id"] for row in result["ai_reviews"]))
        ready, reason = _challenge1_two_reviewer_approval_ready(result)
        self.assertTrue(ready, reason)
        reviewer_one.assert_called_once()
        reviewer_two.assert_called_once()

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
            "instId": "BTC-USDT",
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
            {"instId": "BTC-USDT", "fundingTime": self.client.now_ms - 60 * 60_000, "fundingRate": "0.0001", "fundingKind": "0"},
            {"instId": "BTC-USDT", "fundingTime": self.client.now_ms - 10 * 60_000, "fundingRate": "0.1", "fundingKind": "1"},
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
                patch.object(ai_supervisor._ai, "run_free_ai_second_opinion", return_value=review_one) as reviewer_one,
                patch.object(ai_supervisor._ai, "run_independent_ai_supervisor", return_value=review_two) as reviewer_two,
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

    def test_gated_v2_single_shot_runs_real_executor_and_post_fill_tpsl_verification(self):
        store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        close_store = store.close
        store.close = lambda: None
        trade_client = FakeBloFinDemoTradeClient(now_ms=self.client.now_ms)
        with patch.dict(os.environ, {
            "BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED": "true",
            "CHALLENGE_1_V2_SINGLE_SHOT_WRITE": "true",
            "SINGLE_SHOT_DEMO_TEST": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_DEMO_AUTOPILOT_ENABLED": "false",
        }):
            with (
                patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"),
                patch.object(web_app, "BloFinDemoEventStore", return_value=store),
                patch.object(web_app, "BloFinDemoReadOnly", return_value=self.client),
                patch.object(web_app, "BloFinDemoTradeClient", return_value=trade_client),
                patch.object(web_app, "_blofin_demo_autopilot_enabled", return_value=False),
                patch.object(web_app, "get_fresh_execution_quote", return_value={
                    "instrument_id": "BTC-USDT", "ticker_ts_ms": self.client.now_ms,
                    "mark_ts_ms": self.client.now_ms, "last": 100.0,
                    "bidPrice": 99.99, "askPrice": 100.01, "markPrice": 100.0,
                    "spread_fraction": 0.0002, "quote_age_ms": 0,
                }),
                patch.object(v2_orchestration, "analyze_signal", return_value=dict(self.record)),
                patch.object(ai_supervisor._ai, "run_free_ai_second_opinion", return_value=approved_review(
                    "OpenRouter", "nemotron/test-model",
                )),
                patch.object(ai_supervisor._ai, "run_independent_ai_supervisor", return_value=approved_review(
                    "Groq", "llama/test-model",
                )),
            ):
                web_app._run_blofin_demo_execution_job(
                    self.job_id, "BTC-USDT", "CHALLENGE_1_V2",
                )

        job = web_app.BLOFIN_EXECUTION_JOBS[self.job_id]
        result = job["result"]
        self.assertEqual(job["stage"], "V2_SINGLE_SHOT_POST_FILL_VERIFIED")
        self.assertEqual(result["status"], "FILLED")
        self.assertTrue(result["orders_submitted"])
        self.assertTrue(result["trade_executed"])
        self.assertTrue(result["post_fill_tp_sl_verified"])
        self.assertEqual(result["position_id"], trade_client.position_id)
        self.assertEqual(result["protection"]["tp_verified"], True)
        self.assertEqual(result["protection"]["sl_verified"], True)
        self.assertEqual(
            sum(1 for call in trade_client.calls if isinstance(call, tuple) and call[0] == "place_order"),
            1,
        )
        order = store.latest_order()
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["position_id"], trade_client.position_id)
        self.assertTrue(store.single_shot_lock_status()["locked"])
        close_store()

    def test_v2_write_flag_without_all_independent_safety_switches_stays_blocked(self):
        with patch.dict(os.environ, {
            "BLOFIN_CHALLENGE1_V2_ORCHESTRATION_ENABLED": "true",
            "CHALLENGE_1_V2_SINGLE_SHOT_WRITE": "true",
            "SINGLE_SHOT_DEMO_TEST": "false",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_DEMO_AUTOPILOT_ENABLED": "false",
        }):
            with (
                patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"),
                patch.object(web_app, "BloFinDemoEventStore", return_value=self.store),
                patch.object(web_app, "BloFinDemoReadOnly", return_value=self.client),
                patch.object(v2_orchestration, "analyze_signal", return_value=dict(self.record)),
                patch.object(ai_supervisor._ai, "run_free_ai_second_opinion", return_value=approved_review(
                    "OpenRouter", "nemotron/test-model",
                )),
                patch.object(ai_supervisor._ai, "run_independent_ai_supervisor", return_value=approved_review(
                    "Groq", "llama/test-model",
                )),
            ):
                web_app._run_blofin_demo_execution_job(
                    self.job_id, "BTC-USDT", "CHALLENGE_1_V2",
                )
        job = web_app.BLOFIN_EXECUTION_JOBS[self.job_id]
        self.assertEqual(job["stage"], "V2_BLOCKED")
        self.assertFalse(job["result"]["orders_submitted"])
        self.assertFalse(job["result"]["trade_executed"])

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


    def test_confirmed_dashboard_fast_trigger_is_sent_to_v2_ai_reviewers(self):
        now_ms = int(time.time() * 1000)
        signal_time = pd.Timestamp(now_ms, unit="ms", tz="UTC").floor("5min") - pd.Timedelta(minutes=5)
        fast_id = f"challenge1-v4:BTC-USDT:5m:{signal_time.isoformat()}"
        fast = {
            "signal": "LONG", "instrument_id": "BTC-USDT",
            "signal_id": fast_id, "signal_bar_open_utc": signal_time.isoformat(),
            "reason_fa": "موج صعودی ۵ دقیقه‌ای تأیید شد.",
        }
        fast_path = {
            "signal": "LONG", "signal_id": f"challenge1-v2-fast:BTC-USDT:5m:{signal_time.isoformat()}:LONG",
            "instrument_id": "BTC-USDT", "execution_profile": "CHALLENGE_1_V2",
            "entry": 100.0, "stop_loss": 98.5, "take_profit": 102.25,
            "gross_rr": 1.5, "confidence_score": 100.0,
            "score_parts": {
                "confirmed_5m_pattern": 35, "confirmed_5m_elliott_wave": 35,
                "completed_15m_ema21_alignment": 20, "btc_master_alignment": 10,
            },
            "regime": {"status": "RANGE", "direction": "NONE"},
            "signal_candle_open_utc": signal_time.isoformat(),
            "challenge1_v2_evidence": {
                "decision_engine_version": "CHALLENGE_1_V2_FAST_5M_15M_ELLIOTT",
                "signal_id": f"challenge1-v2-fast:BTC-USDT:5m:{signal_time.isoformat()}:LONG",
                "instrument_id": "BTC-USDT", "setup_timeframe": "5m",
                "entry_execution_timeframe": "15m",
                "context_timeframes": ["15m", "1H", "4H"],
                "higher_timeframes_used": True, "completed_candles_only": True,
                "signal_candle_open_utc": signal_time.isoformat(),
            },
            "orders_submitted": False, "fast_trigger_active": True,
        }
        approved = {
            "approved": True, "status": "APPROVED",
            "ai_review": {"status": "APPROVED"},
            "second_ai_review": {"status": "APPROVED"},
            "ai_reviews": [{"reviewer_id": "nemotron"}, {"reviewer_id": "groq"}],
            "ai_consensus": {"status": "APPROVED_BY_BOTH"},
        }
        client = FakeBloFinReadOnly(now_ms=now_ms)
        with (
            patch.object(v2_orchestration, "analyze_signal", return_value={"signal": "NO_TRADE", "orders_submitted": False}),
            patch.object(v2_orchestration, "analyze_blofin_challenge1", return_value=fast),
            patch.object(v2_orchestration, "build_fast_trigger_signal", return_value=fast_path),
            patch.object(v2_orchestration, "challenge1_v2_execution_evidence_ready", return_value=(True, "ok")),
            patch.object(v2_orchestration, "run_challenge1_v2_supervisor_reviews", return_value=approved) as reviewers,
        ):
            result = v2_orchestration.analyze_challenge1_v2_market(client, "BTC-USDT", local_now_ms=now_ms)
        reviewers.assert_called_once()
        self.assertEqual(result["signal"], "LONG")
        self.assertTrue(result["fast_trigger_active"])
        self.assertEqual(result["market_data_proof"]["signal_timeframe"], "5m")
        self.assertTrue(result["fast_trigger_diagnostic"]["fast_path_selected"])
        self.assertFalse(result["orders_submitted"])


class Challenge1EarlyWave3Tests(unittest.TestCase):
    def _frames(self):
        idx5 = pd.date_range("2026-01-01T00:00:00Z", periods=80, freq="5min")
        idx15 = pd.date_range("2026-01-01T00:00:00Z", periods=80, freq="15min")
        values5 = [100.0 + (i % 5) * 0.08 for i in range(80)]
        values15 = [100.0 + (i % 5) * 0.12 for i in range(80)]
        values5[-3:] = [108.8, 109.2, 110.1]
        values15[-4:] = [108.0, 108.4, 108.7, 109.0]
        def frame(index, values):
            close = pd.Series(values, index=index, dtype=float)
            return pd.DataFrame({
                "open": close - 0.03, "high": close + 0.12,
                "low": close - 0.12, "close": close, "volume": 1000.0,
            }, index=index)
        return frame(idx5, values5), frame(idx15, values15)

    def _wave(self):
        points = [
            {"kind":"LOW","price":100.0,"timestamp":"2026-01-01T04:00:00+00:00","confirmed_at":"2026-01-01T04:05:00+00:00"},
            {"kind":"HIGH","price":109.0,"timestamp":"2026-01-01T05:00:00+00:00","confirmed_at":"2026-01-01T05:05:00+00:00"},
            {"kind":"LOW","price":104.0,"timestamp":"2026-01-01T05:30:00+00:00","confirmed_at":"2026-01-01T05:35:00+00:00"},
        ]
        return {"pattern":"BULLISH","phase":"WAVES_1_AND_2_CONFIRMED","pivots":points,"confirmed_pivot_count":3}

    def test_early_w3_breakout_is_detected_before_any_1h_close(self):
        from crypto_super_radar.challenge1_v2 import detect_early_wave3_breakout
        five, fifteen = self._frames()
        asof = five.index[-1] + pd.Timedelta(minutes=5)
        with (
            patch("crypto_super_radar.challenge1_v2.monitor_elliott_waves", side_effect=[self._wave(), self._wave()]),
            patch("crypto_super_radar.challenge1_v2.rsi", return_value=pd.Series([55.0] * len(five), index=five.index)),
        ):
            result = detect_early_wave3_breakout(five, fifteen, asof=asof, entry_price=110.1)
        self.assertEqual(result["signal"], "LONG")
        self.assertLessEqual(result["breakout_distance_pct"], 0.012)
        self.assertLessEqual((result["entry"] - result["stop_loss"]) / result["entry"], 0.035)
        self.assertGreaterEqual(result["risk_reward"], 1.5)

    def test_extended_w3_breakout_is_blocked_by_anti_top_chase_guard(self):
        from crypto_super_radar.challenge1_v2 import detect_early_wave3_breakout
        five, fifteen = self._frames()
        asof = five.index[-1] + pd.Timedelta(minutes=5)
        with (
            patch("crypto_super_radar.challenge1_v2.monitor_elliott_waves", side_effect=[self._wave(), self._wave()]),
            patch("crypto_super_radar.challenge1_v2.rsi", return_value=pd.Series([55.0] * len(five), index=five.index)),
        ):
            result = detect_early_wave3_breakout(five, fifteen, asof=asof, entry_price=112.0)
        self.assertEqual(result["signal"], "NO_TRADE")
        self.assertIn("Anti-top-chase", result["reason"])


if __name__ == "__main__":
    unittest.main()
