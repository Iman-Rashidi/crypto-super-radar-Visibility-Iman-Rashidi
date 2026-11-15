from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from crypto_super_radar.blofin_demo_readonly import DEMO_REST_URL
from crypto_super_radar.blofin_demo_executor import BloFinDemoTradeClient
from crypto_super_radar.blofin_demo_smoke_test import BloFinDemoSmokeTestRunner, BloFinDemoExecutionBlocked


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"code": "0", "data": {"instId": "BTC-USDT", "positionSide": "net"}}


class FakeSmokeClient:
    base_url = DEMO_REST_URL
    api_key = "demo-key"
    api_secret = "demo-secret"
    passphrase = "demo-pass"

    def __init__(self, *, existing_position=False, multi_position=False):
        self.calls = []
        self.mode = {
            "positionMode": "net_mode",
            "multiPosition": "true" if multi_position else "false",
        }
        self.position = (
            {"instId": "BTC-USDT", "positionSide": "net", "positions": "0.1", "positionId": "pos-1"}
            if existing_position else None
        )

    def check_clock_skew(self, *, inst_id):
        self.calls.append("clock")
        return {"offset_ms": 0}

    def get_tickers(self, *, inst_id):
        self.calls.append("ticker")
        return [{"instId": inst_id, "last": "100000", "ts": "1800000000000"}]

    def get_mark_price(self, *, inst_id):
        self.calls.append("mark")
        return [{"instId": inst_id, "markPrice": "100000"}]

    def get_tradable_instrument(self, inst_id):
        self.calls.append("instrument")
        return {
            "instId": inst_id, "minSize": "0.1", "lotSize": "0.1",
            "contractValue": "0.001", "tickSize": "0.5", "maxMarketSize": "100",
        }

    def get_position_mode(self):
        self.calls.append("position_mode")
        return dict(self.mode)

    def get_positions(self, *, inst_id=None):
        self.calls.append("positions")
        return [dict(self.position)] if self.position else []

    def get_open_orders(self, *, inst_id=None):
        self.calls.append("pending_orders")
        return []

    def get_futures_balance(self, *, product_type="USDT-FUTURES"):
        self.calls.append("balance")
        return {"totalEquity": "1000"}

    def set_futures_leverage(self, inst_id, leverage, *, margin_mode, position_side=None):
        self.calls.append(("leverage", leverage, margin_mode, position_side))
        return {"code": "0"}

    def place_futures_order(self, **kwargs):
        self.calls.append(("entry", dict(kwargs)))
        self.entry_kwargs = dict(kwargs)
        self.position = {
            "instId": "BTC-USDT", "positionSide": "net",
            "positions": kwargs["size"], "positionId": "pos-test-1",
        }
        return {"code": "0", "data": [{"orderId": "entry-order-1", "clientOrderId": kwargs["client_order_id"]}]}

    def get_order_detail(self, inst_id, *, client_order_id):
        self.calls.append("order_detail")
        return {
            "orderId": "entry-order-1", "clientOrderId": client_order_id,
            "state": "filled", "filledSize": "0.1", "averagePrice": "100010",
        }

    def get_fills_history(self, *, inst_id, order_id=None, limit=100):
        self.calls.append("fills")
        return [{"orderId": order_id, "fillSize": "0.1", "fillPrice": "100010"}]

    def close_futures_position(self, **kwargs):
        self.calls.append(("close", dict(kwargs)))
        self.close_kwargs = dict(kwargs)
        self.position = None
        return {"code": "0", "data": {"instId": "BTC-USDT", "clientOrderId": kwargs["client_order_id"]}}

    def get_positions_history(self, *, inst_id, state=None, limit=20):
        return [{"instId": inst_id, "state": state, "realizedPnl": "-0.01"}]


class BloFinDemoSmokeRunnerTests(unittest.TestCase):
    def test_independent_test_opens_and_closes_without_signal_argument(self):
        client = FakeSmokeClient()
        runner = BloFinDemoSmokeTestRunner(
            client=client, sleep_fn=lambda _seconds: None, poll_attempts=1,
        )
        result = runner.run()
        self.assertEqual(result["status"], "CLOSED_VERIFIED")
        self.assertEqual(result["environment"], "BLOFIN_DEMO")
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["leverage"], 1)
        self.assertTrue(result["position_closed"])
        self.assertEqual(result["open_position_count_after_close"], 0)
        self.assertEqual(client.entry_kwargs["side"], "buy")
        self.assertEqual(client.entry_kwargs["size"], "0.1")
        self.assertEqual(client.close_kwargs["position_side"], "net")
        self.assertEqual(client.calls.count("positions"), 3)

    def test_existing_position_blocks_before_any_order_write(self):
        client = FakeSmokeClient(existing_position=True)
        runner = BloFinDemoSmokeTestRunner(client=client, sleep_fn=lambda _seconds: None)
        with self.assertRaises(BloFinDemoExecutionBlocked):
            runner.run()
        self.assertFalse(any(isinstance(item, tuple) and item[0] in {"entry", "close"} for item in client.calls))

    def test_multi_position_mode_requires_exact_position_id(self):
        client = FakeSmokeClient(multi_position=True)
        client.mode["positionMode"] = "long_short_mode"
        client.get_position_mode = lambda: {"positionMode": "long_short_mode", "multiPosition": "true"}
        runner = BloFinDemoSmokeTestRunner(client=client, sleep_fn=lambda _seconds: None, poll_attempts=1)
        # Simulate exchange position response without a positionId after a successful entry.
        original = client.get_positions
        def no_id_positions(*, inst_id=None):
            rows = original(inst_id=inst_id)
            if rows:
                rows[0].pop("positionId", None)
            return rows
        client.get_positions = no_id_positions
        with self.assertRaises(Exception):
            runner.run()
        self.assertFalse(any(isinstance(item, tuple) and item[0] == "close" for item in client.calls))


class BloFinDemoCloseEndpointTests(unittest.TestCase):
    def test_close_position_uses_demo_allowlisted_endpoint_and_exact_position(self):
        session = Mock()
        session.post.return_value = FakeResponse()
        client = BloFinDemoTradeClient(
            api_key="demo-key", api_secret="demo-secret", passphrase="demo-pass",
            session=session, clock_ms=lambda: 1_800_000_000_000,
        )
        client.close_futures_position(
            inst_id="BTC-USDT", margin_mode="isolated", position_side="long",
            client_order_id="SMKC0123456789abcdef", position_id="position123",
        )
        url = session.post.call_args.args[0]
        body = json.loads(session.post.call_args.kwargs["data"])
        self.assertTrue(url.startswith(DEMO_REST_URL))
        self.assertTrue(url.endswith("/api/v1/trade/close-position"))
        self.assertEqual(body["positionId"], "position123")
        self.assertEqual(body["positionSide"], "long")
        self.assertEqual(body["marginMode"], "isolated")

    def test_close_position_rejects_invalid_position_id(self):
        session = Mock()
        client = BloFinDemoTradeClient(
            api_key="demo-key", api_secret="demo-secret", passphrase="demo-pass",
            session=session, clock_ms=lambda: 1_800_000_000_000,
        )
        with self.assertRaises(ValueError):
            client.close_futures_position(
                inst_id="BTC-USDT", margin_mode="isolated", position_side="net",
                client_order_id="SMKC0123456789abcdef", position_id="bad position id",
            )
        session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
