"""Large-trade flow proxy for CEX spot and perpetual markets.

This detects whale-sized executions, not wallet identities. It samples the
latest bounded trade feed from Binance and Bybit and requires venue agreement.
"""
from __future__ import annotations

from typing import Any, Optional

from .config import AppConfig
from .providers import PublicHttpClient


class CexWhaleScanner:
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
            binance_rows = self.http.get(binance_url, {"symbol": symbol, "limit": 1000})
            bybit_payload = self.http.get(
                "https://api.bybit.com/v5/market/recent-trade",
                {"category": category, "symbol": symbol, "limit": 1000},
            )
            bybit_rows = ((bybit_payload.get("result") or {}).get("list") or [])
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
                    "binance": a, "bybit": b,
                    "coverage": "LATEST_UP_TO_1000_TRADES_PER_VENUE",
                }
            net_a, net_b = a["net_aggressive_flow_usd"], b["net_aggressive_flow_usd"]
            sign_a = 1 if net_a > 0 else -1 if net_a < 0 else 0
            sign_b = 1 if net_b > 0 else -1 if net_b < 0 else 0
            if sign_a != sign_b:
                return {
                    "status": "UNKNOWN", "reason": "Binance and Bybit large-trade flow directions disagree.",
                    "binance": a, "bybit": b, "coverage": "LATEST_UP_TO_1000_TRADES_PER_VENUE",
                }
            return {
                "status": "KNOWN",
                "net_whale_trade_flow_usd": (net_a + net_b) / 2.0,
                "binance": a, "bybit": b,
                "coverage": "LATEST_UP_TO_1000_TRADES_PER_VENUE",
                "interpretation": "Positive flow means aggressive large-trade buying; negative means aggressive large-trade selling. This is not wallet attribution or a full-day total.",
            }
        except Exception as exc:
            return {"status": "UNKNOWN", "reason": f"CEX large-trade scan failed: {type(exc).__name__}"}
