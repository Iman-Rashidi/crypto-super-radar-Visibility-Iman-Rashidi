"""Deterministic, exchange-agnostic position exit rules."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone
from typing import Any, Mapping

class ActionType(str, Enum):
    HOLD = "HOLD"
    MOVE_STOP = "MOVE_STOP"
    CLOSE = "CLOSE"

@dataclass(frozen=True)
class Action:
    action: ActionType
    reason: str
    stop_loss: float | None = None

class PositionManager:
    def on_tick(self, position: Mapping[str, Any], candle: Mapping[str, Any], indicators: Mapping[str, Any]) -> Action:
        side = str(position.get("side", "")).upper()
        if side not in {"LONG", "SHORT"}: return Action(ActionType.HOLD, "invalid_side")
        try:
            entry=float(position["entry_price"]); initial=float(position.get("initial_stop_loss", position.get("stop_loss")))
            current=float(candle.get("close", candle.get("price"))); atr=float(indicators.get("atr14", 0))
            stop=float(position.get("stop_loss", initial)); opened=position.get("entry_time")
            opened_dt=datetime.fromisoformat(str(opened).replace("Z", "+00:00")) if not isinstance(opened, datetime) else opened
            r=abs(entry-initial)
            if min(entry,current,r) <= 0: return Action(ActionType.HOLD, "invalid_price_or_risk")
        except (KeyError, TypeError, ValueError, OverflowError): return Action(ActionType.HOLD, "incomplete_position_data")
        signed=(current-entry) if side=="LONG" else (entry-current)
        multiple=signed/r
        previous_rsi=indicators.get("previous_rsi14"); current_rsi=indicators.get("rsi14")
        crossed=False
        try:
            crossed=(float(previous_rsi)>=50>float(current_rsi)) if side=="LONG" else (float(previous_rsi)<=50<float(current_rsi))
        except (TypeError, ValueError): pass
        if crossed and multiple>=0.5: return Action(ActionType.CLOSE, "MOMENTUM_EXIT")
        swing=indicators.get("last_confirmed_swing_low" if side=="LONG" else "last_confirmed_swing_high")
        try:
            if swing is not None and (current<float(swing) if side=="LONG" else current>float(swing)):
                return Action(ActionType.CLOSE, "STRUCTURE_STOP")
        except (TypeError, ValueError): pass
        now=indicators.get("now")
        now_dt=datetime.fromisoformat(str(now).replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
        if opened_dt.tzinfo is None: opened_dt=opened_dt.replace(tzinfo=timezone.utc)
        if (now_dt-opened_dt).total_seconds()>48*3600 and multiple<0.3: return Action(ActionType.CLOSE, "TIME_STOP")
        if multiple>=1.5 and atr>0:
            candidate=current-1.5*atr if side=="LONG" else current+1.5*atr
            improves=candidate>stop if side=="LONG" else candidate<stop
            if improves: return Action(ActionType.MOVE_STOP, "ATR_TRAILING_1_5R", float(candidate))
        if multiple>=1.0:
            improves=entry>stop if side=="LONG" else entry<stop
            if improves: return Action(ActionType.MOVE_STOP, "BREAK_EVEN_1R", entry)
        return Action(ActionType.HOLD, "NO_EXIT_RULE_TRIGGERED")
