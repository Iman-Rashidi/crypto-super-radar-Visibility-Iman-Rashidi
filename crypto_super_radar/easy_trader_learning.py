"""Persistent, AI-independent learning memory for the Easy Trader path.

The module records closed-candle/order-flow evidence, delayed outcomes, prediction
errors and model versions. It never submits exchange orders. Training starts with
a logistic baseline and adds a small two-hidden-layer NumPy MLP challenger only
after a substantially larger labelled sample is available.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

FEATURE_NAMES = (
    "side_sign",
    "candle_pressure",
    "aggressor_delta_ratio",
    "depth_imbalance",
    "large_print_imbalance",
    "rvol_scaled",
    "candle_body_ratio",
    "candle_close_location",
    "pattern_score_scaled",
)

ROUND_TRIP_COST_PCT = 0.15
OUTCOME_HORIZON_BARS = 3
MIN_LOGISTIC_LABELS = 100
MIN_MLP_LABELS = 500
TRAINING_BATCH_NEW_LABELS = 50

PHASE_LABELS_FA = {
    "OBSERVE": "فاز صفر: مشاهده و ذخیره تجربه",
    "REPLAY": "فاز یک: بازپخش و آموزش اولیه",
    "PAPER_LEARNING": "فاز دو: یادگیری در شبیه‌سازی Paper",
    "PAPER_STRICT": "فاز سه: Paper با کنترل سخت‌گیرانه",
    "DEMO_REVIEW_READY": "فاز چهار: آماده بررسی مستقل Demo؛ هنوز غیرفعال",
}
CONTROL_LABELS_FA = {
    0: "سطح صفر: فقط ثبت تجربه",
    1: "سطح یک: آستانه احتمال بالاتر",
    2: "سطح دو: محدودسازی سیگنال‌های Paper",
    3: "سطح سه: فقط شبیه‌سازی محافظه‌کارانه",
    4: "سطح چهار: توقف ورود؛ ادامه ثبت و یادگیری",
    5: "سطح پنج: توقف به‌دلیل خطای داده/سیستم",
}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _safe_feature_vector(features: Mapping[str, Any]) -> list[float]:
    result: list[float] = []
    for name in FEATURE_NAMES:
        value = _finite(features.get(name))
        if value is None:
            raise ValueError(f"feature {name} is missing or invalid")
        result.append(_clamp(value, -1.0, 1.0))
    return result


def build_easy_trader_features(
    frame: Any,
    market_flow: Mapping[str, Any] | None,
    pattern: Mapping[str, Any] | None,
    side: str,
) -> dict[str, float]:
    """Construct only the Easy Trader's permitted flow/candle feature vector."""
    if frame is None or getattr(frame, "empty", True):
        raise ValueError("completed candle frame is unavailable")
    if side not in {"BUY", "SELL", "WAIT"}:
        raise ValueError("side must be BUY, SELL or WAIT")
    flow = market_flow if isinstance(market_flow, Mapping) else {}
    pressure = flow.get("pressure") if isinstance(flow.get("pressure"), Mapping) else {}
    aggressor = flow.get("aggressor_flow") if isinstance(flow.get("aggressor_flow"), Mapping) else {}
    book = flow.get("order_book") if isinstance(flow.get("order_book"), Mapping) else {}
    large = flow.get("large_trades") if isinstance(flow.get("large_trades"), list) else []
    latest = frame.iloc[-1]

    buy_pct = _finite(pressure.get("buy_pct"))
    buy_pct = 50.0 if buy_pct is None else buy_pct
    buy_quote = _finite(aggressor.get("buy_quote")) or 0.0
    sell_quote = _finite(aggressor.get("sell_quote")) or 0.0
    delta = _finite(aggressor.get("net_delta_quote"))
    gross = buy_quote + sell_quote
    delta_ratio = _clamp((delta or 0.0) / gross, -1.0, 1.0) if gross > 0 else 0.0

    imbalance = _finite(book.get("imbalance_pct"))
    if str(book.get("status") or "").upper() != "PASS" or imbalance is None:
        imbalance = 0.0

    large_buy = sum(
        _finite(item.get("quote_value")) or 0.0
        for item in large
        if isinstance(item, Mapping) and str(item.get("side") or "").upper() == "BUY"
    )
    large_sell = sum(
        _finite(item.get("quote_value")) or 0.0
        for item in large
        if isinstance(item, Mapping) and str(item.get("side") or "").upper() == "SELL"
    )
    large_total = large_buy + large_sell
    large_imbalance = (large_buy - large_sell) / large_total if large_total > 0 else 0.0

    open_price = _finite(latest.get("open"))
    high = _finite(latest.get("high"))
    low = _finite(latest.get("low"))
    close = _finite(latest.get("close"))
    if None in (open_price, high, low, close) or high <= low or close <= 0:
        raise ValueError("latest completed candle has invalid OHLC values")
    candle_range = high - low
    rvol = _finite(flow.get("rvol"))
    rvol_scaled = _clamp(math.log1p(max(0.0, rvol or 0.0)) / 3.0, 0.0, 1.0)
    score = _finite(pattern.get("score")) if isinstance(pattern, Mapping) else None
    score_scaled = _clamp((score or 0.0) / 30.0, -1.0, 1.0)

    return {
        "side_sign": 1.0 if side == "BUY" else -1.0 if side == "SELL" else 0.0,
        "candle_pressure": _clamp((buy_pct - 50.0) / 50.0, -1.0, 1.0),
        "aggressor_delta_ratio": delta_ratio,
        "depth_imbalance": _clamp(imbalance / 100.0, -1.0, 1.0),
        "large_print_imbalance": _clamp(large_imbalance, -1.0, 1.0),
        "rvol_scaled": rvol_scaled,
        "candle_body_ratio": _clamp((close - open_price) / candle_range, -1.0, 1.0),
        "candle_close_location": _clamp(((close - low) / candle_range) * 2.0 - 1.0, -1.0, 1.0),
        "pattern_score_scaled": score_scaled,
    }


def evaluate_flow_candle_signal(
    frame: Any,
    market_flow: Mapping[str, Any] | None,
    candle_patterns: Sequence[Mapping[str, Any]] | None,
    *,
    freshness_status: str,
) -> dict[str, Any]:
    """Create an Easy Trader candidate from confirmed candles and order-flow only."""
    empty = {
        "signal": "WAIT",
        "status": "WAITING",
        "source": "FLOW_AND_CONFIRMED_CANDLE_ONLY",
        "reason_fa": "در انتظار الگوی کندلی تأییدشده و دست‌کم دو شاهد جریان سفارش هم‌جهت.",
        "pattern": None,
        "flow_votes": [],
        "supporting_flow_count": 0,
        "opposing_flow_count": 0,
        "entry_reference_price": None,
        "stop_loss": None,
        "take_profit": None,
        "signal_candle_at": None,
        "signal_key": None,
    }
    if freshness_status != "FRESH":
        return {**empty, "status": "BLOCKED", "reason_fa": "کندل بسته‌شده تازه نیست؛ سیگنال جدید متوقف شد."}
    if frame is None or getattr(frame, "empty", True) or len(frame) < 2:
        return {**empty, "status": "BLOCKED", "reason_fa": "تاریخچه کندل بسته‌شده کافی نیست."}

    confirmed = [
        item for item in (candle_patterns or [])
        if isinstance(item, Mapping)
        and str(item.get("state") or "").upper() == "CONFIRMED"
        and str(item.get("direction") or "").upper() in {"BULLISH", "BEARISH"}
    ]
    if not confirmed:
        pending = [
            item for item in (candle_patterns or [])
            if isinstance(item, Mapping)
            and str(item.get("state") or "").upper() in {"NEEDS_CONFIRMATION", "FORMING"}
        ]
        return {
            **empty,
            "reason_fa": (
                "الگوی کندلی در انتظار تأیید کندل بعدی است."
                if pending else "الگوی صعودی یا نزولی تأییدشده‌ای در کندل بسته‌شده پیدا نشد."
            ),
            "pattern": dict(pending[0]) if pending else None,
        }

    bullish_score = sum(
        max(0.0, _finite(item.get("score")) or 0.0)
        for item in confirmed if str(item.get("direction")).upper() == "BULLISH"
    )
    bearish_score = sum(
        max(0.0, -(_finite(item.get("score")) or 0.0))
        for item in confirmed if str(item.get("direction")).upper() == "BEARISH"
    )
    if bullish_score and bearish_score and max(bullish_score, bearish_score) < 1.5 * min(bullish_score, bearish_score):
        return {**empty, "reason_fa": "الگوهای کندلی تأییدشده متعارض‌اند؛ سیگنال صادر نشد."}
    direction = "BULLISH" if bullish_score >= bearish_score else "BEARISH"
    side = "BUY" if direction == "BULLISH" else "SELL"
    candidates = [item for item in confirmed if str(item.get("direction")).upper() == direction]
    pattern = max(candidates, key=lambda item: abs(_finite(item.get("score")) or 0.0))

    flow = market_flow if isinstance(market_flow, Mapping) else {}
    pressure = flow.get("pressure") if isinstance(flow.get("pressure"), Mapping) else {}
    aggressor = flow.get("aggressor_flow") if isinstance(flow.get("aggressor_flow"), Mapping) else {}
    book = flow.get("order_book") if isinstance(flow.get("order_book"), Mapping) else {}
    large = flow.get("large_trades") if isinstance(flow.get("large_trades"), list) else []
    votes: list[dict[str, str]] = []

    buy_pct = _finite(pressure.get("buy_pct"))
    if buy_pct is not None:
        vote = "BUY" if buy_pct >= 55.0 else "SELL" if buy_pct <= 45.0 else None
        if vote:
            votes.append({"source": "CANDLE_PRESSURE", "source_group": "CANDLE", "direction": vote})

    buy_quote = _finite(aggressor.get("buy_quote")) or 0.0
    sell_quote = _finite(aggressor.get("sell_quote")) or 0.0
    delta = _finite(aggressor.get("net_delta_quote"))
    gross = buy_quote + sell_quote
    delta_ratio = delta / gross if delta is not None and gross > 0 else None
    vote = "BUY" if delta_ratio is not None and delta_ratio >= 0.02 else (
        "SELL" if delta_ratio is not None and delta_ratio <= -0.02 else None
    )
    if vote:
        votes.append({"source": "AGGRESSOR_CVD", "source_group": "AGGREGATE_TRADES", "direction": vote})

    imbalance = _finite(book.get("imbalance_pct"))
    if str(book.get("status") or "").upper() == "PASS" and imbalance is not None:
        vote = "BUY" if imbalance >= 12.0 else "SELL" if imbalance <= -12.0 else None
        if vote:
            votes.append({"source": "TOP_20_DEPTH", "source_group": "ORDER_BOOK", "direction": vote})

    large_buy = sum(
        _finite(item.get("quote_value")) or 0.0
        for item in large if isinstance(item, Mapping) and str(item.get("side") or "").upper() == "BUY"
    )
    large_sell = sum(
        _finite(item.get("quote_value")) or 0.0
        for item in large if isinstance(item, Mapping) and str(item.get("side") or "").upper() == "SELL"
    )
    if large_buy > 0 or large_sell > 0:
        vote = "BUY" if large_buy >= 1.5 * large_sell and large_buy > 0 else (
            "SELL" if large_sell >= 1.5 * large_buy and large_sell > 0 else None
        )
        if vote:
            votes.append({"source": "LARGE_PRINTS", "source_group": "AGGREGATE_TRADES", "direction": vote})

    supporting = sum(item["direction"] == side for item in votes)
    opposing = sum(item["direction"] != side for item in votes)
    supporting_groups = {
        item.get("source_group", item["source"])
        for item in votes if item["direction"] == side
    }
    opposing_groups = {
        item.get("source_group", item["source"])
        for item in votes if item["direction"] != side
    }
    latest = frame.iloc[-1]
    entry = _finite(latest.get("close"))
    timestamp = frame.index[-1].isoformat() if hasattr(frame.index[-1], "isoformat") else str(frame.index[-1])
    base = {
        **empty,
        "pattern": {
            "name_fa": pattern.get("name_fa"),
            "name_en": pattern.get("name_en"),
            "direction": direction,
            "score": _finite(pattern.get("score")),
            "invalidation_price": _finite(pattern.get("invalidation_price")),
            "state": "CONFIRMED",
        },
        "flow_votes": votes,
        "supporting_flow_count": supporting,
        "opposing_flow_count": opposing,
        "supporting_independent_sources": len(supporting_groups),
        "opposing_independent_sources": len(opposing_groups),
        "signal_candle_at": timestamp,
        "entry_reference_price": entry,
    }
    if len(supporting_groups) < 2 or len(supporting_groups) <= len(opposing_groups):
        return {
            **base,
            "reason_fa": f"الگوی {pattern.get('name_fa') or pattern.get('name_en')} تأیید شده، اما جریان سفارش کافی هم‌جهت نیست ({len(supporting_groups)} منبع مستقل موافق / {len(opposing_groups)} منبع مستقل مخالف).",
        }
    if entry is None or entry <= 0:
        return {**base, "status": "BLOCKED", "reason_fa": "قیمت کندل معتبر نیست."}

    stop = _finite(pattern.get("invalidation_price"))
    if side == "BUY":
        if stop is None or stop >= entry:
            stop = float(frame["low"].tail(3).min())
        risk = entry - stop
        target = entry + 2.0 * risk if risk > 0 else None
    else:
        if stop is None or stop <= entry:
            stop = float(frame["high"].tail(3).max())
        risk = stop - entry
        target = entry - 2.0 * risk if risk > 0 else None
    if risk <= 0 or target is None or target <= 0:
        return {**base, "status": "BLOCKED", "reason_fa": "سطوح حد ضرر و حد سود معتبر از ساختار کندل به دست نیامد."}
    return {
        **base,
        "signal": side,
        "status": "CANDIDATE",
        "stop_loss": round(float(stop), 12),
        "take_profit": round(float(target), 12),
        "signal_key": f"EASY|{side}|{timestamp}|{pattern.get('name_en') or 'CANDLE'}",
        "reason_fa": f"الگوی {pattern.get('name_fa') or pattern.get('name_en')} تأیید شده و {supporting} شاهد جریان سفارش هم‌جهت دارد.",
    }


class EasyTraderLearningMemory:
    """Append-only learning memory and model registry on a SQLite/Turso connection."""

    def __init__(self, connection: Any):
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise ValueError("a SQLite-compatible database connection is required")
        self.db = connection
        self.lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self.lock:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS easy_learning_samples (
                    sample_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    candle_at TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    reference_price REAL NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('BUY','SELL','WAIT')),
                    features_json TEXT NOT NULL,
                    pattern_name TEXT,
                    predicted_probability REAL,
                    predicted_class INTEGER,
                    model_version TEXT,
                    phase_at_observation TEXT NOT NULL,
                    control_level_at_observation INTEGER NOT NULL,
                    label INTEGER,
                    net_return_pct REAL,
                    outcome_at_ms INTEGER,
                    error_flag INTEGER,
                    error_code TEXT,
                    created_at_ms INTEGER NOT NULL,
                    UNIQUE(symbol, interval, candle_at)
                )
            """)
            self.db.execute("""
                CREATE INDEX IF NOT EXISTS idx_easy_learning_pending
                ON easy_learning_samples(symbol, interval, label, candle_at)
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS easy_learning_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time_ms INTEGER NOT NULL,
                    sample_key TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            self.db.execute("""
                CREATE INDEX IF NOT EXISTS idx_easy_learning_events_time
                ON easy_learning_events(event_time_ms, event_type)
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS easy_learning_models (
                    model_version TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    model_kind TEXT NOT NULL,
                    feature_names_json TEXT NOT NULL,
                    model_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    validation_count INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER NOT NULL
                )
            """)
            self.db.execute("""
                CREATE INDEX IF NOT EXISTS idx_easy_learning_models_active
                ON easy_learning_models(symbol, interval, active, created_at_ms)
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS easy_learning_state (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    control_level INTEGER NOT NULL,
                    last_training_count INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(symbol, interval)
                )
            """)
            self.db.commit()

    def _event(self, event_type: str, payload: Mapping[str, Any], sample_key: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO easy_learning_events(event_time_ms,sample_key,event_type,payload_json) VALUES (?,?,?,?)",
            (int(time.time() * 1000), sample_key, event_type, _json(dict(payload))),
        )

    def record_observation(
        self,
        *,
        symbol: str,
        interval: str,
        candle_at: str,
        observed_at_ms: int,
        reference_price: float,
        side: str,
        features: Mapping[str, Any],
        pattern_name: str | None = None,
        probability: float | None = None,
        model_version: str | None = None,
        phase: str = "OBSERVE",
        control_level: int = 0,
    ) -> dict[str, Any]:
        market = str(symbol or "").strip().upper()
        timeframe = str(interval or "").strip()
        timestamp = str(candle_at or "").strip()
        if not market.isascii() or not market.isalnum() or not timeframe or not timestamp:
            raise ValueError("symbol, interval and candle timestamp are required")
        if side not in {"BUY", "SELL", "WAIT"}:
            raise ValueError("side must be BUY, SELL or WAIT")
        if isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int) or observed_at_ms <= 0:
            raise ValueError("observed_at_ms must be a positive integer")
        price = _finite(reference_price)
        if price is None or price <= 0:
            raise ValueError("reference_price must be positive and finite")
        vector = _safe_feature_vector(features)
        probability_value = _finite(probability)
        if probability_value is not None and not 0.0 <= probability_value <= 1.0:
            raise ValueError("probability must be between zero and one")
        key_material = "|".join((market, timeframe, timestamp))
        sample_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        predicted_class = int(probability_value >= 0.5) if probability_value is not None else None
        with self.lock:
            cursor = self.db.execute("""
                INSERT OR IGNORE INTO easy_learning_samples(
                    sample_key,symbol,interval,candle_at,observed_at_ms,reference_price,
                    side,features_json,pattern_name,predicted_probability,predicted_class,
                    model_version,phase_at_observation,control_level_at_observation,created_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                sample_key, market, timeframe, timestamp, observed_at_ms, price,
                side, _json(dict(zip(FEATURE_NAMES, vector))), pattern_name,
                probability_value, predicted_class, model_version, str(phase),
                int(control_level), int(time.time() * 1000),
            ))
            inserted = int(cursor.rowcount or 0) > 0
            if inserted:
                self._event("OBSERVATION_RECORDED", {
                    "symbol": market, "interval": timeframe, "candle_at": timestamp,
                    "side": side, "pattern_name": pattern_name,
                    "predicted_probability": probability_value, "model_version": model_version,
                    "phase": phase, "control_level": int(control_level),
                    "feature_names": list(FEATURE_NAMES),
                }, sample_key)
            self.db.commit()
        return {"sample_key": sample_key, "inserted": inserted, "side": side}

    def label_matured(
        self,
        *,
        symbol: str,
        interval: str,
        completed_closes: Sequence[tuple[str, float]],
        horizon_bars: int = OUTCOME_HORIZON_BARS,
        round_trip_cost_pct: float = ROUND_TRIP_COST_PCT,
    ) -> dict[str, int]:
        """Label old observations only after the requested number of future candles close."""
        if horizon_bars < 1:
            raise ValueError("horizon_bars must be positive")
        cost = _finite(round_trip_cost_pct)
        if cost is None or cost < 0 or cost >= 10:
            raise ValueError("round_trip_cost_pct must be in [0,10)")
        normalized: list[tuple[str, float]] = []
        for stamp, close in completed_closes:
            price = _finite(close)
            if not isinstance(stamp, str) or not stamp.strip() or price is None or price <= 0:
                raise ValueError("completed candle closes contain invalid values")
            normalized.append((stamp, price))
        index = {stamp: i for i, (stamp, _) in enumerate(normalized)}
        market, timeframe = str(symbol).strip().upper(), str(interval).strip()
        with self.lock:
            rows = self.db.execute("""
                SELECT sample_key,candle_at,reference_price,side,predicted_probability,predicted_class
                FROM easy_learning_samples
                WHERE symbol=? AND interval=? AND side IN ('BUY','SELL') AND label IS NULL
                ORDER BY candle_at ASC
            """, (market, timeframe)).fetchall()
            labelled = 0
            errors = 0
            for row in rows:
                position = index.get(str(row["candle_at"]))
                if position is None or position + horizon_bars >= len(normalized):
                    continue
                entry = float(row["reference_price"])
                exit_price = normalized[position + horizon_bars][1]
                side = str(row["side"])
                gross_return = (
                    (exit_price / entry - 1.0) * 100.0
                    if side == "BUY" else (entry / exit_price - 1.0) * 100.0
                )
                net_return = gross_return - cost
                label = int(net_return > 0.0)
                probability = _finite(row["predicted_probability"])
                predicted = row["predicted_class"]
                error_flag = int(int(predicted) != label) if predicted is not None else None
                outcome_at = int(time.time() * 1000)
                self.db.execute("""
                    UPDATE easy_learning_samples
                    SET label=?,net_return_pct=?,outcome_at_ms=?,error_flag=?
                    WHERE sample_key=? AND label IS NULL
                """, (label, round(net_return, 8), outcome_at, error_flag, row["sample_key"]))
                self._event("OUTCOME_LABELLED", {
                    "label": label, "net_return_pct": round(net_return, 8),
                    "gross_return_pct": round(gross_return, 8),
                    "round_trip_cost_pct": cost, "horizon_bars": horizon_bars,
                    "predicted_probability": probability, "predicted_class": predicted,
                    "prediction_error": bool(error_flag) if error_flag is not None else None,
                }, str(row["sample_key"]))
                labelled += 1
                errors += int(error_flag == 1)
            if labelled:
                self._escalate_controls_locked(market, timeframe)
                self.db.commit()
        return {"labelled": labelled, "prediction_errors": errors}

    def _escalate_controls_locked(self, symbol: str, interval: str) -> dict[str, Any]:
        rows = self.db.execute("""
            SELECT error_flag,label,net_return_pct
            FROM easy_learning_samples
            WHERE symbol=? AND interval=? AND label IS NOT NULL
            ORDER BY outcome_at_ms DESC LIMIT 50
        """, (symbol, interval)).fetchall()
        level_row = self.db.execute(
            "SELECT control_level FROM easy_learning_state WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
        level = int(level_row["control_level"]) if level_row else 0
        count = len(rows)
        prediction_rows = [row for row in rows if row["error_flag"] is not None]
        prediction_error_rate = (
            sum(int(row["error_flag"] or 0) for row in prediction_rows) / len(prediction_rows)
            if prediction_rows else None
        )
        loss_rate = (
            sum(1 for row in rows if int(row["label"] or 0) == 0) / count
            if count else None
        )
        error_rate = max(
            value for value in (prediction_error_rate, loss_rate)
            if value is not None
        ) if prediction_error_rate is not None or loss_rate is not None else None
        consecutive_errors = 0
        for row in rows:
            is_error = (
                int(row["error_flag"] or 0) == 1
                if row["error_flag"] is not None else int(row["label"] or 0) == 0
            )
            if is_error:
                consecutive_errors += 1
            else:
                break
        new_level = level
        if count >= 20 and error_rate is not None:
            if error_rate >= 0.60:
                new_level = max(new_level, 4)
            elif error_rate >= 0.50:
                new_level = max(new_level, 3)
            elif error_rate >= 0.40:
                new_level = max(new_level, 2)
        if consecutive_errors >= 5:
            new_level = max(new_level, 4)
        if new_level > level:
            self._event("CONTROL_LEVEL_ESCALATED", {
                "old_level": level, "new_level": new_level,
                "rolling_error_rate": error_rate, "rolling_samples": count,
                "consecutive_errors": consecutive_errors,
            })
        phase = self._phase_for(market= symbol, interval=interval, control_level=new_level)
        self.db.execute("""
            INSERT INTO easy_learning_state(symbol,interval,phase,control_level,updated_at_ms)
            VALUES (?,?,?,?,?)
            ON CONFLICT(symbol,interval) DO UPDATE SET
                phase=excluded.phase,control_level=excluded.control_level,
                updated_at_ms=excluded.updated_at_ms
        """, (symbol, interval, phase, new_level, int(time.time() * 1000)))
        return {"control_level": new_level, "error_rate": error_rate, "phase": phase}

    def record_error(
        self,
        *,
        symbol: str,
        interval: str,
        code: str,
        detail: str = "",
    ) -> dict[str, Any]:
        """Record an operational/data error without storing credentials or headers."""
        market, timeframe = str(symbol).strip().upper(), str(interval).strip()
        safe_code = str(code or "UNKNOWN_ERROR")[:80]
        safe_detail = str(detail or "")[:240]
        with self.lock:
            self._event("LEARNING_ERROR", {
                "symbol": market, "interval": timeframe,
                "code": safe_code, "detail": safe_detail,
            })
            recent = self.db.execute("""
                SELECT event_time_ms,payload_json FROM easy_learning_events
                WHERE event_type='LEARNING_ERROR'
                ORDER BY event_time_ms DESC LIMIT 50
            """).fetchall()
            repeated = sum(
                1 for row in recent
                if json.loads(row["payload_json"]).get("code") == safe_code
                and json.loads(row["payload_json"]).get("symbol") == market
                and json.loads(row["payload_json"]).get("interval") == timeframe
                and int(time.time() * 1000) - int(row["event_time_ms"]) <= 3_600_000
            )
            state = self.db.execute(
                "SELECT control_level FROM easy_learning_state WHERE symbol=? AND interval=?",
                (market, timeframe),
            ).fetchone()
            level = int(state["control_level"]) if state else 0
            new_level = max(level, 5 if repeated >= 3 else 4 if repeated >= 2 else level)
            phase = self._phase_for(market=market, interval=timeframe, control_level=new_level)
            self.db.execute("""
                INSERT INTO easy_learning_state(symbol,interval,phase,control_level,updated_at_ms)
                VALUES (?,?,?,?,?)
                ON CONFLICT(symbol,interval) DO UPDATE SET
                    phase=excluded.phase,control_level=excluded.control_level,
                    updated_at_ms=excluded.updated_at_ms
            """, (market, timeframe, phase, new_level, int(time.time() * 1000)))
            if new_level > level:
                self._event("CONTROL_LEVEL_ESCALATED", {
                    "old_level": level, "new_level": new_level,
                    "cause": "repeated_operational_error", "error_code": safe_code,
                    "repetitions_last_hour": repeated,
                })
            self.db.commit()
        return {"control_level": new_level, "repetitions_last_hour": repeated, "phase": phase}

    def _phase_for(self, market: str, interval: str, control_level: int) -> str:
        return self._derive_phase(market, interval, control_level)

    def _labelled_dataset(self, symbol: str, interval: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
        rows = self.db.execute("""
            SELECT candle_at,features_json,label
            FROM easy_learning_samples
            WHERE symbol=? AND interval=? AND label IS NOT NULL
            ORDER BY candle_at ASC,observed_at_ms ASC
        """, (symbol, interval)).fetchall()
        # One labelled example per closed candle is enforced by the table's unique key.
        x = np.asarray([
            [_finite(json.loads(row["features_json"]).get(name)) or 0.0 for name in FEATURE_NAMES]
            for row in rows
        ], dtype=float)
        y = np.asarray([int(row["label"]) for row in rows], dtype=float)
        stamps = [str(row["candle_at"]) for row in rows]
        return x, y, stamps

    def _active_model(self, symbol: str, interval: str) -> dict[str, Any] | None:
        row = self.db.execute("""
            SELECT model_version,model_kind,model_json,metrics_json,sample_count
            FROM easy_learning_models
            WHERE symbol=? AND interval=? AND active=1
            ORDER BY created_at_ms DESC LIMIT 1
        """, (symbol, interval)).fetchone()
        if not row:
            return None
        return {
            "model_version": row["model_version"],
            "model_kind": row["model_kind"],
            "model": json.loads(row["model_json"]),
            "metrics": json.loads(row["metrics_json"]),
            "sample_count": int(row["sample_count"]),
        }

    def predict(self, symbol: str, interval: str, features: Mapping[str, Any]) -> dict[str, Any]:
        vector = np.asarray(_safe_feature_vector(features), dtype=float)
        market, timeframe = str(symbol).strip().upper(), str(interval).strip()
        with self.lock:
            active = self._active_model(market, timeframe)
        if not active:
            return {"probability": None, "model_version": None, "model_kind": "WARMUP",
                    "status": "INSUFFICIENT_LABELS"}
        probability = self._predict_model(active["model_kind"], active["model"], vector)
        return {
            "probability": round(float(probability), 6),
            "model_version": active["model_version"],
            "model_kind": active["model_kind"],
            "status": "PREDICTED",
        }

    @staticmethod
    def _predict_model(kind: str, model: Mapping[str, Any], vector: np.ndarray) -> float:
        if kind == "LOGISTIC":
            weights = np.asarray(model["weights"], dtype=float)
            return float(_sigmoid(np.asarray([float(model["bias"]) + np.dot(weights, vector)]))[0])
        if kind == "MLP_2_HIDDEN":
            w1, b1 = np.asarray(model["w1"]), np.asarray(model["b1"])
            w2, b2 = np.asarray(model["w2"]), np.asarray(model["b2"])
            w3, b3 = np.asarray(model["w3"]), float(model["b3"])
            h1 = np.tanh(vector @ w1 + b1)
            h2 = np.tanh(h1 @ w2 + b2)
            return float(_sigmoid(np.asarray([float(h2 @ w3 + b3)]))[0])
        raise ValueError("unknown learning model kind")

    @staticmethod
    def _fit_logistic(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
        weights = np.zeros(x.shape[1], dtype=float)
        prior = _clamp(float(np.mean(y)), 0.01, 0.99)
        bias = math.log(prior / (1.0 - prior))
        for _ in range(600):
            probability = _sigmoid(x @ weights + bias)
            error = probability - y
            weights -= 0.06 * ((x.T @ error) / len(y) + 0.02 * weights)
            bias -= 0.06 * float(np.mean(error))
        return {"weights": weights.tolist(), "bias": float(bias)}

    @staticmethod
    def _fit_mlp(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
        rng = np.random.default_rng(20261115)
        n, features = x.shape
        w1 = rng.normal(0.0, 0.22, (features, 8))
        b1 = np.zeros(8)
        w2 = rng.normal(0.0, 0.22, (8, 4))
        b2 = np.zeros(4)
        w3 = rng.normal(0.0, 0.22, 4)
        b3 = 0.0
        for epoch in range(450):
            h1 = np.tanh(x @ w1 + b1)
            h2 = np.tanh(h1 @ w2 + b2)
            p = _sigmoid(h2 @ w3 + b3)
            error = (p - y) / n
            d_w3 = h2.T @ error + 0.015 * w3
            d_b3 = float(np.sum(error))
            d_h2 = np.outer(error, w3) * (1.0 - h2 * h2)
            d_w2 = h1.T @ d_h2 + 0.015 * w2
            d_b2 = np.sum(d_h2, axis=0)
            d_h1 = (d_h2 @ w2.T) * (1.0 - h1 * h1)
            d_w1 = x.T @ d_h1 + 0.015 * w1
            d_b1 = np.sum(d_h1, axis=0)
            learning_rate = 0.025 * (0.995 ** epoch)
            for parameter, gradient in (
                (w1, d_w1), (b1, d_b1), (w2, d_w2),
                (b2, d_b2), (w3, d_w3),
            ):
                parameter -= learning_rate * gradient
            b3 -= learning_rate * d_b3
        return {
            "w1": w1.tolist(), "b1": b1.tolist(),
            "w2": w2.tolist(), "b2": b2.tolist(),
            "w3": w3.tolist(), "b3": float(b3),
        }

    @staticmethod
    def _metrics(probability: np.ndarray, y: np.ndarray) -> dict[str, float]:
        clipped = np.clip(probability, 1e-7, 1.0 - 1e-7)
        baseline_probability = np.full(len(y), float(np.mean(y)))
        accuracy = float(np.mean((probability >= 0.5) == (y >= 0.5)))
        baseline_accuracy = max(float(np.mean(y >= 0.5)), float(np.mean(y < 0.5)))
        return {
            "brier": float(np.mean((probability - y) ** 2)),
            "log_loss": float(-np.mean(y * np.log(clipped) + (1 - y) * np.log(1 - clipped))),
            "accuracy": accuracy,
            "baseline_brier": float(np.mean((baseline_probability - y) ** 2)),
            "baseline_accuracy": baseline_accuracy,
            "positive_rate": float(np.mean(y)),
        }

    def train_if_ready(self, symbol: str, interval: str) -> dict[str, Any]:
        market, timeframe = str(symbol).strip().upper(), str(interval).strip()
        with self.lock:
            x, y, stamps = self._labelled_dataset(market, timeframe)
            unique_candles = len(set(stamps))
            state = self.db.execute(
                "SELECT last_training_count,control_level FROM easy_learning_state WHERE symbol=? AND interval=?",
                (market, timeframe),
            ).fetchone()
            last_count = int(state["last_training_count"]) if state else 0
            control_level = int(state["control_level"]) if state else 0
            if len(y) < MIN_LOGISTIC_LABELS or len(y) - last_count < TRAINING_BATCH_NEW_LABELS:
                return {"status": "WARMUP", "labelled_samples": int(len(y)),
                        "minimum_samples": MIN_LOGISTIC_LABELS}
            if unique_candles < MIN_LOGISTIC_LABELS or len(np.unique(y)) < 2:
                return {"status": "WARMUP", "labelled_candles": unique_candles,
                        "reason": "both outcome classes and enough distinct candles are required"}
            split_at = max(1, int(unique_candles * 0.80))
            ordered_unique = sorted(set(stamps))
            train_stamps = set(ordered_unique[:split_at])
            train_mask = np.asarray([stamp in train_stamps for stamp in stamps])
            test_mask = ~train_mask
            x_train, y_train = x[train_mask], y[train_mask]
            x_test, y_test = x[test_mask], y[test_mask]
            if len(y_test) < 20 or len(np.unique(y_train)) < 2:
                return {"status": "WARMUP", "reason": "chronological holdout is too small or one-class"}
            logistic = self._fit_logistic(x_train, y_train)
            logistic_probability = _sigmoid(
                x_test @ np.asarray(logistic["weights"]) + float(logistic["bias"])
            )
            logistic_metrics = self._metrics(logistic_probability, y_test)
            chosen_kind, chosen_model, chosen_metrics = "LOGISTIC", logistic, logistic_metrics

            if unique_candles >= MIN_MLP_LABELS and min(np.sum(y_train == 0), np.sum(y_train == 1)) >= 100:
                mlp = self._fit_mlp(x_train, y_train)
                mlp_probability = np.asarray([
                    self._predict_model("MLP_2_HIDDEN", mlp, row) for row in x_test
                ])
                mlp_metrics = self._metrics(mlp_probability, y_test)
                if mlp_metrics["brier"] <= logistic_metrics["brier"] - 0.005:
                    chosen_kind, chosen_model, chosen_metrics = "MLP_2_HIDDEN", mlp, mlp_metrics

            accepted = (
                chosen_metrics["brier"] < chosen_metrics["baseline_brier"]
                and chosen_metrics["accuracy"] >= chosen_metrics["baseline_accuracy"]
                and min(np.sum(y_train == 0), np.sum(y_train == 1)) >= 25
            )
            current = self._active_model(market, timeframe)
            if current and chosen_metrics["brier"] >= float(current["metrics"].get("brier", 1.0)):
                accepted = False
            version = f"{market}-{timeframe}-{int(time.time() * 1000)}"
            self.db.execute("""
                INSERT INTO easy_learning_models(
                    model_version,symbol,interval,model_kind,feature_names_json,model_json,
                    metrics_json,sample_count,validation_count,active,created_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                version, market, timeframe, chosen_kind, _json(list(FEATURE_NAMES)),
                _json(chosen_model), _json({**chosen_metrics, "accepted": accepted,
                                            "chronological_holdout_pct": 20.0,
                                            "deep_learning_candidate_tested": unique_candles >= MIN_MLP_LABELS}),
                int(len(y_train)), int(len(y_test)), 0, int(time.time() * 1000),
            ))
            if accepted:
                self.db.execute(
                    "UPDATE easy_learning_models SET active=0 WHERE symbol=? AND interval=?",
                    (market, timeframe),
                )
                self.db.execute(
                    "UPDATE easy_learning_models SET active=1 WHERE model_version=?",
                    (version,),
                )
            self.db.execute("""
                INSERT INTO easy_learning_state(symbol,interval,phase,control_level,last_training_count,updated_at_ms)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(symbol,interval) DO UPDATE SET
                    last_training_count=excluded.last_training_count,
                    updated_at_ms=excluded.updated_at_ms
            """, (market, timeframe, "REPLAY", control_level, int(len(y)), int(time.time() * 1000)))
            self._event("MODEL_TRAINED", {
                "model_version": version, "model_kind": chosen_kind,
                "accepted": accepted, "metrics": chosen_metrics,
                "training_count": int(len(y_train)), "validation_count": int(len(y_test)),
                "distinct_candles": unique_candles,
            })
            self.db.commit()
        state_snapshot = self.snapshot(market, timeframe)
        return {
            "status": "ACCEPTED" if accepted else "CHALLENGER_REJECTED",
            "model_version": version,
            "model_kind": chosen_kind,
            "metrics": chosen_metrics,
            "learning_state": state_snapshot,
        }

    def _derive_phase(self, symbol: str, interval: str, control_level: int) -> str:
        row = self.db.execute("""
            SELECT COUNT(*) AS n FROM easy_learning_samples
            WHERE symbol=? AND interval=? AND label IS NOT NULL
        """, (symbol, interval)).fetchone()
        count = int(row["n"] or 0)
        model = self._active_model(symbol, interval)
        if control_level >= 4:
            return "OBSERVE"
        if count < MIN_LOGISTIC_LABELS:
            return "OBSERVE"
        if not model:
            return "REPLAY"
        metrics = model["metrics"]
        if count >= 1000 and float(metrics.get("accuracy", 0.0)) >= 0.55:
            return "DEMO_REVIEW_READY"
        if count >= 600 and float(metrics.get("accuracy", 0.0)) >= 0.53:
            return "PAPER_STRICT"
        if count >= 300:
            return "PAPER_LEARNING"
        return "REPLAY"

    def snapshot(self, symbol: str, interval: str) -> dict[str, Any]:
        market, timeframe = str(symbol).strip().upper(), str(interval).strip()
        with self.lock:
            counts = self.db.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN label IS NOT NULL THEN 1 ELSE 0 END) AS labelled
                FROM easy_learning_samples WHERE symbol=? AND interval=?
            """, (market, timeframe)).fetchone()
            total = int(counts["total"] or 0)
            labelled = int(counts["labelled"] or 0)
            state = self.db.execute(
                "SELECT control_level FROM easy_learning_state WHERE symbol=? AND interval=?",
                (market, timeframe),
            ).fetchone()
            control_level = int(state["control_level"]) if state else 0
            error_rows = self.db.execute("""
                SELECT error_flag,label FROM easy_learning_samples
                WHERE symbol=? AND interval=? AND label IS NOT NULL
                ORDER BY outcome_at_ms DESC LIMIT 50
            """, (market, timeframe)).fetchall()
            prediction_rows = [row for row in error_rows if row["error_flag"] is not None]
            error_rate = (
                sum(int(row["error_flag"] or 0) for row in prediction_rows) / len(prediction_rows)
                if prediction_rows else None
            )
            loss_rate = (
                sum(1 for row in error_rows if int(row["label"] or 0) == 0) / len(error_rows)
                if error_rows else None
            )
            model = self._active_model(market, timeframe)
            phase = self._derive_phase(market, timeframe, control_level)
            probability_threshold = min(0.85, 0.55 + control_level * 0.05)
            eligible = (
                phase in {"PAPER_LEARNING", "PAPER_STRICT", "DEMO_REVIEW_READY"}
                and control_level < 4
                and model is not None
            )
            self.db.execute("""
                INSERT INTO easy_learning_state(symbol,interval,phase,control_level,updated_at_ms)
                VALUES (?,?,?,?,?)
                ON CONFLICT(symbol,interval) DO UPDATE SET phase=excluded.phase,
                    control_level=excluded.control_level,updated_at_ms=excluded.updated_at_ms
            """, (market, timeframe, phase, control_level, int(time.time() * 1000)))
            self.db.commit()
        return {
            "symbol": market,
            "interval": timeframe,
            "phase": phase,
            "phase_fa": PHASE_LABELS_FA.get(phase, phase),
            "control_level": control_level,
            "control_label_fa": CONTROL_LABELS_FA.get(control_level, "سطح کنترل نامشخص"),
            "sample_count": total,
            "labelled_count": labelled,
            "unlabelled_count": max(0, total - labelled),
            "rolling_prediction_error_rate": round(error_rate, 4) if error_rate is not None else None,
            "rolling_loss_rate": round(loss_rate, 4) if loss_rate is not None else None,
            "active_model_version": model["model_version"] if model else None,
            "active_model_kind": model["model_kind"] if model else None,
            "active_model_metrics": model["metrics"] if model else None,
            "minimum_probability_threshold": probability_threshold,
            "paper_learning_eligible": eligible,
            "demo_or_live_orders_enabled": False,
            "ai_dependency": "NONE",
            "storage_backend": self._storage_backend(),
            "note_fa": (
                "حافظه و آموزش مستقل از Nemotron است. در فاز یادگیری هیچ سفارش صرافی ارسال نمی‌شود. "
                "آمادگی Demo به معنی مجوز اجرای خودکار یا معامله Live نیست."
            ),
        }

    @staticmethod
    def _storage_backend() -> str:
        try:
            from .database import storage_backend
            return storage_backend()
        except Exception:
            return "UNKNOWN"

    def close(self) -> None:
        close = getattr(self.db, "close", None)
        if callable(close):
            close()


_DEFAULT_MEMORY = None
_DEFAULT_MEMORY_LOCK = threading.Lock()


def get_default_memory() -> EasyTraderLearningMemory:
    """Return one process-local repository backed by configured Turso or local SQLite."""
    global _DEFAULT_MEMORY
    with _DEFAULT_MEMORY_LOCK:
        if _DEFAULT_MEMORY is None:
            from .database import connect_database
            _DEFAULT_MEMORY = EasyTraderLearningMemory(connect_database())
        return _DEFAULT_MEMORY
