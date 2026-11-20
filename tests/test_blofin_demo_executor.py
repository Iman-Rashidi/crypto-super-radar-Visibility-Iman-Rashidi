from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from crypto_super_radar.blofin_demo_executor import (
    BloFinDemoExecutionBlocked,
    BloFinDemoExecutionUnknown,
    BloFinDemoEventStore,
    BloFinDemoExecutor,
    BloFinDemoRiskPolicy,
    BloFinDemoTradeClient,
    build_demo_order_plan,
)
from crypto_super_radar.blofin_demo_readonly import (
    BloFinDemoHTTPError,
    BloFinDemoReadOnly,
    BloFinDemoResponseError,
    DEMO_REST_URL,
    _signature,
)


def signal_record(**overrides):
    pass_count = int(overrides.get("gate_pass_count", 0))
    fail_count = int(overrides.get("gate_fail_count", 12))
    unknown_count = int(overrides.get("gate_unknown_count", 0))
    statuses = (
        ["PASS"] * max(0, min(12, pass_count))
        + ["FAIL"] * max(0, min(12, fail_count))
        + ["UNKNOWN"] * max(0, min(12, unknown_count))
    )[:12]
    statuses += ["UNKNOWN"] * (12 - len(statuses))
    record = {
        "signal": "LONG",
        "signal_id": "challenge1-v4:BTC-USDT:5m:2026-11-13T18:00:00+00:00",
        "signal_bar_open_utc": "2026-11-13T18:00:00+00:00",
        "inst_id": "BTC-USDT",
        "instrument_id": "BTC-USDT",
        "execution_profile": "CHALLENGE_1",
        "execution": {
            "status": "PASS",
            "direction": "LONG",
            "signal_id": "challenge1-v4:BTC-USDT:5m:2026-11-13T18:00:00+00:00",
        },
        "confirmation_score": None,
        "estimated_risk_score": None,
        "gate_pass_count": pass_count,
        "gate_fail_count": fail_count,
        "gate_unknown_count": unknown_count,
        "gates": [{"gate_id": gate_id, "status": statuses[gate_id - 1]} for gate_id in range(1, 13)],
        "entry_price_usd": 100.0,
        "stop_loss_usd": 98.0,
        "take_profit_usd": 106.0,
    }
    record.update(overrides)
    side = str(record["signal"]).upper()
    pattern_direction = "BULLISH" if side == "LONG" else "BEARISH"
    pattern_name = "Bullish Engulfing" if side == "LONG" else "Bearish Engulfing"
    base_bar = datetime.fromisoformat("2026-11-13T18:00:00+00:00")
    bar_offset = int(overrides.get("bar_offset_minutes", 0))
    signal_bar = str(
        overrides.get("signal_bar_open_utc")
        or (base_bar + timedelta(minutes=bar_offset)).isoformat()
    )
    last_time = datetime.fromisoformat(signal_bar)
    candles_5m = [
        {
            "open_time_utc": (last_time - timedelta(minutes=10)).isoformat(),
            "open": 99.0, "high": 100.0, "low": 98.5, "close": 99.4, "volume": 900.0,
        },
        {
            "open_time_utc": (last_time - timedelta(minutes=5)).isoformat(),
            "open": 99.4, "high": 100.5, "low": 99.0, "close": 100.0, "volume": 950.0,
        },
        {
            "open_time_utc": signal_bar,
            "open": 100.0 if side == "LONG" else 101.0,
            "high": 101.5 if side == "LONG" else 101.2,
            "low": 99.5 if side == "LONG" else 99.4,
            "close": 101.0 if side == "LONG" else 100.0,
            "volume": 1100.0,
        },
    ]
    signal_id = f"challenge1-v4:BTC-USDT:5m:{signal_bar}"
    record["signal_id"] = signal_id
    record["signal_bar_open_utc"] = signal_bar
    record["execution"] = {"status": "PASS", "direction": side, "signal_id": signal_id}
    if side == "SHORT":
        if "stop_loss_usd" not in overrides:
            record["stop_loss_usd"] = 102.0
        if "take_profit_usd" not in overrides:
            record["take_profit_usd"] = 96.0
    wave_phase = "WAVE_3_CONFIRMED"
    elliott_wave = {
        "status": "CONFIRMED",
        "pattern": pattern_direction,
        "phase": wave_phase,
        "confirmed_pivot_count": 5,
        "invalidation_price": 97.0 if side == "LONG" else 103.0,
        "wave_count": 2,
        "next_wave": 3,
    }
    record["challenge1_evidence"] = {
        "decision_engine_version": "CHALLENGE_1_LOCAL_RULES_V5_5M_ELLIOTT_AWARE",
        "setup_timeframe": "5m",
        "completed_candles_only": True,
        "higher_timeframes_used": False,
        "legacy_12_gates_used_for_entry": False,
        "external_ai_required": True,
        "tinyfish_required": False,
        "volume_is_advisory": True,
        "decision": side,
        "detected_patterns": [{
            "name_en": pattern_name, "direction": pattern_direction,
            "category": "CANDLESTICK", "timeframe": "5m",
        }],
        "chosen_pattern": {
            "name_en": pattern_name, "direction": pattern_direction,
            "category": "CANDLESTICK", "timeframe": "5m",
        },
        "confirmation_candle": dict(candles_5m[-1]),
        "candles_5m": candles_5m,
        "elliott_wave": elliott_wave,
        "wave_alignment": "ALIGNED",
    }
    reviewers = [
        {
            "reviewer_id": "nemotron",
            "review": {
                "reviewer_id": "nemotron", "provider": "OpenRouter",
                "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "returned_model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "status": "APPROVED", "paper_entry_eligible": True,
                "request_attempted": True, "response_source": "LIVE_API",
                "response_validated": True, "model_verified": True,
                "expected_side": side, "trader_proposal": side,
                "elliott_wave_phase": wave_phase,
                "elliott_wave_direction": pattern_direction,
            },
        },
        {
            "reviewer_id": "second_ai",
            "review": {
                "reviewer_id": "second_ai", "provider": "Groq",
                "model": "openai/gpt-oss-120b",
                "returned_model": "openai/gpt-oss-120b",
                "status": "APPROVED", "paper_entry_eligible": True,
                "request_attempted": True, "response_source": "LIVE_API",
                "response_validated": True, "model_verified": True,
                "expected_side": side, "trader_proposal": side,
                "elliott_wave_phase": wave_phase,
                "elliott_wave_direction": pattern_direction,
            },
        },
    ]
    final_request_time = last_time + timedelta(minutes=5)
    initial_request_time = last_time
    reviewed_at_utc = datetime.now(timezone.utc).isoformat()
    for row in reviewers:
        review = row["review"]
        if row["reviewer_id"] == "nemotron":
            review["request_started_at_utc"] = final_request_time.isoformat()
        else:
            review["requested_at_utc"] = final_request_time.isoformat()
        review["completed_at_utc"] = (final_request_time + timedelta(seconds=2)).isoformat()
        review["reviewed_signal_id"] = signal_id
        review["reviewed_at_utc"] = reviewed_at_utc

    record["ai_reviews"] = reviewers
    record["ai_consensus"] = {
        "status": "APPROVED_BY_BOTH",
        "primary_status": "APPROVED", "second_status": "APPROVED",
        "primary_verified": True, "second_verified": True,
        "primary_approved": True, "second_approved": True,
        "providers_independent": True, "models_independent": True,
        "expected_side": side, "elliott_wave_phase": wave_phase,
        "elliott_wave_direction": pattern_direction,
        "two_reviewer_approval": True,
        "decision": "AUTHORIZE_DEMO_EXECUTION",
        "entry_gate_enforced": True,
        "reviewed_signal_id": signal_id,
        "reviewed_at_utc": reviewed_at_utc,
    }
    initial_reviews = copy.deepcopy(reviewers)
    for row in initial_reviews:
        review = row["review"]
        if row["reviewer_id"] == "nemotron":
            review["request_started_at_utc"] = initial_request_time.isoformat()
        else:
            review["requested_at_utc"] = initial_request_time.isoformat()
        review["completed_at_utc"] = (initial_request_time + timedelta(seconds=2)).isoformat()
    initial_bar = last_time - timedelta(minutes=5)
    initial_signal_id = f"challenge1-v4:BTC-USDT:5m:{initial_bar.isoformat()}"
    for row in initial_reviews:
        row["review"]["reviewed_signal_id"] = initial_signal_id
        row["review"]["reviewed_at_utc"] = reviewed_at_utc
    initial_consensus = copy.deepcopy(record["ai_consensus"])
    initial_consensus["reviewed_signal_id"] = initial_signal_id
    initial_consensus["reviewed_at_utc"] = reviewed_at_utc
    evaluation = {
        "status": "PASS",
        "minimum_window_seconds": 300,
        "elapsed_seconds": 300.5,
        "initial_signal_id": initial_signal_id,
        "final_signal_id": signal_id,
        "initial_signal_bar_open_utc": initial_bar.isoformat(),
        "final_signal_bar_open_utc": signal_bar,
        "initial_direction": side,
        "final_direction": side,
        "initial_pattern_name": pattern_name,
        "final_pattern_name": pattern_name,
        "initial_pattern_direction": pattern_direction,
        "final_pattern_direction": pattern_direction,
        "initial_pattern_category": "CANDLESTICK",
        "final_pattern_category": "CANDLESTICK",
        "direction_preserved": True,
        "pattern_preserved": True,
        "new_closed_candle_confirmed": True,
        "review_rounds_completed": 2,
        "entry_evaluation_heartbeat_seconds": 3.0,
        "window_started_at_utc": initial_request_time.isoformat(),
        "window_completed_at_utc": final_request_time.isoformat(),
        "initial_round": {
            "ai_reviews": initial_reviews,
            "ai_consensus": initial_consensus,
        },
        "final_round": {
            "reviewer_ids": ["nemotron", "second_ai"],
            "consensus_status": "APPROVED_BY_BOTH",
            "reviewed_signal_id": signal_id,
        },
    }
    record["entry_evaluation_window"] = evaluation
    record["ai_consensus"]["entry_evaluation_window"] = copy.deepcopy(evaluation)
    return record


def instrument_record(**overrides):
    record = {
        "instId": "BTC-USDT",
        "apiPermission": "full",
        "state": "live",
        "contractType": "linear",
        "settleCurrency": "USDT",
        "contractValue": "0.001",
        "minSize": "0.1",
        "lotSize": "0.1",
        "tickSize": "0.1",
        "maxMarketSize": "100000",
    }
    record.update(overrides)
    return record


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            error = requests.HTTPError("mock HTTP error")
            error.response = self
            raise error

    def json(self):
        return self.payload


class BloFinDemoOrderPlanTests(unittest.TestCase):
    def test_contract_size_is_sized_from_risk_and_contract_value(self):
        plan = build_demo_order_plan(
            signal_record(), instrument_record(), inst_id="BTC-USDT",
            equity_usdt=1000, leverage=3,
        )
        # Risk budget = 2.5 USDT; loss per contract = 2 * 0.001 USDT.
        self.assertEqual(plan["quantity"], "1250")
        self.assertEqual(plan["risk_budget_usdt"], 2.5)
        self.assertEqual(plan["leverage"], 3)
        self.assertEqual(len(plan["client_order_id"]), 32)
        self.assertTrue(plan["client_order_id"].isalnum())

    def test_legacy_gate_rows_are_observational_not_entry_requirements(self):
        for overrides in (
            {"gate_fail_count": 12, "gate_pass_count": 0, "gate_unknown_count": 0},
            {"gate_unknown_count": 12, "gate_pass_count": 0, "gate_fail_count": 0},
        ):
            with self.subTest(overrides=overrides):
                plan = build_demo_order_plan(
                    signal_record(**overrides), instrument_record(),
                    inst_id="BTC-USDT", equity_usdt=1000, leverage=1,
                    policy=BloFinDemoRiskPolicy(max_leverage=1),
                )
                self.assertEqual(plan["side"], "LONG")

    def test_legacy_execution_profiles_are_rejected(self):
        for profile in ("EASY", "STANDARD", "STRICT"):
            with self.subTest(profile=profile), self.assertRaises(BloFinDemoExecutionBlocked):
                build_demo_order_plan(
                    signal_record(execution_profile=profile), instrument_record(),
                    inst_id="BTC-USDT", equity_usdt=1000, leverage=1,
                )

    def test_rejects_reduced_only_or_non_linear_contract(self):
        for overrides in (
            {"apiPermission": "reduce_only"},
            {"contractType": "inverse"},
            {"settleCurrency": "BTC"},
            {"state": "suspend"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(BloFinDemoExecutionBlocked):
                    build_demo_order_plan(
                        signal_record(), instrument_record(**overrides),
                        inst_id="BTC-USDT", equity_usdt=1000,
                    )

    def test_rejects_bad_reward_risk_and_leverage(self):
        with self.assertRaises(BloFinDemoExecutionBlocked):
            build_demo_order_plan(
                signal_record(take_profit_usd=101), instrument_record(),
                inst_id="BTC-USDT", equity_usdt=1000,
            )
        with self.assertRaises(BloFinDemoExecutionBlocked):
            build_demo_order_plan(
                signal_record(), instrument_record(), inst_id="BTC-USDT",
                equity_usdt=1000, leverage=4,
            )

    def test_short_stop_is_rounded_toward_entry(self):
        plan = build_demo_order_plan(
            signal_record(
                signal="SHORT", stop_loss_usd=102.04, take_profit_usd=96.04
            ),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )
        self.assertEqual(plan["stop_loss"], "102")
        self.assertEqual(plan["take_profit"], "96")
        self.assertEqual(plan["position_side_long_short"], "short")

    def test_minimum_size_blocks_too_small_risk_budget(self):
        policy = BloFinDemoRiskPolicy(risk_per_trade_pct=0.00001)
        with self.assertRaises(BloFinDemoExecutionBlocked):
            build_demo_order_plan(
                signal_record(), instrument_record(minSize="1000", lotSize="1000"),
                inst_id="BTC-USDT", equity_usdt=100, policy=policy,
            )


class BloFinDemoTradeClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.get.return_value = FakeResponse({
            "code": "0", "data": {"apiKey": "not-a-real-key", "readOnly": 0}
        })
        self.session.post.return_value = FakeResponse({
            "code": "0", "data": {"code": "0", "orderId": "demo-order-1"}
        })
        self.client = BloFinDemoTradeClient(
            api_key="not-a-real-key",
            api_secret="not-a-real-secret",
            passphrase="not-a-real-passphrase",
            session=self.session,
            clock_ms=lambda: 1_800_000_000_000,
        )

    def test_existing_demo_credentials_are_reused_when_trade_key_is_not_separate(self):
        from crypto_super_radar.blofin_demo_executor import BloFinDemoTradeClient
        with patch.dict(os.environ, {
            "BLOFIN_DEMO_TRADE_API_KEY": "",
            "BLOFIN_DEMO_TRADE_API_SECRET": "",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "",
            "BLOFIN_DEMO_API_KEY": "existing-demo-key",
            "BLOFIN_DEMO_API_SECRET": "existing-demo-secret",
            "BLOFIN_DEMO_PASSPHRASE": "existing-demo-pass",
        }, clear=False):
            client = BloFinDemoTradeClient(session=Mock())
        self.assertEqual(client.api_key, "existing-demo-key")
        self.assertEqual(client.api_secret, "existing-demo-secret")
        self.assertEqual(client.passphrase, "existing-demo-pass")

    def test_order_payload_is_compact_signed_and_contains_attached_tp_sl(self):
        response = self.client.place_futures_order(
            inst_id="BTC-USDT", side="buy", position_side="net", size="2.5",
            client_order_id="0123456789abcdef0123456789abcdef",
            take_profit="104", stop_loss="98",
        )
        self.assertEqual(response["data"]["orderId"], "demo-order-1")
        args, kwargs = self.session.post.call_args
        self.assertTrue(args[0].startswith(DEMO_REST_URL))
        self.assertTrue(args[0].endswith("/api/v1/trade/order"))
        body = kwargs["data"].decode("utf-8")
        payload = json.loads(body)
        self.assertEqual(body, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        self.assertEqual(payload["tpTriggerPrice"], "104")
        self.assertEqual(payload["tpOrderPrice"], "-1")
        self.assertEqual(payload["slTriggerPrice"], "98")
        self.assertEqual(payload["slOrderPrice"], "-1")
        self.assertEqual(payload["tpTriggerPriceType"], "mark")
        self.assertEqual(payload["slTriggerPriceType"], "mark")
        headers = kwargs["headers"]
        expected = _signature(
            "not-a-real-secret", "POST", "/api/v1/trade/order",
            "1800000000000", headers["ACCESS-NONCE"], body,
        )
        self.assertEqual(headers["ACCESS-SIGN"], expected)
        self.assertTrue(headers["ACCESS-NONCE"])

    def test_set_leverage_is_capped_at_three_and_uses_isolated_margin(self):
        self.client.set_futures_leverage("BTC-USDT", 3, position_side="long")
        args, kwargs = self.session.post.call_args
        self.assertTrue(args[0].endswith("/api/v1/account/set-leverage"))
        payload = json.loads(kwargs["data"])
        self.assertEqual(payload["leverage"], "3")
        self.assertEqual(payload["marginMode"], "isolated")
        self.assertEqual(payload["positionSide"], "long")
        with self.assertRaises(ValueError):
            self.client.set_futures_leverage("BTC-USDT", 4)

    def test_write_post_is_blocked_when_key_is_read_only(self):
        self.session.get.return_value = FakeResponse({
            "code": "0", "data": {"apiKey": "not-a-real-key", "readOnly": 1}
        })
        with self.assertRaises(BloFinDemoExecutionBlocked):
            self.client.set_futures_leverage("BTC-USDT", 1)
        self.session.post.assert_not_called()

    def test_nested_order_error_is_rejected(self):
        self.session.post.return_value = FakeResponse({
            "code": "0", "data": {"code": "102089", "msg": "position mode mismatch"}
        })
        with self.assertRaises(BloFinDemoResponseError):
            self.client.place_futures_order(
                inst_id="BTC-USDT", side="buy", position_side="net", size="1",
                client_order_id="0123456789abcdef0123456789abcdef",
                take_profit="104", stop_loss="98",
            )

    def test_only_explicitly_allowlisted_post_paths_are_permitted(self):
        with self.assertRaises(BloFinDemoExecutionBlocked):
            self.client._post("/api/v1/account/set-position-mode", {"positionMode": "net_mode"})

    def test_stop_loss_amendment_changes_only_sl_leg_and_uses_request_id(self):
        self.client.amend_tpsl_stop_loss(
            inst_id="BTC-USDT",
            tpsl_id="2510128401",
            new_stop_loss="101.25",
            request_id="amend123",
        )
        args, kwargs = self.session.post.call_args
        self.assertTrue(args[0].endswith("/api/v1/trade/amend-tpsl"))
        body = json.loads(kwargs["data"])
        self.assertEqual(body["instId"], "BTC-USDT")
        self.assertEqual(body["tpslId"], "2510128401")
        self.assertEqual(body["requestId"], "amend123")
        self.assertEqual(body["newSlTriggerPrice"], "101.25")
        self.assertNotIn("newTpTriggerPrice", body)
        self.assertGreater(body["ttl"], 0)

    def test_stop_loss_amendment_rejects_zero_and_invalid_ids_before_post(self):
        with self.assertRaises(ValueError):
            self.client.amend_tpsl_stop_loss(
                inst_id="BTC-USDT", tpsl_id="2510128401",
                new_stop_loss="0", request_id="amend123",
            )
        with self.assertRaises(ValueError):
            self.client.amend_tpsl_stop_loss(
                inst_id="BTC-USDT", tpsl_id="2510128401",
                new_stop_loss="101", request_id="bad request id",
            )
        with self.assertRaises(ValueError):
            self.client.amend_tpsl_stop_loss(
                inst_id="BTC-USDT", tpsl_id="invalid-id",
                new_stop_loss="101", request_id="amend123",
            )
        self.session.post.assert_not_called()

    def test_trade_post_refuses_redirects_and_never_follows_location(self):
        self.session.post.return_value = FakeResponse(
            {"code": "0", "data": {"orderId": "unused"}}, status=302,
            headers={"Location": "https://api.blofin.com/api/v1/trade/order"},
        )
        with self.assertRaises(BloFinDemoHTTPError) as caught:
            self.client.place_futures_order(
                inst_id="BTC-USDT", side="buy", position_side="net", size="1",
                client_order_id="0123456789abcdef0123456789abcdef",
                take_profit="104", stop_loss="98",
            )
        self.assertEqual(caught.exception.status_code, 302)
        self.session.post.assert_called_once()
        self.assertFalse(self.session.post.call_args.kwargs["allow_redirects"])

    def test_order_post_429_is_not_retried(self):
        self.session.post.return_value = FakeResponse(
            {"code": "0", "data": {"code": "0", "orderId": "unused"}},
            status=429,
            headers={"Retry-After": "1"},
        )
        with self.assertRaises(BloFinDemoHTTPError) as caught:
            self.client.place_futures_order(
                inst_id="BTC-USDT", side="buy", position_side="net", size="1",
                client_order_id="0123456789abcdef0123456789abcdef",
                take_profit="104", stop_loss="98",
            )
        self.assertEqual(caught.exception.status_code, 429)
        self.session.post.assert_called_once()

    def test_tradable_instrument_preserves_exact_decimal_metadata_strings(self):
        client = BloFinDemoReadOnly(session=Mock())
        client.get_futures_instruments = Mock(return_value=[instrument_record(
            contractValue="0.0000000100",
            minSize="0.000000100",
            lotSize="0.0000000100",
            tickSize="0.100000",
            maxMarketSize="10.0000",
        )])
        instrument = client.get_tradable_instrument("BTC-USDT")
        self.assertEqual(instrument["contractValue"], "0.00000001")
        self.assertEqual(instrument["minSize"], "0.0000001")
        self.assertEqual(instrument["lotSize"], "0.00000001")
        self.assertEqual(instrument["tickSize"], "0.1")
        self.assertEqual(instrument["maxMarketSize"], "10")
        for field in ("contractValue", "minSize", "lotSize", "tickSize", "maxMarketSize"):
            self.assertIsInstance(instrument[field], str)

    def test_tradable_instrument_rejects_non_finite_decimal_metadata(self):
        client = BloFinDemoReadOnly(session=Mock())
        client.get_futures_instruments = Mock(return_value=[
            instrument_record(contractValue="NaN")
        ])
        with self.assertRaises(BloFinDemoResponseError):
            client.get_tradable_instrument("BTC-USDT")


class BloFinDemoEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        })
        self.env.start()
        self.store = BloFinDemoEventStore(":memory:")

    def tearDown(self):
        self.store.close()
        self.env.stop()

    def _plan(self):
        return build_demo_order_plan(
            signal_record(), instrument_record(), inst_id="BTC-USDT",
            equity_usdt=1000,
        )

    def test_manual_action_events_are_linked_idempotent_and_append_multiple_requests(self):
        plan = self._plan()
        self.store.reserve_order(plan)
        client_order_id = plan["client_order_id"]
        first = self.store.record_manual_action(
            client_order_id,
            "AI_SUPERVISOR_STOP_AMEND_REQUESTED",
            {"request_id": "req-1", "position_id": "pos-1", "stop": "101"},
        )
        replay = self.store.record_manual_action(
            client_order_id,
            "AI_SUPERVISOR_STOP_AMEND_REQUESTED",
            {"request_id": "req-1", "position_id": "pos-1", "stop": "101"},
        )
        second = self.store.record_manual_action(
            client_order_id,
            "AI_SUPERVISOR_STOP_AMEND_REQUESTED",
            {"request_id": "req-2", "position_id": "pos-1", "stop": "102"},
        )
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(replay["idempotency_suffix"], first["idempotency_suffix"])
        self.assertNotEqual(second["idempotency_suffix"], first["idempotency_suffix"])
        events = self.store.recent_events(client_order_id, limit=20)
        requests = [event for event in events if event["event_type"] == "AI_SUPERVISOR_STOP_AMEND_REQUESTED"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            {event["payload"]["request_id"] for event in requests},
            {"req-1", "req-2"},
        )

    def test_manual_action_audit_rejects_unlinked_or_invalid_events(self):
        with self.assertRaises(ValueError):
            self.store.record_manual_action(
                "", "AI_SUPERVISOR_CLOSE_REQUESTED", {"request_id": "req-1"},
            )
        with self.assertRaises(ValueError):
            self.store.record_manual_action(
                "client123", "not valid", {"request_id": "req-1"},
            )

    def test_reservation_is_atomic_and_duplicate_safe(self):
        plan = self._plan()
        self.assertTrue(self.store.reserve_order(plan))
        self.assertFalse(self.store.reserve_order(plan))
        rows = self.store._db.execute(
            "SELECT status FROM blofin_demo_orders WHERE client_order_id=?",
            (plan["client_order_id"],),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        events = self.store._db.execute(
            "SELECT event_type FROM blofin_demo_execution_events WHERE client_order_id=?",
            (plan["client_order_id"],),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["RESERVED"])

    def test_default_capacity_is_four_and_counts_linked_exchange_positions(self):
        policy = BloFinDemoRiskPolicy(risk_per_trade_pct=0.001)
        self.assertEqual(BloFinDemoRiskPolicy().max_concurrent_positions, 4)
        plans = [
            build_demo_order_plan(
                signal_record(bar_offset_minutes=index * 5),
                instrument_record(),
                inst_id="BTC-USDT",
                equity_usdt=1000,
                leverage=3,
                policy=policy,
            )
            for index in range(5)
        ]

        for index, plan in enumerate(plans):
            reserved = self.store.reserve_order(
                plan,
                max_concurrent_positions=policy.max_concurrent_positions,
                external_open_positions=index,
            )
            self.assertEqual(reserved, index < 4)
            if reserved:
                self.store.update_order(plan["client_order_id"], "FILLED")

        active = self.store._db.execute(
            "SELECT COUNT(*) FROM blofin_demo_orders WHERE status='FILLED'"
        ).fetchone()[0]
        self.assertEqual(active, 4)

    def test_has_any_orders_tracks_reserved_order_history(self):
        self.assertFalse(self.store.has_any_orders())
        self.store.reserve_order(self._plan())
        self.assertTrue(self.store.has_any_orders())

    def test_pending_reconcile_is_unresolved_and_blocks_new_capacity(self):
        first = self._plan()
        self.assertTrue(self.store.reserve_order(first))
        self.store.update_order(first["client_order_id"], "PENDING_RECONCILE")
        budget = self.store.trade_budget_status()
        self.assertEqual(budget["unresolved_orders"], 1)
        second = build_demo_order_plan(
            signal_record(bar_offset_minutes=5),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )
        self.assertFalse(self.store.reserve_order(second, max_concurrent_positions=1))

    def test_list_unresolved_orders_returns_every_supported_state(self):
        first = build_demo_order_plan(
            signal_record(bar_offset_minutes=10),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000, leverage=3,
        )
        second = build_demo_order_plan(
            signal_record(bar_offset_minutes=15),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000, leverage=3,
        )
        self.assertTrue(self.store.reserve_order(first, max_concurrent_positions=3))
        self.assertTrue(self.store.reserve_order(second, max_concurrent_positions=3))
        self.store.update_order(first["client_order_id"], "PENDING_RECONCILE")
        self.store.update_order(second["client_order_id"], "UNKNOWN")

        rows = self.store.list_unresolved_orders(limit=50)
        self.assertEqual(
            {row["client_order_id"] for row in rows},
            {first["client_order_id"], second["client_order_id"]},
        )
        self.assertEqual(
            {row["status"] for row in rows},
            {"PENDING_RECONCILE", "UNKNOWN"},
        )
        self.assertEqual(self.store.trade_budget_status()["unresolved_orders"], 2)

    def test_reservation_records_exact_aggregate_risk_and_margin(self):
        plan = build_demo_order_plan(
            signal_record(), instrument_record(), inst_id="BTC-USDT",
            equity_usdt=1000, leverage=3,
        )
        self.assertGreater(float(plan["reserved_risk_usdt"]), 0)
        self.assertGreater(float(plan["reserved_margin_usdt"]), 0)
        self.assertEqual(plan["aggregate_risk_limit_usdt"], "5")
        self.assertEqual(plan["aggregate_margin_limit_usdt"], "100")
        self.assertTrue(self.store.reserve_order(plan))
        budget = self.store.trade_budget_status()
        self.assertTrue(budget["aggregate_exposure_complete"])
        self.assertEqual(
            budget["aggregate_reserved_risk_usdt"], plan["reserved_risk_usdt"]
        )
        self.assertEqual(
            budget["aggregate_reserved_margin_usdt"], plan["reserved_margin_usdt"]
        )

    def test_aggregate_exposure_budget_blocks_third_risk_reservation_atomically(self):
        plans = [
            build_demo_order_plan(
                signal_record(bar_offset_minutes=index * 5),
                instrument_record(), inst_id="BTC-USDT", equity_usdt=1000, leverage=3,
            )
            for index in range(3)
        ]
        self.assertTrue(self.store.reserve_order(plans[0], max_concurrent_positions=3))
        self.assertTrue(self.store.reserve_order(plans[1], max_concurrent_positions=3))
        self.assertFalse(self.store.reserve_order(plans[2], max_concurrent_positions=3))
        budget = self.store.trade_budget_status()
        self.assertEqual(budget["active_reservations"], 2)
        self.assertLessEqual(float(budget["aggregate_reserved_risk_usdt"]), 5.0)
        self.assertLessEqual(float(budget["aggregate_reserved_margin_usdt"]), 100.0)

    def test_aggregate_margin_budget_blocks_second_full_margin_reservation(self):
        first = build_demo_order_plan(
            signal_record(bar_offset_minutes=5),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000, leverage=1,
        )
        second = build_demo_order_plan(
            signal_record(bar_offset_minutes=10),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000, leverage=1,
        )
        self.assertTrue(self.store.reserve_order(first, max_concurrent_positions=3))
        self.assertFalse(self.store.reserve_order(second, max_concurrent_positions=3))
        self.assertEqual(self.store.trade_budget_status()["active_reservations"], 1)

    def test_legacy_active_reservation_without_exposure_snapshot_fails_closed(self):
        first = build_demo_order_plan(
            signal_record(bar_offset_minutes=0),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000, leverage=3,
        )
        second = build_demo_order_plan(
            signal_record(bar_offset_minutes=5),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000, leverage=3,
        )
        self.assertTrue(self.store.reserve_order(first, max_concurrent_positions=3))
        self.store._db.execute(
            "UPDATE blofin_demo_orders SET reserved_margin_usdt=NULL WHERE client_order_id=?",
            (first["client_order_id"],),
        )
        self.store._db.commit()
        self.assertFalse(self.store.reserve_order(second, max_concurrent_positions=3))
        self.assertFalse(self.store.trade_budget_status()["aggregate_exposure_complete"])

    def test_external_positions_or_pending_orders_fail_closed_without_exposure_metadata(self):
        plan = self._plan()
        self.assertFalse(self.store.reserve_order(plan, external_open_positions=1))
        self.assertFalse(self.store.reserve_order(plan, external_pending_orders=1))
        self.assertFalse(self.store.has_any_orders())

    def test_capacity_reservation_is_atomic_across_independent_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "shared-demo.sqlite3")
            stores = [BloFinDemoEventStore(database_path) for _ in range(8)]
            try:
                plans = [
                    build_demo_order_plan(
                        signal_record(bar_offset_minutes=index * 5),
                        instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
                    )
                    for index in range(len(stores))
                ]
                barrier = threading.Barrier(len(stores))

                def reserve_on_independent_connection(index):
                    barrier.wait(timeout=10)
                    return stores[index].reserve_order(
                        plans[index], max_concurrent_positions=1,
                    )

                with ThreadPoolExecutor(max_workers=len(stores)) as pool:
                    results = list(pool.map(
                        reserve_on_independent_connection, range(len(stores))
                    ))
                self.assertEqual(results.count(True), 1)
                self.assertEqual(results.count(False), len(stores) - 1)
                total = stores[0]._db.execute(
                    "SELECT COUNT(*) FROM blofin_demo_orders"
                ).fetchone()[0]
                self.assertEqual(total, 1)
            finally:
                for store in stores:
                    store.close()

    def test_autopilot_state_round_trips_through_durable_store(self):
        state = {
            "enabled": True,
            "status": "SEARCHING_FOR_SIGNAL",
            "cycles_started": 12,
            "last_cycle_status": "completed",
            "last_cycle_diagnostics": {
                "signal": "NO_TRADE",
                "orders_submitted": False,
                "trade_executed": False,
            },
        }
        self.assertIsNone(self.store.load_autopilot_state())
        self.store.save_autopilot_state(state)
        self.assertEqual(self.store.load_autopilot_state(), state)

        updated = dict(state, cycles_started=13)
        self.store.save_autopilot_state(updated)
        self.assertEqual(self.store.load_autopilot_state(), updated)

    def test_trade_budget_tracks_daily_count_and_signal_idempotency(self):
        plan = self._plan()
        self.assertFalse(self.store.has_signal(plan["signal_id"]))
        self.store.reserve_order(plan)
        self.assertTrue(self.store.has_signal(plan["signal_id"]))
        budget = self.store.trade_budget_status()
        self.assertEqual(budget["orders_today"], 1)
        self.assertEqual(budget["orders_total"], 1)
        self.assertIsNone(budget["max_orders_per_utc_day"])
        self.assertTrue(budget["daily_capacity_available"])
        self.assertEqual(budget["unresolved_orders"], 1)

    def test_challenge1_decision_and_not_executed_outcome_are_durable_and_idempotent(self):
        challenge = signal_record(signal="NO_TRADE")
        challenge["reason_fa"] = "No completed pattern confirmation."
        experience_id = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT",
            challenge_record=challenge,
        )
        duplicate_id = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT",
            challenge_record=challenge,
        )
        self.assertEqual(experience_id, duplicate_id)

        first = self.store.record_experience_not_executed(
            experience_id=experience_id,
            instrument_id="BTC-USDT",
            signal="NO_TRADE",
            stage="CHALLENGE_1_SIGNAL_REJECTED",
            reason="No completed pattern confirmation.",
        )
        second = self.store.record_experience_not_executed(
            experience_id=experience_id,
            instrument_id="BTC-USDT",
            signal="NO_TRADE",
            stage="CHALLENGE_1_SIGNAL_REJECTED",
            reason="No completed pattern confirmation.",
        )
        self.assertEqual(first["event_id"], second["event_id"])
        verified = self.store.learning_memory.verify_experience(experience_id)
        self.assertEqual(verified["status"], "PASS")
        experience = self.store.learning_memory.get_experience(experience_id)
        self.assertEqual(
            [event["event_type"] for event in experience["events"]],
            ["DECISION_RECORDED", "OUTCOME_LABELLED"],
        )
        outcome = experience["events"][-1]["payload"]
        self.assertEqual(outcome["realized_result"], "NOT_EXECUTED")
        self.assertIsNone(outcome["realized_net_pnl"])

    def test_challenge1_analysis_failure_is_persisted_as_unknown_not_executed_without_fake_candles(self):
        timestamp = "2026-11-16T14:00:00+00:00"
        first = self.store.record_challenge1_analysis_failure(
            cycle_id="cycle-market-data-failure-01",
            instrument_id="ETH-USDT",
            failure_stage="CHALLENGE_1_MARKET_DATA_ANALYSIS",
            error_type="DataProviderError",
            reason="Insufficient completed candle data.",
            timestamp_utc=timestamp,
        )
        second = self.store.record_challenge1_analysis_failure(
            cycle_id="cycle-market-data-failure-01",
            instrument_id="ETH-USDT",
            failure_stage="CHALLENGE_1_MARKET_DATA_ANALYSIS",
            error_type="DataProviderError",
            reason="Insufficient completed candle data.",
            timestamp_utc=timestamp,
        )
        self.assertEqual(first, second)
        self.assertEqual(self.store.learning_memory.verify_experience(first)["status"], "PASS")
        experience = self.store.learning_memory.get_experience(first)
        self.assertEqual(
            [event["event_type"] for event in experience["events"]],
            ["DECISION_RECORDED", "OUTCOME_LABELLED"],
        )
        decision = experience["events"][0]["payload"]
        self.assertEqual(decision["decision"]["decision_class"], "DATA_INSUFFICIENT")
        snapshot = decision["input_snapshot"]
        self.assertEqual(snapshot["data_quality"], "UNKNOWN")
        self.assertIsNone(snapshot["candle_timestamp_utc"])
        self.assertFalse(snapshot["ohlcv_available"])
        self.assertFalse(snapshot["future_data_in_input"])
        outcome = experience["events"][-1]["payload"]
        self.assertEqual(outcome["realized_result"], "NOT_EXECUTED")
        self.assertEqual(outcome["decision_correctness"], "UNKNOWN")
        self.assertIsNone(outcome["realized_net_pnl"])

    def test_closed_position_outcome_uses_exact_position_id_and_deducts_trading_fees(self):
        challenge = signal_record()
        experience_id = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT",
            challenge_record=challenge,
        )
        plan = self._plan()
        plan["experience_id"] = experience_id
        self.assertTrue(self.store.reserve_order(plan))
        self.store.update_order(plan["client_order_id"], "FILLED", exchange_order_id="demo-order-77")
        self.assertTrue(self.store.attach_position_id(experience_id, "demo-position-77"))

        mismatched = [{
            "historyId": "h-wrong",
            "positionId": "another-position",
            "instId": "BTC-USDT",
            "realizedPnl": "99",
            "fee": "0",
        }]
        mismatch_result = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT", history_rows=mismatched,
            client_order_id=plan["client_order_id"], position_id="demo-position-77",
        )
        self.assertEqual(mismatch_result["status"], "WAITING_FOR_FINAL_CLOSED_POSITION")

        order_row = self.store.get_order(plan["client_order_id"])
        opened_ms = int(order_row["created_at"]) * 1000 + 5_000
        expected_size = str(plan["quantity"])
        partial = [{
            "historyId": "h-partial",
            "positionId": "demo-position-77",
            "instId": "BTC-USDT",
            "side": "buy",
            "createTime": str(opened_ms),
            "openAveragePrice": str(plan["entry_reference_price"]),
            "closePositions": str(float(expected_size) * 0.4),
            "maxPositions": expected_size,
            "realizedPnl": "0.5",
            "fee": "0.1",
        }]
        closed = [{
            "historyId": "h-closed",
            "positionId": "demo-position-77",
            "instId": "BTC-USDT",
            "side": "buy",
            "createTime": str(opened_ms),
            "openAveragePrice": str(plan["entry_reference_price"]),
            "closePositions": str(float(expected_size) * 0.6),
            "maxPositions": expected_size,
            "realizedPnl": "1.0",
            "fee": "0.1",
        }]
        missing_fee_history = [dict(closed[0])]
        missing_fee_history[0].pop("fee", None)
        missing_fee = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=missing_fee_history,
            partial_rows=partial,
            client_order_id=plan["client_order_id"],
            position_id="demo-position-77",
        )
        self.assertEqual(missing_fee["status"], "WAITING_FOR_EXCHANGE_FEE_DATA")
        self.assertEqual(self.store.pending_learning_outcome_count(), 1)

        saved = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=closed,
            partial_rows=partial,
            client_order_id=plan["client_order_id"],
            position_id="demo-position-77",
        )
        self.assertEqual(saved["status"], "RECORDED")
        experience = self.store.learning_memory.get_experience(experience_id)
        self.assertEqual(experience["learning_status"], "OUTCOME_RECORDED")
        outcome = experience["events"][-1]["payload"]
        self.assertEqual(outcome["realized_result"], "PROFIT")
        self.assertAlmostEqual(outcome["realized_net_pnl"], 1.3)
        self.assertFalse(outcome["evidence"]["funding_fees_included"])
        self.assertAlmostEqual(
            outcome["evidence"]["realized_pnl_after_reported_trading_fees"],
            1.3,
        )
        self.assertEqual(self.store.orders_pending_learning_outcome(), [])

    def test_closed_position_outcome_requires_exact_position_id_and_full_quantity(self):
        challenge = signal_record()
        experience_id = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT",
            challenge_record=challenge,
        )
        plan = self._plan()
        plan["experience_id"] = experience_id
        self.assertTrue(self.store.reserve_order(plan))
        self.store.update_order(
            plan["client_order_id"], "FILLED", exchange_order_id="demo-open-order-1"
        )
        self.assertEqual(self.store.pending_learning_outcome_count(), 1)
        self.assertTrue(self.store.attach_position_id(experience_id, "demo-position-77"))

        history = {
            "historyId": "closed-history-1",
            "positionId": "demo-position-77",
            "instId": "BTC-USDT",
            "side": "buy",
            "positionSide": "net",
            "createTime": str((int(self.store.get_order(plan["client_order_id"])["created_at"]) * 1000) + 5_000),
            "openAveragePrice": str(plan["entry_reference_price"]),
            "closeAveragePrice": "104.0",
            "closePositions": str(plan["quantity"]),
            "maxPositions": str(plan["quantity"]),
            "realizedPnl": "2.50",
            "fee": "0.10",
        }

        # A close record for a different exchange position must not label this trade.
        wrong_position = dict(history, positionId="another-position")
        waiting = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=[wrong_position],
            client_order_id=plan["client_order_id"],
            position_id="demo-position-77",
        )
        self.assertEqual(waiting["status"], "WAITING_FOR_FINAL_CLOSED_POSITION")
        self.assertEqual(
            self.store.learning_memory.get_experience(experience_id)["learning_status"],
            "PENDING_OUTCOME",
        )

        recorded = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=[history],
            client_order_id=plan["client_order_id"],
            position_id="demo-position-77",
        )
        self.assertEqual(recorded["status"], "RECORDED")
        self.assertEqual(self.store.pending_learning_outcome_count(), 0)
        self.assertEqual(recorded["realized_result"], "PROFIT")
        self.assertAlmostEqual(recorded["realized_pnl_after_reported_trading_fees"], 2.40)

        experience = self.store.learning_memory.get_experience(experience_id)
        self.assertEqual(experience["learning_status"], "OUTCOME_RECORDED")
        self.assertEqual(
            [event["event_type"] for event in experience["events"]],
            ["DECISION_RECORDED", "OUTCOME_LABELLED"],
        )
        outcome = experience["events"][-1]["payload"]
        self.assertEqual(outcome["decision_correctness"], "UNKNOWN")
        self.assertEqual(outcome["evidence"]["position_id"], "demo-position-77")
        self.assertFalse(outcome["evidence"]["funding_fees_included"])
        self.assertEqual(experience["verification"]["status"], "PASS")

        duplicate = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=[history],
            client_order_id=plan["client_order_id"],
            position_id="demo-position-77",
        )
        self.assertEqual(duplicate["status"], "ALREADY_RECORDED")
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM learning_experience_events WHERE experience_id=?",
                (experience_id,),
            ).fetchone()[0],
            2,
        )

    def test_outcome_waits_until_the_exchange_position_id_is_linked(self):
        challenge = signal_record()
        experience_id = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT",
            challenge_record=challenge,
        )
        plan = self._plan()
        plan["experience_id"] = experience_id
        self.assertTrue(self.store.reserve_order(plan))
        self.store.update_order(
            plan["client_order_id"], "FILLED", exchange_order_id="demo-open-order-2"
        )
        waiting = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=[{
                "historyId": "closed-history-2",
                "positionId": "demo-position-88",
                "instId": "BTC-USDT",
                "side": "buy",
                "createTime": str((int(self.store.get_order(plan["client_order_id"])["created_at"]) * 1000) + 5_000),
                "openAveragePrice": str(plan["entry_reference_price"]),
                "closePositions": str(plan["quantity"]),
                "maxPositions": str(plan["quantity"]),
                "realizedPnl": "1.0",
                "fee": "0.1",
            }],
            client_order_id=plan["client_order_id"],
        )
        self.assertEqual(waiting["status"], "WAITING_FOR_EXCHANGE_POSITION_LINK")
        self.assertEqual(
            self.store.learning_memory.get_experience(experience_id)["learning_status"],
            "PENDING_OUTCOME",
        )

    def test_closed_position_outcome_fails_closed_without_exact_local_identity(self):
        result = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=[],
        )
        self.assertEqual(result["status"], "IDENTITY_REQUIRED")
        self.assertEqual(
            result["reason_code"], "CLIENT_ORDER_ID_OR_POSITION_ID_REQUIRED",
        )

    def test_closed_position_outcome_targets_exact_order_when_multiple_orders_share_symbol(self):
        challenge_a = signal_record(bar_offset_minutes=0)
        challenge_b = signal_record(bar_offset_minutes=5)
        experience_a = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT", challenge_record=challenge_a,
        )
        experience_b = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT", challenge_record=challenge_b,
        )
        plan_a = build_demo_order_plan(
            challenge_a, instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )
        plan_b = build_demo_order_plan(
            challenge_b, instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )
        plan_a["experience_id"] = experience_a
        plan_b["experience_id"] = experience_b
        # This test targets identity reconciliation, not risk capacity. Keep the
        # synthetic reservations well inside the aggregate guard while preserving
        # the valid Challenge 1 signal/candle identity produced by the fixture.
        for plan in (plan_a, plan_b):
            plan["reserved_risk_usdt"] = "1"
            plan["reserved_margin_usdt"] = "10"
        self.assertNotEqual(plan_a["signal_id"], plan_b["signal_id"])
        self.assertTrue(self.store.reserve_order(plan_a, max_concurrent_positions=2))
        self.assertTrue(self.store.reserve_order(plan_b, max_concurrent_positions=2))
        self.store.update_order(
            plan_a["client_order_id"], "FILLED", exchange_order_id="demo-order-a",
        )
        self.store.update_order(
            plan_b["client_order_id"], "FILLED", exchange_order_id="demo-order-b",
        )
        self.assertTrue(self.store.attach_position_id(experience_a, "demo-position-a"))
        self.assertTrue(self.store.attach_position_id(experience_b, "demo-position-b"))

        order_a = self.store.get_order(plan_a["client_order_id"])
        history_a = {
            "historyId": "history-a-final",
            "positionId": "demo-position-a",
            "instId": "BTC-USDT",
            "side": "buy",
            "createTime": str(int(order_a["created_at"]) * 1000 + 5_000),
            "openAveragePrice": str(plan_a["entry_reference_price"]),
            "closeAveragePrice": "104.0",
            "closePositions": str(plan_a["quantity"]),
            "maxPositions": str(plan_a["quantity"]),
            "realizedPnl": "2.5",
            "fee": "0.1",
        }

        wrong_identity = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=[history_a],
            client_order_id=plan_a["client_order_id"],
            position_id="demo-position-b",
        )
        self.assertEqual(wrong_identity["status"], "POSITION_ID_MISMATCH")
        self.assertEqual(
            self.store.learning_memory.get_experience(experience_b)["learning_status"],
            "PENDING_OUTCOME",
        )

        recorded = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=[history_a],
            client_order_id=plan_a["client_order_id"],
            position_id="demo-position-a",
        )
        self.assertEqual(recorded["status"], "RECORDED")
        self.assertEqual(recorded["experience_id"], experience_a)
        self.assertEqual(recorded["position_id"], "demo-position-a")
        self.assertEqual(
            self.store.learning_memory.get_experience(experience_b)["learning_status"],
            "PENDING_OUTCOME",
        )

    def test_learning_memory_integrity_report_verifies_persisted_demo_experiences(self):
        challenge = signal_record(signal="NO_TRADE")
        experience_id = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT",
            challenge_record=challenge,
        )
        report = self.store.learning_memory.integrity_report()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["checked_count"], 1)
        self.assertEqual(report["failed_count"], 0)
        self.assertEqual(report["experience_count"], 1)
        self.assertEqual(
            self.store.learning_memory.verify_experience(experience_id)["status"],
            "PASS",
        )

    def test_event_journal_is_append_only(self):
        plan = self._plan()
        self.store.reserve_order(plan)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._db.execute(
                "UPDATE blofin_demo_execution_events SET event_type='EDITED' WHERE client_order_id=?",
                (plan["client_order_id"],),
            )

    def test_daily_loss_and_peak_drawdown_are_recorded(self):
        first = self.store.equity_guard(1000, now=1_800_000_000)
        later = self.store.equity_guard(950, now=1_800_000_100)
        self.assertEqual(first["daily_loss_pct"], 0)
        self.assertAlmostEqual(later["daily_loss_pct"], 0.05)
        self.assertAlmostEqual(later["drawdown_pct"], 0.05)


class FakeExecutorClient:
    base_url = DEMO_REST_URL
    api_key = "fake-demo-key"
    api_secret = "fake-demo-secret"
    passphrase = "fake-demo-passphrase"


    def __init__(self):
        self.calls = []
        self.ticker_quote = {"bidPrice": "99.99", "askPrice": "100.01"}
        self.order_response = {"code": "0", "data": [{"code": "0", "orderId": "simulated-demo-id"}]}

    def verify_trade_permission(self):
        return {"readOnly": 0, "trade_permission": True}

    def check_clock_skew(self, *, inst_id):
        self.calls.append("clock")
        return {"offset_ms": 0}


    def get_tickers(self, *, inst_id=None):
        self.calls.append("ticker")
        return [{"instId": inst_id, "last": "100", "ts": "1800000000000", **self.ticker_quote}]

    def get_position_mode(self):
        self.calls.append("position_mode")
        return {"positionMode": "net_mode"}

    def get_tradable_instrument(self, inst_id):
        self.calls.append("instrument")
        return instrument_record()

    def get_futures_balance(self, *, product_type="USDT-FUTURES"):
        self.calls.append("balance")
        return {"totalEquity": "1000"}

    def get_positions(self):
        self.calls.append("positions")
        return []

    def get_open_orders(self):
        self.calls.append("pending_orders")
        return []

    def set_futures_leverage(self, inst_id, leverage, *, margin_mode, position_side=None):
        self.calls.append(("set_leverage", leverage, position_side))
        return {"code": "0"}

    def place_futures_order(self, **kwargs):
        self.calls.append(("place_order", kwargs))
        return self.order_response


class BloFinDemoExecutorTests(unittest.TestCase):
    def test_two_key_safety_gate_blocks_by_default(self):
        client = Mock()
        client.base_url = DEMO_REST_URL
        store = Mock()
        store.backend = "TURSO_CLOUD_SQLITE"
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=False)
        with self.assertRaises(BloFinDemoExecutionBlocked):
            executor.execute(signal_record(), inst_id="BTC-USDT", allow_demo_orders=True)
        client.check_clock_skew.assert_not_called()

    def test_read_only_api_key_blocks_before_market_or_order_actions(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        def deny_trade_permission():
            client.calls.append("trade_permission")
            raise BloFinDemoExecutionBlocked(
                "BloFin API key is read-only (readOnly=1); no write request was sent."
            )
        client.verify_trade_permission = deny_trade_permission
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaises(BloFinDemoExecutionBlocked):
                executor.execute(
                    signal_record(), inst_id="BTC-USDT", leverage=1,
                    allow_demo_orders=True,
                )
            self.assertEqual(client.calls, ["trade_permission"])
            self.assertFalse(any(
                isinstance(call, tuple) and call[0] in {"set_leverage", "place_order"}
                for call in client.calls
            ))
        finally:
            store.close()

    def test_demo_pipeline_submits_only_after_every_gate_and_reserves_once(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        # The test store is in-memory by design; mark the backend only for exercising
        # the full orchestration path without pretending this is durable production storage.
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        executor = BloFinDemoExecutor(
            client=client, store=store, trading_enabled=True,
        )
        original_place = client.place_futures_order

        def assert_pending_before_post(**kwargs):
            order = store.get_order(kwargs["client_order_id"])
            self.assertEqual(order["status"], "PENDING_RECONCILE")
            events = store.recent_events(kwargs["client_order_id"])
            self.assertEqual(events[-1]["event_type"], "PENDING_RECONCILE")
            return original_place(**kwargs)

        client.place_futures_order = assert_pending_before_post
        try:
            result = executor.execute(
                signal_record(), inst_id="BTC-USDT", leverage=1,
                allow_demo_orders=True,
            )
            self.assertEqual(result["status"], "ACCEPTED")
            self.assertEqual(result["environment"], "BLOFIN_DEMO")
            self.assertEqual(result["exchange_order_id"], "simulated-demo-id")
            self.assertIn(("set_leverage", 1, None), client.calls)
            self.assertTrue(any(isinstance(x, tuple) and x[0] == "place_order" for x in client.calls))
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM blofin_demo_orders").fetchone()[0], 1
            )
        finally:
            store.close()

    def test_execution_cost_gate_rejects_wide_spread_and_missing_quote(self):
        from crypto_super_radar.blofin_demo_executor import apply_demo_execution_cost_gate

        plan = {
            "side": "LONG", "entry_reference_price": "100",
            "stop_loss": "98", "take_profit": "104", "quantity": "1",
        }
        instrument = {"contractValue": "0.001"}
        with self.assertRaises(BloFinDemoExecutionBlocked):
            apply_demo_execution_cost_gate(
                dict(plan), instrument,
                {"bidPrice": "80", "askPrice": "105"},
            )
        with self.assertRaises(BloFinDemoExecutionBlocked):
            apply_demo_execution_cost_gate(dict(plan), instrument, {})

    def test_executor_blocks_missing_or_invalid_two_reviewer_quorum_before_any_exchange_call(self):
        def sync_evaluation(item):
            item["ai_consensus"]["entry_evaluation_window"] = copy.deepcopy(
                item["entry_evaluation_window"]
            )

        def shorten_window(item):
            item["entry_evaluation_window"]["elapsed_seconds"] = 299.9
            sync_evaluation(item)

        def reuse_initial_signal(item):
            item["entry_evaluation_window"]["final_signal_id"] = (
                item["entry_evaluation_window"]["initial_signal_id"]
            )
            item["entry_evaluation_window"]["new_closed_candle_confirmed"] = False
            sync_evaluation(item)

        def remove_initial_reviewer(item):
            item["entry_evaluation_window"]["initial_round"]["ai_reviews"].pop()
            sync_evaluation(item)

        def stale_initial_reviewer(item):
            review = item["entry_evaluation_window"]["initial_round"]["ai_reviews"][0]["review"]
            review["response_source"] = "CACHE"
            review["request_attempted"] = False
            sync_evaluation(item)

        def collapse_review_rounds(item):
            final_request = item["ai_reviews"][0]["review"]["request_started_at_utc"]
            item["entry_evaluation_window"]["initial_round"]["ai_reviews"][0]["review"][
                "completed_at_utc"
            ] = final_request
            sync_evaluation(item)

        def mismatch_final_pattern(item):
            item["challenge1_evidence"]["chosen_pattern"]["name_en"] = "Different Pattern"
            sync_evaluation(item)

        def incomplete_candle_advance(item):
            from datetime import timedelta
            final_bar = datetime.fromisoformat(
                item["entry_evaluation_window"]["final_signal_bar_open_utc"]
            )
            item["entry_evaluation_window"]["initial_signal_bar_open_utc"] = (
                final_bar - timedelta(minutes=1)
            ).isoformat()
            sync_evaluation(item)

        def mismatch_final_round_identity(item):
            item["entry_evaluation_window"]["final_round"]["reviewed_signal_id"] = "stale-signal"
            sync_evaluation(item)

        mutations = {
            "missing evaluation proof": lambda item: item.pop("entry_evaluation_window"),
            "window shorter than five minutes": shorten_window,
            "same signal candle after revalidation": reuse_initial_signal,
            "first round missing reviewer": remove_initial_reviewer,
            "cached first-round approval": stale_initial_reviewer,
            "review rounds not sequential": collapse_review_rounds,
            "final record pattern differs from evaluation": mismatch_final_pattern,
            "new candle is less than five minutes later": incomplete_candle_advance,
            "final reviewer round references stale signal": mismatch_final_round_identity,
            "missing consensus": lambda item: item.pop("ai_consensus"),
            "missing second reviewer": lambda item: item["ai_reviews"].pop(),
            "veto": lambda item: item["ai_reviews"][0]["review"].update(
                status="VETO", paper_entry_eligible=False,
            ),
            "unknown response": lambda item: item["ai_reviews"][1]["review"].update(
                status="UNKNOWN", paper_entry_eligible=False,
            ),
            "duplicate provider": lambda item: item["ai_reviews"][1]["review"].update(
                provider="OpenRouter",
            ),
            "duplicate model": lambda item: item["ai_reviews"][1]["review"].update(
                model="nvidia/nemotron-3-ultra-550b-a55b:free",
                returned_model="nvidia/nemotron-3-ultra-550b-a55b:free",
            ),
            "direction disagreement": lambda item: item["ai_reviews"][1]["review"].update(
                trader_proposal="SHORT",
            ),
            "wave phase disagreement": lambda item: item["ai_reviews"][1]["review"].update(
                elliott_wave_phase="WAVE_4_CONFIRMED",
            ),
            "cached response": lambda item: item["ai_reviews"][1]["review"].update(
                response_source="CACHE", request_attempted=False,
            ),
            "stale approval": lambda item: item["ai_reviews"][0]["review"].update(
                reviewed_at_utc=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            ),
            "approval for another signal": lambda item: item["ai_reviews"][1]["review"].update(
                reviewed_signal_id="different-signal-id",
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                with patch.dict(os.environ, {
                    "TURSO_DATABASE_URL": "",
                    "TURSO_AUTH_TOKEN": "",
                }):
                    store = BloFinDemoEventStore(":memory:")
                store.backend = "TURSO_CLOUD_SQLITE"
                client = FakeExecutorClient()
                executor = BloFinDemoExecutor(
                    client=client, store=store, trading_enabled=True,
                )
                bad_record = signal_record()
                mutate(bad_record)
                try:
                    with self.assertRaises(BloFinDemoExecutionBlocked):
                        executor.execute(
                            bad_record, inst_id="BTC-USDT", leverage=1,
                            allow_demo_orders=True,
                        )
                    self.assertFalse(any(
                        isinstance(call, tuple) and call[0] in {"set_leverage", "place_order"}
                        for call in client.calls
                    ))
                    self.assertEqual(store._db.execute(
                        "SELECT COUNT(*) FROM blofin_demo_orders"
                    ).fetchone()[0], 0)
                finally:
                    store.close()

    def test_timeout_keeps_unknown_order_reserved_without_resubmission(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        attempts = []

        def timeout_after_post(**kwargs):
            attempts.append(dict(kwargs))
            raise TimeoutError("simulated network timeout after request write")

        client.place_futures_order = timeout_after_post
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaises(BloFinDemoExecutionUnknown):
                executor.execute(
                    signal_record(), inst_id="BTC-USDT", leverage=1,
                    allow_demo_orders=True,
                )
            order = store.latest_order()
            self.assertEqual(order["status"], "UNKNOWN")
            self.assertEqual(len(attempts), 1)
            event_types = [
                event["event_type"]
                for event in store.recent_events(order["client_order_id"])
            ]
            self.assertIn("PENDING_RECONCILE", event_types)
            self.assertIn("UNKNOWN", event_types)
        finally:
            store.close()

    def test_minimum_size_only_uses_exchange_contract_minimum(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            result = executor.execute(
                signal_record(), inst_id="BTC-USDT", leverage=1,
                allow_demo_orders=True, minimum_size_only=True,
            )
            order_call = next(x for x in client.calls if isinstance(x, tuple) and x[0] == "place_order")
            self.assertEqual(order_call[1]["size"], "0.1")
            self.assertEqual(result["leverage"], 1)
            self.assertEqual(result["quantity_contracts"], "0.1")
        finally:
            store.close()

    def test_executor_allows_unlimited_orders_without_daily_quota_or_cooldown(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        policy = BloFinDemoRiskPolicy(max_leverage=1)
        self.assertIsNone(policy.max_orders_per_utc_day)
        self.assertEqual(policy.min_seconds_between_orders, 0)
        executor = BloFinDemoExecutor(
            client=client, store=store, policy=policy, trading_enabled=True,
        )
        try:
            results = []
            for index in range(7):
                result = executor.execute(
                    signal_record(bar_offset_minutes=index * 5),
                    inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
                )
                results.append(result)
                # This quota test models each Demo position as closed before the
                # next cycle; it does not weaken the concurrent-position limit.
                store.update_order(result["client_order_id"], "CLOSED")
            self.assertTrue(all(row["status"] == "ACCEPTED" for row in results))
            budget = store.trade_budget_status()
            self.assertEqual(budget["orders_today"], 7)
            self.assertEqual(budget["orders_total"], 7)
            self.assertIsNone(budget["max_orders_per_utc_day"])
            self.assertTrue(budget["daily_capacity_available"])
        finally:
            store.close()

    def test_pending_orders_or_existing_positions_block_new_entry(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        client.get_positions = lambda: [{"positions": "1"}]
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaises(BloFinDemoExecutionBlocked):
                executor.execute(signal_record(), inst_id="BTC-USDT", allow_demo_orders=True)
            self.assertFalse(any(isinstance(x, tuple) and x[0] == "place_order" for x in client.calls))
        finally:
            store.close()


    def test_ambiguous_order_is_reconciled_by_client_id_without_resubmission(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        plan = build_demo_order_plan(
            signal_record(), instrument_record(), inst_id="BTC-USDT",
            equity_usdt=1000,
        )
        store.reserve_order(plan)
        store.update_order(plan["client_order_id"], "PENDING_RECONCILE")
        client = FakeExecutorClient()
        client.get_order_detail = lambda inst_id, *, client_order_id: {
            "clientOrderId": client_order_id,
            "orderId": "exchange-order-after-reconcile",
            "state": "live",
        }
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=False)
        try:
            result = executor.reconcile_unknown_order(
                inst_id="BTC-USDT", client_order_id=plan["client_order_id"]
            )
            self.assertEqual(result["status"], "ACCEPTED")
            self.assertEqual(result["reconciliation"], "READ_ONLY")
            self.assertFalse(any(
                isinstance(call, tuple) and call[0] == "place_order"
                for call in client.calls
            ))
            self.assertEqual(
                store.get_order(plan["client_order_id"])["status"], "ACCEPTED"
            )
        finally:
            store.close()

    def test_reconcile_unresolved_orders_sweeps_all_pending_ids_without_any_write(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        plans = [
            build_demo_order_plan(
                signal_record(bar_offset_minutes=index * 5),
                instrument_record(), inst_id="BTC-USDT", equity_usdt=1000, leverage=3,
            )
            for index in range(2)
        ]
        try:
            for index, plan in enumerate(plans):
                self.assertTrue(store.reserve_order(plan, max_concurrent_positions=3))
                store.update_order(
                    plan["client_order_id"],
                    "PENDING_RECONCILE" if index == 0 else "UNKNOWN",
                )

            client = FakeExecutorClient()
            checked_ids = []

            def read_order_detail(inst_id, *, client_order_id):
                checked_ids.append(client_order_id)
                return {
                    "clientOrderId": client_order_id,
                    "orderId": "exchange-" + client_order_id[:8],
                    "state": "live",
                }

            client.get_order_detail = read_order_detail
            executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=False)
            result = executor.reconcile_unresolved_orders(limit=50)

            self.assertEqual(result["status"], "RECONCILED")
            self.assertEqual(result["reconciliation"], "READ_ONLY")
            self.assertEqual(result["attempted"], 2)
            self.assertEqual(result["remaining_unresolved"], 0)
            self.assertFalse(result["orders_submitted"])
            self.assertEqual(set(checked_ids), {p["client_order_id"] for p in plans})
            self.assertTrue(all(
                store.get_order(p["client_order_id"])["status"] == "ACCEPTED"
                for p in plans
            ))
            self.assertFalse(any(
                isinstance(call, tuple) and call[0] in {"set_leverage", "place_order"}
                for call in client.calls
            ))
        finally:
            store.close()

    def test_challenge1_ignores_even_a_configured_daily_order_count_limit(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        policy = BloFinDemoRiskPolicy(max_leverage=1, max_orders_per_utc_day=1)
        executor = BloFinDemoExecutor(
            client=client, store=store, policy=policy, trading_enabled=True,
        )
        try:
            first = executor.execute(
                signal_record(bar_offset_minutes=20),
                inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
            )
            store.update_order(first["client_order_id"], "CLOSED")
            second = executor.execute(
                signal_record(bar_offset_minutes=25),
                inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
            )
            self.assertEqual(first["status"], "ACCEPTED")
            self.assertEqual(second["status"], "ACCEPTED")
            budget = store.trade_budget_status()
            self.assertEqual(budget["orders_today"], 2)
            self.assertIsNone(budget["max_orders_per_utc_day"])
            self.assertEqual(store.get_order(first["client_order_id"])["status"], "CLOSED")
            self.assertEqual(store.get_order(second["client_order_id"])["status"], "ACCEPTED")
            self.assertEqual(store.pending_learning_outcome_count(), 1)
        finally:
            store.close()

    def test_stale_learning_outcome_does_not_block_entry_when_exchange_is_flat(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        old_signal = signal_record(bar_offset_minutes=5)
        experience_id = store.record_challenge1_decision(
            instrument_id="BTC-USDT",
            challenge_record=old_signal,
        )
        old_plan = build_demo_order_plan(
            old_signal, instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )
        old_plan["experience_id"] = experience_id
        self.assertTrue(store.reserve_order(old_plan))
        store.update_order(
            old_plan["client_order_id"], "FILLED", exchange_order_id="old-demo-order",
        )
        self.assertTrue(store.attach_position_id(experience_id, "old-demo-position"))
        # Exchange history has finalized the order as CLOSED. The learning
        # experience remains pending until exact realized PnL is reconciled, but
        # a closed order must no longer count as active exposure.
        store.update_order(old_plan["client_order_id"], "CLOSED")
        self.assertEqual(store.pending_learning_outcome_count(), 0)
        self.assertEqual(
            store.learning_memory.get_experience(experience_id)["learning_status"],
            "PENDING_OUTCOME",
        )

        # The old history is deliberately incomplete, so its PnL remains UNKNOWN.
        # Fresh exchange positions and open orders are nevertheless both empty.
        client = FakeExecutorClient()
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            fresh = executor.execute(
                signal_record(bar_offset_minutes=10),
                inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
            )
            self.assertEqual(fresh["status"], "ACCEPTED")
            self.assertTrue(any(
                isinstance(call, tuple) and call[0] == "place_order"
                for call in client.calls
            ))
            self.assertEqual(store.pending_learning_outcome_count(), 1)
            old_experience = store.learning_memory.get_experience(experience_id)
            self.assertEqual(old_experience["learning_status"], "PENDING_OUTCOME")
            self.assertEqual(
                old_experience["events"][-1]["event_type"],
                "DECISION_RECORDED",
            )
        finally:
            store.close()

class BloFinDemoUnifiedCredentialEnvironmentTests(unittest.TestCase):
    def test_trade_client_uses_unified_credentials_but_stays_demo_and_unverified(self):
        with patch.dict(os.environ, {
            "BLOFIN_DEMO_TRADE_API_KEY": "",
            "BLOFIN_DEMO_TRADE_API_SECRET": "",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "",
            "BLOFIN_DEMO_API_KEY": "",
            "BLOFIN_DEMO_API_SECRET": "",
            "BLOFIN_DEMO_PASSPHRASE": "",
            "BLOFIN_API_KEY": "shared-account-key",
            "BLOFIN_API_SECRET": "shared-account-secret",
            "BLOFIN_PASSPHRASE": "shared_passphrase",
        }, clear=False):
            client = BloFinDemoTradeClient()
        self.assertEqual(client.api_key, "shared-account-key")
        self.assertEqual(client.api_secret, "shared-account-secret")
        self.assertEqual(client.passphrase, "shared_passphrase")
        self.assertEqual(client.base_url, DEMO_REST_URL)
        self.assertFalse(client._trade_permission_verified)


if __name__ == "__main__":
    unittest.main()
