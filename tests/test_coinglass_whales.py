import unittest
from unittest.mock import patch

from crypto_super_radar.market_intelligence import _coinglass_whale_data


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


class CoinGlassWhaleTests(unittest.TestCase):
    def setUp(self):
        from crypto_super_radar import market_intelligence
        market_intelligence._CG_WHALE_ALERT_CACHE.clear()
        market_intelligence._CG_WHALE_POSITION_CACHE.clear()

    @patch.dict("os.environ", {"COINGLASS_API_KEY": "test-key"})
    def test_filters_symbol_and_normalizes_alerts_and_positions(self):
        http = FakeHttp([
            {"code": "0", "data": [
                {"user": "0xlong", "symbol": "BTC", "position_size": 2,
                 "position_value_usd": 2000000, "entry_price": 50000,
                 "liq_price": 40000, "position_action": 1, "create_time": 1700000000000},
                {"user": "0xother", "symbol": "ETH", "position_size": 3,
                 "position_value_usd": 3000000, "position_action": 1, "create_time": 1700000000000},
                {"user": "0xshort", "symbol": "BTC", "position_size": -1,
                 "position_value_usd": 1200000, "position_action": 2, "create_time": 1700000001000},
            ]},
            {"code": "0", "data": [
                {"user": "0xlong", "symbol": "BTC", "position_size": 2,
                 "position_value_usd": 2000000, "entry_price": 50000,
                 "mark_price": 51000, "liq_price": 40000, "leverage": 5,
                 "margin_balance": 400000, "unrealized_pnl": 20000,
                 "funding_fee": -120, "margin_mode": "cross", "update_time": 1700000002000},
                {"user": "0xshort", "symbol": "BTC", "position_size": -1,
                 "position_value_usd": 1200000, "leverage": 3,
                 "unrealized_pnl": -10000, "update_time": 1700000002000},
                {"user": "0xeth", "symbol": "ETH", "position_size": 4,
                 "position_value_usd": 4000000, "leverage": 2},
            ]},
        ])
        result = _coinglass_whale_data("BTC", http)
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["alert_count"], 2)
        self.assertEqual(result["open_alert_count"], 1)
        self.assertEqual(result["close_alert_count"], 1)
        self.assertEqual(result["position_count"], 2)
        self.assertEqual(result["wallet_count"], 2)
        self.assertEqual(result["long_notional_usd"], 2000000)
        self.assertEqual(result["short_notional_usd"], 1200000)
        self.assertEqual(result["net_directional_notional_usd"], 800000)
        self.assertEqual(result["positions"][0]["leverage"], 5)
        self.assertEqual(result["positions"][0]["unrealized_pnl_usd"], 20000)
        self.assertEqual(len(http.calls), 2)

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_api_key_is_explicit(self):
        result = _coinglass_whale_data("BTC", FakeHttp([]))
        self.assertEqual(result["status"], "NOT_CONFIGURED")
        self.assertEqual(result["alerts"], [])

    @patch.dict("os.environ", {"COINGLASS_API_KEY": "test-key"})
    def test_one_endpoint_failure_keeps_other_endpoint_partial(self):
        http = FakeHttp([
            RuntimeError("plan access denied"),
            {"code": "0", "data": []},
        ])
        result = _coinglass_whale_data("BTC", http)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["alerts_status"], "UNAVAILABLE")
        self.assertEqual(result["positions_status"], "KNOWN")
        self.assertEqual(result["position_count"], 0)
        self.assertIsNone(result["long_notional_usd"])

    @patch.dict("os.environ", {"COINGLASS_API_KEY": "test-key"})
    def test_symbol_required(self):
        result = _coinglass_whale_data(None, FakeHttp([]))
        self.assertEqual(result["status"], "NO_SYMBOL")
        self.assertEqual(result["alerts"], [])


if __name__ == "__main__":
    unittest.main()
