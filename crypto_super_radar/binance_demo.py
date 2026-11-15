"""Read-only Binance Demo account preflight. No order or cancel methods exist.

The hosts are deliberately hard-coded to Demo only. This module must not be
extended with order endpoints until exchange-confirmed protective exits,
partial-fill reconciliation and STOP BOT recovery are tested.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode
from typing import Any

import requests

SPOT_DEMO_HOST = "https://demo-api.binance.com"
FUTURES_DEMO_HOST = "https://demo-fapi.binance.com"
SPOT_ACCOUNT_PATH = "/api/v3/account"
FUTURES_ACCOUNT_PATH = "/fapi/v2/account"


class DemoPreflightError(RuntimeError):
    pass


def configuration_status() -> dict[str, Any]:
    spot_key = bool(os.getenv("CSR_BINANCE_DEMO_SPOT_API_KEY", "").strip())
    spot_secret = bool(os.getenv("CSR_BINANCE_DEMO_SPOT_API_SECRET", "").strip())
    futures_key = bool(os.getenv("CSR_BINANCE_DEMO_FUTURES_API_KEY", "").strip())
    futures_secret = bool(os.getenv("CSR_BINANCE_DEMO_FUTURES_API_SECRET", "").strip())
    return {
        "mode": "BINANCE_DEMO_READ_ONLY_PREFLIGHT",
        "spot_credentials_configured": spot_key and spot_secret,
        "spot_credentials_complete": spot_key == spot_secret,
        "futures_credentials_configured": futures_key and futures_secret,
        "futures_credentials_complete": futures_key == futures_secret,
        "order_submission_available": False,
        "orders_submitted": False,
        "private_keys_requested": False,
        "note": "Only account-read preflight is implemented. No order endpoint is exposed.",
    }


class BinanceDemoReadOnlyClient:
    """Signed GET-only account checker with separate Spot and USD-M Demo keys."""

    def __init__(self, *, session: Any = None, clock: Any = None):
        self.session = session or requests.Session()
        self.clock = clock or time.time

    @staticmethod
    def _signed_params(params: dict[str, Any], secret: str, timestamp_ms: int) -> dict[str, Any]:
        values = dict(params)
        values["timestamp"] = int(timestamp_ms)
        values["recvWindow"] = 5000
        query = urlencode(values)
        values["signature"] = hmac.new(
            secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return values

    def _get(self, host: str, path: str, *, api_key: str, secret: str) -> dict[str, Any]:
        if host not in {SPOT_DEMO_HOST, FUTURES_DEMO_HOST}:
            raise DemoPreflightError("Only the hard-coded Binance Demo hosts are allowed.")
        allowed = {
            SPOT_DEMO_HOST: {SPOT_ACCOUNT_PATH},
            FUTURES_DEMO_HOST: {FUTURES_ACCOUNT_PATH},
        }
        if path not in allowed[host]:
            raise DemoPreflightError("This read-only preflight endpoint is not allowlisted.")
        if not api_key.strip() or not secret.strip():
            raise DemoPreflightError("The matching Demo USER_DATA API credentials are not configured.")
        params = self._signed_params({}, secret, int(self.clock() * 1000))
        try:
            response = self.session.get(
                host + path, params=params,
                headers={"X-MBX-APIKEY": api_key}, timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise DemoPreflightError("Binance Demo account preflight request failed.") from exc
        except ValueError as exc:
            raise DemoPreflightError("Binance Demo returned invalid JSON.") from exc
        if not isinstance(payload, dict) or "code" in payload and int(payload.get("code", 0)) < 0:
            raise DemoPreflightError("Binance Demo returned an invalid or rejected account response.")
        return payload

    def spot_account(self) -> dict[str, Any]:
        return self._get(
            SPOT_DEMO_HOST, SPOT_ACCOUNT_PATH,
            api_key=os.getenv("CSR_BINANCE_DEMO_SPOT_API_KEY", ""),
            secret=os.getenv("CSR_BINANCE_DEMO_SPOT_API_SECRET", ""),
        )

    def futures_account(self) -> dict[str, Any]:
        return self._get(
            FUTURES_DEMO_HOST, FUTURES_ACCOUNT_PATH,
            api_key=os.getenv("CSR_BINANCE_DEMO_FUTURES_API_KEY", ""),
            secret=os.getenv("CSR_BINANCE_DEMO_FUTURES_API_SECRET", ""),
        )
