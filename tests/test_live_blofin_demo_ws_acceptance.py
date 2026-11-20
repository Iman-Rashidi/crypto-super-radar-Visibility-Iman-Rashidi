import unittest

from scripts.live_blofin_demo_ws_acceptance import classify_ws_acceptance, parse_contract_value
from crypto_super_radar.blofin_demo_futures_ws import DEFAULT_PUBLIC_CHANNELS


class BloFinDemoWebSocketAcceptanceScriptTests(unittest.TestCase):
    def test_contract_value_requires_exact_live_linear_usdt_instrument(self):
        payload = {
            "code": "0",
            "data": [{
                "instId": "BTC-USDT", "state": "live", "contractType": "linear",
                "settleCurrency": "USDT", "contractValue": "0.001",
            }],
        }
        self.assertEqual(parse_contract_value(payload, "BTC-USDT"), 0.001)

    def test_contract_value_rejects_wrong_settlement_or_duplicate_rows(self):
        payload = {
            "code": "0",
            "data": [{
                "instId": "BTC-USDT", "state": "live", "contractType": "inverse",
                "settleCurrency": "BTC", "contractValue": "1",
            }],
        }
        with self.assertRaises(ValueError):
            parse_contract_value(payload, "BTC-USDT")
        payload["data"].append(dict(payload["data"][0]))
        with self.assertRaises(ValueError):
            parse_contract_value(payload, "BTC-USDT")

    def test_ws_acceptance_requires_acknowledgements_and_core_data(self):
        report = classify_ws_acceptance({
            "subscription_ack_counts": {channel: 1 for channel in DEFAULT_PUBLIC_CHANNELS},
            "channel_observation_counts": {
                "tickers": 2, "books5": 3, "mark-price-candle1m": 1,
                "trades": 0, "funding-rate": 0,
            },
            "last_error": None,
        })
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["trade_observation"], "NO_TRADE_SEEN_DURING_PROBE")
        self.assertEqual(report["funding_rate_observation"], "NO_FUNDING_PUSH_DURING_PROBE")

    def test_ws_acceptance_is_inconclusive_when_a_required_channel_is_not_observed(self):
        report = classify_ws_acceptance({
            "subscription_ack_counts": {channel: 1 for channel in DEFAULT_PUBLIC_CHANNELS},
            "channel_observation_counts": {"tickers": 1, "books5": 1},
            "last_error": None,
        })
        self.assertEqual(report["status"], "INCONCLUSIVE")
        self.assertIn("mark-price-candle1m", report["required_channels_without_observation"])

    def test_ws_protocol_error_is_a_failure(self):
        report = classify_ws_acceptance({
            "subscription_ack_counts": {},
            "channel_observation_counts": {},
            "last_error": "BloFinDemoWebSocketError: INVALID_TICKER",
        })
        self.assertEqual(report["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
