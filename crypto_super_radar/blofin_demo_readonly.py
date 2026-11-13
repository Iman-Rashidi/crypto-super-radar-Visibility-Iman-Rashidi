"""BloFin Demo Futures read-only adapter.

This module is intentionally incapable of submitting, amending, cancelling,
transferring, or withdrawing. The REST host is pinned to BloFin Demo.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from typing import Any, Mapping
from urllib.parse import urlencode

import requests

DEMO_REST_URL = "https://demo-trading-openapi.blofin.com"
DEMO_PUBLIC_WS_URL = "wss://demo-trading-openapi.blofin.com/ws/public"
DEMO_PRIVATE_WS_URL = "wss://demo-trading-openapi.blofin.com/ws/private"
WS_LOGIN_PATH = "/users/self/verify"


class BloFinDemoError(RuntimeError):
    """Base error for the read-only BloFin Demo adapter."""


class BloFinDemoAuthError(BloFinDemoError):
    """Missing or rejected read-only API credentials."""


class BloFinDemoResponseError(BloFinDemoError):
    """Unexpected or unsuccessful BloFin response."""


def _canonical_query(params: Mapping[str, Any] | None) -> str:
    if not params:
        return ""
    clean = [(str(k), str(v)) for k, v in params.items() if v is not None]
    return urlencode(clean)


def _signature(secret: str, method: str, path: str, timestamp: str, nonce: str) -> str:
    prehash = f"{path}{method.upper()}{timestamp}{nonce}"
    digest_hex = hmac.new(
        secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    # BloFin signs the hex digest as UTF-8 text, then Base64-encodes those bytes.
    return base64.b64encode(digest_hex.encode("utf-8")).decode("ascii")


class BloFinDemoReadOnly:
    """Read-only REST client pinned to the BloFin Demo API host."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        session: Any = None,
        clock_ms: Any = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("BLOFIN_DEMO_API_KEY", "")
        self.api_secret = api_secret if api_secret is not None else os.getenv("BLOFIN_DEMO_API_SECRET", "")
        self.passphrase = passphrase if passphrase is not None else os.getenv("BLOFIN_DEMO_PASSPHRASE", "")
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.timeout = timeout
        self.base_url = DEMO_REST_URL

    def _auth_headers(self, path_with_query: str) -> dict[str, str]:
        if not all((self.api_key, self.api_secret, self.passphrase)):
            raise BloFinDemoAuthError(
                "Set BLOFIN_DEMO_API_KEY, BLOFIN_DEMO_API_SECRET and BLOFIN_DEMO_PASSPHRASE."
            )
        timestamp = str(int(self.clock_ms()))
        nonce = str(uuid.uuid4())
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": _signature(
                self.api_secret, "GET", path_with_query, timestamp, nonce
            ),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        private: bool = False,
    ) -> Any:
        query = _canonical_query(params)
        path_with_query = f"{path}?{query}" if query else path
        headers = self._auth_headers(path_with_query) if private else {}
        url = f"{self.base_url}{path_with_query}"
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise BloFinDemoError("BloFin Demo request failed.") from exc
        except (TypeError, ValueError) as exc:
            raise BloFinDemoResponseError("BloFin returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise BloFinDemoResponseError("BloFin response must be a JSON object.")
        if str(payload.get("code", "0")) != "0":
            # Do not echo response bodies: they may contain account details.
            raise BloFinDemoResponseError(
                f"BloFin rejected the read request (code={payload.get('code', 'unknown')})."
            )
        if "data" not in payload:
            raise BloFinDemoResponseError("BloFin response is missing the data field.")
        return payload["data"]

    # Public market-data endpoints: no API key is sent.
    def get_futures_instruments(self, *, inst_id: str | None = None) -> Any:
        return self._get(
            "/api/v1/market/instruments",
            params={"instId": inst_id, "instType": "SWAP"},
        )

    def get_tickers(self, *, inst_id: str | None = None) -> Any:
        return self._get("/api/v1/market/tickers", params={"instId": inst_id})

    def get_candles(
        self, inst_id: str, *, bar: str = "1m", limit: int = 100
    ) -> Any:
        if not inst_id or not isinstance(limit, int) or not 1 <= limit <= 300:
            raise ValueError("inst_id is required and limit must be between 1 and 300.")
        return self._get(
            "/api/v1/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": limit},
        )

    # Private endpoints: READ permission only.
    def get_futures_balance(self, *, product_type: str = "USDT-FUTURES") -> Any:
        return self._get(
            "/api/v1/account/balance",
            params={"productType": product_type},
            private=True,
        )

    def get_positions(self, *, inst_id: str | None = None) -> Any:
        return self._get(
            "/api/v1/account/positions",
            params={"instId": inst_id},
            private=True,
        )

    def get_open_orders(self, *, inst_id: str | None = None) -> Any:
        return self._get(
            "/api/v1/trade/orders-pending",
            params={"instId": inst_id},
            private=True,
        )

    def check_clock_skew(self, *, inst_id: str = "BTC-USDT") -> dict[str, int]:
        """Estimate exchange clock offset from the exchange timestamp in ticker data."""
        rows = self.get_tickers(inst_id=inst_id)
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise BloFinDemoResponseError("Ticker timestamp is unavailable.")
        try:
            exchange_ms = int(rows[0]["ts"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BloFinDemoResponseError("Ticker timestamp is invalid.") from exc
        local_ms = int(self.clock_ms())
        offset_ms = exchange_ms - local_ms
        return {"exchange_time_ms": exchange_ms, "local_time_ms": local_ms, "offset_ms": offset_ms}

    def websocket_login_args(self) -> dict[str, str]:
        """Build the authenticated private-WebSocket login payload; sends no trade action."""
        if not all((self.api_key, self.api_secret, self.passphrase)):
            raise BloFinDemoAuthError(
                "Set BLOFIN_DEMO_API_KEY, BLOFIN_DEMO_API_SECRET and BLOFIN_DEMO_PASSPHRASE."
            )
        timestamp = str(int(self.clock_ms()))
        nonce = str(uuid.uuid4())
        sign = _signature(self.api_secret, "GET", WS_LOGIN_PATH, timestamp, nonce)
        return {
            "apiKey": self.api_key,
            "passphrase": self.passphrase,
            "timestamp": timestamp,
            "sign": sign,
            "nonce": nonce,
        }

    @staticmethod
    def websocket_urls() -> dict[str, str]:
        return {"public": DEMO_PUBLIC_WS_URL, "private": DEMO_PRIVATE_WS_URL}
