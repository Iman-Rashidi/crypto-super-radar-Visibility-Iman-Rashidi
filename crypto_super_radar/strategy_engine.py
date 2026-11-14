"""Deterministic, fail-closed swing strategy selector for CRYPTO SUPER RADAR.

Research lineage:
- Turtle / Richard Dennis & William Eckhardt: 20/55-bar Donchian breakouts.
- Ed Seykota: systematic trend-following and rule consistency.
- Linda Bradford Raschke: strong-trend EMA20 pullback continuation.
- Paul Tudor Jones: risk is fixed before entry; sizing remains in the executor.

This is a testable adaptation, not a claim to reproduce any trader's complete system.
It emits a candidate only from validated completed daily candles. It never places orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any

import pandas as pd


STRATEGY_VERSION = "CSR_MASTER_STRATEGY_V1"


@dataclass(frozen=True)
class MasterStrategyConfig:
    minimum_bars: int = 220
    donchian_fast_period: int = 20
    donchian_slow_period: int = 55
    atr_period: int = 20
    adx_period: int = 14
    ema_pullback_period: int = 20
    ema_regime_fast_period: int = 50
    ema_regime_slow_period: int = 200
    adx_pullback_threshold: float = 30.0
    stop_atr_multiple: float = 2.0
    target_atr_multiple: float = 4.0
    minimum_breakout_volume_ratio: float = 1.0
    maximum_candle_age_hours: float = 48.0
    maximum_recent_gap_hours: float = 36.0

    def __post_init__(self) -> None:
        if self.minimum_bars < max(
            self.donchian_slow_period + 2,
            self.ema_regime_slow_period + 1,
            self.atr_period + 2,
        ):
            raise ValueError("minimum_bars is too small for the configured indicators")
        if not 1 <= self.donchian_fast_period < self.donchian_slow_period:
            raise ValueError("Donchian periods must be positive and fast < slow")
        if min(
            self.atr_period, self.adx_period, self.ema_pullback_period,
            self.ema_regime_fast_period, self.ema_regime_slow_period,
        ) < 2:
            raise ValueError("Indicator periods must be at least 2")
        numeric = (
            self.adx_pullback_threshold, self.stop_atr_multiple,
            self.target_atr_multiple, self.minimum_breakout_volume_ratio,
            self.maximum_candle_age_hours, self.maximum_recent_gap_hours,
        )
        if not all(math.isfinite(float(value)) and float(value) > 0 for value in numeric):
            raise ValueError("Strategy thresholds must be finite and positive")
        if self.target_atr_multiple / self.stop_atr_multiple < 1.0:
            raise ValueError("Initial reward/risk must be at least 1:1")


class MasterStrategyEngine:
    """Select one deterministic strategy or fail closed to NO_TRADE."""

    def __init__(self, config: MasterStrategyConfig | None = None):
        self.config = config or MasterStrategyConfig()

    @staticmethod
    def _unknown(reason: str, **evidence: Any) -> dict[str, Any]:
        return {
            "version": STRATEGY_VERSION,
            "status": "UNKNOWN",
            "decision": "NO_TRADE",
            "strategy": None,
            "direction": "NO_TRADE",
            "reason_code": "INSUFFICIENT_OR_INVALID_DATA",
            "reason": reason,
            **evidence,
        }

    @staticmethod
    def _no_trade(reason: str, reason_code: str, **evidence: Any) -> dict[str, Any]:
        return {
            "version": STRATEGY_VERSION,
            "status": "NO_TRADE",
            "decision": "NO_TRADE",
            "strategy": None,
            "direction": "NO_TRADE",
            "reason_code": reason_code,
            "reason": reason,
            **evidence,
        }

    @staticmethod
    def _adx(frame: pd.DataFrame, period: int) -> pd.Series:
        high = pd.to_numeric(frame["high"], errors="coerce")
        low = pd.to_numeric(frame["low"], errors="coerce")
        close = pd.to_numeric(frame["close"], errors="coerce")
        up = high.diff()
        down = -low.diff()
        plus_dm = up.where((up > down) & (up > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)
        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        alpha = 1.0 / float(period)
        smoothed_tr = true_range.ewm(
            alpha=alpha, adjust=False, min_periods=period,
        ).mean()
        plus_di = 100.0 * plus_dm.ewm(
            alpha=alpha, adjust=False, min_periods=period,
        ).mean() / smoothed_tr.replace(0.0, float("nan"))
        minus_di = 100.0 * minus_dm.ewm(
            alpha=alpha, adjust=False, min_periods=period,
        ).mean() / smoothed_tr.replace(0.0, float("nan"))
        denominator = (plus_di + minus_di).replace(0.0, float("nan"))
        dx = 100.0 * (plus_di - minus_di).abs() / denominator
        return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    def evaluate(
        self,
        candles: Any,
        *,
        base_signal: str,
        now: datetime | pd.Timestamp | None = None,
    ) -> dict[str, Any]:
        """Evaluate one completed daily OHLCV frame; no network or order calls."""
        cfg = self.config
        required = {"open", "high", "low", "close", "volume"}
        if not isinstance(candles, pd.DataFrame) or candles.empty:
            return self._unknown("Validated daily OHLCV candles are unavailable.")
        if not required.issubset(candles.columns):
            return self._unknown("OHLCV frame is missing one or more required columns.")
        if not isinstance(candles.index, pd.DatetimeIndex):
            return self._unknown("Candle index must be a DatetimeIndex.")
        if candles.index.has_duplicates:
            return self._unknown("Duplicate candle timestamps block strategy evaluation.")
        if not candles.index.is_monotonic_increasing:
            return self._unknown("Candles are not in chronological order.")
        if len(candles) < cfg.minimum_bars:
            return self._unknown(
                "Insufficient daily history for EMA200 and Donchian validation.",
                available_bars=int(len(candles)),
                required_bars=int(cfg.minimum_bars),
            )

        frame = candles.loc[:, ["open", "high", "low", "close", "volume"]].copy()
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        else:
            frame.index = frame.index.tz_convert("UTC")
        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        numeric = frame[list(required)]
        if numeric.isna().any().any() or not all(math.isfinite(float(value)) for value in numeric.to_numpy().ravel()):
            return self._unknown("Missing or non-finite OHLCV observations block strategy evaluation.")
        if (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame["volume"] < 0).any():
            return self._unknown("Prices must be positive and volume cannot be negative.")
        malformed = (
            (frame["high"] < frame[["open", "low", "close"]].max(axis=1))
            | (frame["low"] > frame[["open", "high", "close"]].min(axis=1))
        )
        if malformed.any():
            return self._unknown("Malformed OHLC ranges block strategy evaluation.")

        current = pd.Timestamp(now or datetime.now(timezone.utc))
        if current.tzinfo is None:
            current = current.tz_localize("UTC")
        else:
            current = current.tz_convert("UTC")
        latest_ts = frame.index[-1]
        age_hours = float((current - latest_ts).total_seconds() / 3600.0)
        if age_hours < -1.0 or age_hours > cfg.maximum_candle_age_hours:
            return self._unknown(
                "Latest completed daily candle is future-dated or stale.",
                latest_candle_at_utc=latest_ts.isoformat(),
                candle_age_hours=round(age_hours, 4),
            )
        recent_deltas = frame.index[-61:].to_series().diff().dropna().dt.total_seconds()
        if recent_deltas.empty:
            return self._unknown("Daily candle cadence cannot be verified.")
        median_gap_hours = float(recent_deltas.median() / 3600.0)
        max_gap_hours = float(recent_deltas.max() / 3600.0)
        if not 20.0 <= median_gap_hours <= 28.0 or max_gap_hours > cfg.maximum_recent_gap_hours:
            return self._unknown(
                "Recent candles do not provide continuous daily coverage.",
                median_gap_hours=round(median_gap_hours, 4),
                maximum_gap_hours=round(max_gap_hours, 4),
            )

        close = frame["close"]
        high = frame["high"]
        low = frame["low"]
        open_ = frame["open"]
        volume = frame["volume"]
        close_now = float(close.iloc[-1])
        ema20 = close.ewm(span=cfg.ema_pullback_period, adjust=False).mean()
        ema50 = close.ewm(span=cfg.ema_regime_fast_period, adjust=False).mean()
        ema200 = close.ewm(span=cfg.ema_regime_slow_period, adjust=False).mean()
        previous_close = close.shift(1)
        tr = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_series = tr.ewm(
            alpha=1.0 / float(cfg.atr_period), adjust=False,
            min_periods=cfg.atr_period,
        ).mean()
        adx_series = self._adx(frame, cfg.adx_period)
        atr_value = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else None
        adx_value = float(adx_series.iloc[-1]) if pd.notna(adx_series.iloc[-1]) else None
        if atr_value is None or adx_value is None or atr_value <= 0:
            return self._unknown("ATR or ADX could not be calculated from the available candles.")

        return_30d_pct = float((close.iloc[-1] / close.iloc[-31] - 1.0) * 100.0)
        regime = "NEUTRAL"
        regime_side = "NO_TRADE"
        if close_now > float(ema50.iloc[-1]) > float(ema200.iloc[-1]) and return_30d_pct > 0:
            regime, regime_side = "BULLISH", "LONG"
        elif close_now < float(ema50.iloc[-1]) < float(ema200.iloc[-1]) and return_30d_pct < 0:
            regime, regime_side = "BEARISH", "SHORT"

        fast_high = float(high.shift(1).rolling(cfg.donchian_fast_period).max().iloc[-1])
        fast_low = float(low.shift(1).rolling(cfg.donchian_fast_period).min().iloc[-1])
        slow_high = float(high.shift(1).rolling(cfg.donchian_slow_period).max().iloc[-1])
        slow_low = float(low.shift(1).rolling(cfg.donchian_slow_period).min().iloc[-1])
        previous_volume = volume.iloc[-cfg.donchian_fast_period - 1:-1]
        median_previous_volume = float(previous_volume.median())
        current_volume = float(volume.iloc[-1])
        if not math.isfinite(median_previous_volume) or median_previous_volume <= 0:
            return self._unknown("A valid prior-volume baseline is unavailable.")
        volume_ratio = current_volume / median_previous_volume
        long_breakout_period = (
            cfg.donchian_slow_period if close_now > slow_high else
            cfg.donchian_fast_period if close_now > fast_high else None
        )
        short_breakout_period = (
            cfg.donchian_slow_period if close_now < slow_low else
            cfg.donchian_fast_period if close_now < fast_low else None
        )
        pullback_side = None
        latest_ema20 = float(ema20.iloc[-1])
        previous_ema20 = float(ema20.iloc[-2])
        latest_close = close_now
        prior_close = float(close.iloc[-2])
        latest_open = float(open_.iloc[-1])
        latest_low = float(low.iloc[-1])
        latest_high = float(high.iloc[-1])
        touch_buffer = 0.25 * atr_value
        if (
            regime_side == "LONG"
            and adx_value >= cfg.adx_pullback_threshold
            and prior_close <= previous_ema20
            and latest_close > latest_ema20
            and latest_low <= latest_ema20 + touch_buffer
            and latest_close > latest_open
        ):
            pullback_side = "LONG"
        elif (
            regime_side == "SHORT"
            and adx_value >= cfg.adx_pullback_threshold
            and prior_close >= previous_ema20
            and latest_close < latest_ema20
            and latest_high >= latest_ema20 - touch_buffer
            and latest_close < latest_open
        ):
            pullback_side = "SHORT"

        strategy = None
        side = "NO_TRADE"
        reason_code = "NO_VALID_SETUP"
        reason = "Neither a valid Donchian breakout nor a strong-trend EMA20 pullback is present."
        if regime_side == "LONG" and long_breakout_period and volume_ratio >= cfg.minimum_breakout_volume_ratio:
            side = "LONG"
            strategy = f"TURTLE_{long_breakout_period}"
            reason_code = "DONCHIAN_BREAKOUT"
            reason = f"Completed daily close broke the prior {long_breakout_period}-day high with volume confirmation."
        elif regime_side == "SHORT" and short_breakout_period and volume_ratio >= cfg.minimum_breakout_volume_ratio:
            side = "SHORT"
            strategy = f"TURTLE_{short_breakout_period}"
            reason_code = "DONCHIAN_BREAKOUT"
            reason = f"Completed daily close broke the prior {short_breakout_period}-day low with volume confirmation."
        elif pullback_side is not None:
            side = pullback_side
            strategy = "RASCHKE_EMA20_PULLBACK"
            reason_code = "STRONG_TREND_PULLBACK"
            reason = f"ADX(14) is at least {cfg.adx_pullback_threshold:g}; price reclaimed EMA20 after a pullback."

        evidence = {
            "market_regime": regime,
            "base_signal": str(base_signal or "").upper(),
            "adx14": round(adx_value, 4),
            "atr20": round(atr_value, 8),
            "atr_pct": round(atr_value / close_now * 100.0, 6),
            "ema20": round(latest_ema20, 8),
            "ema50": round(float(ema50.iloc[-1]), 8),
            "ema200": round(float(ema200.iloc[-1]), 8),
            "return_30d_pct": round(return_30d_pct, 6),
            "donchian_20_high": round(fast_high, 8),
            "donchian_20_low": round(fast_low, 8),
            "donchian_55_high": round(slow_high, 8),
            "donchian_55_low": round(slow_low, 8),
            "breakout_volume_ratio": round(volume_ratio, 6),
            "latest_candle_at_utc": latest_ts.isoformat(),
            "candle_age_hours": round(age_hours, 4),
        }
        if strategy is None:
            return self._no_trade(reason, reason_code, **evidence)
        if regime_side != side:
            return self._no_trade(
                "Selected setup conflicts with the validated EMA50/EMA200 market regime.",
                "REGIME_CONFLICT",
                **evidence,
            )
        normalized_base = str(base_signal or "").upper().replace(" ", "_")
        if normalized_base != side:
            return self._no_trade(
                "Master Strategy direction does not match the 12-gate engine direction.",
                "BASE_SIGNAL_CONFLICT",
                selected_strategy=strategy,
                selected_direction=side,
                **evidence,
            )

        direction_sign = 1.0 if side == "LONG" else -1.0
        entry = close_now
        stop = entry - direction_sign * cfg.stop_atr_multiple * atr_value
        target = entry + direction_sign * cfg.target_atr_multiple * atr_value
        if min(entry, stop, target) <= 0 or not all(math.isfinite(v) for v in (entry, stop, target)):
            return self._unknown("Calculated execution levels are invalid.", **evidence)
        risk_distance = abs(entry - stop)
        reward_distance = abs(target - entry)
        reward_risk = reward_distance / risk_distance
        if reward_risk < 1.0:
            return self._unknown("Calculated initial reward/risk is below the minimum.", **evidence)

        return {
            "version": STRATEGY_VERSION,
            "status": "APPROVED",
            "decision": side,
            "strategy": strategy,
            "direction": side,
            "reason_code": reason_code,
            "reason": reason,
            "entry_price": round(entry, 8),
            "stop_loss": round(stop, 8),
            "take_profit": round(target, 8),
            "initial_stop_atr_multiple": cfg.stop_atr_multiple,
            "initial_target_atr_multiple": cfg.target_atr_multiple,
            "initial_reward_risk": round(reward_risk, 4),
            "risk_sizing": "Executor sizes quantity from stop distance and fixed equity-risk budget.",
            "exit_management": "Exchange-attached initial mark-price TP/SL; dynamic Donchian trailing exit is not yet wired.",
            **evidence,
        }

    def evaluate_record(self, candles: Any, *, base_signal: str, now=None) -> dict[str, Any]:
        """Compatibility alias for call sites that prefer record-oriented naming."""
        return self.evaluate(candles, base_signal=base_signal, now=now)
