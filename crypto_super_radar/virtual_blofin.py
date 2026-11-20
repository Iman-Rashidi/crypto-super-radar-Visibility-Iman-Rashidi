"""Conservative offline BloFin Demo Futures matching simulator.

This module never connects to BloFin and cannot submit real exchange orders.
OHLCV data cannot reproduce queue position, intrabar order flow, or historical
mark/index differences exactly; trade-through fills and stop-first precedence
are deliberately conservative approximations.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
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
    rules: InstrumentRules, costs: CostModel | None = None,
) -> PositionSize:
    """Floor to lot size against all-in modeled stop loss, never the exchange minimum.

    The risk budget includes adverse stop slippage, entry and stop fees, spread,
    and the configured funding reserve. It does not size from price distance alone.
    """
    equity = _dec(equity_usdt, "equity_usdt", positive=True)
    entry_d = _dec(entry, "entry", positive=True)
    stop_d = _dec(stop, "stop", positive=True)
    if not math.isfinite(risk_fraction) or not 0 < risk_fraction <= 0.0025:
        raise SimulationError("risk_fraction must be in (0, 0.25%]")
    if entry_d == stop_d:
        raise SimulationError("entry and stop must differ")
    cost_model = costs or CostModel()
    is_long = stop_d < entry_d
    slip = Decimal(str(cost_model.stop_slippage))
    stop_fill = stop_d * (Decimal("1") - slip if is_long else Decimal("1") + slip)
    contract = rules.contract_value
    entry_notional_per_contract = entry_d * contract
    stop_notional_per_contract = stop_fill * contract
    entry_fee_rate = cost_model.maker_fee if cost_model.entry_post_only else cost_model.taker_fee
    entry_fee = entry_notional_per_contract * Decimal(str(entry_fee_rate))
    stop_fee = stop_notional_per_contract * Decimal(str(cost_model.taker_fee))
    spread_reserve = entry_notional_per_contract * Decimal(str(cost_model.spread_fraction))
    funding_reserve = entry_notional_per_contract * Decimal(str(cost_model.funding_reserve))
    gross_loss_at_slipped_fill = abs(entry_d - stop_fill) * contract
    loss_per_contract = (
        gross_loss_at_slipped_fill + entry_fee + stop_fee + spread_reserve + funding_reserve
    )
    if loss_per_contract <= 0:
        raise SimulationError("all-in loss per contract is invalid")
    budget = equity * Decimal(str(risk_fraction))
    raw_qty = budget / loss_per_contract
    quantity = (raw_qty / rules.lot_size).to_integral_value(rounding=ROUND_FLOOR) * rules.lot_size
    if quantity < rules.min_size:
        raise InsufficientEquityForRisk(
            f"INSUFFICIENT_EQUITY_FOR_RISK: calculated={quantity}, minSize={rules.min_size}"
        )
    if rules.max_market_size is not None:
        quantity = min(
            quantity,
            (rules.max_market_size / rules.lot_size).to_integral_value(rounding=ROUND_FLOOR)
            * rules.lot_size,
        )
    if quantity < rules.min_size:
        raise InsufficientEquityForRisk("INSUFFICIENT_EQUITY_FOR_RISK: maxMarketSize is below minSize")
    actual_risk = quantity * loss_per_contract
    if actual_risk > budget:
        raise SimulationError("all-in lot-rounded risk would exceed the risk budget")
    notional = quantity * contract * entry_d
    return PositionSize(quantity, notional, budget, actual_risk)


def _snap_price(price: Any, tick_size: Decimal, rounding: str, name: str) -> float:
    value = _dec(price, name, positive=True)
    ticks = (value / tick_size).to_integral_value(
        rounding=ROUND_FLOOR if rounding == "FLOOR" else ROUND_CEILING
    )
    snapped = ticks * tick_size
    if snapped <= 0:
        raise SimulationError(f"{name} is invalid after tick-size rounding")
    return float(snapped)


def _snap_trade_prices(
    *, direction: str, entry: Any, stop: Any, target: Any, tick_size: Decimal
) -> tuple[float, float, float]:
    """Snap post-only entry, protective stop, and profit target conservatively."""
    side = str(direction).upper()
    if side not in {"LONG", "SHORT"}:
        raise SimulationError("direction must be LONG or SHORT")
    if side == "LONG":
        return (
            _snap_price(entry, tick_size, "FLOOR", "entry"),
            _snap_price(stop, tick_size, "FLOOR", "stop"),
            _snap_price(target, tick_size, "CEILING", "target"),
        )
    return (
        _snap_price(entry, tick_size, "CEILING", "entry"),
        _snap_price(stop, tick_size, "CEILING", "stop"),
        _snap_price(target, tick_size, "FLOOR", "target"),
    )


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


def _net_breakeven_limit_price(
    *,
    entry: float,
    quantity: float,
    contract_value: float,
    side: str,
    accumulated_fees: float,
    funding_paid: float,
    spread_fraction: float,
    exit_fee_rate: float,
    tick_size: float,
) -> float:
    """Solve a conservative net break-even limit including the selected exit fee.

    A long closes with a sell limit rounded upward; a short closes with a buy
    limit rounded downward. Rounding therefore never makes the modeled exit
    less protective than the calculated net break-even.
    """
    entry_d = _dec(entry, "entry", positive=True)
    quantity_d = _dec(quantity, "quantity", positive=True)
    contract_d = _dec(contract_value, "contract_value", positive=True)
    tick_d = _dec(tick_size, "tick_size", positive=True)
    fee_d = _dec(accumulated_fees, "accumulated_fees")
    funding_d = _dec(funding_paid, "funding_paid")
    spread_d = _dec(spread_fraction, "spread_fraction")
    exit_fee_d = _dec(exit_fee_rate, "exit_fee_rate")
    if side not in {"LONG", "SHORT"}:
        raise SimulationError("side must be LONG or SHORT")
    if not 0 <= spread_d <= Decimal("0.05") or not 0 <= exit_fee_d < Decimal("1"):
        raise SimulationError("time-exit cost inputs are outside safe bounds")
    signed = Decimal("1") if side == "LONG" else Decimal("-1")
    position_value = quantity_d * contract_d
    spread_cost = entry_d * position_value * spread_d
    numerator = entry_d * position_value * signed + fee_d + funding_d + spread_cost
    denominator = position_value * (signed - exit_fee_d)
    if denominator == 0:
        raise SimulationError("net break-even denominator is zero")
    raw_price = numerator / denominator
    if not raw_price.is_finite() or raw_price <= 0:
        raise SimulationError("net break-even limit price is invalid")
    rounding = ROUND_CEILING if side == "LONG" else ROUND_FLOOR
    rounded_ticks = (raw_price / tick_d).to_integral_value(rounding=rounding)
    limit_price = rounded_ticks * tick_d
    if limit_price <= 0:
        raise SimulationError("rounded net break-even limit price is invalid")
    return float(limit_price)


@dataclass
class TradeResult:
    status: str
    exit_reason: str
    quantity_contracts: float
    filled_entry: float | None
    average_exit: float | None
    net_pnl_usdt: float | None
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
        entry, stop, target = _snap_trade_prices(
            direction=side,
            entry=limit_price,
            stop=stop_price,
            target=target_price,
            tick_size=rules.tick_size,
        )
        next_bar = rows[start_index + 1]
        low = _float(next_bar.get("low"), "next.low")
        high = _float(next_bar.get("high"), "next.high")
        tick = float(rules.tick_size)
        filled = (low <= entry - tick) if side == "LONG" else (high >= entry + tick)
        if not filled:
            return TradeResult("UNFILLED", "LIMIT_EXPIRED_ONE_BAR", 0, None, None, 0, 0, 0, 1, False, None,
                               {"entry_limit": entry, "fill_rule": "one_tick_trade_through"})
        cc = CostModel(**{**asdict(self.costs), "spread_fraction": spread_fraction})
        size = size_position(
            equity_usdt=equity_usdt,
            risk_fraction=risk_fraction,
            entry=entry,
            stop=stop,
            rules=rules,
            costs=cc,
        )
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
        spread_cost = entry_notional * cc.spread_fraction
        fees = entry_fee
        funding_total = 0.0
        realized_gross = 0.0
        tp1 = False
        current_stop = stop
        initial_risk_per_unit = abs(entry - stop)
        net_risk_per_unit = initial_risk_per_unit + entry * (cc.maker_fee + cc.taker_fee + cc.stop_slippage + cc.spread_fraction + cc.funding_reserve)
        tp1_net_reward_distance = 1.2 * net_risk_per_unit + entry * (cc.maker_fee + cc.maker_fee + cc.spread_fraction + cc.funding_reserve)
        tp1_price = entry + tp1_net_reward_distance * (1 if side == "LONG" else -1)
        # Net break-even stop is a Stop-Market exit: use taker fee and
        # adverse trigger slippage, not the maker fee of a resting TP limit.
        risk_free_buffer = entry * (
            cc.maker_fee + cc.taker_fee + cc.stop_slippage
            + cc.spread_fraction + cc.funding_reserve
        )
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
            fill_spread_cost = entry_notional * cc.spread_fraction
            fill_slippage_cost = abs(fill_exit - stop) * qty_total * cv
            fill_net = fill_gross - fill_fees - fill_funding - fill_spread_cost
            return TradeResult("CLOSED", "SL", qty_total, entry, fill_exit, fill_net, fill_fees,
                               fill_funding, 0, False, rr_items["net_rr"], {
                                   **rr_items, "entry_order_type": "post_only_limit",
                                   "entry_fill_model": "one_tick_trade_through",
                                   "exit_reason": "SL", "mark_price_trigger_model": "OHLC_MARK_STOP_FIRST",
                                   "same_bar_tp_sl_precedence": "SL_FIRST_FILL_CANDLE",
                                   "gross_realized_pnl_usdt": fill_gross, "total_fees_usdt": fill_fees,
                                   "funding_paid_usdt": fill_funding, "spread_cost_usdt": fill_spread_cost,
                                   "slippage_cost_usdt": fill_slippage_cost, "net_pnl_usdt": fill_net,
                                   "entry_ts_ms": entry_ts, "exit_ts_ms": fill_bar_close_ts,
                               })
        exit_price = None
        exit_reason = None
        exit_ts = None
        held = 0
        time_exit_limit_price = None
        time_exit_submitted_ts = None
        for idx in range(start_index + 2, len(rows)):
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
                    notional_now = entry * qty_left * cv
                    funding_total += notional_now * rate * (1 if side == "LONG" else -1)
                    event["_applied"] = True

            # Exchange stop protection has priority over every limit exit when
            # OHLC data cannot establish the intrabar event sequence.
            stop_hit = (ml <= current_stop) if side == "LONG" else (mh >= current_stop)
            if stop_hit:
                exit_price = current_stop * (1 - cc.stop_slippage if side == "LONG" else 1 + cc.stop_slippage)
                exit_reason, exit_ts = "SL", close_ts
                break

            # TIME_EXIT is a resting limit submitted only after the twelfth
            # completed holding candle. It cannot fill on its submission bar.
            if time_exit_limit_price is not None:
                time_exit_filled = (
                    last_high >= time_exit_limit_price + tick
                    if side == "LONG"
                    else last_low <= time_exit_limit_price - tick
                )
                if time_exit_filled:
                    exit_price = time_exit_limit_price
                    exit_reason, exit_ts = "TIME_EXIT", close_ts
                    break
                continue

            tp1_hit = tp1_qty > 0 and not tp1 and (
                last_high >= tp1_price + tick if side == "LONG" else last_low <= tp1_price - tick
            )
            target_hit = (last_high >= target + tick) if side == "LONG" else (last_low <= target - tick)
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

            if not tp1 and held >= self.max_holding_bars:
                time_exit_limit_price = _net_breakeven_limit_price(
                    entry=entry,
                    quantity=qty_left,
                    contract_value=cv,
                    side=side,
                    accumulated_fees=fees,
                    funding_paid=funding_total,
                    spread_fraction=cc.spread_fraction,
                    exit_fee_rate=cc.taker_fee,
                    tick_size=tick,
                )
                time_exit_submitted_ts = close_ts

        if exit_price is None:
            last = rows[-1]
            observed_through_ts = int(last.get("close_ts") or (_timestamp_ms(last.get("ts")) + 15 * 60_000))
            diagnostics = {
                **rr_items,
                "entry_order_type": "post_only_limit",
                "entry_fill_model": "one_tick_trade_through",
                "exit_reason": "TIME_EXIT_LIMIT_PENDING" if time_exit_limit_price is not None else "POSITION_OPEN",
                "mark_price_trigger_model": "OHLC_MARK_STOP_FIRST",
                "same_bar_tp_sl_precedence": "SL_FIRST",
                "funding_event_count": sum(1 for x in funding if x.get("_applied") is True),
                "gross_realized_pnl_usdt": realized_gross,
                "total_fees_usdt": fees,
                "funding_paid_usdt": funding_total,
                "spread_cost_usdt": spread_cost,
                "slippage_cost_usdt": 0.0,
                "net_pnl_usdt": None,
                "realized_net_pnl_so_far_usdt": realized_gross - fees - funding_total - spread_cost,
                "remaining_qty": qty_left,
                "entry_ts_ms": entry_ts,
                "observed_through_ts_ms": observed_through_ts,
                "time_exit_order_type": "LIMIT" if time_exit_limit_price is not None else None,
                "time_exit_limit_price": time_exit_limit_price,
                "time_exit_submitted_ts_ms": time_exit_submitted_ts,
                "time_exit_fill_model": "one_tick_trade_through_after_submission" if time_exit_limit_price is not None else None,
                "time_exit_fee_class": "TAKER_CONSERVATIVE" if time_exit_limit_price is not None else None,
            }
            return TradeResult(
                "OPEN",
                "TIME_EXIT_LIMIT_PENDING" if time_exit_limit_price is not None else "POSITION_OPEN",
                qty_total, entry, None, None,
                fees, funding_total, held, tp1, rr_items["net_rr"], diagnostics,
            )

        realized_gross += (exit_price - entry) * qty_left * cv * (1 if side == "LONG" else -1)
        exit_fee_rate = cc.taker_fee if exit_reason == "TIME_EXIT" else (cc.maker_fee if exit_reason == "TP" else cc.taker_fee)
        fees += exit_price * qty_left * cv * exit_fee_rate
        slippage_cost = abs(exit_price - current_stop) * qty_left * cv if exit_reason == "SL" else 0.0
        net = realized_gross - fees - funding_total - spread_cost
        diagnostics = {
            **rr_items,
            "entry_order_type": "post_only_limit",
            "entry_fill_model": "one_tick_trade_through",
            "exit_reason": exit_reason,
            "time_exit_order_type": "LIMIT" if exit_reason == "TIME_EXIT" else None,
            "time_exit_limit_price": time_exit_limit_price,
            "time_exit_submitted_ts_ms": time_exit_submitted_ts,
            "time_exit_fill_model": "one_tick_trade_through_after_submission" if exit_reason == "TIME_EXIT" else None,
            "time_exit_fee_class": "TAKER_CONSERVATIVE" if exit_reason == "TIME_EXIT" else None,
            "mark_price_trigger_model": "OHLC_MARK_STOP_FIRST",
            "same_bar_tp_sl_precedence": "SL_FIRST",
            "funding_event_count": sum(1 for x in funding if x.get("_applied") is True),
            "gross_realized_pnl_usdt": realized_gross,
            "total_fees_usdt": fees,
            "funding_paid_usdt": funding_total,
            "spread_cost_usdt": spread_cost,
            "slippage_cost_usdt": slippage_cost,
            "net_pnl_usdt": net,
            "remaining_qty": qty_left,
            "entry_ts_ms": entry_ts,
            "exit_ts_ms": exit_ts,
        }
        return TradeResult("CLOSED", exit_reason, qty_total, entry, exit_price, net, fees,
                           funding_total, held, tp1, rr_items["net_rr"], diagnostics)
