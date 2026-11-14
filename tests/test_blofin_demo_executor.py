from __future__ import annotations

import json
import os
import sqlite3
import unittest
from unittest.mock import Mock, patch

from crypto_super_radar.blofin_demo_executor import (
    BloFinDemoEventStore,
    BloFinDemoExecutionBlocked,
    BloFinDemoExecutionUnknown,
    BloFinDemoExecutor,
    BloFinDemoRiskPolicy,
    BloFinDemoTradeClient,
    build_demo_order_plan,
)
from crypto_super_radar.blofin_demo_readonly import (
    BloFinDemoResponseError,
    DEMO_REST_URL,
    _signature,
)


def signal_record(**overrides):
    record = {
        "signal": "LONG",
        "signal_id": "candle-2026-11-13T18:00:00Z",
        "inst_id": "BTC-USDT",
        "confirmation_score": 90,
        "estimated_risk_score": 20,
        "gate_pass_count": 12,
        "gate_fail_count": 0,
        "gate_unknown_count": 0,
        "entry_price_usd": 100.0,
        "stop_loss_usd": 98.0,
        "take_profit_usd": 104.0,
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

    def test_rejects_failed_or_unknown_confirmation_gate(self):
        for overrides in (
            {"gate_fail_count": 1, "gate_pass_count": 11},
            {"gate_unknown_count": 1, "gate_pass_count": 11},
            {"gate_pass_count": 11},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(BloFinDemoExecutionBlocked):
                    build_demo_order_plan(
                        signal_record(**overrides), instrument_record(),
                        inst_id="BTC-USDT", equity_usdt=1000,
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

    def test_has_any_orders_tracks_the_one_shot_reservation(self):
        self.assertFalse(self.store.has_any_orders())
        self.store.reserve_order(self._plan())
        self.assertTrue(self.store.has_any_orders())

    def test_trade_budget_tracks_daily_count_and_signal_idempotency(self):
        plan = self._plan()
        self.assertFalse(self.store.has_signal(plan["signal_id"]))
        self.store.reserve_order(plan)
        self.assertTrue(self.store.has_signal(plan["signal_id"]))
        budget = self.store.trade_budget_status(2)
        self.assertEqual(budget["orders_today"], 1)
        self.assertEqual(budget["max_orders_per_utc_day"], 2)
        self.assertTrue(budget["daily_capacity_available"])
        self.assertEqual(budget["unresolved_orders"], 1)

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

    def test_executor_allows_multiple_orders_within_daily_budget(self):
        with patch.dict(os.environ, {
            "TURSO_DATABASE_URL": "",
            "TURSO_AUTH_TOKEN": "",
        }):
            store = BloFinDemoEventStore(":memory:")
        store.backend = "TURSO_CLOUD_SQLITE"
        client = FakeExecutorClient()
        policy = BloFinDemoRiskPolicy(
            max_orders_per_utc_day=2,
            min_seconds_between_orders=0,
        )
        executor = BloFinDemoExecutor(
            client=client, store=store, policy=policy, trading_enabled=True,
        )
        try:
            first = executor.execute(
                signal_record(), inst_id="BTC-USDT", leverage=1,
                allow_demo_orders=True,
            )
            second = executor.execute(
                signal_record(signal_id="a-new-completed-candle"),
                inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
            )
            self.assertEqual(first["status"], "ACCEPTED")
            self.assertEqual(second["status"], "ACCEPTED")
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM blofin_demo_orders").fetchone()[0], 2
            )
            calls_before = len(client.calls)
            with self.assertRaises(BloFinDemoExecutionBlocked):
                executor.execute(
                    signal_record(signal_id="third-completed-candle"),
                    inst_id="BTC-USDT", leverage=1, allow_demo_orders=True,
                )
            self.assertEqual(len(client.calls), calls_before)
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
        store.update_order(plan["client_order_id"], "UNKNOWN")
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
