from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from crypto_super_radar.blofin_demo_executor import (
    BloFinDemoEventStore,
    BloFinDemoExecutionBlocked,
    BloFinDemoExecutionUnknown,
    BloFinDemoExecutor,
    BloFinDemoOrderRejected,
    BloFinDemoRiskPolicy,
    BloFinDemoTradeClient,
    _challenge1_two_reviewer_approval_ready,
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
    signal_id = f"challenge1-v4:{record['inst_id']}:5m:{signal_bar}"
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
                "model": "openai/gpt-oss-20b",
                "returned_model": "openai/gpt-oss-20b",
                "status": "APPROVED", "paper_entry_eligible": True,
                "request_attempted": True, "response_source": "LIVE_API",
                "response_validated": True, "model_verified": True,
                "expected_side": side, "trader_proposal": side,
                "elliott_wave_phase": wave_phase,
                "elliott_wave_direction": pattern_direction,
            },
        },
    ]
    reviewed_at_utc = datetime.now(timezone.utc).isoformat()
    for item in reviewers:
        item["review"]["reviewed_signal_id"] = record["signal_id"]
        item["review"]["reviewed_at_utc"] = reviewed_at_utc
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
        "reviewed_signal_id": record["signal_id"],
        "reviewed_at_utc": reviewed_at_utc,
    }
    return record



def v2_signal_record(**overrides):
    """Build a structurally valid V2 preflight record with signal-bound review fixtures."""
    record = signal_record()
    asof = datetime.now(timezone.utc).replace(microsecond=0)
    signal_open = asof - timedelta(minutes=15)
    h1_open = asof - timedelta(hours=1)
    h4_open = asof - timedelta(hours=4)
    signal_id = f"challenge1-v2:BTC-USDT:15m:{signal_open.isoformat()}:LONG"
    wave_phase = "WAVE_3_CONFIRMED"
    wave_direction = "BULLISH"
    wave = {
        "status": "CANDIDATE",
        "pattern": wave_direction,
        "phase": wave_phase,
        "wave_count": 3,
        "next_wave": 4,
        "confirmed_pivot_count": 4,
        "invalidation_price": 97.0,
        "timeframe": "1H",
        "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
        "latest_closed_candle_open_utc": h1_open.isoformat(),
        "data_asof_utc": asof.isoformat(),
        "wave_alignment": "ALIGNED",
        "pivots": [
            {"index": 10, "kind": "LOW", "price": 90.0,
             "timestamp": (asof - timedelta(hours=12)).isoformat(),
             "confirmed_at": (asof - timedelta(hours=11)).isoformat()},
            {"index": 20, "kind": "HIGH", "price": 105.0,
             "timestamp": (asof - timedelta(hours=9)).isoformat(),
             "confirmed_at": (asof - timedelta(hours=8)).isoformat()},
            {"index": 30, "kind": "LOW", "price": 95.0,
             "timestamp": (asof - timedelta(hours=6)).isoformat(),
             "confirmed_at": (asof - timedelta(hours=5)).isoformat()},
            {"index": 40, "kind": "HIGH", "price": 110.0,
             "timestamp": (asof - timedelta(hours=3)).isoformat(),
             "confirmed_at": (asof - timedelta(hours=2)).isoformat()},
        ],
    }
    funding_time_ms = int(asof.timestamp() * 1000) - 60_000
    evidence = {
        "decision_engine_version": "CHALLENGE_1_V2_1H_4H_15M",
        "signal_id": signal_id,
        "instrument_id": "BTC-USDT",
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
        "funding_event_time_ms": funding_time_ms,
        "funding_max_age_ms": 900_000,
        "elliott_wave": wave,
        "wave_alignment": "ALIGNED",
    }
    record.update({
        "signal": "LONG",
        "signal_id": signal_id,
        "instrument_id": "BTC-USDT",
        "inst_id": "BTC-USDT",
        "execution_profile": "CHALLENGE_1_V2",
        "entry": 100.0,
        "stop_loss": 98.0,
        "take_profit": 106.0,
        "gross_rr": 3.0,
        "confidence_score": 85.0,
        "score_parts": {
            "h1_h4_alignment": 30,
            "adx_gt_28": 20,
            "value_zone_rejection": 25,
            "volume_gt_1_5x": 0,
            "btc_alignment": 10,
        },
        "funding_rate": 0.0001,
        "funding_kind": "0",
        "funding_event_time_ms": funding_time_ms,
        "funding_max_age_ms": 900_000,
        "spread_fraction": 0.0002,
        "signal_candle_open_utc": signal_open.isoformat(),
        "orders_submitted": False,
        "regime": {
            "status": "TREND",
            "direction": "LONG",
            "candle_open_utc": h1_open.isoformat(),
            "candle_open_utc_4h": h4_open.isoformat(),
            "aligned_4h": True,
        },
        "challenge1_v2_evidence": evidence,
    })
    reviewed_at = datetime.now(timezone.utc).isoformat()
    groq_review = {
        "reviewer_id": "groq", "provider": "Groq",
        "model": "openai/gpt-oss-20b",
        "returned_model": "openai/gpt-oss-20b",
        "status": "APPROVED", "paper_entry_eligible": True,
        "request_attempted": True, "response_source": "LIVE_API",
        "response_validated": True, "model_verified": True,
        "expected_side": "LONG", "trader_proposal": "LONG",
        "elliott_wave_phase": wave_phase,
        "elliott_wave_direction": wave_direction,
        "reviewed_signal_id": signal_id, "reviewed_at_utc": reviewed_at,
    }
    record["ai_review"] = groq_review
    record["second_ai_review"] = None
    record["ai_reviews"] = [{
        "reviewer_id": "groq", "actor": "Groq Sole Supervisor",
        "review": groq_review,
    }]
    record["ai_consensus"] = {
        "status": "APPROVED_BY_GROQ",
        "decision": "AUTHORIZE_DEMO_EXECUTION_WITH_ONE_REVIEWER",
        "single_reviewer_approval": True,
        "single_groq_reviewer_approval": True,
        "two_reviewer_approval": False,
        "entry_gate_enforced": True,
        "reviewer_id": "groq",
        "reviewer_verified": True,
        "provider_is_groq": True,
        "model_is_groq_allowlisted": True,
        "reviewer_approved": True,
        "technical_approved": True,
        "demo_advisory_mode": True,
        "groq_approval_positive": True,
        "groq_vetoed": False,
        "expected_side": "LONG",
        "elliott_wave_phase": wave_phase,
        "elliott_wave_direction": wave_direction,
        "reviewed_signal_id": signal_id,
        "reviewed_at_utc": reviewed_at,
    }
    record.update(overrides)
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


class BloFinDemoV2OrderPlanTests(unittest.TestCase):
    def test_v2_plan_accepts_only_its_own_multitimeframe_evidence_contract(self):
        record = v2_signal_record()
        ready, reason = _challenge1_two_reviewer_approval_ready(record)
        self.assertTrue(ready, reason)
        plan = build_demo_order_plan(
            record, instrument_record(), inst_id="BTC-USDT",
            equity_usdt=1000, leverage=1,
        )
        self.assertEqual(plan["signal_id"], record["signal_id"])
        self.assertEqual(plan["side"], "LONG")
        self.assertEqual(plan["stop_loss"], "98")
        self.assertEqual(plan["take_profit"], "106")
        self.assertLessEqual(Decimal(plan["reserved_risk_usdt"]), Decimal("2.5"))

    def test_v2_sizing_reserves_one_quarter_margin_per_slot(self):
        policy = BloFinDemoRiskPolicy(
            risk_per_trade_pct=0.0025, daily_loss_limit_pct=0.02,
            max_margin_usage_pct=0.25, max_aggregate_risk_pct=0.01,
            max_position_notional_usdt=5000.0, max_leverage=3,
            max_concurrent_positions=4, min_reward_risk=1.4,
        )
        plan = build_demo_order_plan(
            v2_signal_record(), instrument_record(), inst_id="BTC-USDT",
            equity_usdt=100000, leverage=2, policy=policy,
        )
        self.assertEqual(plan["risk_budget_usdt"], 250.0)
        self.assertEqual(plan["per_position_margin_budget_usdt"], "6250")
        self.assertEqual(plan["leverage"], 2)
        self.assertEqual(plan["aggregate_margin_limit_usdt"], "25000")

    def test_btc_contract_quantity_is_even_lot_and_half_is_exchange_valid(self):
        policy = BloFinDemoRiskPolicy(
            risk_per_trade_pct=0.0025, max_margin_usage_pct=0.25,
            max_aggregate_risk_pct=0.01, max_concurrent_positions=4,
            max_leverage=3, min_reward_risk=1.4,
        )
        specs = instrument_record(contractValue="0.001", lotSize="0.1", minSize="0.1")
        plan = build_demo_order_plan(v2_signal_record(), specs, inst_id="BTC-USDT",
                                     equity_usdt=1000, leverage=2, policy=policy)
        quantity = Decimal(plan["quantity"])
        lot = Decimal("0.1")
        self.assertEqual(quantity % (lot * 2), Decimal("0"))
        self.assertEqual((quantity / 2) % lot, Decimal("0"))
        with self.assertRaises(BloFinDemoExecutionBlocked):
            build_demo_order_plan(v2_signal_record(), specs, inst_id="BTC-USDT",
                                  equity_usdt=0.1, leverage=2, policy=policy)

    def test_easy_demo_accepts_groq_normalized_ambiguous_wave_but_strict_mode_blocks(self):
        record = v2_signal_record()
        wave = record["challenge1_v2_evidence"]["elliott_wave"]
        wave.update(phase="NO_RULE_VALID_IMPULSE", pattern="AMBIGUOUS", wave_alignment="UNCONFIRMED")
        review = record["ai_reviews"][0]["review"]
        review.update(elliott_wave_phase="WAVE_3_CONFIRMED", elliott_wave_direction="BULLISH",
                       returned_model="openai/gpt-oss-20b:active-revision")
        record["ai_consensus"].update(
            status="APPROVED_WITH_GROQ_ADVISORY",
            demo_advisory_mode=True,
            reviewer_approved=True,
            technical_approved=True,
            expected_side="LONG",
            reviewed_signal_id=record["signal_id"],
            elliott_wave_phase="WAVE_3_CONFIRMED",
            elliott_wave_direction="BULLISH",
        )
        with patch.dict(os.environ, {"BLOFIN_DEMO_TRADING_ENABLED":"true",
                                     "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE":"true"}):
            with patch("crypto_super_radar.blofin_demo_executor.challenge1_v2_execution_evidence_ready",
                       return_value=(True,"mock evidence")):
                ready, reason = _challenge1_two_reviewer_approval_ready(record)
        self.assertTrue(ready, reason)
        with patch.dict(os.environ, {"BLOFIN_DEMO_TRADING_ENABLED":"true",
                                     "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE":"false"}):
            with patch("crypto_super_radar.blofin_demo_executor.challenge1_v2_execution_evidence_ready",
                       return_value=(True,"mock evidence")):
                ready, _ = _challenge1_two_reviewer_approval_ready(record)
        self.assertFalse(ready)

    def test_v2_plan_rejects_missing_or_unsupported_funding_proof(self):
        for field, value in (("funding_kind", None), ("funding_kind", "1"),
                             ("funding_event_time_ms", None), ("funding_max_age_ms", 0)):
            record = v2_signal_record()
            if value is None:
                record.pop(field, None)
                record["challenge1_v2_evidence"].pop(field, None)
            else:
                record[field] = value
                record["challenge1_v2_evidence"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(BloFinDemoExecutionBlocked):
                build_demo_order_plan(
                    record, instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
                )

    def test_v2_quorum_rejects_a_reviewer_bound_to_another_signal(self):
        record = v2_signal_record()
        record["ai_reviews"][0]["review"]["reviewed_signal_id"] = "another-signal"
        ready, reason = _challenge1_two_reviewer_approval_ready(record)
        self.assertFalse(ready)
        self.assertIn("different signal", reason)

    def test_v2_event_journal_keeps_multitimeframe_funding_and_reviewer_evidence(self):
        store = BloFinDemoEventStore(":memory:")
        record = v2_signal_record()
        try:
            experience_id = store.record_challenge1_v2_decision(
                instrument_id="BTC-USDT", challenge_record=record,
            )
            self.assertEqual(
                experience_id,
                store.record_challenge1_v2_decision(
                    instrument_id="BTC-USDT", challenge_record=record,
                ),
            )
            self.assertEqual(store.learning_memory.verify_experience(experience_id)["status"], "PASS")
            experience = store.learning_memory.get_experience(experience_id)
            snapshot = experience["events"][0]["payload"]["input_snapshot"]
            self.assertEqual(snapshot["setup_timeframe"], "15m")
            self.assertEqual(snapshot["context_timeframes"], ["1H", "4H"])
            self.assertEqual(snapshot["funding_kind"], "0")
            self.assertEqual(len(snapshot["ai_reviews"]), 1)
            self.assertEqual(snapshot["challenge1_v2_evidence"]["signal_id"], record["signal_id"])
        finally:
            store.close()

class BloFinDemoOrderPlanTests(unittest.TestCase):
    def test_four_concurrent_positions_is_supported_but_five_is_rejected(self):
        self.assertEqual(BloFinDemoRiskPolicy().max_concurrent_positions, 1)
        self.assertEqual(BloFinDemoRiskPolicy(max_concurrent_positions=4).max_concurrent_positions, 4)
        with self.assertRaises(ValueError):
            BloFinDemoRiskPolicy(max_concurrent_positions=5)

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

    def test_v2_order_payload_omits_exchange_take_profit_but_keeps_full_size_sl(self):
        self.client.place_futures_order(
            inst_id="BTC-USDT", side="buy", position_side="net", size="2.5",
            client_order_id="0123456789abcdef0123456789abcdef",
            take_profit=None, stop_loss="98",
        )
        payload = json.loads(self.session.post.call_args.kwargs["data"])
        self.assertNotIn("tpTriggerPrice", payload)
        self.assertNotIn("tpOrderPrice", payload)
        self.assertEqual(payload["size"], "2.5")
        self.assertEqual(payload["slTriggerPrice"], "98")
        self.assertEqual(payload["slOrderPrice"], "-1")
        self.assertEqual(payload["slTriggerPriceType"], "mark")

    def test_market_reduce_only_exit_uses_exact_isolated_net_position(self):
        self.client.get_position_mode = Mock(return_value={"positionMode":"net_mode"})
        self.client.get_positions = Mock(return_value=[{
            "instId":"BTC-USDT","positionId":"position-1","positionSide":"net",
            "positions":"2.0","marginMode":"isolated",
        }])
        self.client.get_tradable_instrument = Mock(return_value=instrument_record())
        receipt = self.client.place_market_exit_reduce_only(
            inst_id="BTC-USDT", client_order_id="TMKT12345678901234567890",
            position_id="position-1",
        )
        self.assertEqual(receipt["data"]["orderId"], "demo-order-1")
        payload = json.loads(self.session.post.call_args.kwargs["data"])
        self.assertEqual(payload["orderType"], "market")
        self.assertEqual(payload["reduceOnly"], "true")
        self.assertEqual(payload["size"], "2")
        self.assertEqual(payload["side"], "sell")
        self.assertEqual(payload["marginMode"], "isolated")
        self.assertNotIn("positionId", payload)

    def test_cancel_order_reads_back_exact_client_id_then_posts_exchange_order_id(self):
        self.client.get_order_detail = Mock(side_effect=[
            {"instId":"BTC-USDT","clientOrderId":"TIME12345678901234567890",
             "orderId":"exchange-order-7","state":"live"},
            {"instId":"BTC-USDT","clientOrderId":"TIME12345678901234567890",
             "orderId":"exchange-order-7","state":"canceled"},
        ])
        result = self.client.cancel_order(
            inst_id="BTC-USDT", client_order_id="TIME12345678901234567890",
            order_id="exchange-order-7",
        )
        self.assertEqual(result["status"], "CANCELED")
        self.assertTrue(self.session.post.call_args.args[0].endswith("/api/v1/trade/cancel-order"))
        self.assertEqual(json.loads(self.session.post.call_args.kwargs["data"]), {"orderId":"exchange-order-7"})

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


    def _configure_net_position(self, *, size="2.0"):
        self.client.get_position_mode = Mock(return_value={"positionMode": "net_mode"})
        self.client.get_positions = Mock(return_value=[{
            "instId": "BTC-USDT", "positionSide": "net",
            "positions": size, "positionId": "position-1",
            "marginMode": "isolated",
        }])
        self.client.get_tradable_instrument = Mock(return_value=instrument_record())
        self.client._post = Mock(return_value={"code": "0", "data": {"orderId": "exit-order-1"}})

    def test_partial_close_is_exact_half_reduce_only_market_order(self):
        self._configure_net_position(size="2.0")
        result = self.client.partial_close_reduce_only(
            inst_id="BTC-USDT", client_order_id="partialclose00000000000000000001",
            position_id="position-1",
        )
        self.assertEqual(result["data"]["orderId"], "exit-order-1")
        path, body = self.client._post.call_args.args
        self.assertEqual(path, "/api/v1/trade/order")
        self.assertEqual(body["orderType"], "market")
        self.assertEqual(body["size"], "1")
        self.assertEqual(body["side"], "sell")
        self.assertEqual(body["reduceOnly"], "true")
        self.assertEqual(body["clientOrderId"], "partialclose00000000000000000001")

    def test_partial_close_blocks_when_half_is_not_lot_representable(self):
        self._configure_net_position(size="1.1")
        with self.assertRaises(BloFinDemoExecutionBlocked):
            self.client.partial_close_reduce_only(
                inst_id="BTC-USDT", client_order_id="partialclose00000000000000000002",
            )
        self.client._post.assert_not_called()

    def test_reduce_only_exits_block_hedge_mode_because_blofin_ignores_reduce_only(self):
        self._configure_net_position()
        self.client.get_position_mode.return_value = {"positionMode": "long_short_mode"}
        with self.assertRaises(BloFinDemoExecutionBlocked):
            self.client.partial_close_reduce_only(
                inst_id="BTC-USDT", client_order_id="partialclose00000000000000000003",
            )
        self.client._post.assert_not_called()

    def test_cancel_tpsl_posts_only_the_exact_tpsl_id(self):
        self.session.post.return_value = FakeResponse({
            "code": "0", "data": [{"code": "0", "tpslId": "101"}]
        })
        with patch("crypto_super_radar.blofin_demo_executor._wait_for_trade_write_slot"):
            result = self.client.cancel_tpsl_order(inst_id="BTC-USDT", tpsl_id="101")
        self.assertEqual(result["code"], "0")
        body = json.loads(self.session.post.call_args.kwargs["data"])
        self.assertEqual(body, [{"instId": "BTC-USDT", "tpslId": "101"}])
        self.assertIn(self.client.CANCEL_TPSL_PATH, self.client.ALLOWED_POST_PATHS)

    def test_amend_sl_in_place_and_verify_active_exchange_protection(self):
        self._configure_net_position()
        active = [{
            "tpslId": "tpsl-1", "instId": "BTC-USDT", "positionSide": "net",
            "marginMode": "isolated", "side": "sell",
            "state": "live", "slTriggerPrice": "98", "size": "2", "actualSize": "2",
        }]
        amended = [{**active[0], "slTriggerPrice": "100.2"}]
        self.client.get_open_tpsl_orders = Mock(side_effect=[active, amended])
        result = self.client.amend_or_replace_sl(
            inst_id="BTC-USDT", tpsl_id="tpsl-1", new_stop_loss="100.2",
            request_id="amendsl0000000000000000000001", position_id="position-1",
        )
        self.assertTrue(result["sl_verified"])
        path, body = self.client._post.call_args.args
        self.assertEqual(path, "/api/v1/trade/amend-tpsl")
        self.assertEqual(body["newSlTriggerPrice"], "100.2")
        self.assertEqual(body["tpslId"], "tpsl-1")
        self.assertNotIn("cancel", path)
        self.assertEqual(self.client.get_open_tpsl_orders.call_args_list[0].kwargs["tpsl_id"], "tpsl-1")
        self.assertEqual(self.client.get_open_tpsl_orders.call_args_list[1].kwargs["tpsl_id"], "tpsl-1")

    def test_amend_sl_blocks_if_existing_protective_order_is_not_verified(self):
        self._configure_net_position()
        self.client.get_open_tpsl_orders = Mock(return_value=[])
        with self.assertRaises(BloFinDemoExecutionBlocked):
            self.client.amend_or_replace_sl(
                inst_id="BTC-USDT", tpsl_id="tpsl-1", new_stop_loss="100.2",
                request_id="amendsl0000000000000000000002",
            )
        self.client._post.assert_not_called()

    def test_limit_exit_is_reduce_only_and_uses_remaining_position_size(self):
        self._configure_net_position(size="-2.0")
        self.client.place_limit_exit_reduce_only(
            inst_id="BTC-USDT", limit_price="100.2",
            client_order_id="timeexit00000000000000000000001",
            position_id="position-1",
        )
        path, body = self.client._post.call_args.args
        self.assertEqual(path, "/api/v1/trade/order")
        self.assertEqual(body["orderType"], "limit")
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["price"], "100.2")
        self.assertEqual(body["size"], "2")
        self.assertEqual(body["reduceOnly"], "true")


class BloFinDemoEventStoreTests(unittest.TestCase):
    def test_v2_exit_state_persists_tp1_bar_count_and_limit_exit_id(self):
        store = BloFinDemoEventStore(":memory:")
        try:
            initial = {
                "position_id": "position-v2-1",
                "client_order_id": "0123456789abcdef0123456789abcdef",
                "instrument_id": "BTC-USDT",
                "side": "LONG",
                "entry_price": "100",
                "initial_stop_loss": "98",
                "original_size": "2",
                "remaining_size": "2",
                "tp1_executed": False,
                "candles_elapsed": 0,
                "status": "OPEN",
            }
            store.save_v2_position_exit_state(initial)
            updated = {
                **initial, "remaining_size": "1",
                "tp1_executed": True, "candles_elapsed": 12,
                "exit_order_id": "timeexitorder123",
                "sl_tpsl_id": "tpsl-v2-1", "status": "TIME_EXIT_PENDING",
            }
            store.save_v2_position_exit_state(updated)
            loaded = store.get_v2_position_exit_state("position-v2-1")
            self.assertTrue(loaded["tp1_executed"])
            self.assertEqual(loaded["candles_elapsed"], 12)
            self.assertEqual(loaded["exit_order_id"], "timeexitorder123")
            self.assertEqual(loaded["remaining_size"], "1")
            self.assertEqual(loaded["status"], "TIME_EXIT_PENDING")
            self.assertEqual(len(store.list_v2_position_exit_states()), 1)
        finally:
            store.close()

    def test_v2_exit_state_cannot_be_reassigned_to_another_order(self):
        store = BloFinDemoEventStore(":memory:")
        try:
            state = {
                "position_id": "position-v2-2",
                "client_order_id": "fedcba9876543210fedcba9876543210",
                "instrument_id": "ETH-USDT", "side": "SHORT",
                "entry_price": "200", "initial_stop_loss": "204",
                "original_size": "3", "remaining_size": "3",
            }
            store.save_v2_position_exit_state(state)
            with self.assertRaises(BloFinDemoExecutionBlocked):
                store.save_v2_position_exit_state({
                    **state, "client_order_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                })
        finally:
            store.close()

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

    def test_v2_autopilot_bypasses_legacy_single_shot_and_enforces_four_slots(self):
        # Consume the manual lifetime lock first. V2 must bypass it only with
        # an explicitly enabled V2 profile and complete V2 evidence.
        legacy = self._plan()
        self.assertTrue(self.store.reserve_order(legacy, single_shot=True))
        self.store.update_order(legacy["client_order_id"], "CLOSED")

        def make_v2_plan(symbol, index):
            record = v2_signal_record(inst_id=symbol, instrument_id=symbol)
            signal_id = f"challenge1-v2:{symbol}:15m:{record['signal_candle_open_utc']}:LONG"
            record["signal_id"] = signal_id
            evidence = record["challenge1_v2_evidence"]
            evidence["signal_id"] = signal_id
            evidence["instrument_id"] = symbol
            record["ai_consensus"]["reviewed_signal_id"] = signal_id
            for reviewer in record["ai_reviews"]:
                reviewer["review"]["reviewed_signal_id"] = signal_id
            return build_demo_order_plan(
                record, instrument_record(instId=symbol), inst_id=symbol,
                equity_usdt=1000, policy=BloFinDemoRiskPolicy(risk_per_trade_pct=0.0001),
            )

        symbols = ("ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
        with patch.dict(os.environ, {"CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED": "true"}):
            for index, symbol in enumerate(symbols):
                plan = make_v2_plan(symbol, index)
                self.assertEqual(plan["execution_profile"], "CHALLENGE_1_V2")
                self.assertTrue(self.store.reserve_order(
                    plan, max_concurrent_positions=4, v2_autopilot=True,
                    single_shot=True,
                ))
            fifth = make_v2_plan("DOGE-USDT", 5)
            self.assertFalse(self.store.reserve_order(
                fifth, max_concurrent_positions=4, v2_autopilot=True,
                single_shot=True,
            ))
        # The consumed legacy one-shot lock remains untouched by V2 reservations.
        lock_status = self.store.single_shot_lock_status()
        self.assertTrue(lock_status["locked"])
        self.assertEqual(lock_status["consumed_orders"], 1)

    def test_v2_autopilot_rejects_duplicate_active_instrument(self):
        first_record = v2_signal_record()
        first = build_demo_order_plan(
            first_record, instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )

        # Opposite direction gives a distinct, valid signal identity on the same
        # closed 15m candle while preserving the exact same contract.
        duplicate_record = v2_signal_record()
        duplicate_record["signal"] = "SHORT"
        duplicate_record["regime"]["direction"] = "SHORT"
        duplicate_record["stop_loss_usd"] = 102.0
        duplicate_record["stop_loss"] = 102.0
        duplicate_record["take_profit_usd"] = 96.0
        duplicate_record["take_profit"] = 96.0
        duplicate_record["challenge1_v2_evidence"]["elliott_wave"]["pattern"] = "BEARISH"
        duplicate_record["challenge1_v2_evidence"]["elliott_wave"]["invalidation_price"] = 103.0
        duplicate_signal_id = (
            f"challenge1-v2:BTC-USDT:15m:{duplicate_record['signal_candle_open_utc']}:SHORT"
        )
        duplicate_record["signal_id"] = duplicate_signal_id
        duplicate_record["challenge1_v2_evidence"]["signal_id"] = duplicate_signal_id
        duplicate_record["ai_consensus"].update({
            "reviewed_signal_id": duplicate_signal_id,
            "expected_side": "SHORT",
            "elliott_wave_direction": "BEARISH",
        })
        for reviewer in duplicate_record["ai_reviews"]:
            reviewer["review"].update({
                "reviewed_signal_id": duplicate_signal_id,
                "expected_side": "SHORT",
                "trader_proposal": "SHORT",
                "elliott_wave_direction": "BEARISH",
            })
        duplicate = build_demo_order_plan(
            duplicate_record, instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )

        with patch.dict(os.environ, {"CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED": "true"}):
            self.assertEqual(first["candle_guard_open_utc"], duplicate["candle_guard_open_utc"])
            self.assertTrue(self.store.reserve_order(
                first, max_concurrent_positions=4, v2_autopilot=True,
            ))
            # A completed position must not make the same 15m candle tradable
            # again in the opposite direction.
            self.store.update_order(first["client_order_id"], "CLOSED")
            self.assertFalse(self.store.reserve_order(
                duplicate, max_concurrent_positions=4, v2_autopilot=True,
            ))

    def test_v2_one_hour_setup_uses_15m_entry_candle_for_dedup(self):
        record = v2_signal_record()
        evidence = record["challenge1_v2_evidence"]
        evidence["setup_timeframe"] = "1H"
        evidence["entry_execution_timeframe"] = "15m"
        plan = build_demo_order_plan(
            record, instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )
        self.assertEqual(plan["candle_guard_timeframe"], "15m")
        self.assertEqual(
            plan["candle_guard_open_utc"],
            evidence["signal_candle_open_utc"],
        )

    def test_v2_reservation_requires_closed_candle_identity(self):
        plan = build_demo_order_plan(
            v2_signal_record(), instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )
        plan.pop("candle_guard_open_utc")
        with patch.dict(os.environ, {"CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED": "true"}):
            self.assertFalse(self.store.reserve_order(
                plan, max_concurrent_positions=4, v2_autopilot=True,
            ))
        self.assertFalse(self.store.has_any_orders())

    def test_has_any_orders_tracks_reserved_order_history(self):
        self.assertFalse(self.store.has_any_orders())
        self.store.reserve_order(self._plan())
        self.assertTrue(self.store.has_any_orders())

    def test_single_shot_lifetime_lock_survives_closed_order_until_manual_reset(self):
        first = self._plan()
        self.assertTrue(self.store.reserve_order(first, single_shot=True))
        status = self.store.single_shot_lock_status()
        self.assertTrue(status["locked"])
        self.assertEqual(status["max_lifetime_orders"], 1)
        self.assertEqual(status["consumed_orders"], 1)
        self.store.update_order(first["client_order_id"], "CLOSED")

        second = build_demo_order_plan(
            signal_record(bar_offset_minutes=5),
            instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
        )
        self.assertFalse(self.store.reserve_order(second, single_shot=True))
        # The consumed lock blocks even a later caller that omits the flag.
        self.assertFalse(self.store.reserve_order(second, single_shot=False))
        with self.assertRaises(BloFinDemoExecutionBlocked):
            self.store.reset_single_shot_lock()
        reset = self.store.reset_single_shot_lock(manual_confirmation=True)
        self.assertFalse(reset["locked"])
        self.assertEqual(reset["consumed_orders"], 0)
        self.assertTrue(self.store.reserve_order(second, single_shot=True))

    def test_single_shot_lock_is_atomic_across_connections_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "single-shot-lock.sqlite3")
            stores = [BloFinDemoEventStore(database_path) for _ in range(4)]
            try:
                plans = [
                    build_demo_order_plan(
                        signal_record(bar_offset_minutes=index * 5),
                        instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
                    )
                    for index in range(len(stores))
                ]
                barrier = threading.Barrier(len(stores))

                def reserve(index):
                    barrier.wait(timeout=10)
                    return stores[index].reserve_order(
                        plans[index], max_concurrent_positions=4, single_shot=True,
                    )

                with ThreadPoolExecutor(max_workers=len(stores)) as pool:
                    results = list(pool.map(reserve, range(len(stores))))
                self.assertEqual(results.count(True), 1)
                self.assertEqual(results.count(False), len(stores) - 1)
                self.assertTrue(stores[0].single_shot_lock_status()["locked"])
            finally:
                for store in stores:
                    store.close()

            recovered = BloFinDemoEventStore(database_path)
            try:
                status = recovered.single_shot_lock_status()
                self.assertTrue(status["locked"])
                self.assertEqual(status["consumed_orders"], 1)
                retry_plan = build_demo_order_plan(
                    signal_record(bar_offset_minutes=60),
                    instrument_record(), inst_id="BTC-USDT", equity_usdt=1000,
                )
                self.assertFalse(
                    recovered.reserve_order(
                        retry_plan, max_concurrent_positions=4, single_shot=False,
                    )
                )
            finally:
                recovered.close()

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
        order = self.store.get_order(plan["client_order_id"])
        self.assertIsNotNone(order)
        self.assertAlmostEqual(
            float(order["notional_usdt"]), float(plan["notional_usdt"]), places=8
        )
        reserved_event = self.store._db.execute(
            "SELECT payload_json FROM blofin_demo_execution_events "
            "WHERE client_order_id=? AND event_type='RESERVED'",
            (plan["client_order_id"],),
        ).fetchone()
        self.assertIsNotNone(reserved_event)
        self.assertAlmostEqual(
            float(json.loads(reserved_event["payload_json"])["notional_usdt"]),
            float(plan["notional_usdt"]), places=8,
        )
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

    @staticmethod
    def _small_reservation_plan(index: int):
        return {
            "client_order_id": f"{index:032x}",
            "signal_id": f"capacity-four-signal-{index}",
            "inst_id": "BTC-USDT",
            "side": "LONG",
            "quantity": "0.1",
            "entry_reference_price": 100.0,
            "stop_loss": "99",
            "take_profit": "102",
            "leverage": 1,
            "reserved_risk_usdt": "0.5",
            "reserved_margin_usdt": "10",
            "aggregate_risk_limit_usdt": "5",
            "aggregate_margin_limit_usdt": "100",
        }

    def test_four_reservations_fit_below_aggregate_limits_and_fifth_is_blocked(self):
        plans = [self._small_reservation_plan(index) for index in range(5)]
        for plan in plans[:4]:
            self.assertTrue(self.store.reserve_order(plan, max_concurrent_positions=4))
        self.assertFalse(self.store.reserve_order(plans[4], max_concurrent_positions=4))
        budget = self.store.trade_budget_status()
        self.assertEqual(budget["active_reservations"], 4)
        self.assertEqual(budget["aggregate_reserved_risk_usdt"], "2")
        self.assertEqual(budget["aggregate_reserved_margin_usdt"], "40")

    def test_five_position_reservation_limit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 4"):
            self.store.reserve_order(self._small_reservation_plan(0), max_concurrent_positions=5)

    def test_four_slot_reservation_is_atomic_across_independent_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "shared-demo-four.sqlite3")
            stores = [BloFinDemoEventStore(database_path) for _ in range(8)]
            try:
                plans = [self._small_reservation_plan(index) for index in range(len(stores))]
                barrier = threading.Barrier(len(stores))

                def reserve_on_independent_connection(index):
                    barrier.wait(timeout=10)
                    return stores[index].reserve_order(
                        plans[index], max_concurrent_positions=4,
                    )

                with ThreadPoolExecutor(max_workers=len(stores)) as pool:
                    results = list(pool.map(
                        reserve_on_independent_connection, range(len(stores))
                    ))
                self.assertEqual(results.count(True), 4)
                self.assertEqual(results.count(False), 4)
                self.assertEqual(
                    stores[0]._db.execute("SELECT COUNT(*) FROM blofin_demo_orders").fetchone()[0],
                    4,
                )
                budget = stores[0].trade_budget_status()
                self.assertEqual(budget["active_reservations"], 4)
                self.assertEqual(budget["aggregate_reserved_risk_usdt"], "2")
                self.assertEqual(budget["aggregate_reserved_margin_usdt"], "40")
            finally:
                for store in stores:
                    store.close()

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

    def test_closed_position_outcome_uses_exact_position_id_and_realized_pnl_without_double_fee(self):
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
        self.assertAlmostEqual(outcome["realized_net_pnl"], 1.5)
        self.assertTrue(outcome["evidence"]["funding_fees_included"])
        self.assertAlmostEqual(
            float(outcome["evidence"]["realized_pnl_after_reported_trading_fees"]),
            1.5,
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
        self.assertAlmostEqual(float(recorded["realized_pnl_after_reported_trading_fees"]), 2.50)

        experience = self.store.learning_memory.get_experience(experience_id)
        self.assertEqual(experience["learning_status"], "OUTCOME_RECORDED")
        self.assertEqual(
            [event["event_type"] for event in experience["events"]],
            ["DECISION_RECORDED", "OUTCOME_LABELLED"],
        )
        outcome = experience["events"][-1]["payload"]
        self.assertEqual(outcome["decision_correctness"], "UNKNOWN")
        self.assertEqual(outcome["evidence"]["position_id"], "demo-position-77")
        self.assertTrue(outcome["evidence"]["funding_fees_included"])
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


    def test_confirmed_closed_position_allows_only_bounded_quantity_rounding(self):
        def create_order(pid, signal_id):
            challenge = signal_record(signal_id=signal_id)
            experience_id = self.store.record_challenge1_decision(
                instrument_id="BTC-USDT", challenge_record=challenge,
            )
            plan = self._plan()
            plan["signal_id"] = signal_id
            plan["experience_id"] = experience_id
            self.assertTrue(self.store.reserve_order(plan))
            self.store.update_order(plan["client_order_id"], "FILLED", exchange_order_id="rounding-" + pid)
            self.assertTrue(self.store.attach_position_id(experience_id, pid))
            self.store._db.execute(
                "UPDATE blofin_demo_orders SET entry_price=?, quantity=? WHERE client_order_id=?",
                ("100", "100", plan["client_order_id"]),
            )
            opened_ms = int(self.store.get_order(plan["client_order_id"])["created_at"]) * 1000 + 5000
            return plan, {
                "historyId": "closed-" + pid, "positionId": pid, "instId": "BTC-USDT",
                "positionSide": "net", "createTime": str(opened_ms),
                "openAveragePrice": "100", "closeAveragePrice": "101",
                "closePositions": "99.6", "maxPositions": "100", "realizedPnl": "1.25",
            }

        plan, history = create_order("rounding-pass", "rounding-pass")
        accepted = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT", history_rows=[history],
            client_order_id=plan["client_order_id"], position_id="rounding-pass",
            position_confirmed_closed=True,
        )
        self.assertEqual(accepted["status"], "RECORDED")
        accepted_experience = self.store.learning_memory.get_experience(
            self.store.get_order(plan["client_order_id"])["experience_id"]
        )
        accepted_evidence = accepted_experience["events"][-1]["payload"]["evidence"]
        self.assertEqual(accepted_evidence["quantity_tolerance"], "0.500")

        plan, history = create_order("rounding-fail", "rounding-fail")
        history["closePositions"] = "99.4"
        rejected = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT", history_rows=[history],
            client_order_id=plan["client_order_id"], position_id="rounding-fail",
            position_confirmed_closed=True,
        )
        self.assertEqual(rejected["status"], "WAITING_FOR_FULL_POSITION_SIZE_RECONCILIATION")
        self.assertEqual(rejected["rejected_rows"][0]["reason_code"], "CLOSED_QUANTITY_MISMATCH")

    def test_blofin_artifact_three_positions_without_side_exact_net_pnl(self):
        fixtures = [
            ("BTC-USDT", "1000000995117", "0.0829", "60264.7", "60629.1", "25.445180074204390167", "-4.002796656"),
            ("ETH-USDT", "1000000995119", "2.962", "1688.6", "1699.18", "24.750250412466287336", "-6.020762616"),
            ("SOL-USDT", "1000000995121", "70.67", "70.76", "70.73", "-8.081731340987836122", "-5.99945898"),
        ]
        total = Decimal("0")
        for index, (inst, pid, qty, entry, close, pnl, fee) in enumerate(fixtures):
            challenge = signal_record(
                inst_id=inst, instrument_id=inst,
                signal_id="artifact-fixture-{}-{}".format(index, pid),
            )
            experience_id = self.store.record_challenge1_decision(
                instrument_id=inst, challenge_record=challenge,
            )
            plan = build_demo_order_plan(
                challenge, instrument_record(instId=inst), inst_id=inst, equity_usdt=1000,
            )
            plan["experience_id"] = experience_id
            self.assertTrue(self.store.reserve_order(plan))
            self.store.update_order(plan["client_order_id"], "FILLED", exchange_order_id="artifact-order-{}".format(index))
            self.assertTrue(self.store.attach_position_id(experience_id, pid))
            self.store._db.execute(
                "UPDATE blofin_demo_orders SET entry_price=?, quantity=? WHERE client_order_id=?",
                (entry, qty, plan["client_order_id"]),
            )
            opened_ms = int(self.store.get_order(plan["client_order_id"])["created_at"]) * 1000 + 5000
            history = {
                "positionId": pid, "instId": inst, "positionSide": "net",
                "createTime": str(opened_ms), "openAveragePrice": entry,
                "closeAveragePrice": close, "closePositions": qty, "maxPositions": qty,
                "realizedPnl": pnl, "fee": fee,
            }
            result = self.store.record_closed_position_outcome(
                instrument_id=inst, history_rows=[history],
                client_order_id=plan["client_order_id"], position_id=pid,
            )
            self.assertEqual(result["status"], "RECORDED")
            self.assertEqual(Decimal(result["net_pnl"]), Decimal(pnl))
            self.assertEqual(self.store.get_order(plan["client_order_id"])["status"], "CLOSED")
            total += Decimal(result["net_pnl"])
        self.assertEqual(total, Decimal("42.113699145682841381"))

    def test_reconcile_stuck_after_six_confirmed_closed_cycles_is_durable_and_keeps_slot(self):
        challenge = signal_record(signal_id="reconcile-stuck-test")
        experience_id = self.store.record_challenge1_decision(
            instrument_id="BTC-USDT", challenge_record=challenge,
        )
        plan = self._plan()
        plan["signal_id"] = "reconcile-stuck-test"
        plan["experience_id"] = experience_id
        self.assertTrue(self.store.reserve_order(plan))
        self.store.update_order(plan["client_order_id"], "FILLED", exchange_order_id="stuck-order")
        self.assertTrue(self.store.attach_position_id(experience_id, "stuck-position"))
        for _ in range(6):
            result = self.store.record_closed_position_outcome(
                instrument_id="BTC-USDT", history_rows=[], partial_rows=[],
                client_order_id=plan["client_order_id"], position_id="stuck-position",
                position_confirmed_closed=True,
            )
        self.assertEqual(result["status"], "RECONCILE_STUCK")
        self.assertEqual(result["reconcile_attempts"], 6)
        self.assertEqual(
            self.store._db.execute(
                "SELECT status FROM blofin_demo_orders WHERE client_order_id=?",
                (plan["client_order_id"],),
            ).fetchone()[0],
            "RECONCILE_STUCK",
        )
        self.assertEqual(
            self.store._db.execute(
                "SELECT attempts FROM blofin_demo_reconcile_attempts WHERE client_order_id=?",
                (plan["client_order_id"],),
            ).fetchone()[0],
            6,
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
            plan["notional_usdt"] = float(
                Decimal(plan["reserved_margin_usdt"]) * Decimal(str(plan["leverage"]))
            )
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
        self.order_args = None
        self.fill_fraction = Decimal("1")
        self.fill_state = "filled"
        self.position_id = "demo-position-123"
        self.protection_rows_override = None
        self.position_rows_override = None

    def verify_trade_permission(self):
        self.calls.append("trade_permission")
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

    def get_positions(self, *, inst_id=None):
        self.calls.append(("positions_read", inst_id))
        if self.order_args is None:
            return []
        if self.position_rows_override is not None:
            return self.position_rows_override
        filled = self._filled_size()
        if filled <= 0:
            return []
        signed = filled if self.order_args["side"] == "buy" else -filled
        return [{
            "positionId": self.position_id,
            "instId": self.order_args["inst_id"],
            "marginMode": "isolated",
            "positionSide": self.order_args["position_side"],
            "positions": str(signed),
        }]

    def get_open_orders(self):
        self.calls.append("pending_orders")
        return []

    def get_order_detail(self, inst_id, *, client_order_id):
        self.calls.append(("order_detail_read", inst_id, client_order_id))
        if self.order_args is None:
            return None
        filled = self._filled_size()
        state = self.fill_state
        if self.fill_fraction == 0:
            state = "live"
        elif self.fill_fraction < 1 and state == "filled":
            state = "partially_filled"
        return {
            "clientOrderId": client_order_id, "orderId": "simulated-demo-id",
            "instId": inst_id, "side": self.order_args["side"],
            "size": self.order_args["size"], "filledSize": str(filled),
            "state": state, "positionId": self.position_id,
        }

    def get_open_tpsl_orders(self, *, inst_id=None, client_order_id=None, limit=100):
        self.calls.append(("tpsl_read", inst_id, client_order_id))
        if self.protection_rows_override is not None:
            return self.protection_rows_override
        if self.order_args is None:
            return []
        filled = self._filled_size()
        if filled <= 0:
            return []
        return [{
            "tpslId": "simulated-tpsl-123", "clientOrderId": client_order_id,
            "instId": inst_id,
            "marginMode": "isolated", "positionSide": self.order_args["position_side"],
            "side": "sell" if self.order_args["side"] == "buy" else "buy",
            "size": str(filled), "actualSize": str(filled), "state": "live",
            "tpTriggerPrice": self.order_args["take_profit"],
            "slTriggerPrice": self.order_args["stop_loss"],
            "tpTriggerPriceType": "mark", "slTriggerPriceType": "mark",
        }]

    def _filled_size(self):
        if self.order_args is None:
            return Decimal("0")
        return Decimal(self.order_args["size"]) * self.fill_fraction

    def set_futures_leverage(self, inst_id, leverage, *, margin_mode, position_side=None):
        self.calls.append(("set_leverage", leverage, position_side))
        return {"code": "0"}

    def place_futures_order(self, **kwargs):
        self.calls.append(("place_order", kwargs))
        self.order_args = dict(kwargs)
        return self.order_response

    def close_futures_position(self, **kwargs):
        self.calls.append(("emergency_close", kwargs))
        self.position_rows_override = []
        return {"code": "0", "data": {"orderId": "simulated-emergency-close"}}


class BloFinDemoV2ExecutorTests(unittest.TestCase):
    def test_v2_fill_verification_accepts_full_size_sl_without_exchange_tp(self):
        store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        client.order_args = {
            "inst_id": "BTC-USDT", "side": "buy", "position_side": "net",
            "size": "2", "take_profit": None, "stop_loss": "98",
        }
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=False)
        try:
            result = executor._verify_filled_position_and_protection(
                order={"instrument_id": "BTC-USDT", "side": "LONG",
                       "position_side": "net", "take_profit": "104",
                       "exchange_tp_required": 0, "stop_loss": "98"},
                detail={"positionId": client.position_id},
                filled_size=Decimal("2"),
                client_order_id="0123456789abcdef0123456789abcdef",
            )
            self.assertTrue(result["verified"])
            self.assertTrue(result["sl_verified"])
            self.assertFalse(result["tp_required"])
            self.assertFalse(result["tp_verified"])
        finally:
            store.close()


    def test_v2_executor_blocks_missing_reviewer_before_any_exchange_call_or_reservation(self):
        store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        record = v2_signal_record()
        record["ai_reviews"].pop()
        try:
            with self.assertRaises(BloFinDemoExecutionBlocked):
                executor.execute(
                    record, inst_id="BTC-USDT", leverage=1,
                    allow_demo_orders=True,
                )
            self.assertEqual(client.calls, [])
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM blofin_demo_orders").fetchone()[0],
                0,
            )
        finally:
            store.close()

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

    def test_executor_rejects_hedge_mode_before_leverage_or_order_write(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()

        def return_hedge_mode():
            client.calls.append("position_mode")
            return {"positionMode": "long_short_mode"}

        client.get_position_mode = return_hedge_mode
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaisesRegex(BloFinDemoExecutionBlocked, "One-Way net_mode"):
                executor.execute(
                    signal_record(), inst_id="BTC-USDT", leverage=1,
                    allow_demo_orders=True,
                )
            self.assertIn("position_mode", client.calls)
            self.assertFalse(any(
                isinstance(call, tuple) and call[0] in {"set_leverage", "place_order"}
                for call in client.calls
            ))
            self.assertFalse(store.has_any_orders())
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
            self.assertEqual(result["status"], "FILLED")
            self.assertEqual(result["environment"], "BLOFIN_DEMO")
            self.assertEqual(result["position_id"], "demo-position-123")
            self.assertTrue(result["protection"]["tp_verified"])
            self.assertTrue(result["protection"]["sl_verified"])
            self.assertEqual(result["exchange_order_id"], "simulated-demo-id")
            self.assertIn(("set_leverage", 1, None), client.calls)
            self.assertTrue(any(isinstance(x, tuple) and x[0] == "place_order" for x in client.calls))
            self.assertTrue(any(isinstance(x, tuple) and x[0] == "order_detail_read" for x in client.calls))
            self.assertTrue(any(isinstance(x, tuple) and x[0] == "tpsl_read" for x in client.calls))
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM blofin_demo_orders").fetchone()[0], 1
            )
        finally:
            store.close()

    def test_net_cost_gate_blocks_two_to_one_gross_rr_when_net_rr_falls_below_policy(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaises(BloFinDemoExecutionBlocked):
                executor.execute(
                    signal_record(take_profit_usd=104.0), inst_id="BTC-USDT",
                    leverage=1, allow_demo_orders=True,
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

    def test_net_cost_gate_requires_bid_and_ask_before_any_write(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        client.ticker_quote = {}
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaises(BloFinDemoExecutionBlocked):
                executor.execute(
                    signal_record(), inst_id="BTC-USDT",
                    leverage=1, allow_demo_orders=True,
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

    def test_net_cost_gate_rejects_excessive_spread_before_reserving_order(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        client.ticker_quote = {"bidPrice": "80", "askPrice": "105"}
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaises(BloFinDemoExecutionBlocked):
                executor.execute(
                    signal_record(take_profit_usd=120.0), inst_id="BTC-USDT",
                    leverage=1, allow_demo_orders=True,
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

    def test_executor_blocks_missing_or_invalid_two_reviewer_quorum_before_any_exchange_call(self):
        mutations = {
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

    def test_partial_fill_uses_actual_filled_size_for_position_and_protection(self):
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        client.fill_fraction = Decimal("0.4")
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            result = executor.execute(signal_record(), inst_id="BTC-USDT", leverage=1, allow_demo_orders=True)
            self.assertEqual(result["status"], "PARTIALLY_FILLED")
            self.assertEqual(Decimal(result["filled_size"]), Decimal(result["quantity_contracts"]) * Decimal("0.4"))
            self.assertTrue(result["protection"]["tp_verified"])
            self.assertTrue(result["protection"]["sl_verified"])
            self.assertEqual(store.get_order(result["client_order_id"])["status"], "PARTIALLY_FILLED")
            self.assertEqual(store.trade_budget_status()["unresolved_orders"], 1)
        finally:
            store.close()

    def test_filled_order_without_exact_tp_sl_is_emergency_closed_and_blocks_same_cycle(self):
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        client.protection_rows_override = []
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaisesRegex(BloFinDemoExecutionBlocked, "emergency close and flat verification succeeded"):
                executor.execute(signal_record(), inst_id="BTC-USDT", leverage=1, allow_demo_orders=True)
            client_order_id = client.order_args["client_order_id"]
            self.assertIn("emergency_close", [call[0] for call in client.calls if isinstance(call, tuple)])
            self.assertEqual(store.get_order(client_order_id)["status"], "CLOSED")
            self.assertEqual(store.trade_budget_status()["unresolved_orders"], 0)
        finally:
            store.close()

    def test_position_id_mismatch_blocks_ambiguous_emergency_close(self):
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        client.position_rows_override = [{
            "positionId": "different-position", "instId": "BTC-USDT",
            "marginMode": "isolated", "positionSide": "net", "positions": "1",
        }]
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaisesRegex(BloFinDemoExecutionUnknown, "position ID could not be proven"):
                executor.execute(signal_record(), inst_id="BTC-USDT", leverage=1, allow_demo_orders=True)
            self.assertNotIn("emergency_close", [call[0] for call in client.calls if isinstance(call, tuple)])
            latest = store.latest_order()
            self.assertEqual(latest["status"], "CRITICAL_UNPROTECTED")
            self.assertIsNone(latest.get("position_id"))
        finally:
            store.close()

    def test_restart_recovery_reads_durable_journal_and_verifies_position_tp_sl(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "blofin-demo-journal.sqlite3")
            with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
                first_store = BloFinDemoEventStore(database_path)
            plan = build_demo_order_plan(signal_record(), instrument_record(), inst_id="BTC-USDT", equity_usdt=1000)
            plan["position_side"] = "net"
            self.assertTrue(first_store.reserve_order(plan))
            first_store.update_order(plan["client_order_id"], "PENDING_RECONCILE", exchange_order_id="simulated-demo-id")
            first_store.close()
            with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
                recovered_store = BloFinDemoEventStore(database_path)
            recovered_store.backend = "TURSO_CLOUD_SQLITE"
            client = FakeExecutorClient()
            client.order_args = {
                "inst_id": "BTC-USDT", "side": "buy", "position_side": "net",
                "size": plan["quantity"], "take_profit": plan["take_profit"],
                "stop_loss": plan["stop_loss"], "client_order_id": plan["client_order_id"],
            }
            executor = BloFinDemoExecutor(client=client, store=recovered_store, trading_enabled=False)
            try:
                result = executor.recover_unresolved_orders_from_journal(limit=10)
                self.assertEqual(result["status"], "RECONCILED")
                self.assertEqual(result["attempted"], 1)
                self.assertEqual(recovered_store.get_order(plan["client_order_id"])["status"], "FILLED")
                self.assertEqual(recovered_store.get_order(plan["client_order_id"])["position_id"], "demo-position-123")
                self.assertFalse(any(isinstance(call, tuple) and call[0] == "place_order" for call in client.calls))
            finally:
                recovered_store.close()

    def test_definitive_exchange_rejection_is_rejected_not_pending_timeout(self):
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        def reject(**kwargs):
            raise BloFinDemoOrderRejected("BloFin returned an explicit order code=51000")
        client.place_futures_order = reject
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaises(BloFinDemoExecutionBlocked):
                executor.execute(signal_record(), inst_id="BTC-USDT", leverage=1, allow_demo_orders=True)
            self.assertEqual(store.latest_order()["status"], "REJECTED")
        finally:
            store.close()

    def test_malformed_exchange_response_is_ambiguous_not_a_definitive_rejection(self):
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        def malformed(**kwargs):
            raise BloFinDemoResponseError("BloFin Demo returned invalid JSON")
        client.place_futures_order = malformed
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            with self.assertRaises(BloFinDemoExecutionUnknown):
                executor.execute(
                    signal_record(), inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
                )
            self.assertEqual(store.latest_order()["status"], "PENDING_RECONCILE")
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
            self.assertEqual(order["status"], "PENDING_RECONCILE")
            self.assertEqual(len(attempts), 1)
            event_types = [
                event["event_type"]
                for event in store.recent_events(order["client_order_id"])
            ]
            self.assertIn("PENDING_RECONCILE", event_types)
            self.assertIn("PENDING_RECONCILE", event_types)
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
                client.order_args = None
            self.assertTrue(all(row["status"] == "FILLED" for row in results))
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
            "clientOrderId": client_order_id, "orderId": "exchange-order-after-reconcile",
            "instId": inst_id, "side": "buy", "size": plan["quantity"],
            "filledSize": "0", "state": "live",
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
                local = store.get_order(client_order_id)
                return {
                    "clientOrderId": client_order_id, "orderId": "exchange-" + client_order_id[:8],
                    "instId": inst_id, "side": "buy" if local["side"] == "LONG" else "sell",
                    "size": local["quantity"], "filledSize": "0", "state": "live",
                }

            client.get_order_detail = read_order_detail
            executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=False)
            result = executor.reconcile_unresolved_orders(limit=50)

            # Active exchange orders remain unresolved until a fill/terminal state is verified.
            self.assertEqual(result["status"], "UNRESOLVED")
            self.assertEqual(result["reconciliation"], "READ_ONLY")
            self.assertEqual(result["attempted"], 2)
            self.assertEqual(result["remaining_unresolved"], 2)
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
            client.order_args = None
            second = executor.execute(
                signal_record(bar_offset_minutes=25),
                inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
            )
            self.assertEqual(first["status"], "FILLED")
            self.assertEqual(second["status"], "FILLED")
            budget = store.trade_budget_status()
            self.assertEqual(budget["orders_today"], 2)
            self.assertIsNone(budget["max_orders_per_utc_day"])
            self.assertEqual(store.get_order(first["client_order_id"])["status"], "CLOSED")
            self.assertEqual(store.get_order(second["client_order_id"])["status"], "FILLED")
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
            self.assertEqual(fresh["status"], "FILLED")
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




class Challenge1V2DemoAdapterContractTests(unittest.TestCase):
    def test_v2_profile_fails_closed_without_v2_multitimeframe_evidence(self):
        record = {
            "signal": "LONG",
            "signal_id": "challenge1-v2:BTC-USDT:15m:2026-11-20T10:00:00+00:00:LONG",
            "instrument_id": "BTC-USDT",
            "execution_profile": "CHALLENGE_1_V2",
            "orders_submitted": False,
        }
        ready, reason = _challenge1_two_reviewer_approval_ready(record)
        self.assertFalse(ready)
        self.assertIn("V2 analysis evidence is incomplete", reason)

    def test_v2_order_plan_never_falls_back_to_legacy_five_minute_evidence(self):
        record = {
            "signal": "LONG",
            "signal_id": "challenge1-v2:BTC-USDT:15m:2026-11-20T10:00:00+00:00:LONG",
            "instrument_id": "BTC-USDT",
            "execution_profile": "CHALLENGE_1_V2",
            "orders_submitted": False,
        }
        with self.assertRaisesRegex(
            BloFinDemoExecutionBlocked,
            "Challenge 1 V2 multi-timeframe evidence is incomplete",
        ):
            build_demo_order_plan(
                record,
                instrument_record(),
                inst_id="BTC-USDT",
                equity_usdt=1000,
                leverage=1,
            )

if __name__ == "__main__":
    unittest.main()


class BloFinDemoEmergencyCloseTests(unittest.TestCase):
    def _executor(self, *, close_position=True, tpsl_rows=None):
        class Store:
            def __init__(self):
                self.updates = []
            def update_order(self, client_order_id, status, **kwargs):
                self.updates.append((client_order_id, status, kwargs))
        class Client:
            def __init__(self):
                self.positions = [{
                    "instId": "BTC-USDT", "positionId": "pos-1",
                    "marginMode": "isolated", "positionSide": "net", "positions": "2",
                }]
                self.tpsl_rows = list(tpsl_rows or [])
                self.close_calls = []
                self.cancelled = []
            def get_positions(self, *, inst_id=None):
                return [p for p in self.positions if p["instId"] == inst_id]
            def close_futures_position(self, **kwargs):
                self.close_calls.append(kwargs)
                if close_position:
                    self.positions = []
                return {"code": "0", "data": {"orderId": "close-1"}}
            def get_open_tpsl_orders(self, *, inst_id=None, limit=100):
                return [r for r in self.tpsl_rows if r["instId"] == inst_id]
            def cancel_tpsl_order(self, *, inst_id, tpsl_id):
                self.cancelled.append(tpsl_id)
                self.tpsl_rows = [r for r in self.tpsl_rows if r.get("tpslId") != tpsl_id]
                return {"code": "0", "data": [{"code": "0", "tpslId": tpsl_id}]}
        store, client = Store(), Client()
        executor = BloFinDemoExecutor(
            client=client, store=store, policy=BloFinDemoRiskPolicy(), trading_enabled=True
        )
        return executor, client, store

    def test_unprotected_fill_is_closed_and_orphan_sl_is_canceled(self):
        tpsl = [{
            "tpslId": "tpsl-1", "instId": "BTC-USDT",
            "marginMode": "isolated", "positionSide": "net", "side": "sell",
            "state": "live", "size": "2", "actualSize": None,
            "slTriggerPrice": "98", "slTriggerPriceType": "mark",
        }]
        executor, client, store = self._executor(tpsl_rows=tpsl)
        with self.assertRaisesRegex(BloFinDemoExecutionBlocked, "emergency close and flat verification succeeded"):
            executor._emergency_close_unprotected_filled_position(
                inst_id="BTC-USDT", side="LONG", client_order_id="entry-1",
                filled_size="2", stop_loss="98", position_id="pos-1",
            )
        self.assertEqual(len(client.close_calls), 1)
        self.assertEqual(client.close_calls[0]["position_id"], "pos-1")
        self.assertEqual(client.cancelled, ["tpsl-1"])
        self.assertEqual(client.positions, [])
        self.assertEqual(store.updates[-1][1], "CLOSED")

    def test_unprotected_fill_that_cannot_be_flattened_remains_critical(self):
        executor, client, store = self._executor(close_position=False)
        with patch("crypto_super_radar.blofin_demo_executor.time.sleep"):
            with self.assertRaisesRegex(BloFinDemoExecutionUnknown, "remains open"):
                executor._emergency_close_unprotected_filled_position(
                    inst_id="BTC-USDT", side="LONG", client_order_id="entry-1",
                    filled_size="2", stop_loss="98", position_id="pos-1",
                )
        self.assertEqual(store.updates[-1][1], "CRITICAL_UNPROTECTED")
        self.assertEqual(len(client.close_calls), 1)
