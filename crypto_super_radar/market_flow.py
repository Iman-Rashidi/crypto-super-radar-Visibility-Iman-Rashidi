"""Lightweight volume-pressure and large-trade flow analytics.

Large prints are observable trades, not proof of a single whale or wallet identity.
Candle-derived pressure is an estimate; aggressor-side flow comes from aggTrades.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def analyze_market_flow(
    candles: pd.DataFrame,
    *,
    aggregate_trades: list[dict[str, Any]] | None = None,
    order_book: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return quote-volume pressure, RVOL, aggressor CVD, large prints and depth bias."""
    empty = {
        "status": "INSUFFICIENT_DATA",
        "source": "Binance Spot",
        "volume_unit": "USDT quote notional",
        "pressure": {"buy_pct": None, "sell_pct": None, "buy_quote": None, "sell_quote": None, "color": "UNKNOWN"},
        "rvol": None,
        "volume_state": "UNKNOWN",
        "cumulative_delta_quote": None,
        "aggressor_flow": {"buy_quote": None, "sell_quote": None, "net_delta_quote": None, "buy_pct": None},
        "order_book": {"status": "UNKNOWN", "bid_depth_quote": None, "ask_depth_quote": None, "imbalance_pct": None},
        "large_trades": [],
        "large_trade_threshold_quote": None,
        "whale_flow": {"label": "نامشخص", "bias": "UNKNOWN", "score": 0, "evidence": []},
        "disclaimer": "Large trades are not wallet identities. Candle pressure is an estimate; aggressor flow is derived from public aggregate trades.",
    }
    if not isinstance(candles, pd.DataFrame) or len(candles) < 22:
        return empty

    frame = candles.tail(100).copy()
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return empty
    values = frame[["high", "low", "close", "volume"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values[:, 3] < 0).any():
        return empty

    quote_volume = frame["volume"].astype(float) * frame["close"].astype(float)
    previous_mean = float(quote_volume.iloc[-21:-1].mean())
    current_quote_volume = float(quote_volume.iloc[-1])
    rvol = current_quote_volume / previous_mean if previous_mean > 0 else None

    ranges = (frame["high"] - frame["low"]).astype(float).clip(lower=1e-15)
    buy_fraction = ((frame["close"] - frame["low"]) / ranges).clip(0.0, 1.0)
    estimated_buy = quote_volume * buy_fraction
    estimated_sell = quote_volume - estimated_buy
    buy_quote = float(estimated_buy.iloc[-1])
    sell_quote = float(estimated_sell.iloc[-1])
    buy_pct = 100.0 * buy_quote / current_quote_volume if current_quote_volume > 0 else 50.0
    sell_pct = 100.0 - buy_pct
    recent_estimated_delta = float((estimated_buy - estimated_sell).tail(20).sum())

    pressure_color = "GREEN" if buy_pct >= 55 else "RED" if buy_pct <= 45 else "NEUTRAL"
    volume_state = "EXTREME" if rvol is not None and rvol >= 2.5 else "HIGH" if rvol is not None and rvol >= 1.5 else "NORMAL"

    book = {"status": "UNKNOWN", "bid_depth_quote": None, "ask_depth_quote": None, "imbalance_pct": None}
    book_bias = 0
    if isinstance(order_book, dict):
        try:
            bids = order_book.get("bids") or []
            asks = order_book.get("asks") or []
            bid_depth = sum(_number(level[0]) * _number(level[1]) for level in bids[:20] if len(level) >= 2)
            ask_depth = sum(_number(level[0]) * _number(level[1]) for level in asks[:20] if len(level) >= 2)
            total_depth = bid_depth + ask_depth
            if bid_depth > 0 and ask_depth > 0 and total_depth > 0:
                imbalance = 100.0 * (bid_depth - ask_depth) / total_depth
                book = {
                    "status": "PASS",
                    "bid_depth_quote": round(bid_depth, 2),
                    "ask_depth_quote": round(ask_depth, 2),
                    "imbalance_pct": round(imbalance, 2),
                    "levels_per_side": min(20, len(bids), len(asks)),
                    "source": "Binance Spot depth snapshot",
                    "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                book_bias = 1 if imbalance >= 12 else -1 if imbalance <= -12 else 0
        except (TypeError, ValueError, IndexError):
            pass

    trades = aggregate_trades if isinstance(aggregate_trades, list) else []
    normalized_trades: list[dict[str, Any]] = []
    for row in trades:
        if not isinstance(row, dict):
            continue
        price = _number(row.get("p", row.get("price")))
        quantity = _number(row.get("q", row.get("quantity")))
        timestamp = row.get("T", row.get("timestamp"))
        if price <= 0 or quantity <= 0:
            continue
        quote = price * quantity
        # Binance m=True means buyer was maker, hence seller was aggressor.
        buyer_is_maker = row.get("m", row.get("isBuyerMaker"))
        if not isinstance(buyer_is_maker, bool):
            continue
        normalized_trades.append({
            "price": price,
            "quantity": quantity,
            "quote": quote,
            "side": "SELL" if buyer_is_maker else "BUY",
            "timestamp": int(timestamp) if _finite(timestamp) else None,
            "trade_id": str(row.get("a", row.get("id", "")))[:48],
        })

    aggressor = {"buy_quote": None, "sell_quote": None, "net_delta_quote": None, "buy_pct": None}
    large_prints: list[dict[str, Any]] = []
    large_buy = large_sell = 0.0
    whale_score = 0
    evidence: list[str] = []
    cvd = None
    if normalized_trades:
        buy_flow = sum(t["quote"] for t in normalized_trades if t["side"] == "BUY")
        sell_flow = sum(t["quote"] for t in normalized_trades if t["side"] == "SELL")
        total_flow = buy_flow + sell_flow
        cvd = buy_flow - sell_flow
        aggressor = {
            "buy_quote": round(buy_flow, 2),
            "sell_quote": round(sell_flow, 2),
            "net_delta_quote": round(cvd, 2),
            "buy_pct": round(100.0 * buy_flow / total_flow, 2) if total_flow else 50.0,
            "trade_count": len(normalized_trades),
            "source": "Binance Spot aggregate trades (aggressor side)",
        }
        notionals = np.asarray([t["quote"] for t in normalized_trades], dtype=float)
        median = float(np.median(notionals))
        percentile = float(np.quantile(notionals, 0.995)) if len(notionals) >= 20 else float(np.max(notionals))
        threshold = max(percentile, median * 20.0, 10_000.0)
        selected = [t for t in normalized_trades if t["quote"] >= threshold]
        large_buy = sum(t["quote"] for t in selected if t["side"] == "BUY")
        large_sell = sum(t["quote"] for t in selected if t["side"] == "SELL")
        large_prints = [
            {
                "side": t["side"],
                "price": round(t["price"], 12),
                "quote_value": round(t["quote"], 2),
                "timestamp_ms": t["timestamp"],
                "trade_id": t["trade_id"],
                "classification": "LARGE_PRINT_NOT_WALLET_ID",
            }
            for t in sorted(selected, key=lambda x: x["timestamp"] or 0, reverse=True)[:8]
        ]
        whale_score += 2 if large_buy > large_sell * 1.5 and large_buy > 0 else -2 if large_sell > large_buy * 1.5 and large_sell > 0 else 0
        if selected:
            evidence.append(f"{len(selected)} large prints above the adaptive {threshold:.0f} USDT threshold")
        evidence.append("Aggressor-side CVD is available from Binance aggregate trades")

    if buy_pct >= 58:
        whale_score += 1
        evidence.append("Latest completed candle closes in the upper part of its range")
    elif buy_pct <= 42:
        whale_score -= 1
        evidence.append("Latest completed candle closes in the lower part of its range")
    whale_score += book_bias
    if book_bias:
        evidence.append("Top-of-book depth imbalance supports the same directional pressure")

    if whale_score >= 2:
        bias, label = "BULLISH_FLOW", "فشار جریان خرید / انباشت احتمالی"
    elif whale_score <= -2:
        bias, label = "BEARISH_FLOW", "فشار جریان فروش / توزیع احتمالی"
    else:
        bias, label = "MIXED", "جریان مختلط یا بدون تأیید کافی"

    return {
        "status": "PASS" if len(normalized_trades) >= 20 else "PARTIAL",
        "source": "Binance Spot",
        "volume_unit": "USDT quote notional",
        "current_quote_volume": round(current_quote_volume, 2),
        "pressure": {
            "buy_pct": round(buy_pct, 2),
            "sell_pct": round(sell_pct, 2),
            "buy_quote": round(buy_quote, 2),
            "sell_quote": round(sell_quote, 2),
            "estimated_cvd_20_bars": round(recent_estimated_delta, 2),
            "color": pressure_color,
            "method": "Candle close-location estimate; not exchange aggressor delta",
        },
        "rvol": round(rvol, 3) if rvol is not None else None,
        "volume_state": volume_state,
        "cumulative_delta_quote": round(cvd, 2) if cvd is not None else None,
        "aggressor_flow": aggressor,
        "order_book": book,
        "large_trades": large_prints,
        "large_trade_threshold_quote": round(threshold, 2) if normalized_trades else None,
        "whale_flow": {
            "label": label,
            "bias": bias,
            "score": max(-4, min(4, whale_score)),
            "evidence": evidence,
            "classification": "INFERRED_FLOW_NOT_IDENTIFIED_WHALE",
        },
        "disclaimer": (
            "Public aggregate trades and a depth snapshot cannot identify beneficial owners or prove a whale wallet. "
            "Large prints are flagged by adaptive notional thresholds; spoofable order-book liquidity is only a snapshot. "
            "The flow label is an evidence-based scenario, not a guaranteed forecast."
        ),
    }
