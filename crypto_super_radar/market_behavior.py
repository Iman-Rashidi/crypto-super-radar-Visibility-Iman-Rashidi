"""Market-structure, Fibonacci confluence, and chronological analogue risk checks."""
from __future__ import annotations

import math
from typing import Any
import numpy as np
import pandas as pd

_OHLC = {"open", "high", "low", "close"}


def _wilson(wins: int, count: int) -> tuple[float, float]:
    if count <= 0:
        return 0.0, 1.0
    z = 1.959963984540054
    p = wins / count
    den = 1 + z * z / count
    center = (p + z * z / (2 * count)) / den
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * count)) / count) / den
    return max(0.0, center - margin), min(1.0, center + margin)


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "UNKNOWN", "reason": reason, "estimated_error_pct": None,
            "error_rate_upper95_pct": None, "analogue_count": 0,
            "structure": {"status": "UNKNOWN"}, "fibonacci": {"status": "UNKNOWN"},
            "levels": {"status": "UNKNOWN"}}


def analyze_market_behavior(
    candles: pd.DataFrame, current_price: Any, *, side: str = "LONG",
    horizon_days: Any = 5, fee_bps_per_side: float = 8.0,
    slippage_bps_per_side: float = 15.0, minimum_analogs: int = 12,
    max_analogs: int = 30, pattern_window: int = 20,
) -> dict[str, Any]:
    """Estimate risk before entry from confirmed pivots, Fib levels, and past analogues.

    Analogue error rates are historical estimates, not market probabilities or guarantees.
    """
    side = str(side).upper()
    if side not in {"LONG", "SHORT"}:
        return _unknown("Directional signal is unavailable.")
    try:
        price, horizon = float(current_price), int(horizon_days)
        cost = 2 * (float(fee_bps_per_side) + float(slippage_bps_per_side)) / 10000
    except (TypeError, ValueError, OverflowError):
        return _unknown("Price, horizon, or costs are invalid.")
    if not math.isfinite(price) or price <= 0 or not 1 <= horizon <= 14:
        return _unknown("A positive quote and a 1–14 day horizon are required.")
    if not math.isfinite(cost) or cost < 0 or minimum_analogs < 5:
        return _unknown("Invalid costs or analogue sample configuration.")
    if candles is None or not isinstance(candles, pd.DataFrame) or not _OHLC.issubset(candles.columns):
        return _unknown("Validated OHLC history is unavailable.")
    if not isinstance(candles.index, pd.DatetimeIndex) or candles.index.has_duplicates:
        return _unknown("Unique timestamped OHLC candles are required.")
    f = candles.loc[:, ["open", "high", "low", "close"]].copy().sort_index()
    for col in _OHLC:
        f[col] = pd.to_numeric(f[col], errors="coerce")
    a = f[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(a).all() or (a <= 0).any():
        return _unknown("OHLC history has missing or invalid values.")
    if ((f.high < f.low) | (f.high < f.open) | (f.high < f.close)
            | (f.low > f.open) | (f.low > f.close)).any():
        return _unknown("Malformed OHLC relationships were detected.")
    if f.index.tz is None:
        f.index = f.index.tz_localize("UTC")
    else:
        f.index = f.index.tz_convert("UTC")
    now = pd.Timestamp.now(tz="UTC")
    if f.index[-1] > now + pd.Timedelta(minutes=5):
        return _unknown("Future-dated candles are not accepted.")
    cadence = f.index.to_series().diff().dropna().dt.total_seconds().median()
    if not math.isfinite(float(cadence)) or not 20*3600 <= float(cadence) <= 28*3600:
        return _unknown("Historical analogue analysis requires validated daily candles.")
    close, high, low = (f[c].to_numpy(dtype=float) for c in ("close", "high", "low"))
    n = len(f)
    if n < max(250, pattern_window + horizon + 80):
        return _unknown("At least 250 daily candles are required.")
    if abs(close[-1] - price) / price > 0.05:
        return _unknown("Current quote differs by over 5% from the latest validated close.")

    # Pivots are confirmed only after three bars have closed on the right.
    rad, highs, lows = 3, [], []
    for i in range(rad, n-rad):
        if high[i] >= np.max(high[i-rad:i+rad+1]) and high[i] > np.max(high[i-rad:i]):
            highs.append({"index": i, "price": float(high[i])})
        if low[i] <= np.min(low[i-rad:i+rad+1]) and low[i] < np.min(low[i-rad:i]):
            lows.append({"index": i, "price": float(low[i])})
    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = highs[-1]["price"] > highs[-2]["price"], lows[-1]["price"] > lows[-2]["price"]
        lh, ll = highs[-1]["price"] < highs[-2]["price"], lows[-1]["price"] < lows[-2]["price"]
        trend = "BULLISH" if hh and hl else "BEARISH" if lh and ll else "RANGE_OR_MIXED"
        structure = {"status": "KNOWN", "trend": trend, "higher_high": hh, "higher_low": hl,
                     "lower_high": lh, "lower_low": ll, "confirmed_highs": len(highs),
                     "confirmed_lows": len(lows), "last_high": highs[-1], "last_low": lows[-1]}
    else:
        structure = {"status": "UNKNOWN", "trend": "UNKNOWN",
                     "confirmed_highs": len(highs), "confirmed_lows": len(lows)}

    prev = np.r_[close[0], close[:-1]]
    atr = float(np.median(np.maximum(high-low, np.maximum(abs(high-prev), abs(low-prev)))[-14:]))
    if not math.isfinite(atr) or atr <= 0:
        return _unknown("Volatility scale could not be calculated.")

    # Most recent confirmed low followed by a confirmed high defines the impulse.
    fib = {"status": "UNKNOWN", "levels": {}, "nearest_level": None,
           "distance_atr": None, "confluence": False}
    if side == "LONG":
        impulse_high = next((p for p in reversed(highs)
                             if any(q["index"] < p["index"] for q in lows)), None)
        impulse_low = next((q for q in reversed(lows)
                            if impulse_high and q["index"] < impulse_high["index"]), None)
    else:
        impulse_low = next((q for q in reversed(lows)
                            if any(p["index"] < q["index"] for p in highs)), None)
        impulse_high = next((p for p in reversed(highs)
                             if impulse_low and p["index"] < impulse_low["index"]), None)
    if impulse_high and impulse_low and impulse_high["price"] > impulse_low["price"]:
        span = impulse_high["price"] - impulse_low["price"]
        if side == "LONG":
            lvls = {f"{r:.3f}": impulse_high["price"] - span*r for r in (.382, .5, .618, .786)}
            extensions = {f"{r:.3f}": float(impulse_low["price"]+span*r) for r in (1.0, 1.272, 1.618)}
        else:
            lvls = {f"{r:.3f}": impulse_low["price"] + span*r for r in (.382, .5, .618, .786)}
            extensions = {f"{r:.3f}": float(impulse_high["price"]-span*r) for r in (1.0, 1.272, 1.618)}
        nearest, fibprice = min(lvls.items(), key=lambda x: abs(price-x[1]))
        d = abs(price-fibprice)/atr
        fib = {"status": "KNOWN", "levels": {k: float(v) for k,v in lvls.items()},
               "impulse_low": impulse_low, "impulse_high": impulse_high,
               "nearest_level": nearest, "nearest_price": float(fibprice),
               "distance_atr": round(float(d), 3), "confluence": d <= .5,
               "extensions": extensions}

    support = [p["price"] for p in lows if p["index"] >= n-60 and p["price"] < price]
    resistance = [p["price"] for p in highs if p["index"] >= n-60 and p["price"] > price]
    if side == "LONG":
        s = max(support) if support else float(np.min(low[-20:]))
        stop = s - .25*atr
        targets = list(resistance)
        if fib["status"] == "KNOWN":
            targets += [v for v in fib["extensions"].values() if v > price]
        target = min(targets) if targets else None
        risk, reward = price-stop, target-price if target is not None else None
    else:
        r = min(resistance) if resistance else float(np.max(high[-20:]))
        stop = r + .25*atr
        targets = [float(np.min(low[-60:]))]
        target = max((v for v in targets if v < price), default=None)
        risk, reward = stop-price, price-target if target is not None else None
    rr = max(0, reward-price*cost)/(risk+price*cost) if reward is not None and risk > 0 else None
    levels = {"status": "KNOWN" if target is not None and stop > 0 else "UNKNOWN",
              "support": float(s) if side == "LONG" else None,
              "resistance": float(r) if side == "SHORT" else (float(min(resistance)) if resistance else None),
              "stop_loss": float(stop), "take_profit": float(target) if target is not None else None,
              "atr14_robust": atr,
              "net_reward_risk": round(float(rr), 4) if rr is not None else None,
              "risk_distance_pct": round(abs(price-stop)/price*100, 4) if stop > 0 else None}

    # Nearest shape analogues; every analog outcome is fully observed before the
    # current 20-bar shape starts. Greedy temporal spacing reduces pseudo-sample size.
    ret = np.diff(np.log(close))
    start = len(ret)-pattern_window
    analogs = []
    if start > pattern_window+horizon:
        cur = ret[start:]
        curstd = float(np.std(cur, ddof=1))
        if curstd > 1e-12:
            curz = (cur-cur.mean())/curstd
            candidates = []
            for end in range(pattern_window, start-horizon+1):
                hist = ret[end-pattern_window:end]
                sd = float(np.std(hist, ddof=1))
                if sd <= 1e-12:
                    continue
                dist = float(np.sqrt(np.mean((curz-(hist-hist.mean())/sd)**2)))
                forward = float(close[end+horizon]/close[end]-1)
                adverse = float((np.min(low[end+1:end+horizon+1]) if side=="LONG" else np.max(high[end+1:end+horizon+1]))/close[end]-1)
                candidates.append((dist, end, forward, adverse))
            chosen = []
            spacing = pattern_window+horizon
            for row in sorted(candidates, key=lambda x:x[0]):
                if all(abs(row[1]-old[1]) >= spacing for old in chosen):
                    chosen.append(row)
                    if len(chosen) == max_analogs:
                        break
            analogs = chosen
    if len(analogs) < minimum_analogs:
        hist = {"status": "UNKNOWN", "reason": "Too few independent historical analogues.",
                "analogue_count": len(analogs), "required_analogs": minimum_analogs,
                "estimated_error_pct": None, "error_rate_upper95_pct": None,
                "win_rate_pct": None, "win_rate_ci95_pct": None, "expectancy_net_pct": None,
                "worst_analogue_adverse_pct": None, "pattern_similarity_score": None}
    else:
        net = [((1+row[2])*(1-cost)-1)*(1 if side=="LONG" else -1) for row in analogs]
        wins = sum(x > 0 for x in net)
        lower, upper = _wilson(wins, len(net))
        estimate, upper_error = 1-wins/len(net), 1-lower
        expectancy = float(np.mean(net))
        adverse = [row[3] if side=="LONG" else -row[3] for row in analogs]
        distance = float(np.mean([row[0] for row in analogs]))
        ok = expectancy > 0 and upper_error < .75
        hist = {"status": "PASS" if ok else "FAIL",
                "reason": "Positive cost-adjusted analogue expectancy and sufficient confidence."
                          if ok else "Analogue expectancy is non-positive or uncertainty is excessive.",
                "analogue_count": len(net), "required_analogs": minimum_analogs,
                "win_rate_pct": round(wins/len(net)*100, 2),
                "win_rate_ci95_pct": [round(lower*100,2), round(upper*100,2)],
                "estimated_error_pct": round(estimate*100,2),
                "error_rate_upper95_pct": round(upper_error*100,2),
                "expectancy_net_pct": round(expectancy*100,4),
                "worst_analogue_adverse_pct": round(float(np.min(adverse))*100,4),
                "pattern_similarity_score": round(100/(1+distance),2),
                "method": "normalized 20-candle log-return shape; independent chronological analogues; Wilson 95% interval",
                "cost_pct_round_trip": round(cost*100,4)}

    aligned = structure.get("trend") == ("BULLISH" if side=="LONG" else "BEARISH")
    fib_ok = bool(fib["confluence"])
    level_ok = levels["status"]=="KNOWN" and rr is not None and rr >= 1.8
    status = "PASS" if hist["status"]=="PASS" and aligned and fib_ok and level_ok else (
        "UNKNOWN" if hist["status"]=="UNKNOWN" or structure["status"]=="UNKNOWN"
        or fib["status"]=="UNKNOWN" or levels["status"]=="UNKNOWN" else "FAIL")
    reasons = []
    if not aligned: reasons.append("confirmed swing trend is not aligned")
    if not fib_ok: reasons.append("price lacks Fibonacci retracement confluence")
    if not level_ok: reasons.append("structural stop/target net reward-risk is below 1.8")
    if hist["status"] != "PASS": reasons.append(hist["reason"])
    return {"status": status,
            "reason": "Structure, Fibonacci, and historical analogue checks pass."
                      if status=="PASS" else "; ".join(reasons) or "Market history is unknown.",
            "side": side, "horizon_days": horizon, "structure": structure,
            "fibonacci": fib, "levels": levels, "historical_analogues": hist,
            "estimated_error_pct": hist.get("estimated_error_pct"),
            "error_rate_upper95_pct": hist.get("error_rate_upper95_pct"),
            "analogue_count": hist.get("analogue_count",0),
            "risk_before_entry": {"risk_distance_pct": levels.get("risk_distance_pct"),
                "net_reward_risk": levels.get("net_reward_risk"),
                "historical_error_estimate_pct": hist.get("estimated_error_pct"),
                "historical_error_upper95_pct": hist.get("error_rate_upper95_pct"),
                "max_analogue_adverse_pct": hist.get("worst_analogue_adverse_pct")}}
