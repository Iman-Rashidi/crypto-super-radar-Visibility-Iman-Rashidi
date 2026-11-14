"""Independent opportunity-oriented Easy engine.

The Easy profile keeps only essential data/price/stop validity as blockers.
Whale, full sentiment, walk-forward, market-regime and other research outputs
are diagnostics; Standard/Strict remain on their original engines.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

from .config import AppConfig
from .easy_risk_management import (
    calculate_position_size, classify_market_cycle, calculate_fractional_kelly,
    historical_var_expected_shortfall, stress_test_positions, portfolio_risk_summary,
    assess_leverage_margin, STRATEGY_STYLE_GUIDE,
)
from .easy_trade_management import evaluate_breakout_retest
from .models import AnalysisResult, AssetSpec, SafetyStatus, Signal
from .technical_indicators import analyze_technical_indicators
from .price_action import analyze_easy_price_action
from .chart_patterns import monitor_chart_patterns
from .elliott_waves import monitor_elliott_waves
from .market_intelligence import fetch_market_intelligence
from .market_sentiment import fetch_market_sentiment
from .fundamental_engine import build_fundamental_report
from .indicators import latest_candle_age_hours


def _num(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None



def _trend_structure(frame: pd.DataFrame, *, radius: int = 2) -> dict[str, Any]:
    """Non-repainting HH/HL or LH/LL structure using pivots confirmed after right bars."""
    required = {"high", "low", "close"}
    if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns) or len(frame) < 2 * radius + 8:
        return {"status": "UNKNOWN", "trend": "UNKNOWN", "reason": "Insufficient timeframe OHLC history."}
    high = pd.to_numeric(frame.high, errors="coerce").to_numpy(float)
    low = pd.to_numeric(frame.low, errors="coerce").to_numpy(float)
    close = pd.to_numeric(frame.close, errors="coerce").to_numpy(float)
    if not np.isfinite(high).all() or not np.isfinite(low).all() or not np.isfinite(close).all():
        return {"status": "UNKNOWN", "trend": "UNKNOWN", "reason": "Non-finite timeframe data."}
    highs, lows = [], []
    for i in range(radius, len(frame) - radius):
        # A pivot only becomes visible after the full right-hand radius has closed.
        if high[i] >= np.max(high[i-radius:i+radius+1]) and high[i] > np.max(high[i-radius:i]):
            highs.append({"index": i, "price": float(high[i])})
        if low[i] <= np.min(low[i-radius:i+radius+1]) and low[i] < np.min(low[i-radius:i]):
            lows.append({"index": i, "price": float(low[i])})
    if len(highs) < 2 or len(lows) < 2:
        return {"status": "UNKNOWN", "trend": "UNKNOWN",
                "confirmed_highs": len(highs), "confirmed_lows": len(lows),
                "reason": "Two confirmed swing highs and lows are not yet available."}
    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]
    trend = "UPTREND" if hh and hl else "DOWNTREND" if lh and ll else "SIDEWAYS_OR_TRANSITION"
    return {
        "status": "KNOWN", "trend": trend, "higher_high": hh, "higher_low": hl,
        "lower_high": lh, "lower_low": ll,
        "last_high": highs[-1], "prior_high": highs[-2],
        "last_low": lows[-1], "prior_low": lows[-2],
        "confirmed_highs": len(highs), "confirmed_lows": len(lows),
        "pivot_radius": radius, "confirmation_delay_bars": radius,
        "lookahead_bias": False,
    }


def _multi_timeframe_structure(
    daily: pd.DataFrame, supplied: Any = None,
) -> dict[str, Any]:
    """Use separate supplied OHLCV frames when present; derive only completed higher frames."""
    required = {"open", "high", "low", "close", "volume"}
    reports: dict[str, Any] = {}
    supplied = supplied if isinstance(supplied, dict) else {}
    aliases = {"1m":"1m","5m":"5m","15m":"15m","30m":"30m","1h":"1h","4h":"4h","1D":"1D","1W":"1W","1M":"1M"}
    for tf in ("1m","5m","15m","30m","1h","4h"):
        frame = supplied.get(tf)
        if isinstance(frame, pd.DataFrame) and required.issubset(frame.columns):
            reports[tf] = _trend_structure(frame)
            reports[tf]["source"] = "INDEPENDENT_OHLCV"
        else:
            reports[tf] = {"status":"UNKNOWN","trend":"UNKNOWN",
                           "reason":"No independent OHLCV series is configured for this timeframe."}
    if not isinstance(daily.index, pd.DatetimeIndex) or not required.issubset(daily.columns):
        reports["1D"] = {"status":"UNKNOWN","trend":"UNKNOWN","reason":"Daily OHLCV unavailable."}
        reports["1W"] = {"status":"UNKNOWN","trend":"UNKNOWN","reason":"Daily OHLCV unavailable."}
        reports["1M"] = {"status":"UNKNOWN","trend":"UNKNOWN","reason":"Daily OHLCV unavailable."}
        return {"status":"PARTIAL","timeframes":reports,"conflict":"UNKNOWN"}
    reports["1D"] = _trend_structure(daily)
    reports["1D"]["source"] = "VALIDATED_DAILY_OHLCV"
    frame = daily.copy()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    for tf, rule in (("1W","W-SUN"),("1M","ME")):
        try:
            agg = frame.resample(rule, label="right", closed="right").agg({
                "open":"first","high":"max","low":"min","close":"last","volume":"sum"
            }).dropna()
            # Keep only fully elapsed calendar periods, never the currently-forming week/month.
            now = pd.Timestamp.now(tz="UTC")
            completed = agg.index < now.normalize() if tf == "1W" else agg.index <= now.normalize() - pd.offsets.MonthEnd(1)
            agg = agg.loc[completed]
            reports[tf] = _trend_structure(agg)
            reports[tf]["source"] = "AGGREGATED_COMPLETED_DAILY_BARS"
        except Exception as exc:
            reports[tf] = {"status":"UNKNOWN","trend":"UNKNOWN","reason":type(exc).__name__}
    known = [v["trend"] for v in reports.values() if v.get("status") == "KNOWN"
             and v.get("trend") in {"UPTREND","DOWNTREND"}]
    if len(set(known)) > 1:
        conflict = "CONFLICT"
    elif known and len(known) == len(reports):
        conflict = "ALIGNED"
    elif known:
        conflict = "PARTIAL_ALIGNMENT"
    else:
        conflict = "UNKNOWN"
    return {
        "status": "KNOWN" if reports["1D"].get("status") == "KNOWN" else "PARTIAL",
        "timeframes": reports, "directional_timeframe_count": len(known),
        "conflict": conflict, "used_as_hard_gate": False,
        "policy": "Higher timeframe conflict is a diagnostic only; Easy entry is not blocked by it.",
    }


def _failure(asset: AssetSpec, reason: str, metrics: Optional[dict[str, Any]] = None) -> AnalysisResult:
    return AnalysisResult(
        asset=asset.raw, signal=Signal.NO_TRADE, safety_status=SafetyStatus.UNKNOWN,
        confirmation_score=None, estimated_risk_score=None, opportunity_rank=None,
        entry_price=None, take_profit=None, stop_loss=None, expected_holding_days=None,
        position_notional_usd=None, risk_budget_usd=None, gates=[],
        summary_en="Easy analysis unavailable: essential validated market evidence is missing.",
        summary_fa=f"تحلیل Easy در دسترس نیست: {reason}",
        metrics={"engine_profile": "EASY", "decision_policy": {
            "mandatory_legacy_gates": 0, "twelve_gate_engine_used": False,
            "nonessential_diagnostics_are_hard_gates": False,
            "essential_inputs": ["validated_snapshot_price", "closed_valid_ohlcv", "atr_for_stop"],
        }, **(metrics or {})},
    )


class EasyModeEngine:
    """Standalone Easy engine for the dashboard's opportunity scan."""

    def __init__(self, config: Optional[AppConfig] = None, data_service: Any = None,
                 sentiment_fetcher=None):
        if data_service is None:
            raise ValueError("EasyModeEngine requires an explicitly shared market data service.")
        self.config = config or AppConfig()
        self.data = data_service
        self.sentiment_fetcher = sentiment_fetcher or fetch_market_sentiment
        self.sentiment_cache: dict[str, Any] = {}

    def analyze(
        self, asset_input: str | AssetSpec, headlines: Optional[Iterable[str]] = None,
        open_portfolio_risk_pct: float = 0.0, social_posts: Optional[Iterable[str]] = None,
    ) -> AnalysisResult:
        asset = asset_input if isinstance(asset_input, AssetSpec) else AssetSpec.parse(asset_input)
        try:
            snapshot = self.data.snapshot(asset)
            candles = self.data.candles(asset, snapshot)
        except Exception as exc:
            return _failure(asset, f"قیمت یا کندل معتبر دریافت نشد ({type(exc).__name__}).",
                            {"data_error": type(exc).__name__})
        if candles is None or not isinstance(candles, pd.DataFrame) or candles.empty:
            return _failure(asset, "تاریخچه OHLCV معتبر موجود نیست.")
        price = _num(getattr(snapshot, "price_usd", None))
        if price is None or price <= 0:
            return _failure(asset, "قیمت معتبر و مثبت بازار موجود نیست.")
        if not isinstance(candles.index, pd.DatetimeIndex) or candles.index.has_duplicates or not candles.index.is_monotonic_increasing:
            return _failure(asset, "Timestamp کندل‌ها باید یکتا و مرتب باشند.")
        age = latest_candle_age_hours(candles)
        max_age = max(float(self.config.thresholds.max_candle_age_hours), 36.0)
        if age is None or age < -1.0 or age > max_age:
            return _failure(asset, "کندل روزانه برای تحلیل قابل‌اتکا بیش از حد قدیمی یا زمان آن نامشخص است.",
                            {"candle_age_hours": age, "max_candle_age_hours": max_age})
        tech = analyze_technical_indicators(candles)
        if tech.get("status") != "KNOWN":
            return _failure(asset, "داده کافی یا معتبر برای اندیکاتورها وجود ندارد.",
                            {"technical_indicators": tech, "candle_age_hours": age})
        close = pd.to_numeric(candles["close"], errors="coerce")
        if close.isna().any() or (close <= 0).any() or len(close) < 50:
            return _failure(asset, "حداقل ۵۰ کندل بسته‌شده و معتبر برای ساختار کوتاه‌مدت لازم است.")
        entry = price
        ma = tech.get("moving_averages", {})
        ema20, ema50 = _num(ma.get("ema20")), _num(ma.get("ema50"))
        rsi = _num((tech.get("rsi") or {}).get("value"))
        macd_hist = _num((tech.get("macd") or {}).get("histogram"))
        obv_trend = str((tech.get("obv") or {}).get("trend", "UNKNOWN")).upper()
        volume_series = pd.to_numeric(candles["volume"], errors="coerce")
        prior_volume_mean = float(volume_series.iloc[-21:-1].mean()) if len(volume_series) >= 21 else None
        relative_volume = (
            float(volume_series.iloc[-1]) / prior_volume_mean
            if prior_volume_mean is not None and math.isfinite(prior_volume_mean) and prior_volume_mean > 0
            else None
        )
        atr_value = _num((tech.get("atr") or {}).get("value"))
        if ema20 is None or ema50 is None or rsi is None or atr_value is None or atr_value <= 0:
            return _failure(asset, "EMA20/EMA50، RSI و ATR برای تحلیل پایه باید قابل محاسبه باشند.",
                            {"technical_indicators": tech, "candle_age_hours": age})
        trend = "BULLISH" if entry > ema20 > ema50 else "BEARISH" if entry < ema20 < ema50 else "RANGE"
        bull, bear = 0.0, 0.0
        if trend == "BULLISH": bull += 35
        elif trend == "BEARISH": bear += 35
        if rsi > 50: bull += 15
        elif rsi < 50: bear += 15
        if macd_hist is not None:
            if macd_hist > 0: bull += 15
            elif macd_hist < 0: bear += 15
        if obv_trend in {"BULLISH", "RISING"}: bull += 10
        elif obv_trend in {"BEARISH", "FALLING"}: bear += 10
        if relative_volume is not None and relative_volume >= 1.0:
            if trend == "BULLISH": bull += 5
            elif trend == "BEARISH": bear += 5
        direction = "NONE"
        if bull >= 55 and bull - bear >= 15:
            direction = "LONG"
        elif asset.market_type == "perp" and bear >= 55 and bear - bull >= 15:
            direction = "SHORT"

        price_action = analyze_easy_price_action(candles, last_candle_closed=True)
        pattern = monitor_chart_patterns(candles.tail(120))
        cycle = classify_market_cycle(candles)
        mtf = _multi_timeframe_structure(
            candles, getattr(self.data, "timeframe_candles", {}).get(asset.raw)
            if isinstance(getattr(self.data, "timeframe_candles", {}), dict) else None,
        )
        candidate_side = "LONG" if trend == "BULLISH" else "SHORT" if trend == "BEARISH" and asset.market_type == "perp" else None
        breakout = {"status": "UNKNOWN", "state": "UNKNOWN", "reason": "No directional support/resistance test was selected."}
        sr = (price_action.get("support_resistance") or {}) if isinstance(price_action, dict) else {}
        if candidate_side == "LONG":
            zones = sr.get("resistance_zones") or []
            if zones:
                breakout = evaluate_breakout_retest(candles, zones[0].get("center"), "LONG", last_candle_closed=True)
        elif candidate_side == "SHORT":
            zones = sr.get("support_zones") or []
            if zones:
                breakout = evaluate_breakout_retest(candles, zones[0].get("center"), "SHORT", last_candle_closed=True)

        side_score = bull if direction == "LONG" else bear if direction == "SHORT" else max(bull, bear)
        score = round(max(0.0, min(100.0, side_score)), 2)
        atr_pct = atr_value / entry * 100.0
        discrepancy = _num(getattr(snapshot, "price_discrepancy_pct", None))
        liquidity = _num(getattr(snapshot, "liquidity_usd", None))
        risk_score = min(100.0, max(0.0, atr_pct * 8.0
                         + (min(20.0, discrepancy * 1.5) if discrepancy is not None else 10.0)
                         + (15.0 if liquidity is not None and liquidity < self.config.thresholds.min_dex_liquidity_usd else 0.0)))
        stop_distance = atr_value * self.config.risk.atr_stop_multiple
        if direction == "LONG":
            stop = entry - stop_distance
            target = entry + stop_distance * self.config.risk.atr_target_multiple / max(self.config.risk.atr_stop_multiple, 1e-9)
            side_signal = Signal.LONG
        elif direction == "SHORT":
            stop = entry + stop_distance
            target = entry - stop_distance * self.config.risk.atr_target_multiple / max(self.config.risk.atr_stop_multiple, 1e-9)
            side_signal = Signal.SHORT
        else:
            stop = target = None
            side_signal = Signal.NO_TRADE
        if direction != "NONE" and (stop is None or stop <= 0 or target is None or target <= 0):
            return _failure(asset, "محاسبه حد ضرر و حد سود معتبر ممکن نشد.",
                            {"technical_indicators": tech, "atr14": atr_value})
        sizing = calculate_position_size(
            equity=self.config.risk.paper_equity_usd, entry=entry, stop=stop,
            risk_pct=self.config.risk.risk_per_trade_pct,
            atr_value=atr_value, atr_multiple=self.config.risk.atr_stop_multiple,
            max_notional_pct=self.config.risk.max_position_notional_pct,
            fee_bps_per_side=self.config.risk.fee_bps_per_side,
            slippage_bps_per_side=self.config.risk.slippage_bps_per_side,
        ) if direction != "NONE" else {"status": "UNKNOWN", "reason": "No directional setup."}
        if direction != "NONE" and sizing.get("status") != "KNOWN":
            return _failure(asset, "اندازه موقعیت و بودجه ریسک معتبر محاسبه نشد.",
                            {"easy_position_sizing": sizing, "technical_indicators": tech})

        supplied = list(headlines or [])
        social = list(social_posts or [])
        try:
            sentiment = self.sentiment_fetcher(
                ["https://www.coindesk.com/arc/outboundfeeds/rss/"],
                social_feed_urls=[], social_text=social + supplied,
                cache=self.sentiment_cache,
                limiter=getattr(getattr(self.data, "http", None), "limiter", None),
            )
        except Exception as exc:
            sentiment = {"status": "UNKNOWN", "reason": f"{type(exc).__name__}"}
        try:
            security = self.data.security(asset)
        except Exception as exc:
            security = {"status": "UNKNOWN", "reason": type(exc).__name__}
        whale = getattr(self.data, "whale_evidence", {}).get(asset.raw, {"status": "UNKNOWN"})
        try:
            fundamental_reader = getattr(self.data, "fundamental_evidence", None)
            evidence = fundamental_reader(asset) if callable(fundamental_reader) else {}
            fundamental = build_fundamental_report(evidence if isinstance(evidence, dict) else {})
        except Exception as exc:
            fundamental = {"status": "UNKNOWN", "score": None, "reason": type(exc).__name__}
        try:
            market_intelligence = fetch_market_intelligence(
                asset, direction, snapshot=snapshot,
                http=getattr(self.data, "http", None),
                min_dex_liquidity_usd=self.config.thresholds.min_dex_liquidity_usd,
            )
        except Exception as exc:
            market_intelligence = {"status": "UNKNOWN", "reason": type(exc).__name__}

        close_returns = close.pct_change().dropna().tail(250).to_numpy(dtype=float)
        var_es = historical_var_expected_shortfall(close_returns, confidence=0.95, minimum_observations=60)
        proposed_positions = []
        if direction != "NONE" and sizing.get("status") == "KNOWN":
            proposed_positions.append({
                "quantity": sizing.get("quantity"), "entry": entry, "stop": stop, "side": direction,
            })
        portfolio_risk = portfolio_risk_summary(
            proposed_positions, equity=self.config.risk.paper_equity_usd,
        )
        leverage_margin = (
            assess_leverage_margin(
                sizing.get("position_notional"), self.config.risk.paper_equity_usd, 1.0,
            )
            if asset.market_type == "perp" and direction != "NONE" and sizing.get("status") == "KNOWN"
            else {"status": "NOT_APPLICABLE", "reason": "Spot cash positions do not use borrowed leverage or derivative margin."}
            if asset.market_type in {"cex", "dex"}
            else {"status": "UNKNOWN", "reason": "No directional perpetual setup or notional is available."}
        )
        strategy_style = "SWING" if direction != "NONE" else "UNSELECTED"
        stress = stress_test_positions(
            [{"notional": sizing.get("position_notional"), "side": direction}]
            if direction != "NONE" and sizing.get("status") == "KNOWN" else []
        )
        trade_history = getattr(self.data, "closed_trade_history", {}).get(asset.raw, [])
        kelly_sample = len(trade_history) if isinstance(trade_history, (list, tuple)) else 0
        kelly = calculate_fractional_kelly(
            win_probability=0.5, average_win_loss_ratio=1.0,
            sample_size=kelly_sample,
        )
        if kelly_sample < 30:
            kelly["reason"] = "At least 30 verified closed paper trades are required; no position size is inferred."
        confidence_fields = [tech.get("status") == "KNOWN", price_action.get("status") == "KNOWN",
                             cycle.get("status") == "KNOWN", bool(snapshot.provider_a), bool(snapshot.provider_b)]
        confidence = round(sum(confidence_fields) / len(confidence_fields) * 100.0, 2)
        summary = (
            f"{direction}: Easy opportunity score {score:.1f}/100; risk score is a heuristic diagnostic, not a probability."
            if direction != "NONE" else
            f"NO TRADE: no aligned Easy trend/momentum setup (bull={bull:.0f}, bear={bear:.0f})."
        )
        summary_fa = (
            f"{'سیگنال خرید' if direction == 'LONG' else 'سیگنال فروش مشتقه' if direction == 'SHORT' else 'بدون سیگنال'}؛ "
            f"امتیاز فرصت {score:.1f} از ۱۰۰؛ امتیاز ریسک {risk_score:.1f} تخمینی است، نه احتمال سود."
        )
        metrics = {
            "engine_profile": "EASY",
            "decision_policy": {
                "mandatory_legacy_gates": 0, "twelve_gate_engine_used": False,
                "nonessential_diagnostics_are_hard_gates": False,
                "hard_inputs": ["validated_snapshot_price", "closed_valid_ohlcv", "atr_for_stop"],
                "market_regime_whale_sentiment_walk_forward_fundamental_are_diagnostics": True,
                "signal_score_semantics": "Heuristic evidence alignment, not probability of profit.",
            },
            "easy_trend": trend, "bullish_evidence_score": round(bull, 2),
            "bearish_evidence_score": round(bear, 2), "rsi14": rsi, "atr14": atr_value,
            "atr_pct": atr_pct, "relative_volume_20": relative_volume,
            "technical_indicators": tech, "easy_price_action": price_action,
            "chart_pattern": pattern, "elliott_wave": monitor_elliott_waves(candles),
            "market_cycle": cycle, "breakout_retest": breakout,
            "multi_timeframe_structure": mtf,
            "historical_var_expected_shortfall": var_es,
            "portfolio_risk_summary": portfolio_risk,
            "portfolio_stress_test": stress,
            "fractional_kelly": kelly,
            "leverage_margin_estimate": leverage_margin,
            "portfolio_correlation_analysis": {
                "status": "UNKNOWN",
                "reason": "No synchronized return histories for at least two portfolio assets were supplied to this Easy scan.",
            },
            "beta_hedge_estimate": {
                "status": "UNKNOWN",
                "reason": "A validated hedge benchmark return series and price were not supplied.",
            },
            "strategy_style": strategy_style,
            "strategy_horizon": STRATEGY_STYLE_GUIDE.get(strategy_style, {"holding_horizon": "not_selected"}).get("holding_horizon"),
            "timeframe_conflict": mtf.get("conflict", "UNKNOWN"),
            "market_sentiment": sentiment, "sentiment_status": sentiment.get("status", "UNKNOWN"),
            "whale_intelligence": whale, "whale_status": whale.get("status", security.get("status", "UNKNOWN")),
            "security_diagnostics": security,
            "fundamental_analysis": fundamental, "market_intelligence": market_intelligence,
            "easy_position_sizing": sizing,
            "risk_management_policy": {
                "used_as_hard_gate": False, "leverage_assumed": False,
                "orders_submitted": False, "stop_source": "ATR",
                "stop_distance": stop_distance if direction != "NONE" else None,
                "open_portfolio_risk_pct_reported": float(open_portfolio_risk_pct),
                "open_portfolio_risk_over_limit": float(open_portfolio_risk_pct) > self.config.risk.max_open_portfolio_risk_pct,
            },
            "risk_score_semantics": "Uncalibrated volatility/liquidity/data discrepancy heuristic; diagnostic only.",
            "confidence_level_pct": confidence,
            "confidence_level_semantics": "Presence of essential and supporting evidence, not a profit probability.",
            "market_regime": "UNKNOWN",
            "price_discrepancy_pct": discrepancy,
            "volume_discrepancy_pct": getattr(snapshot, "volume_discrepancy_pct", None),
            "provider_a": snapshot.provider_a, "provider_b": snapshot.provider_b,
            "candle_age_hours": age,
            "mandatory_gate_count": 0,
            "orders_submitted": False,
        }
        return AnalysisResult(
            asset=asset.raw, signal=side_signal, safety_status=(
                SafetyStatus.LOW_RISK if risk_score < 35 else SafetyStatus.HIGH_RISK if risk_score >= 65 else SafetyStatus.UNKNOWN
            ),
            confirmation_score=score, estimated_risk_score=round(risk_score, 2),
            opportunity_rank=score, entry_price=entry if direction != "NONE" else None,
            take_profit=target, stop_loss=stop, expected_holding_days=5 if direction != "NONE" else None,
            position_notional_usd=sizing.get("position_notional"), risk_budget_usd=sizing.get("risk_budget"),
            gates=[], summary_en=summary, summary_fa=summary_fa, metrics=metrics,
        )
