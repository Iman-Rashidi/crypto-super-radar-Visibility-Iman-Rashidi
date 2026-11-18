from __future__ import annotations

import json
import os
import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from crypto_super_radar.blofin_demo_executor import (
    BloFinDemoEventStore,
    BloFinDemoExecutionBlocked,
    BloFinDemoExecutionUnknown,
    BloFinDemoExecutor,
    BloFinDemoRiskPolicy,
    BloFinDemoTradeClient,
    _position_margin_usdt,
    build_demo_order_plan,
)
from crypto_super_radar.blofin_demo_readonly import (
    BloFinDemoResponseError,
    DEMO_REST_URL,
    _signature,
)


def signal_record(**overrides):
    pass_count = int(overrides.get("gate_pass_count", 12))
    fail_count = int(overrides.get("gate_fail_count", 0))
    unknown_count = int(overrides.get("gate_unknown_count", max(0, 12 - pass_count - fail_count)))
    statuses = (
        ["PASS"] * max(0, min(12, pass_count))
        + ["FAIL"] * max(0, min(12, fail_count))
        + ["UNKNOWN"] * max(0, min(12, unknown_count))
    )[:12]
    statuses += ["UNKNOWN"] * (12 - len(statuses))
    record = {
        "signal": "LONG",
        "signal_id": "candle-2026-11-13T18:00:00Z",
        "inst_id": "BTC-USDT",
        "execution_profile": "CHALLENGE_1",
        "confirmation_score": None,
        "estimated_risk_score": None,
        "gate_pass_count": pass_count,
        "gate_fail_count": fail_count,
        "gate_unknown_count": unknown_count,
        "gates": [{"gate_id": gate_id, "status": statuses[gate_id - 1]} for gate_id in range(1, 13)],
        "entry_price_usd": 100.0,
        "stop_loss_usd": 98.0,
        "take_profit_usd": 104.0,
    }
    record.update(overrides)
    side = str(record["signal"]).upper()
    pattern_direction = "BULLISH" if side == "LONG" else "BEARISH"
    pattern_name = "Bullish Engulfing" if side == "LONG" else "Bearish Engulfing"
    record["challenge1_evidence"] = {
        "decision_engine_version": "CHALLENGE_1_LOCAL_RULES_V1",
        "setup_timeframe": "15m",
        "context_timeframe": "1H",
        "completed_candles_only": True,
        "legacy_12_gates_used_for_entry": False,
        "external_ai_required": False,
        "tinyfish_required": False,
        "decision": side,
        "detected_patterns": [{"name_en": pattern_name, "direction": pattern_direction}],
        "chosen_pattern": {"name_en": pattern_name, "direction": pattern_direction},
        "confirmation_candle": {
            "open_time_utc": "2026-11-13T18:00:00+00:00",
            "volume_confirmation": {"confirmed": True, "volume_ratio": 1.4},
        },
    }
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
        self.assertEqual(plan["client_order_id"], "205ccdbb36207dcd1acb9eba0a30ec04")

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

    def test_challenge1_v2_does_not_require_volume_confirmation_or_legacy_engine_version(self):
        record = signal_record()
        evidence = record["challenge1_evidence"]
        evidence["decision_engine_version"] = "CHALLENGE_1_LOCAL_RULES_V2"
        evidence["volume_is_advisory"] = True
        evidence["context_is_advisory"] = True
        evidence["confirmation_candle"]["volume_confirmation"] = {
            "confirmed": False,
            "volume_ratio": 0.82,
            "reason": "Volume did not meet the optional advisory threshold.",
        }
        plan = build_demo_order_plan(
            record, instrument_record(), inst_id="BTC-USDT",
            equity_usdt=1000, leverage=1,
            policy=BloFinDemoRiskPolicy(max_leverage=1),
        )
        self.assertEqual(plan["side"], "LONG")

    def test_challenge1_v3_accepts_closed_5m_pattern_evidence(self):
        record = signal_record()
        evidence = record["challenge1_evidence"]
        evidence["decision_engine_version"] = "CHALLENGE_1_LOCAL_RULES_V3"
        evidence["setup_timeframe"] = "5m"
        evidence["candles_5m"] = [{"time_utc": "2026-11-18T10:00:00Z"}]
        plan = build_demo_order_plan(
            record, instrument_record(), inst_id="BTC-USDT",
            equity_usdt=1000, leverage=1,
            policy=BloFinDemoRiskPolicy(max_leverage=1, min_reward_risk=1.0),
        )
        self.assertEqual(plan["side"], "LONG")

    def test_risk_policy_supports_four_positions_with_aggregate_margin_cap(self):
        policy = BloFinDemoRiskPolicy(
            max_concurrent_positions=4,
            max_total_margin_usage_pct=0.25,
        )
        self.assertEqual(policy.max_concurrent_positions, 4)
        self.assertEqual(policy.max_total_margin_usage_pct, 0.25)
        with self.assertRaises(ValueError):
            BloFinDemoRiskPolicy(max_concurrent_positions=5)
        with self.assertRaises(ValueError):
            BloFinDemoRiskPolicy(max_total_margin_usage_pct=0.0)

    def test_challenge1_rejects_unknown_evidence_engine_version(self):
        record = signal_record()
        record["challenge1_evidence"]["decision_engine_version"] = "UNRECOGNIZED_VERSION"
        with self.assertRaises(BloFinDemoExecutionBlocked):
            build_demo_order_plan(
                record, instrument_record(), inst_id="BTC-USDT",
                equity_usdt=1000, leverage=1,
                policy=BloFinDemoRiskPolicy(max_leverage=1),
            )

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

    def test_cancel_futures_order_targets_one_exact_order_and_instrument(self):
        response = self.client.cancel_futures_order(inst_id="BTC-USDT", order_id="123456789")
        self.assertEqual(response["data"]["orderId"], "demo-order-1")
        args, kwargs = self.session.post.call_args
        self.assertTrue(args[0].endswith("/api/v1/trade/cancel-order"))
        self.assertEqual(json.loads(kwargs["data"]), {
            "instId": "BTC-USDT", "orderId": "123456789"
        })

    def test_cancel_futures_order_rejects_invalid_identifiers_before_post(self):
        with self.assertRaises(ValueError):
            self.client.cancel_futures_order(inst_id="BTC-USDT", order_id="")
        with self.assertRaises(ValueError):
            self.client.cancel_futures_order(inst_id="BTC-USDT", order_id="123/456")
        with self.assertRaises(ValueError):
            self.client.cancel_futures_order(inst_id="BTC-USD", order_id="123456789")
        self.session.post.assert_not_called()

    def test_close_futures_position_accepts_cross_margin_but_requires_exact_position(self):
        self.client.close_futures_position(
            inst_id="BTC-USDT", margin_mode="cross", position_side="net",
            client_order_id="manualclose123", position_id="987654321",
        )
        args, kwargs = self.session.post.call_args
        self.assertTrue(args[0].endswith("/api/v1/trade/close-position"))
        self.assertEqual(json.loads(kwargs["data"]), {
            "instId": "BTC-USDT", "marginMode": "cross",
            "positionSide": "net", "clientOrderId": "manualclose123",
            "positionId": "987654321",
        })

    def test_only_explicitly_allowlisted_post_paths_are_permitted(self):
        with self.assertRaises(BloFinDemoExecutionBlocked):
            self.client._post("/api/v1/account/set-position-mode", {"positionMode": "net_mode"})


class BloFinPositionMarginTests(unittest.TestCase):
    def test_isolated_margin_uses_margin_field(self):
        self.assertEqual(
            _position_margin_usdt({
                "marginMode": "isolated",
                "margin": "12.345",
                "initialMargin": "",
            }),
            Decimal("12.345"),
        )

    def test_cross_margin_uses_initial_margin_field(self):
        self.assertEqual(
            _position_margin_usdt({
                "marginMode": "cross",
                "initialMargin": "45.67",
                "margin": "1.0",
            }),
            Decimal("45.67"),
        )

    def test_unknown_or_missing_mode_specific_margin_fails_closed(self):
        for position in (
            {"marginMode": "isolated", "initialMargin": "12"},
            {"marginMode": "cross", "margin": "12"},
            {"marginMode": "unknown", "margin": "12"},
        ):
            with self.subTest(position=position), self.assertRaises(BloFinDemoExecutionBlocked):
                _position_margin_usdt(position)


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

    def test_manual_action_audit_is_append_only_and_uses_action_identity(self):
        self.store.record_manual_action(
            "MANUALCLOSE123", "MANUAL_POSITION_CLOSE_REQUESTED",
            {"instrument_id": "BTC-USDT", "position_id": "987654321", "unrealized_pnl": "-2.5"},
        )
        events = self.store.recent_events("MANUALCLOSE123")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "MANUAL_POSITION_CLOSE_REQUESTED")
        self.assertIn("position_id", events[0]["payload"])

    def test_has_any_orders_tracks_reserved_order_history(self):
        self.assertFalse(self.store.has_any_orders())
        self.store.reserve_order(self._plan())
        self.assertTrue(self.store.has_any_orders())

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
        )
        self.assertEqual(waiting["status"], "WAITING_FOR_FINAL_CLOSED_POSITION")
        self.assertEqual(
            self.store.learning_memory.get_experience(experience_id)["learning_status"],
            "PENDING_OUTCOME",
        )

        recorded = self.store.record_closed_position_outcome(
            instrument_id="BTC-USDT",
            history_rows=[history],
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
        )
        self.assertEqual(waiting["status"], "WAITING_FOR_EXCHANGE_POSITION_LINK")
        self.assertEqual(
            self.store.learning_memory.get_experience(experience_id)["learning_status"],
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
        self.order_response = {"code": "0", "data": [{"code": "0", "orderId": "simulated-demo-id"}]}

    def verify_trade_permission(self):
        self.calls.append("trade_permission")
        return {"readOnly": 0, "trade_permission": True}

    def check_clock_skew(self, *, inst_id):
        self.calls.append("clock")
        return {"offset_ms": 0}

    def get_tickers(self, *, inst_id=None):
        self.calls.append("ticker")
        return [{"instId": inst_id, "last": "100", "ts": "1800000000000"}]

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

    def test_emergency_cancel_check_blocks_final_demo_order_write(self):
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
                    signal_record(), inst_id="BTC-USDT", leverage=1,
                    allow_demo_orders=True,
                    cancel_check=lambda: True,
                )
            self.assertFalse(any(
                isinstance(call, tuple) and call[0] == "place_order"
                for call in client.calls
            ))
            latest = store.latest_order()
            self.assertEqual(latest["status"], "REJECTED")
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
                results.append(executor.execute(
                    signal_record(signal_id=f"new-completed-candle-{index}"),
                    inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
                ))
            self.assertTrue(all(row["status"] == "ACCEPTED" for row in results))
            budget = store.trade_budget_status()
            self.assertEqual(budget["orders_today"], 7)
            self.assertEqual(budget["orders_total"], 7)
            self.assertIsNone(budget["max_orders_per_utc_day"])
            self.assertTrue(budget["daily_capacity_available"])
        finally:
            store.close()

    def test_stale_learning_outcome_does_not_block_entry_when_exchange_is_flat(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        old_signal = signal_record(signal_id="old-filled-position")
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
        self.assertEqual(store.pending_learning_outcome_count(), 1)

        # The old history is deliberately incomplete, so its PnL remains UNKNOWN.
        # Fresh exchange positions and open orders are nevertheless both empty.
        client = FakeExecutorClient()
        executor = BloFinDemoExecutor(client=client, store=store, trading_enabled=True)
        try:
            fresh_signal = signal_record(signal_id="fresh-completed-candle")
            fresh_signal["challenge1_evidence"]["confirmation_candle"]["open_time_utc"] = "2026-11-13T18:15:00+00:00"
            fresh_experience_id = store.record_challenge1_decision(
                instrument_id="BTC-USDT",
                challenge_record=fresh_signal,
            )
            fresh_signal["learning_experience_id"] = fresh_experience_id
            fresh = executor.execute(
                fresh_signal,
                inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
            )
            self.assertEqual(fresh["status"], "ACCEPTED")
            self.assertTrue(any(
                isinstance(call, tuple) and call[0] == "place_order"
                for call in client.calls
            ))
            self.assertEqual(store.pending_learning_outcome_count(), 2)
            old_experience = store.learning_memory.get_experience(experience_id)
            self.assertEqual(old_experience["learning_status"], "PENDING_OUTCOME")
            self.assertEqual(
                old_experience["events"][-1]["event_type"],
                "DECISION_RECORDED",
            )
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
        self.assertEqual(store.trade_budget_status()["unresolved_orders"], 1)
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

if __name__ == "__main__":
    unittest.main()
