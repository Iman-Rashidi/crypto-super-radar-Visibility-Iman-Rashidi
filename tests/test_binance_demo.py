from __future__ import annotations

import unittest
from unittest.mock import patch

from crypto_super_radar.binance_demo import (
    BinanceDemoReadOnlyClient, DemoPreflightError,
    FUTURES_DEMO_HOST, SPOT_DEMO_HOST,
)


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payload)
    def request(self, *args, **kwargs):
        raise AssertionError("Read-only Demo adapter must never submit requests through a generic method.")


class BinanceDemoReadOnlyTests(unittest.TestCase):
    def test_spot_and_futures_use_separate_demo_hosts(self):
        session = Session({"canTrade": False, "balances": []})
        client = BinanceDemoReadOnlyClient(session=session, clock=lambda: 1_800_000_000)
        with patch.dict("os.environ", {
            "CSR_BINANCE_DEMO_SPOT_API_KEY": "spot-key",
            "CSR_BINANCE_DEMO_SPOT_API_SECRET": "spot-secret",
            "CSR_BINANCE_DEMO_FUTURES_API_KEY": "futures-key",
            "CSR_BINANCE_DEMO_FUTURES_API_SECRET": "futures-secret",
        }):
            client.spot_account()
            client.futures_account()
        self.assertEqual(session.calls[0][0], SPOT_DEMO_HOST + "/api/v3/account")
        self.assertEqual(session.calls[1][0], FUTURES_DEMO_HOST + "/fapi/v2/account")
        self.assertEqual(session.calls[0][1]["headers"]["X-MBX-APIKEY"], "spot-key")
        self.assertEqual(session.calls[1][1]["headers"]["X-MBX-APIKEY"], "futures-key")

    def test_no_credentials_fail_before_network(self):
        client = BinanceDemoReadOnlyClient(session=Session({}))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(DemoPreflightError, "credentials"):
                client.spot_account()
        self.assertEqual(client.session.calls, [])

    def test_client_has_no_order_submission_method(self):
        client = BinanceDemoReadOnlyClient(session=Session({}))
        self.assertFalse(hasattr(client, "submit_order"))

    def test_demo_hosts_are_fixed_non_production_hosts(self):
        self.assertEqual(SPOT_DEMO_HOST, "https://demo-api.binance.com")
        self.assertEqual(FUTURES_DEMO_HOST, "https://demo-fapi.binance.com")
        self.assertNotEqual(SPOT_DEMO_HOST, "https://api.binance.com")
        self.assertNotEqual(FUTURES_DEMO_HOST, "https://fapi.binance.com")
        self.assertTrue(SPOT_DEMO_HOST.startswith("https://demo-"))
        self.assertTrue(FUTURES_DEMO_HOST.startswith("https://demo-"))


if __name__ == "__main__":
    unittest.main()
