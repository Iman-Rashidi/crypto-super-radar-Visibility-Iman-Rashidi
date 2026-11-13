from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from unittest.mock import Mock

from crypto_super_radar.blofin_demo_readonly import (
    BloFinDemoAuthError,
    BloFinDemoReadOnly,
    DEMO_REST_URL,
    DEMO_PRIVATE_WS_URL,
    DEMO_PUBLIC_WS_URL,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("HTTP error")

    def json(self):
        return self.payload


class BloFinDemoReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.get.return_value = FakeResponse({"code": "0", "msg": "success", "data": []})
        self.client = BloFinDemoReadOnly(
            api_key="demo-key",
            api_secret="demo-secret",
            passphrase="demo_pass",
            session=self.session,
            clock_ms=lambda: 1_800_000_000_000,
        )

    def test_client_is_pinned_to_demo_host(self):
        self.assertEqual(self.client.base_url, DEMO_REST_URL)
        self.assertIn("demo-trading", self.client.base_url)
        self.assertEqual(
            self.client.websocket_urls(),
            {"public": DEMO_PUBLIC_WS_URL, "private": DEMO_PRIVATE_WS_URL},
        )

    def test_public_market_calls_do_not_send_credentials(self):
        self.client.get_futures_instruments(inst_id="BTC-USDT")
        call_args, kwargs = self.session.get.call_args
        url = call_args[0]
        self.assertTrue(url.startswith(DEMO_REST_URL))
        self.assertIn("/api/v1/market/instruments?instId=BTC-USDT", url)
        self.assertEqual(kwargs["headers"], {})
        self.assertNotIn("ACCESS-KEY", kwargs["headers"])

    def test_candles_validate_limit_and_use_demo_endpoint(self):
        self.client.get_candles("BTC-USDT", bar="5m", limit=250)
        call_args, _ = self.session.get.call_args
        url = call_args[0]
        self.assertIn("/api/v1/market/candles?", url)
        self.assertIn("bar=5m", url)
        self.assertIn("limit=250", url)
        with self.assertRaises(ValueError):
            self.client.get_candles("BTC-USDT", limit=0)

    def test_private_balance_is_signed_get_and_uses_demo_host(self):
        self.client.get_futures_balance()
        call_args, kwargs = self.session.get.call_args
        url = call_args[0]
        self.assertTrue(url.startswith(DEMO_REST_URL))
        self.assertIn("/api/v1/account/balance?productType=USDT-FUTURES", url)
        headers = kwargs["headers"]
        self.assertEqual(headers["ACCESS-KEY"], "demo-key")
        self.assertEqual(headers["ACCESS-PASSPHRASE"], "demo_pass")
        self.assertEqual(headers["ACCESS-TIMESTAMP"], "1800000000000")
        nonce = headers["ACCESS-NONCE"]
        prehash = f"/api/v1/account/balance?productType=USDT-FUTURESGET1800000000000{nonce}"
        digest_hex = hmac.new(b"demo-secret", prehash.encode(), hashlib.sha256).hexdigest()
        expected = base64.b64encode(digest_hex.encode()).decode()
        self.assertEqual(headers["ACCESS-SIGN"], expected)
        self.assertEqual(kwargs["timeout"], 10.0)

    def test_positions_and_open_orders_use_only_read_endpoints(self):
        self.client.get_positions(inst_id="BTC-USDT")
        call_args1, _ = self.session.get.call_args
        url1 = call_args1[0]
        self.assertIn("/api/v1/account/positions?instId=BTC-USDT", url1)
        self.client.get_open_orders(inst_id="BTC-USDT")
        call_args2, _ = self.session.get.call_args
        url2 = call_args2[0]
        self.assertIn("/api/v1/trade/orders-pending?instId=BTC-USDT", url2)
        self.assertFalse(hasattr(self.client, "place_order"))
        self.assertFalse(hasattr(self.client, "cancel_order"))
        self.assertFalse(hasattr(self.client, "transfer"))
        self.assertFalse(hasattr(self.client, "withdraw"))

    def test_private_call_requires_credentials(self):
        client = BloFinDemoReadOnly(session=self.session)
        with self.assertRaises(BloFinDemoAuthError):
            client.get_positions()

    def test_websocket_login_payload_uses_read_only_auth_scheme(self):
        payload = self.client.websocket_login_args()
        self.assertEqual(payload["apiKey"], "demo-key")
        self.assertEqual(payload["passphrase"], "demo_pass")
        self.assertTrue(payload["timestamp"].isdigit())
        self.assertTrue(payload["nonce"])
        self.assertTrue(payload["sign"])

    def test_clock_skew_uses_exchange_ticker_timestamp(self):
        self.session.get.return_value = FakeResponse({
            "code": "0", "data": [{"instId": "BTC-USDT", "ts": "1800000000250"}]
        })
        result = self.client.check_clock_skew()
        self.assertEqual(result["offset_ms"], 250)

    def test_nonzero_api_code_is_rejected(self):
        self.session.get.return_value = FakeResponse({
            "code": "152405", "msg": "expired", "data": []
        })
        from crypto_super_radar.blofin_demo_readonly import BloFinDemoResponseError
        with self.assertRaises(BloFinDemoResponseError):
            self.client.get_positions()


if __name__ == "__main__":
    unittest.main()
