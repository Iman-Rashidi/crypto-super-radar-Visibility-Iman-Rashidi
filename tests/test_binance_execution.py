from __future__ import annotations

import unittest
from unittest.mock import Mock

from crypto_super_radar.binance_execution import (
    BinanceSpotExecutor, LIVE_CONFIRMATION, OrderSafetyError, OrderStatusUnknown,
)


class FakeResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.data


class BinanceSpotExecutorTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.request.side_effect = [
            FakeResponse({"symbols": [{
                "status": "TRADING", "isSpotTradingAllowed": True,
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.0001", "maxQty": "100", "stepSize": "0.0001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ],
            }]}),
            FakeResponse({"symbol": "BTCUSDT", "bidPrice": "49999", "askPrice": "50000"}),
            FakeResponse({"balances": [{"asset": "USDT", "free": "100"}]}),
            FakeResponse({"symbol": "BTCUSDT", "orderId": 7, "status": "FILLED",
                          "executedQty": "0.0002", "cummulativeQuoteQty": "10"}),
        ]
        self.executor = BinanceSpotExecutor(
            api_key="test-key", api_secret="test-secret", session=self.session,
            clock=lambda: 1_800_000_000,
        )

    def test_testnet_limit_ioc_order_is_signed_bounded_and_not_retried(self):
        result = self.executor.submit_limit_ioc(
            symbol="BTCUSDT", side="BUY", quantity="0.0002",
            reference_price="50000", client_order_id="csr-test-123",
        )
        self.assertEqual(result["mode"], "TESTNET")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["client_order_id"], "csr-test-123")
        self.assertEqual(self.session.request.call_count, 4)
        request = self.session.request.call_args
        self.assertEqual(request.args[0], "POST")
        self.assertEqual(request.args[1], "https://testnet.binance.vision/api/v3/order")
        self.assertIn("signature", request.kwargs["data"])
        self.assertEqual(request.kwargs["headers"]["X-MBX-APIKEY"], "test-key")

    def test_production_is_blocked_without_explicit_switch(self):
        import os
        old = os.environ.pop("CSR_LIVE_TRADING_ENABLED", None)
        try:
            executor = BinanceSpotExecutor(
                api_key="key", api_secret="secret", environment="production",
                session=self.session,
            )
            with self.assertRaises(OrderSafetyError):
                executor.submit_limit_ioc(
                    symbol="BTCUSDT", side="BUY", quantity="0.0002",
                    reference_price="50000", confirm_live=LIVE_CONFIRMATION,
                )
        finally:
            if old is not None:
                os.environ["CSR_LIVE_TRADING_ENABLED"] = old

    def test_production_requires_protective_exit_control(self):
        import os
        old_live = os.environ.get("CSR_LIVE_TRADING_ENABLED")
        old_protect = os.environ.pop("CSR_LIVE_PROTECTIVE_EXITS_READY", None)
        os.environ["CSR_LIVE_TRADING_ENABLED"] = "YES"
        try:
            executor = BinanceSpotExecutor(
                api_key="key", api_secret="secret", environment="production",
                session=self.session,
            )
            with self.assertRaisesRegex(OrderSafetyError, "protective exit"):
                executor.submit_limit_ioc(
                    symbol="BTCUSDT", side="BUY", quantity="0.0002",
                    reference_price="50000", confirm_live=LIVE_CONFIRMATION,
                )
        finally:
            if old_live is None:
                os.environ.pop("CSR_LIVE_TRADING_ENABLED", None)
            else:
                os.environ["CSR_LIVE_TRADING_ENABLED"] = old_live
            if old_protect is not None:
                os.environ["CSR_LIVE_PROTECTIVE_EXITS_READY"] = old_protect

    def test_rejects_bad_symbol_and_excessive_notional_before_network(self):
        with self.assertRaises(OrderSafetyError):
            self.executor.submit_limit_ioc(
                symbol="DOGEUSDT", side="BUY", quantity="1", reference_price="1",
            )
        with self.assertRaises(OrderSafetyError):
            self.executor.submit_limit_ioc(
                symbol="BTCUSDT", side="BUY", quantity="1", reference_price="50000",
                max_notional_usdt="50000",
            )
        self.session.request.assert_not_called()

    def test_order_timeout_is_unknown_and_never_retried(self):
        import requests
        self.session.request.side_effect = [
            FakeResponse({"symbols": [{
                "status": "TRADING", "isSpotTradingAllowed": True,
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.0001", "maxQty": "100", "stepSize": "0.0001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ],
            }]}),
            FakeResponse({"symbol": "BTCUSDT", "bidPrice": "49999", "askPrice": "50000"}),
            FakeResponse({"balances": [{"asset": "USDT", "free": "100"}]}),
            requests.Timeout("uncertain"),
        ]
        with self.assertRaises(OrderStatusUnknown) as raised:
            self.executor.submit_limit_ioc(
                symbol="BTCUSDT", side="BUY", quantity="0.0002",
                reference_price="50000", client_order_id="csr-timeout-1",
            )
        self.assertEqual(raised.exception.client_order_id, "csr-timeout-1")
        self.assertEqual(self.session.request.call_count, 4)

    def test_signal_executor_fails_closed_on_unknown_gate(self):
        from types import SimpleNamespace
        from crypto_super_radar.binance_execution import ValidatedSignalExecutor
        result = SimpleNamespace(
            signal="LONG", gates=[SimpleNamespace(status="PASS") for _ in range(11)]
                   + [SimpleNamespace(status="UNKNOWN")],
            confirmation_score=90, estimated_risk_score=10, stop_loss=90,
            take_profit=130, expected_holding_days=2,
        )
        with self.assertRaisesRegex(OrderSafetyError, "twelve"):
            ValidatedSignalExecutor(self.executor).submit_long(
                result=result, symbol="BTCUSDT", reference_price=100,
            )
        self.session.request.assert_not_called()

    def test_signal_executor_rejects_non_long_even_if_all_gates_pass(self):
        from types import SimpleNamespace
        from crypto_super_radar.binance_execution import ValidatedSignalExecutor
        result = SimpleNamespace(
            signal="SHORT", gates=[SimpleNamespace(status="PASS") for _ in range(12)],
            confirmation_score=90, estimated_risk_score=10, stop_loss=90,
            take_profit=130, expected_holding_days=2,
        )
        with self.assertRaisesRegex(OrderSafetyError, "LONG"):
            ValidatedSignalExecutor(self.executor).submit_long(
                result=result, symbol="BTCUSDT", reference_price=100,
            )
        self.session.request.assert_not_called()

    def test_quote_drift_blocks_order(self):
        self.session.request.side_effect = [
            FakeResponse({"symbols": [{
                "status": "TRADING", "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.0001", "maxQty": "100", "stepSize": "0.0001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                ],
            }]}),
            FakeResponse({"askPrice": "51000", "bidPrice": "50999"}),
        ]
        with self.assertRaisesRegex(OrderSafetyError, "slippage"):
            self.executor.submit_limit_ioc(
                symbol="BTCUSDT", side="BUY", quantity="0.0002",
                reference_price="50000",
            )
        self.assertEqual(self.session.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
