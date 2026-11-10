"""Guarded Binance Spot order adapter. Testnet is the default; production needs two explicit opt-ins.

This adapter never retries order submission. A timeout can mean an order was accepted;
call get_order(client_order_id) to reconcile before taking any further action.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional
from urllib.parse import urlencode

import requests


ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})
LIVE_CONFIRMATION = "I_CONFIRM_REAL_SPOT_ORDER"
ABSOLUTE_MAX_NOTIONAL_USDT = Decimal("25")


class OrderSafetyError(ValueError):
    pass


class OrderStatusUnknown(RuntimeError):
    """Submission may have reached the matching engine; reconcile by client order id."""

    def __init__(self, client_order_id: str):
        super().__init__(
            f"Order status is unknown. Reconcile clientOrderId={client_order_id}; do not retry."
        )
        self.client_order_id = client_order_id


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise OrderSafetyError(f"{name} must be a valid decimal.") from exc
    if not result.is_finite() or result <= 0:
        raise OrderSafetyError(f"{name} must be positive and finite.")
    return result


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class BinanceSpotExecutor:
    """Small, spot-only Binance REST adapter using limit IOC orders."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        environment: Optional[str] = None,
        session: Any = None,
        clock: Any = None,
    ):
        self.api_key = api_key or os.getenv("CSR_BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("CSR_BINANCE_API_SECRET", "")
        self.environment = (environment or os.getenv("CSR_BINANCE_ENV", "testnet")).lower()
        if self.environment not in {"testnet", "production"}:
            raise OrderSafetyError("CSR_BINANCE_ENV must be testnet or production.")
        self.base_url = (
            "https://testnet.binance.vision"
            if self.environment == "testnet"
            else "https://api.binance.com"
        )
        self.session = session or requests.Session()
        self.clock = clock or time.time

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 *, signed: bool = False) -> dict:
        params = dict(params or {})
        headers = {}
        if signed:
            if not self.api_key or not self.api_secret:
                raise OrderSafetyError("Binance API credentials are not configured.")
            params["timestamp"] = int(self.clock() * 1000)
            params["recvWindow"] = 5000
            payload = urlencode(params)
            params["signature"] = hmac.new(
                self.api_secret.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key
        try:
            response = self.session.request(
                method, self.base_url + path, params=params if method == "GET" else None,
                data=params if method != "GET" else None, headers=headers, timeout=10,
            )
        except requests.RequestException as exc:
            if method == "POST" and path == "/api/v3/order":
                client_id = str((params or {}).get("newClientOrderId", "unknown"))
                raise OrderStatusUnknown(client_id) from exc
            raise
        if method == "POST" and path == "/api/v3/order" and response.status_code >= 500:
            client_id = str(params.get("newClientOrderId", "unknown"))
            raise OrderStatusUnknown(client_id)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise OrderSafetyError("Binance returned an invalid response object.")
        if "code" in data and int(data.get("code", 0)) < 0:
            raise OrderSafetyError("Binance rejected the request.")
        return data

    def account(self) -> dict:
        return self._request("GET", "/api/v3/account", signed=True)

    def get_order(self, symbol: str, client_order_id: str) -> dict:
        if symbol not in ALLOWED_SYMBOLS or not client_order_id:
            raise OrderSafetyError("A supported symbol and client order id are required.")
        return self._request(
            "GET", "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True,
        )

    def submit_limit_ioc(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Any,
        reference_price: Any,
        max_slippage_bps: int = 15,
        max_notional_usdt: Any = "25",
        confirm_live: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Submit one bounded limit IOC spot order. No retries are made."""
        if symbol not in ALLOWED_SYMBOLS:
            raise OrderSafetyError("Only BTCUSDT, ETHUSDT and SOLUSDT are allowed.")
        side = str(side).upper()
        if side not in {"BUY", "SELL"}:
            raise OrderSafetyError("Spot orders support BUY or SELL only.")
        if self.environment == "production":
            enabled = os.getenv("CSR_LIVE_TRADING_ENABLED") == "YES"
            if not enabled or confirm_live != LIVE_CONFIRMATION:
                raise OrderSafetyError(
                    "Production orders require CSR_LIVE_TRADING_ENABLED=YES and explicit confirmation."
                )
            # Production is deliberately locked until server-side protective exits are integrated.
            if os.getenv("CSR_LIVE_PROTECTIVE_EXITS_READY") != "YES":
                raise OrderSafetyError(
                    "Production orders are locked until protective exit orders and reconciliation are enabled."
                )
        qty = _decimal(quantity, "quantity")
        ref = _decimal(reference_price, "reference_price")
        cap = _decimal(max_notional_usdt, "max_notional_usdt")
        if cap > ABSOLUTE_MAX_NOTIONAL_USDT:
            raise OrderSafetyError("Per-order notional cap cannot exceed 25 USDT.")
        if not isinstance(max_slippage_bps, int) or not 1 <= max_slippage_bps <= 50:
            raise OrderSafetyError("max_slippage_bps must be an integer from 1 through 50.")
        if qty * ref > cap:
            raise OrderSafetyError("Order exceeds the configured notional cap.")

        info = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = info.get("symbols")
        if not isinstance(symbols, list) or len(symbols) != 1:
            raise OrderSafetyError("Exchange symbol metadata is unavailable.")
        item = symbols[0]
        if item.get("status") != "TRADING" or item.get("isSpotTradingAllowed") is False:
            raise OrderSafetyError("This spot symbol is not currently tradable.")
        filters = {f.get("filterType"): f for f in item.get("filters", [])}
        lot = filters.get("LOT_SIZE")
        price_filter = filters.get("PRICE_FILTER")
        notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
        if not lot or not price_filter:
            raise OrderSafetyError("Required exchange quantity or price filters are missing.")
        qty = _floor_step(qty, _decimal(lot.get("stepSize"), "stepSize"))
        if qty < _decimal(lot.get("minQty"), "minQty"):
            raise OrderSafetyError("Order quantity is below the exchange minimum.")
        if "maxQty" in lot and qty > _decimal(lot["maxQty"], "maxQty"):
            raise OrderSafetyError("Order quantity exceeds the exchange maximum.")

        book = self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        best = _decimal(book.get("askPrice" if side == "BUY" else "bidPrice"), "best quote")
        drift_bps = abs(best - ref) / ref * Decimal(10000)
        if drift_bps > max_slippage_bps:
            raise OrderSafetyError("Live quote moved beyond the allowed signal-price slippage.")
        slippage = Decimal(max_slippage_bps) / Decimal(10000)
        raw_limit = best * (Decimal(1) + slippage if side == "BUY" else Decimal(1) - slippage)
        tick = _decimal(price_filter.get("tickSize"), "tickSize")
        limit_price = _floor_step(raw_limit, tick)
        if limit_price <= 0:
            raise OrderSafetyError("Calculated limit price is invalid.")
        notional = qty * limit_price
        if notional > cap:
            raise OrderSafetyError("Rounded order exceeds the 25 USDT notional cap.")
        if notional_filter:
            minimum = notional_filter.get("minNotional")
            if minimum is not None and notional < _decimal(minimum, "minNotional"):
                raise OrderSafetyError("Order is below the exchange minimum notional.")

        account = self.account()
        balances = account.get("balances", [])
        base_asset, quote_asset = symbol[:-4], symbol[-4:]
        required_asset = quote_asset if side == "BUY" else base_asset
        free = None
        for balance in balances:
            if balance.get("asset") == required_asset:
                free = _decimal(balance.get("free", "0"), "free balance") if Decimal(str(balance.get("free", "0"))) > 0 else Decimal(0)
                break
        if free is None:
            raise OrderSafetyError("Required free account balance is unavailable.")
        required = notional * Decimal("1.01") if side == "BUY" else qty
        if free < required:
            raise OrderSafetyError("Free balance is insufficient for this spot order.")

        cid = client_order_id or ("csr" + uuid.uuid4().hex[:29])
        if len(cid) > 36 or not cid.isascii():
            raise OrderSafetyError("client_order_id must be ASCII and at most 36 characters.")
        response = self._request("POST", "/api/v3/order", {
            "symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "IOC",
            "quantity": format(qty, "f"), "price": format(limit_price, "f"),
            "newClientOrderId": cid, "newOrderRespType": "FULL",
        }, signed=True)
        return {
            "mode": self.environment.upper(),
            "symbol": symbol,
            "side": side,
            "quantity": format(qty, "f"),
            "limit_price": format(limit_price, "f"),
            "client_order_id": cid,
            "exchange_order_id": response.get("orderId"),
            "status": response.get("status", "UNKNOWN"),
            "executed_quantity": response.get("executedQty", "0"),
            "cumulative_quote_quantity": response.get("cummulativeQuoteQty", "0"),
            "response": response,
        }


class ValidatedSignalExecutor:
    """Run the strategy's fail-closed checks, size from exchange USDT, then submit one spot entry."""

    def __init__(self, executor: Optional[BinanceSpotExecutor] = None, *,
                 min_score: float = 75.0, max_risk_score: float = 45.0,
                 min_net_reward_risk: float = 1.8):
        self.executor = executor or BinanceSpotExecutor()
        self.min_score = float(min_score)
        self.max_risk_score = float(max_risk_score)
        self.min_net_reward_risk = float(min_net_reward_risk)

    def submit_long(self, *, result: Any, symbol: str, reference_price: Any,
                    confirm_live: Optional[str] = None,
                    client_order_id: Optional[str] = None) -> dict:
        """Submit only a fully validated LONG from the existing 12-gate engine."""
        if symbol not in ALLOWED_SYMBOLS:
            raise OrderSafetyError("Only BTCUSDT, ETHUSDT and SOLUSDT are allowed.")
        gates = getattr(result, "gates", None)
        if not isinstance(gates, (list, tuple)) or len(gates) != 12:
            raise OrderSafetyError("All twelve mandatory signal gates must be present.")
        if any(str(getattr(g, "status", "")).upper() != "PASS" for g in gates):
            raise OrderSafetyError("Every mandatory signal gate must PASS.")
        if str(getattr(result, "signal", "")).upper() != "LONG":
            raise OrderSafetyError("Spot execution accepts LONG signals only.")
        score = _decimal(getattr(result, "confirmation_score", None), "confirmation score")
        try:
            risk = Decimal(str(getattr(result, "estimated_risk_score", None)))
        except Exception as exc:
            raise OrderSafetyError("risk score must be a valid decimal.") from exc
        if not risk.is_finite() or risk < 0:
            raise OrderSafetyError("risk score must be finite and non-negative.")
        if score < Decimal(str(self.min_score)) or risk >= Decimal(str(self.max_risk_score)):
            raise OrderSafetyError("Signal score or risk did not pass execution limits.")
        price = _decimal(reference_price, "reference_price")
        stop = _decimal(getattr(result, "stop_loss", None), "stop_loss")
        target = _decimal(getattr(result, "take_profit", None), "take_profit")
        if not stop < price < target:
            raise OrderSafetyError("Signal stop, reference and target are not safely ordered.")
        holding = _decimal(getattr(result, "expected_holding_days", None), "expected_holding_days")
        if holding < 1 or holding > 90:
            raise OrderSafetyError("Signal holding period must be between 1 and 90 days.")

        account = self.executor.account()
        free_usdt = None
        for balance in account.get("balances", []):
            if balance.get("asset") == "USDT":
                raw = Decimal(str(balance.get("free", "0")))
                free_usdt = raw if raw.is_finite() and raw > 0 else Decimal(0)
                break
        if not free_usdt or free_usdt <= 0:
            raise OrderSafetyError("No free USDT balance is available.")
        fee = Decimal("0.0008")
        slip = Decimal("0.0015")
        estimated_entry = price * (Decimal(1) + slip)
        stop_fill = stop * (Decimal(1) - slip)
        target_fill = target * (Decimal(1) - slip)
        unit_risk = estimated_entry - stop_fill + estimated_entry * fee + stop_fill * fee
        unit_reward = target_fill - estimated_entry - estimated_entry * fee - target_fill * fee
        net_rr = unit_reward / unit_risk if unit_risk > 0 else Decimal(0)
        if unit_risk <= 0 or unit_reward <= 0 or net_rr < Decimal(str(self.min_net_reward_risk)):
            raise OrderSafetyError("Signal net reward/risk is below the configured minimum.")
        quantity = min(
            free_usdt * Decimal("0.005") / unit_risk,
            Decimal("25") / estimated_entry,
            free_usdt / (estimated_entry * Decimal("1.01")),
        )
        order = self.executor.submit_limit_ioc(
            symbol=symbol, side="BUY", quantity=quantity,
            reference_price=price, max_slippage_bps=15,
            max_notional_usdt="25", confirm_live=confirm_live,
            client_order_id=client_order_id,
        )
        order["signal_confirmation_score"] = str(score)
        order["signal_net_reward_risk"] = str(net_rr.quantize(Decimal("0.001")))
        order["signal_max_holding_days"] = int(holding)
        order["risk_budget_usdt"] = str((free_usdt * Decimal("0.005")).quantize(Decimal("0.01")))
        return order
