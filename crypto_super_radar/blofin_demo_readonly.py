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


class BloFinDemoHTTPError(BloFinDemoError):
    """Sanitized HTTP failure with a safe retry hint."""

    def __init__(self, status_code: int, retry_after_seconds: float | None = None):
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"BloFin Demo HTTP request failed (status={self.status_code}).")


def _check_response_codes(payload: Mapping[str, Any]) -> None:
    """Check both the HTTP API envelope and per-item business result codes."""
    top_code = payload.get("code")
    if top_code is not None and str(top_code) != "0":
        code = str(top_code)[:24]
        if code == "401":
            raise BloFinDemoAuthError("BloFin rejected API authentication (code=401).")
        raise BloFinDemoResponseError(
            f"BloFin rejected the request (code={code})."
        )
    data = payload.get("data")
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if isinstance(row, dict) and row.get("code") is not None:
            item_code = str(row.get("code"))
            if item_code != "0":
                if item_code == "401":
                    raise BloFinDemoAuthError("BloFin rejected API authentication (code=401).")
                raise BloFinDemoResponseError(
                    f"BloFin rejected an item in the response (code={item_code[:24]})."
                )


def _canonical_query(params: Mapping[str, Any] | None) -> str:
    if not params:
        return ""
    clean = [(str(k), str(v)) for k, v in params.items() if v is not None]
    return urlencode(clean)


def _signature(
    secret: str, method: str, path: str, timestamp: str, nonce: str, body: str = ""
) -> str:
    prehash = f"{path}{method.upper()}{timestamp}{nonce}{body}"
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
        # Render dashboard pastes can accidentally retain leading/trailing whitespace.
        # Normalize only the outside; never alter characters inside a credential.
        self.api_key = str(api_key if api_key is not None else os.getenv("BLOFIN_DEMO_API_KEY", "")).strip()
        self.api_secret = str(api_secret if api_secret is not None else os.getenv("BLOFIN_DEMO_API_SECRET", "")).strip()
        self.passphrase = str(passphrase if passphrase is not None else os.getenv("BLOFIN_DEMO_PASSPHRASE", "")).strip()
        for env_name, credential in (
            ("BLOFIN_DEMO_API_KEY", self.api_key),
            ("BLOFIN_DEMO_API_SECRET", self.api_secret),
            ("BLOFIN_DEMO_PASSPHRASE", self.passphrase),
        ):
            if any(ord(char) < 33 or ord(char) > 126 for char in credential):
                raise BloFinDemoAuthError(
                    f"{env_name} contains whitespace, a control character, or a non-ASCII character."
                )
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
        for attempt in range(3):
            try:
                response = self.session.get(url, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                break
            except requests.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 429 and attempt < 2:
                    retry_header = getattr(exc.response, "headers", {}).get("Retry-After")
                    try:
                        server_delay = max(0.0, float(retry_header))
                    except (TypeError, ValueError):
                        server_delay = 0.0
                    time.sleep(max(server_delay, min(0.5 * (2 ** attempt), 4.0)))
                    continue
                if status == 401:
                    raise BloFinDemoAuthError("BloFin rejected API authentication (HTTP 401).") from exc
                if status == 403:
                    # BloFin documents a temporary firewall ban of about five minutes.
                    # Do not hammer the endpoint; let the caller schedule a later retry.
                    raise BloFinDemoHTTPError(403, retry_after_seconds=300.0) from exc
                if status == 429:
                    raise BloFinDemoHTTPError(429, retry_after_seconds=4.0) from exc
                raise BloFinDemoError("BloFin Demo request failed.") from exc
            except requests.RequestException as exc:
                raise BloFinDemoError("BloFin Demo request failed.") from exc
            except (TypeError, ValueError) as exc:
                raise BloFinDemoResponseError("BloFin returned invalid JSON.") from exc
        else:
            raise BloFinDemoHTTPError(429, retry_after_seconds=4.0)
        if not isinstance(payload, dict):
            raise BloFinDemoResponseError("BloFin response must be a JSON object.")
        _check_response_codes(payload)
        if "data" not in payload:
            raise BloFinDemoResponseError("BloFin response is missing the data field.")
        return payload["data"]

    # Public market-data endpoints: no API key is sent.
    def get_futures_instruments(
        self, *, inst_id: str | None = None, api_permission: str | None = None
    ) -> Any:
        if api_permission not in {None, "full", "reduce_only"}:
            raise ValueError("api_permission must be 'full' or 'reduce_only'.")
        return self._get(
            "/api/v1/market/instruments",
            params={"instId": inst_id, "apiPermission": api_permission},
        )

    def get_tradable_instrument(self, inst_id: str) -> dict[str, Any]:
        """Return validated linear USDT contract metadata only when API permission is full."""
        if not inst_id or not isinstance(inst_id, str):
            raise ValueError("inst_id is required.")
        rows = self.get_futures_instruments(inst_id=inst_id, api_permission="full")
        if not isinstance(rows, list):
            raise BloFinDemoResponseError("Instrument response is not a list.")
        match = next(
            (row for row in rows if isinstance(row, dict) and row.get("instId") == inst_id),
            None,
        )
        if match is None or match.get("apiPermission") != "full":
            raise BloFinDemoResponseError("Instrument is not fully enabled for API trading.")
        if match.get("state") != "live" or match.get("contractType") != "linear":
            raise BloFinDemoResponseError("Only live linear contracts are accepted.")
        if match.get("settleCurrency") != "USDT":
            raise BloFinDemoResponseError("Only USDT-settled contracts are accepted in this phase.")
        try:
            values = {
                "contractValue": float(match["contractValue"]),
                "minSize": float(match["minSize"]),
                "lotSize": float(match["lotSize"]),
                "tickSize": float(match["tickSize"]),
            }
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise BloFinDemoResponseError("Instrument contract metadata is incomplete.") from exc
        if any(not (value > 0) for value in values.values()):
            raise BloFinDemoResponseError("Instrument contract metadata is invalid.")
        return {**match, **values}

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
    def get_position_mode(self) -> dict[str, Any]:
        result = self._get("/api/v1/account/position-mode", private=True)
        if not isinstance(result, dict):
            raise BloFinDemoResponseError("Position-mode response is invalid.")
        mode = result.get("positionMode")
        if mode not in {"net_mode", "long_short_mode"}:
            raise BloFinDemoResponseError("Position mode is missing or unsupported.")
        return result

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

    def get_order_detail(self, inst_id: str, *, client_order_id: str) -> Any:
        """Read one order by its client id so an ambiguous POST is never retried blindly."""
        if not inst_id or not client_order_id:
            raise ValueError("inst_id and client_order_id are required.")
        return self._get(
            "/api/v1/trade/order-detail",
            params={"instId": inst_id, "clientOrderId": client_order_id},
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
