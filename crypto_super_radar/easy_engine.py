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
from .easy_risk_management import calculate_position_size, classify_market_cycle
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
        relative_volume = _num((tech.get("volume") or {}).get("relative_volume_20"))
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
        if obv_trend == "BULLISH": bull += 10
        elif obv_trend == "BEARISH": bear += 10
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
