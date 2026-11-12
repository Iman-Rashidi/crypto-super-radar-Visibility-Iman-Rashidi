"""Conservative multi-timeframe technical evidence for Gem Hunter DEX pools.

Only completed, sufficiently covered OHLCV candles are used. This module is
read-only and does not infer a trend from pool age or a single 1h percentage.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

TIMEFRAMES = {
    "15m": {"provider_timeframe": "minute", "aggregate": 15, "limit": 140,
            "interval": timedelta(minutes=15), "max_age": timedelta(minutes=45),
            "minimum_bars": 100},
    "4h": {"provider_timeframe": "hour", "aggregate": 4, "limit": 120,
           "interval": timedelta(hours=4), "max_age": timedelta(hours=12),
           "minimum_bars": 60},
    "1d": {"provider_timeframe": "day", "aggregate": 1, "limit": 90,
           "interval": timedelta(days=1), "max_age": timedelta(hours=60),
           "minimum_bars": 60},
}
CACHE_TTL_SECONDS = 300.0
MAX_CANDIDATES_PER_SCAN = 1
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.RLock()
_SCAN_OFFSET = 0


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    average_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    average_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    if average_gain.empty or pd.isna(average_gain.iloc[-1]) or pd.isna(average_loss.iloc[-1]):
        return None
    up = float(average_gain.iloc[-1])
    down = float(average_loss.iloc[-1])
    if down == 0:
        return 100.0 if up > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + up / down))


def _validated_close(frame: Any, *, interval: timedelta, max_age: timedelta,
                     minimum_bars: int, now: datetime) -> pd.Series:
    if not isinstance(frame, pd.DataFrame) or "close" not in frame.columns:
        raise ValueError("OHLCV frame or close column is missing")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("OHLCV timestamps must be timezone-aware")
    if frame.empty or frame.index.has_duplicates:
        raise ValueError("OHLCV frame is empty or has duplicate timestamps")
    ordered = frame.sort_index()
    if not ordered.index.is_monotonic_increasing:
        raise ValueError("OHLCV timestamps are not ordered")
    if ordered.index[-1].to_pydatetime() > now + timedelta(minutes=2):
        raise ValueError("OHLCV frame contains future timestamps")

    # GeckoTerminal may include the still-open current candle. Exclude it.
    closed = ordered.loc[
        (ordered.index + pd.Timedelta(interval)) <= pd.Timestamp(now)
    ]
    close = pd.to_numeric(closed["close"], errors="coerce")
    if close.isna().any() or (close <= 0).any() or not close.map(math.isfinite).all():
        raise ValueError("OHLCV close values are missing or invalid")
    if len(close) < minimum_bars:
        raise ValueError(f"Insufficient closed candles: {len(close)} < {minimum_bars}")
    if now - close.index[-1].to_pydatetime() > max_age:
        raise ValueError("Latest completed candle is stale")

    # A large event-time gap makes EMA/RSI interpretation unreliable.
    gaps = close.index.to_series().diff().dropna()
    if not gaps.empty and gaps.max() > interval * 3:
        raise ValueError("OHLCV history contains an excessive candle gap")
    return close.astype(float)


def _return_pct(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars:
        return None
    start = _finite(close.iloc[-bars - 1])
    end = _finite(close.iloc[-1])
    if start is None or end is None or start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _summary(close: pd.Series, *, interval_bars_per_day: int) -> dict[str, Any]:
    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
    last = float(close.iloc[-1])
    fast = float(ema20.iloc[-1])
    slow = float(ema50.iloc[-1])
    if last > fast > slow:
        direction = "BULLISH"
    elif last < fast < slow:
        direction = "BEARISH"
    else:
        direction = "MIXED"
    return {
        "bars": int(len(close)),
        "latest_candle_utc": close.index[-1].to_pydatetime().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "close": last,
        "ema20": fast,
        "ema50": slow,
        "rsi14": _rsi(close),
        "direction": direction,
        "return_24h_pct": _return_pct(close, 24 * interval_bars_per_day),
        "return_7d_pct": _return_pct(close, 7 * interval_bars_per_day),
        "return_30d_pct": _return_pct(close, 30 * interval_bars_per_day),
    }


def analyze_multi_timeframe_market(pool_frames: dict[str, Any],
                                   benchmark_frames: dict[str, Any],
                                   *, chain: str, token_address: str,
                                   pool_address: str,
                                   now: datetime | None = None) -> dict[str, Any]:
    """Pure analysis function; all inputs must be completed OHLCV frames."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)

    evidence: dict[str, Any] = {
        "schema_version": "gem-hunter-technical-v1",
        "status": "UNKNOWN",
        "coverage_status": "UNKNOWN",
        "chain": chain,
        "token_address": token_address,
        "pool_address": pool_address,
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "sources": ["GECKOTERMINAL", "BINANCE_SPOT"],
        "timeframes": {},
        "filters": {
            "trend": "UNKNOWN",
            "momentum": "UNKNOWN",
            "relative_strength": "UNKNOWN",
            "market_regime_validation": "UNKNOWN",
        },
        "filter_coverage": {
            "trend": False,
            "momentum": False,
            "relative_strength": False,
            "market_regime_validation": False,
        },
        "metrics": {},
        "reason_codes": [],
        "reason_fa": [],
    }
    pool_close: dict[str, pd.Series] = {}
    benchmark_close: dict[str, pd.Series] = {}
    errors: list[str] = []

    for name, spec in TIMEFRAMES.items():
        try:
            close = _validated_close(
                pool_frames.get(name),
                interval=spec["interval"],
                max_age=spec["max_age"],
                minimum_bars=spec["minimum_bars"],
                now=now,
            )
            pool_close[name] = close
            bars_per_day = int(timedelta(days=1) / spec["interval"])
            evidence["timeframes"][name] = _summary(
                close, interval_bars_per_day=bars_per_day,
            )
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")

    for symbol in ("BTCUSDT", "ETHUSDT"):
        try:
            close = _validated_close(
                benchmark_frames.get(symbol),
                interval=timedelta(days=1),
                max_age=timedelta(hours=36),
                minimum_bars=60,
                now=now,
            )
            benchmark_close[symbol] = close
            evidence["metrics"][symbol] = _summary(close, interval_bars_per_day=1)
        except Exception as exc:
            errors.append(f"{symbol}:{type(exc).__name__}")

    has_4h_and_daily = "4h" in pool_close and "1d" in pool_close
    has_15m_and_4h = "15m" in pool_close and "4h" in pool_close
    has_both_benchmarks = len(benchmark_close) == 2
    has_daily_token_and_benchmarks = "1d" in pool_close and has_both_benchmarks

    evidence["filter_coverage"] = {
        "trend": has_4h_and_daily,
        "momentum": has_15m_and_4h,
        "relative_strength": has_daily_token_and_benchmarks,
        "market_regime_validation": has_both_benchmarks,
    }

    if has_4h_and_daily:
        h4 = evidence["timeframes"]["4h"]
        d1 = evidence["timeframes"]["1d"]
        directions = (h4["direction"], d1["direction"])
        if directions == ("BULLISH", "BULLISH"):
            evidence["filters"]["trend"] = "PASS"
        elif directions == ("BEARISH", "BEARISH"):
            evidence["filters"]["trend"] = "FAIL"

    if has_15m_and_4h:
        m15 = evidence["timeframes"]["15m"]
        h4 = evidence["timeframes"]["4h"]
        short_positive = (
            m15["return_24h_pct"] is not None and m15["return_24h_pct"] > 0
            and m15["rsi14"] is not None and 50 < m15["rsi14"] < 80
            and h4["return_7d_pct"] is not None and h4["return_7d_pct"] > 0
            and h4["rsi14"] is not None and 50 < h4["rsi14"] < 80
        )
        short_negative = (
            m15["return_24h_pct"] is not None and m15["return_24h_pct"] < 0
            and m15["rsi14"] is not None and m15["rsi14"] < 45
            and h4["return_7d_pct"] is not None and h4["return_7d_pct"] < 0
            and h4["rsi14"] is not None and h4["rsi14"] < 45
        )
        if short_positive:
            evidence["filters"]["momentum"] = "PASS"
        elif short_negative:
            evidence["filters"]["momentum"] = "FAIL"

    if has_daily_token_and_benchmarks:
        d1 = evidence["timeframes"]["1d"]
        token_7d = d1["return_7d_pct"]
        token_30d = d1["return_30d_pct"]
        btc = evidence["metrics"]["BTCUSDT"]
        eth = evidence["metrics"]["ETHUSDT"]
        benchmarks_7d = (btc["return_7d_pct"], eth["return_7d_pct"])
        benchmarks_30d = (btc["return_30d_pct"], eth["return_30d_pct"])
        all_rs = (
            token_7d is not None and token_30d is not None
            and all(value is not None for value in (*benchmarks_7d, *benchmarks_30d))
        )
        if all_rs and all(token_7d > value for value in benchmarks_7d) and all(
            token_30d > value for value in benchmarks_30d
        ):
            evidence["filters"]["relative_strength"] = "PASS"
        elif all_rs and all(token_7d < value for value in benchmarks_7d) and all(
            token_30d < value for value in benchmarks_30d
        ):
            evidence["filters"]["relative_strength"] = "FAIL"
        evidence["metrics"].update({
            "token_return_7d_pct": token_7d,
            "token_return_30d_pct": token_30d,
            "relative_strength_7d_vs_btc_pct": (
                token_7d - btc["return_7d_pct"]
                if token_7d is not None and btc["return_7d_pct"] is not None else None
            ),
            "relative_strength_7d_vs_eth_pct": (
                token_7d - eth["return_7d_pct"]
                if token_7d is not None and eth["return_7d_pct"] is not None else None
            ),
        })

    if has_both_benchmarks:
        btc = evidence["metrics"]["BTCUSDT"]
        eth = evidence["metrics"]["ETHUSDT"]
        regime = (btc["direction"], eth["direction"])
        if regime == ("BULLISH", "BULLISH"):
            evidence["filters"]["market_regime_validation"] = "PASS"
        elif regime == ("BEARISH", "BEARISH"):
            evidence["filters"]["market_regime_validation"] = "FAIL"
        evidence["metrics"]["market_regime"] = (
            "BULLISH" if regime == ("BULLISH", "BULLISH")
            else "BEARISH" if regime == ("BEARISH", "BEARISH")
            else "MIXED"
        )

    if len(pool_close) == len(TIMEFRAMES) and has_both_benchmarks:
        evidence["coverage_status"] = "COMPLETE"
        evidence["status"] = "COMPLETE"
        evidence["reason_codes"].append("MULTI_TIMEFRAME_OHLCV_VALIDATED")
        evidence["reason_fa"].append("کندل‌های بسته‌شدهٔ ۱۵دقیقه، ۴ساعته و روزانه و دادهٔ روزانهٔ BTC/ETH از نظر تازگی و پوشش معتبرند.")
    else:
        evidence["coverage_status"] = "PARTIAL" if pool_close or benchmark_close else "UNKNOWN"
        evidence["status"] = evidence["coverage_status"]
        evidence["reason_codes"].append("MULTI_TIMEFRAME_COVERAGE_INCOMPLETE")
        evidence["reason_fa"].append("بعضی شاخص‌ها با شواهد موجود محاسبه شدند؛ تاریخچهٔ کامل تمام بازه‌ها و دو benchmark هنوز موجود نیست.")
    evidence["source_errors"] = errors
    evidence["coverage"] = {
        "pool_timeframes_present": sorted(pool_close),
        "pool_timeframes_required": sorted(TIMEFRAMES),
        "benchmarks_present": sorted(benchmark_close),
        "benchmarks_required": ["BTCUSDT", "ETHUSDT"],
    }
    return evidence


def enrich_discovery_candidates_with_technicals(candidates: Any, data_service,
                                                  *, limit: int = MAX_CANDIDATES_PER_SCAN) -> list[dict]:
    """Fetch a bounded technical evidence sample for exact-pool reconciled candidates."""
    if not isinstance(candidates, list):
        return []
    rows = [dict(item) for item in candidates if isinstance(item, dict)]
    eligible = [
        index for index, row in enumerate(rows)
        if (row.get("market_reconciliation") or {}).get("status") == "PASS"
        and isinstance(row.get("token_address"), str)
        and isinstance(row.get("pool_address"), str)
        and row.get("chain") in {"base", "solana"}
    ]
    if not eligible:
        return rows

    bounded = max(0, min(int(limit), MAX_CANDIDATES_PER_SCAN, len(eligible)))
    global _SCAN_OFFSET
    with _CACHE_LOCK:
        start = _SCAN_OFFSET % len(eligible) if bounded else 0
        if bounded:
            _SCAN_OFFSET = (start + bounded) % len(eligible)
    selected = {eligible[(start + offset) % len(eligible)] for offset in range(bounded)}

    for index, row in enumerate(rows):
        if index not in selected:
            row["technical_evidence"] = {
                "status": "NOT_CHECKED_BUDGET",
                "coverage_status": "NOT_CHECKED_BUDGET",
                "filters": {
                    "trend": "UNKNOWN", "momentum": "UNKNOWN",
                    "relative_strength": "UNKNOWN",
                    "market_regime_validation": "UNKNOWN",
                },
                "reason_codes": ["TECHNICAL_SCAN_BUDGET_EXHAUSTED"],
            }
            continue

        chain = row["chain"]
        token = row["token_address"]
        pool = row["pool_address"]
        key = f"{chain}:{token if chain == 'solana' else token.lower()}:{pool if chain == 'solana' else pool.lower()}"
        now_mono = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and cached[0] > now_mono:
                row["technical_evidence"] = dict(cached[1])
                continue

        try:
            gecko = getattr(data_service, "gecko", None)
            binance = getattr(data_service, "binance", None)
            if gecko is None or binance is None:
                raise RuntimeError("required public market providers are unavailable")
            pool_frames = {}
            for name, spec in TIMEFRAMES.items():
                pool_frames[name] = gecko.candles(
                    chain, pool, token_address=token,
                    timeframe=spec["provider_timeframe"],
                    aggregate=spec["aggregate"],
                    limit=spec["limit"],
                )
            benchmark_frames = {
                symbol: binance.candles(symbol, interval="1d", limit=90)
                for symbol in ("BTCUSDT", "ETHUSDT")
            }
            evidence = analyze_multi_timeframe_market(
                pool_frames, benchmark_frames,
                chain=chain, token_address=token, pool_address=pool,
            )
        except Exception as exc:
            evidence = {
                "schema_version": "gem-hunter-technical-v1",
                "status": "UNKNOWN",
                "coverage_status": "UNKNOWN",
                "chain": chain, "token_address": token, "pool_address": pool,
                "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "sources": ["GECKOTERMINAL", "BINANCE_SPOT"],
                "filters": {
                    "trend": "UNKNOWN", "momentum": "UNKNOWN",
                    "relative_strength": "UNKNOWN",
                    "market_regime_validation": "UNKNOWN",
                },
                "reason_codes": ["TECHNICAL_PROVIDER_UNAVAILABLE"],
                "source_errors": [type(exc).__name__],
            }
        row["technical_evidence"] = evidence
        if evidence.get("coverage_status") == "COMPLETE":
            with _CACHE_LOCK:
                _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, dict(evidence))
    return rows
