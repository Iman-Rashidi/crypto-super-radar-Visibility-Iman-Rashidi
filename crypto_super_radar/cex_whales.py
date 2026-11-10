"""Large-trade flow proxy for CEX spot and perpetual markets.

This detects whale-sized executions, not wallet identities. It samples the
latest bounded trade feed from Binance and Bybit and requires venue agreement.
"""
from __future__ import annotations

from typing import Any, Optional

from .config import AppConfig
from .providers import DataProviderError, PublicHttpClient


class CexWhaleScanner:
    @staticmethod
    def _access_denied(exc: Exception) -> bool:
        return any(code in str(exc) for code in ("HTTP 403", "HTTP 451"))

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            number = float(value)
            return number if number == number and abs(number) != float("inf") else None
        except (TypeError, ValueError, OverflowError):
            return None

    def __init__(self, config: Optional[AppConfig] = None, threshold_usd: float = 100_000.0):
        self.config = config or AppConfig()
        self.http = PublicHttpClient(self.config)
        self.threshold_usd = float(threshold_usd)

    @staticmethod
    def _summary(trades: list[tuple[float, str]]) -> dict[str, Any]:
        buys = [value for value, side in trades if side == "BUY"]
        sells = [value for value, side in trades if side == "SELL"]
        buy_usd, sell_usd = sum(buys), sum(sells)
        return {
            "buy_usd": buy_usd, "sell_usd": sell_usd,
            "net_aggressive_flow_usd": buy_usd - sell_usd,
            "large_buy_count": len(buys), "large_sell_count": len(sells),
            "large_trade_count": len(trades),
        }

    def scan(self, symbol: str, market_type: str) -> dict[str, Any]:
        if market_type not in {"cex", "perp"}:
            return {"status": "FAIL", "reason": "CEX whale scanner only supports spot/perpetual symbols."}
        try:
            if market_type == "perp":
                binance_url = "https://fapi.binance.com/fapi/v1/aggTrades"
                category = "linear"
            else:
                binance_url = "https://data-api.binance.vision/api/v3/aggTrades"
                category = "spot"
            try:
                binance_rows = self.http.get(binance_url, {"symbol": symbol, "limit": 1000})
                source_a = "Binance"
            except DataProviderError as exc:
                if not self._access_denied(exc):
                    raise
                if market_type == "perp":
                    contract = f"{symbol[:-4]}_USDT"
                    rows = self.http.get("https://api.gateio.ws/api/v4/futures/usdt/trades",
                                         {"contract": contract, "limit": 1000})
                    meta = self.http.get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{contract}")
                    multiplier = self._number(meta.get("quanto_multiplier")) if isinstance(meta, dict) else None
                    if multiplier is None or multiplier <= 0 or not isinstance(rows, list):
                        raise DataProviderError("Gate.io trade fallback has incomplete contract metadata") from exc
                    binance_rows = [
                        {"p": row.get("price"), "q": abs(self._number(row.get("size")) or 0) * multiplier,
                         "m": (_number(row.get("size")) or 0) < 0}
                        for row in rows if isinstance(row, dict)
                    ]
                else:
                    rows = self.http.get("https://api.gateio.ws/api/v4/spot/trades",
                                         {"currency_pair": f"{symbol[:-4]}_{symbol[-4:]}", "limit": 1000})
                    if not isinstance(rows, list):
                        raise DataProviderError("Gate.io spot trade fallback returned an invalid schema") from exc
                    binance_rows = [
                        {"p": row.get("price"), "q": row.get("amount"),
                         "m": str(row.get("side", "")).lower() == "sell"}
                        for row in rows if isinstance(row, dict)
                    ]
                source_a = "Gate.io"

            try:
                bybit_payload = self.http.get(
                    "https://api.bybit.com/v5/market/recent-trade",
                    {"category": category, "symbol": symbol, "limit": 1000},
                )
                bybit_rows = ((bybit_payload.get("result") or {}).get("list") or [])
                source_b = "Bybit"
            except DataProviderError as exc:
                if not self._access_denied(exc):
                    raise
                instrument = f"{symbol[:-4]}-{symbol[-4:]}" + ("-SWAP" if market_type == "perp" else "")
                payload = self.http.get("https://www.okx.com/api/v5/market/trades",
                                        {"instId": instrument, "limit": 500})
                if not isinstance(payload, dict) or payload.get("code") != "0" or not isinstance(payload.get("data"), list):
                    raise DataProviderError("OKX trade fallback returned an invalid schema") from exc
                multiplier = 1.0
                if market_type == "perp":
                    meta = self.http.get("https://www.okx.com/api/v5/public/instruments",
                                         {"instType": "SWAP", "instId": instrument})
                    meta_rows = meta.get("data") if isinstance(meta, dict) and meta.get("code") == "0" else None
                    if not isinstance(meta_rows, list) or len(meta_rows) != 1:
                        raise DataProviderError("OKX swap trade fallback omitted contract metadata") from exc
                    multiplier = self._number(meta_rows[0].get("ctVal"))
                    if multiplier is None or multiplier <= 0:
                        raise DataProviderError("OKX swap contract value is missing or invalid") from exc
                bybit_rows = [
                    {"price": row.get("px"), "size": (self._number(row.get("sz")) or 0) * multiplier,
                     "side": str(row.get("side", "")).upper()}
                    for row in payload["data"] if isinstance(row, dict)
                ]
                source_b = "OKX"
            if not isinstance(binance_rows, list) or not bybit_rows:
                return {"status": "UNKNOWN", "reason": "One or both CEX trade feeds returned no usable sample."}
            binance_trades = []
            for row in binance_rows:
                try:
                    notional = float(row["p"]) * float(row["q"])
                    if notional >= self.threshold_usd:
                        side = "SELL" if bool(row["m"]) else "BUY"
                        binance_trades.append((notional, side))
                except (KeyError, TypeError, ValueError):
                    continue
            bybit_trades = []
            for row in bybit_rows:
                try:
                    notional = float(row["price"]) * float(row["size"])
                    side = str(row["side"]).upper()
                    if notional >= self.threshold_usd and side in {"BUY", "SELL"}:
                        bybit_trades.append((notional, side))
                except (KeyError, TypeError, ValueError):
                    continue
            if not binance_rows or not bybit_rows:
                return {"status": "UNKNOWN", "reason": "CEX sample coverage is incomplete."}
            a, b = self._summary(binance_trades), self._summary(bybit_trades)
            if a["large_trade_count"] == 0 or b["large_trade_count"] == 0:
                return {
                    "status": "UNKNOWN",
                    "reason": "Both venues must contain at least one whale-sized trade in the bounded sample.",
                    "binance": a, "bybit": b, "sources": [source_a, source_b],
                    "coverage": "LATEST_UP_TO_1000_TRADES_PER_VENUE",
                }
            net_a, net_b = a["net_aggressive_flow_usd"], b["net_aggressive_flow_usd"]
            sign_a = 1 if net_a > 0 else -1 if net_a < 0 else 0
            sign_b = 1 if net_b > 0 else -1 if net_b < 0 else 0
            if sign_a != sign_b:
                return {
                    "status": "UNKNOWN", "reason": f"{source_a} and {source_b} large-trade flow directions disagree.",
                    "binance": a, "bybit": b, "coverage": "LATEST_UP_TO_1000_TRADES_PER_VENUE",
                }
            return {
                "status": "KNOWN",
                "net_whale_trade_flow_usd": (net_a + net_b) / 2.0,
                "binance": a, "bybit": b,
                "coverage": "LATEST_UP_TO_1000_TRADES_PER_VENUE",
                "sources": [source_a, source_b],
                "interpretation": "Positive flow means aggressive large-trade buying; negative means aggressive large-trade selling. This is not wallet attribution or a full-day total.",
            }
        except Exception as exc:
            return {"status": "UNKNOWN", "reason": f"CEX large-trade scan failed: {type(exc).__name__}"}
