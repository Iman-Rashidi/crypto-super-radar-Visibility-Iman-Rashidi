"""Conservative offline BloFin Demo Futures matching simulator.

This module never connects to BloFin and cannot submit real exchange orders.
OHLCV data cannot reproduce queue position, intrabar order flow, or historical
mark/index differences exactly; trade-through fills and stop-first precedence
are deliberately conservative approximations.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Iterable, Mapping
import math


class SimulationError(ValueError):
    """Invalid simulator input or incomplete market data."""


class InsufficientEquityForRisk(SimulationError):
    """The exchange minimum would breach the requested risk budget."""


def _dec(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise SimulationError(f"{name} is invalid") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise SimulationError(f"{name} must be finite" + (" and positive" if positive else ""))
    return result


def _float(value: Any, name: str) -> float:
    return float(_dec(value, name))


@dataclass(frozen=True)
class InstrumentRules:
    inst_id: str
    contract_value: Decimal
    min_size: Decimal
    lot_size: Decimal
    tick_size: Decimal
    max_market_size: Decimal | None = None

    @classmethod
    def from_exchange(cls, row: Mapping[str, Any]) -> "InstrumentRules":
        """Build rules from BloFin GET /api/v1/market/instruments, never guesses."""
        inst_id = str(row.get("instId") or "").strip().upper()
        if not inst_id.endswith("-USDT"):
            raise SimulationError("Only USDT-settled linear futures are supported")
        if str(row.get("contractType") or "").lower() != "linear" or str(row.get("settleCurrency") or "").upper() != "USDT":
            raise SimulationError("Instrument must be a USDT linear contract")
        return cls(
            inst_id=inst_id,
            contract_value=_dec(row.get("contractValue"), "contractValue", positive=True),
            min_size=_dec(row.get("minSize"), "minSize", positive=True),
            lot_size=_dec(row.get("lotSize"), "lotSize", positive=True),
            tick_size=_dec(row.get("tickSize"), "tickSize", positive=True),
            max_market_size=(
                _dec(row["maxMarketSize"], "maxMarketSize", positive=True)
                if row.get("maxMarketSize") not in (None, "") else None
            ),
        )


@dataclass(frozen=True)
class CostModel:
    maker_fee: float = 0.0002
    taker_fee: float = 0.0006
    stop_slippage: float = 0.0003
    spread_fraction: float = 0.0002
    funding_reserve: float = 0.0002
    min_net_rr: float = 1.8
    max_spread_fraction: float = 0.0004
    entry_post_only: bool = True
    require_funding_history: bool = True

    def __post_init__(self) -> None:
        for name in ("maker_fee", "taker_fee", "stop_slippage", "spread_fraction", "funding_reserve", "max_spread_fraction"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or value > 0.05:
                raise ValueError(f"{name} is outside the allowed range")
        if not math.isfinite(self.min_net_rr) or self.min_net_rr < 1.0:
            raise ValueError("min_net_rr must be >= 1")


@dataclass(frozen=True)
class PositionSize:
    quantity_contracts: Decimal
    notional_usdt: Decimal
    risk_budget_usdt: Decimal
    stop_risk_usdt: Decimal


def size_position(
    *, equity_usdt: Any, risk_fraction: float, entry: Any, stop: Any,
    rules: InstrumentRules,
) -> PositionSize:
    """Floor to lot size; never round the exchange minimum upward."""
    equity = _dec(equity_usdt, "equity_usdt", positive=True)
    entry_d = _dec(entry, "entry", positive=True)
    stop_d = _dec(stop, "stop", positive=True)
    if not math.isfinite(risk_fraction) or not 0 < risk_fraction <= 0.0025:
        raise SimulationError("risk_fraction must be in (0, 0.25%]")
    distance = abs(entry_d - stop_d)
    if distance <= 0:
        raise SimulationError("entry and stop must differ")
    budget = equity * Decimal(str(risk_fraction))
    loss_per_contract = distance * rules.contract_value
    raw_qty = budget / loss_per_contract
    quantity = (raw_qty / rules.lot_size).to_integral_value(rounding=ROUND_FLOOR) * rules.lot_size
    if quantity < rules.min_size:
        raise InsufficientEquityForRisk(
            f"INSUFFICIENT_EQUITY_FOR_RISK: calculated={quantity}, minSize={rules.min_size}"
        )
    if rules.max_market_size is not None:
        quantity = min(quantity, (rules.max_market_size / rules.lot_size).to_integral_value(rounding=ROUND_FLOOR) * rules.lot_size)
    if quantity < rules.min_size:
        raise InsufficientEquityForRisk("INSUFFICIENT_EQUITY_FOR_RISK: maxMarketSize is below minSize")
    actual_risk = quantity * loss_per_contract
    if actual_risk > budget:
        raise SimulationError("lot rounding would exceed the risk budget")
    notional = quantity * rules.contract_value * entry_d
    return PositionSize(quantity, notional, budget, actual_risk)


def net_reward_risk(
    *, entry: Any, stop: Any, target: Any, rules: InstrumentRules,
    costs: CostModel, quantity: Any = 1,
) -> dict[str, float]:
    """Return maker-TP and market-SL net R:R scenarios with explicit cost items."""
    e, s, t, q = (_dec(entry, "entry", positive=True), _dec(stop, "stop", positive=True),
                  _dec(target, "target", positive=True), _dec(quantity, "quantity", positive=True))
    if not ((s < e < t) or (t < e < s)):
        raise SimulationError("stop/entry/target direction is inconsistent")
    cv = rules.contract_value
    risk = abs(e - s) * cv * q
    reward = abs(t - e) * cv * q
    entry_notional = e * cv * q
    exit_tp_notional = t * cv * q
    exit_sl_notional = s * cv * q
    spread_cost = entry_notional * Decimal(str(costs.spread_fraction))
    funding_cost = entry_notional * Decimal(str(costs.funding_reserve))
    entry_fee = entry_notional * Decimal(str(costs.maker_fee if costs.entry_post_only else costs.taker_fee))
    tp_fee = exit_tp_notional * Decimal(str(costs.maker_fee))
    sl_fee = exit_sl_notional * Decimal(str(costs.taker_fee))
    slippage_cost = exit_sl_notional * Decimal(str(costs.stop_slippage))
    tp_total_cost = entry_fee + tp_fee + spread_cost + funding_cost
    sl_total_cost = entry_fee + sl_fee + slippage_cost + spread_cost + funding_cost
    net_reward = reward - tp_total_cost
    net_risk = risk + sl_total_cost
    rr = float(net_reward / net_risk) if net_reward > 0 and net_risk > 0 else 0.0
    return {
        "gross_reward_usdt": float(reward), "gross_risk_usdt": float(risk),
        "entry_fee_usdt": float(entry_fee), "tp_fee_usdt": float(tp_fee),
        "sl_fee_usdt": float(sl_fee), "spread_reserve_usdt": float(spread_cost),
        "funding_reserve_usdt": float(funding_cost), "slippage_reserve_usdt": float(slippage_cost),
        "tp_net_reward_usdt": float(reward - tp_total_cost),
        "sl_net_risk_usdt": float(risk + sl_total_cost), "net_rr": rr,
    }


def _timestamp_ms(value: Any) -> int:
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1000)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SimulationError("candle timestamp is invalid") from exc


@dataclass
class TradeResult:
    status: str
    exit_reason: str
    quantity_contracts: float
    filled_entry: float | None
    average_exit: float | None
    net_pnl_usdt: float
    fees_usdt: float
    funding_usdt: float
    bars_held: int
    tp1_filled: bool
    net_rr_at_entry: float | None
    diagnostics: dict[str, Any]


class VirtualBloFin:
    """Candle-driven simulator. It has no real network or order methods."""

    def __init__(self, *, costs: CostModel | None = None, max_holding_bars: int = 12):
        self.costs = costs or CostModel()
        if not 1 <= max_holding_bars <= 96:
            raise ValueError("max_holding_bars must be 1..96")
        self.max_holding_bars = int(max_holding_bars)

    def simulate_post_only_trade(
        self, *, bars: Iterable[Mapping[str, Any]], start_index: int,
        direction: str, limit_price: Any, stop_price: Any, target_price: Any,
        equity_usdt: Any, risk_fraction: float, rules: InstrumentRules,
        funding_events: Iterable[Mapping[str, Any]], spread_fraction: float | None = None,
    ) -> TradeResult:
        rows = list(bars)
        side = str(direction).upper()
        if side not in {"LONG", "SHORT"}:
            raise SimulationError("direction must be LONG or SHORT")
        if start_index < 0 or start_index + 1 >= len(rows):
            raise SimulationError("not enough future candles to simulate an entry")
        if spread_fraction is None:
            spread_fraction = self.costs.spread_fraction
        if not math.isfinite(spread_fraction) or spread_fraction > self.costs.max_spread_fraction:
            return TradeResult("REJECTED", "WIDE_SPREAD", 0, None, None, 0, 0, 0, 0, False, None,
                               {"spread_fraction": spread_fraction, "max_spread_fraction": self.costs.max_spread_fraction})
        entry = _float(limit_price, "limit_price")
        stop = _float(stop_price, "stop_price")
        target = _float(target_price, "target_price")
        next_bar = rows[start_index + 1]
        low = _float(next_bar.get("low"), "next.low")
        high = _float(next_bar.get("high"), "next.high")
        tick = float(rules.tick_size)
        filled = (low <= entry - tick) if side == "LONG" else (high >= entry + tick)
        if not filled:
            return TradeResult("UNFILLED", "LIMIT_EXPIRED_ONE_BAR", 0, None, None, 0, 0, 0, 1, False, None,
                               {"entry_limit": entry, "fill_rule": "one_tick_trade_through"})
        size = size_position(equity_usdt=equity_usdt, risk_fraction=risk_fraction,
                             entry=entry, stop=stop, rules=rules)
        cc = CostModel(**{**asdict(self.costs), "spread_fraction": spread_fraction})
        rr_items = net_reward_risk(entry=entry, stop=stop, target=target, rules=rules,
                                   costs=cc, quantity=size.quantity_contracts)
        if rr_items["net_rr"] < cc.min_net_rr:
            return TradeResult("REJECTED", "NET_RR_BELOW_MINIMUM", float(size.quantity_contracts),
                               entry, None, 0, 0, 0, 1, False, rr_items["net_rr"], rr_items)

        qty_total = float(size.quantity_contracts)
        half_decimal = (size.quantity_contracts * Decimal("0.5") / rules.lot_size).to_integral_value(rounding=ROUND_FLOOR) * rules.lot_size
        tp1_qty = float(half_decimal) if half_decimal >= rules.min_size else 0.0
        qty_left = qty_total
        cv = float(rules.contract_value)
        entry_notional = entry * qty_total * cv
        entry_fee = entry_notional * cc.maker_fee
        fees = entry_fee
        funding_total = 0.0
        realized_gross = 0.0
        tp1 = False
        current_stop = stop
        initial_risk_per_unit = abs(entry - stop)
        net_risk_per_unit = initial_risk_per_unit + entry * (cc.maker_fee + cc.taker_fee + cc.stop_slippage + cc.spread_fraction + cc.funding_reserve)
        tp1_net_reward_distance = 1.2 * net_risk_per_unit + entry * (cc.maker_fee + cc.maker_fee + cc.spread_fraction + cc.funding_reserve)
        tp1_price = entry + tp1_net_reward_distance * (1 if side == "LONG" else -1)
        risk_free_buffer = entry * (cc.maker_fee + cc.maker_fee + cc.spread_fraction + cc.funding_reserve)
        be_stop = entry + risk_free_buffer if side == "LONG" else entry - risk_free_buffer
        funding = sorted((dict(item) for item in funding_events), key=lambda x: _timestamp_ms(x.get("fundingTime")))
        if cc.require_funding_history and not funding:
            raise SimulationError("funding history is unavailable; funding cannot be assumed zero")
        entry_ts = _timestamp_ms(next_bar.get("ts"))
        fill_bar_close_ts = int(next_bar.get("close_ts") or (entry_ts + 15 * 60_000))
        fill_mark_high, fill_mark_low = next_bar.get("mark_high"), next_bar.get("mark_low")
        if fill_mark_high is None or fill_mark_low is None:
            raise SimulationError("historical mark-price OHLC is required on the entry-fill candle")
        fill_stop_hit = (_float(fill_mark_low, "fill_mark_low") <= stop) if side == "LONG" else (_float(fill_mark_high, "fill_mark_high") >= stop)
        if fill_stop_hit:
            fill_exit = stop * (1 - cc.stop_slippage if side == "LONG" else 1 + cc.stop_slippage)
            fill_funding = 0.0
            for event in funding:
                fts = _timestamp_ms(event.get("fundingTime"))
                if entry_ts < fts <= fill_bar_close_ts:
                    if event.get("fundingRate") is None:
                        raise SimulationError("fundingRate is missing; funding cannot be assumed zero")
                    fill_funding += entry * qty_total * cv * _float(event.get("fundingRate"), "fundingRate") * (1 if side == "LONG" else -1)
            fill_exit_fee = fill_exit * qty_total * cv * cc.taker_fee
            fill_gross = (fill_exit - entry) * qty_total * cv * (1 if side == "LONG" else -1)
            fill_fees = entry_fee + fill_exit_fee
            fill_net = fill_gross - fill_fees - fill_funding
            return TradeResult("CLOSED", "SL", qty_total, entry, fill_exit, fill_net, fill_fees,
                               fill_funding, 0, False, rr_items["net_rr"], {
                                   **rr_items, "entry_order_type": "post_only_limit",
                                   "entry_fill_model": "one_tick_trade_through",
                                   "exit_reason": "SL", "mark_price_trigger_model": "OHLC_MARK_STOP_FIRST",
                                   "same_bar_tp_sl_precedence": "SL_FIRST_FILL_CANDLE",
                                   "gross_realized_pnl_usdt": fill_gross, "total_fees_usdt": fill_fees,
                                   "funding_paid_usdt": fill_funding, "net_pnl_usdt": fill_net,
                                   "entry_ts_ms": entry_ts, "exit_ts_ms": fill_bar_close_ts,
                               })
        exit_price = None
        exit_reason = "TIME_EXIT"
        exit_ts = entry_ts
        held = 0
        for idx in range(start_index + 2, min(len(rows), start_index + 2 + self.max_holding_bars)):
            bar = rows[idx]
            held += 1
            ts = _timestamp_ms(bar.get("ts"))
            close_ts = int(bar.get("close_ts") or (ts + 15 * 60_000))
            mark_high, mark_low = bar.get("mark_high"), bar.get("mark_low")
            if mark_high is None or mark_low is None:
                raise SimulationError("historical mark-price OHLC is required; last-price candles are not a substitute")
            mh, ml = _float(mark_high, "mark_high"), _float(mark_low, "mark_low")
            last_high, last_low = _float(bar.get("high"), "last_high"), _float(bar.get("low"), "last_low")
            for event in funding:
                fts = _timestamp_ms(event.get("fundingTime"))
                if entry_ts < fts <= close_ts and event.get("_applied") is not True:
                    if event.get("fundingRate") is None:
                        raise SimulationError("fundingRate is missing; funding cannot be assumed zero")
                    rate = _float(event.get("fundingRate"), "fundingRate")
                    notional_now = entry * qty_total * cv
                    funding_total += notional_now * rate * (1 if side == "LONG" else -1)
                    event["_applied"] = True
            stop_hit = (ml <= current_stop) if side == "LONG" else (mh >= current_stop)
            tp1_hit = tp1_qty > 0 and not tp1 and (
                last_high >= tp1_price + tick if side == "LONG" else last_low <= tp1_price - tick
            )
            target_hit = (last_high >= target + tick) if side == "LONG" else (last_low <= target - tick)
            if stop_hit:
                exit_price = current_stop * (1 - cc.stop_slippage if side == "LONG" else 1 + cc.stop_slippage)
                exit_reason, exit_ts = "SL", close_ts
                break
            if not tp1 and tp1_hit:
                close_qty = min(tp1_qty, qty_left)
                realized_gross += (tp1_price - entry) * close_qty * cv * (1 if side == "LONG" else -1)
                fees += tp1_price * close_qty * cv * cc.maker_fee
                qty_left -= close_qty
                tp1 = True
                current_stop = max(current_stop, be_stop) if side == "LONG" else min(current_stop, be_stop)
            favourable_1_5r = (last_high >= entry + 1.5 * initial_risk_per_unit) if side == "LONG" else (last_low <= entry - 1.5 * initial_risk_per_unit)
            if tp1 and favourable_1_5r:
                close = _float(bar.get("close"), "close")
                atr_value = bar.get("atr_15m")
                if atr_value is not None:
                    atr_v = _float(atr_value, "atr_15m")
                    candidate = close - 1.8 * atr_v if side == "LONG" else close + 1.8 * atr_v
                    current_stop = max(current_stop, candidate) if side == "LONG" else min(current_stop, candidate)
            if target_hit and qty_left > 0:
                exit_price, exit_reason, exit_ts = target, "TP", close_ts
                break
        if exit_price is None:
            last = rows[min(start_index + 1 + self.max_holding_bars, len(rows) - 1)]
            exit_price = _float(last.get("close"), "time_exit_close")
            exit_price *= (1 - cc.stop_slippage if side == "LONG" else 1 + cc.stop_slippage)
            exit_reason = "TIME_EXIT_MARKET_FALLBACK"
            exit_ts = int(last.get("close_ts") or (_timestamp_ms(last.get("ts")) + 15 * 60_000))
            held = min(self.max_holding_bars, len(rows) - start_index - 2)
        realized_gross += (exit_price - entry) * qty_left * cv * (1 if side == "LONG" else -1)
        exit_fee_rate = cc.maker_fee if exit_reason == "TP" else cc.taker_fee
        fees += exit_price * qty_left * cv * exit_fee_rate
        net = realized_gross - fees - funding_total
        diagnostics = {
            **rr_items,
            "entry_order_type": "post_only_limit",
            "entry_fill_model": "one_tick_trade_through",
            "exit_reason": exit_reason,
            "mark_price_trigger_model": "OHLC_MARK_STOP_FIRST",
            "same_bar_tp_sl_precedence": "SL_FIRST",
            "funding_event_count": sum(1 for x in funding if x.get("_applied") is True),
            "gross_realized_pnl_usdt": realized_gross,
            "total_fees_usdt": fees,
            "funding_paid_usdt": funding_total,
            "net_pnl_usdt": net,
            "remaining_qty": qty_left,
            "entry_ts_ms": entry_ts,
            "exit_ts_ms": exit_ts,
        }
        return TradeResult("CLOSED", exit_reason, qty_total, entry, exit_price, net, fees,
                           funding_total, held, tp1, rr_items["net_rr"], diagnostics)
