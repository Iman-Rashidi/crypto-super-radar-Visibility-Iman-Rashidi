"""Independent, Demo-only smoke test for the BloFin futures execution path.

This test intentionally bypasses market signals and strategy gates. It does NOT
bypass account/environment safeguards: it is pinned to BloFin Demo, BTC-USDT,
isolated 1x, minimum contract size, and only closes the single position created
by its own uniquely identified test order.
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Callable, Mapping

from .blofin_demo_readonly import DEMO_REST_URL
from .blofin_demo_executor import (
    BloFinDemoExecutionBlocked,
    BloFinDemoExecutionUnknown,
    BloFinDemoTradeClient,
    _decimal,
    _equity_from_balance,
    _format_decimal,
)


class BloFinDemoSmokeTestRunner:
    """Open one tiny fixed-direction Demo position and close only that position."""

    TEST_NAME = "تست ربات در محیط دمو صرافی"
    INSTRUMENT_ID = "BTC-USDT"
    LEVERAGE = 1
    MARGIN_MODE = "isolated"
    MAX_NOTIONAL_EQUITY_FRACTION = Decimal("0.02")
    BRACKET_DISTANCE = Decimal("0.01")

    def __init__(
        self,
        *,
        client: BloFinDemoTradeClient | None = None,
        on_event: Callable[..., None] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        poll_attempts: int = 12,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.client = client or BloFinDemoTradeClient(timeout=10.0)
        self.on_event = on_event
        self.sleep_fn = sleep_fn
        self.poll_attempts = max(1, min(int(poll_attempts), 30))
        self.poll_interval_seconds = max(0.0, min(float(poll_interval_seconds), 3.0))

    def _emit(self, stage: str, message_fa: str, **details: Any) -> None:
        if self.on_event is not None:
            self.on_event(stage=stage, message_fa=message_fa, **details)

    @staticmethod
    def _first_row(value: Any, label: str) -> dict[str, Any]:
        # Trade POST methods return a BloFin response envelope; GET methods may return decoded data.
        if isinstance(value, Mapping) and "data" in value:
            if value.get("code") not in (None, "0", 0):
                raise BloFinDemoExecutionBlocked(f"{label} was rejected by BloFin.")
            value = value.get("data")
        if isinstance(value, list):
            value = next((item for item in value if isinstance(item, Mapping)), None)
        if not isinstance(value, Mapping):
            raise BloFinDemoExecutionBlocked(f"{label} response is unavailable or invalid.")
        return dict(value)

    @staticmethod
    def _active_positions(rows: Any, instrument_id: str) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            raise BloFinDemoExecutionBlocked("Positions response is not a list.")
        active: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise BloFinDemoExecutionBlocked("A position row cannot be interpreted.")
            row_inst = str(row.get("instId") or instrument_id).upper()
            if row_inst != instrument_id:
                continue
            raw_size = row.get("positions")
            if raw_size in (None, ""):
                raise BloFinDemoExecutionBlocked("Position size is missing; refusing to guess.")
            size = _decimal(raw_size, "position_size")
            if abs(size) > Decimal("0"):
                active.append(dict(row))
        return active

    @staticmethod
    def _filled_size(order_detail: Mapping[str, Any], fills: Any) -> Decimal:
        total = Decimal("0")
        if isinstance(fills, list):
            for fill in fills:
                if isinstance(fill, Mapping) and fill.get("fillSize") not in (None, ""):
                    total += _decimal(fill["fillSize"], "fill_size")
        if total <= 0 and order_detail.get("filledSize") not in (None, ""):
            total = _decimal(order_detail["filledSize"], "filled_size")
        return total

    @staticmethod
    def _round_up(value: Decimal, tick: Decimal) -> Decimal:
        return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick

    @staticmethod
    def _round_down(value: Decimal, tick: Decimal) -> Decimal:
        return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick

    def run(self) -> dict[str, Any]:
        client = self.client
        if client.base_url != DEMO_REST_URL:
            raise BloFinDemoExecutionBlocked("Smoke test is pinned to BloFin Demo only.")
        if not all((client.api_key, client.api_secret, client.passphrase)):
            raise BloFinDemoExecutionBlocked("BloFin Demo Trade credentials are not configured.")

        self._emit("PREFLIGHT", "آزمایش مستقل آغاز شد؛ نماد BTC-USDT و محیط Demo بررسی می‌شوند.")
        self._emit("PREFLIGHT_CLOCK", "در حال بررسی اختلاف ساعت صرافی و سرور.")
        clock = client.check_clock_skew(inst_id=self.INSTRUMENT_ID)
        if abs(int(clock.get("offset_ms", 10**12))) >= 60_000:
            raise BloFinDemoExecutionBlocked("Exchange/local clock skew is 60 seconds or more.")

        self._emit("PREFLIGHT_MARKET_DATA", "در حال دریافت قیمت آخرین معامله و Mark Price بیت‌کوین.")
        ticker = self._first_row(client.get_tickers(inst_id=self.INSTRUMENT_ID), "Ticker")
        mark_row = self._first_row(client.get_mark_price(inst_id=self.INSTRUMENT_ID), "Mark price")
        mark_price = _decimal(mark_row.get("markPrice"), "mark_price")
        if mark_price <= 0 or ticker.get("last") in (None, ""):
            raise BloFinDemoExecutionBlocked("Fresh BTC-USDT ticker and mark price are required.")

        self._emit("PREFLIGHT_CONTRACT", "در حال اعتبارسنجی مشخصات قرارداد BTC-USDT.")
        instrument = client.get_tradable_instrument(self.INSTRUMENT_ID)
        self._emit("PREFLIGHT_POSITION_MODE", "در حال خواندن حالت حساب و موقعیت‌ها.")
        mode = client.get_position_mode()
        position_mode = mode.get("positionMode")
        if position_mode not in {"net_mode", "long_short_mode"}:
            raise BloFinDemoExecutionBlocked("BloFin position mode could not be confirmed.")

        # Never absorb or close a pre-existing user position/order on the same instrument.
        self._emit("PREFLIGHT_EXISTING_POSITION", "در حال بررسی پوزیشن باز قبلی BTC-USDT.")
        existing_positions = self._active_positions(
            client.get_positions(inst_id=self.INSTRUMENT_ID), self.INSTRUMENT_ID
        )
        if existing_positions:
            raise BloFinDemoExecutionBlocked(
                "BTC-USDT already has an open position; smoke test stopped without touching it."
            )
        self._emit("PREFLIGHT_PENDING_ORDERS", "در حال بررسی سفارش‌های معلق BTC-USDT.")
        pending = client.get_open_orders(inst_id=self.INSTRUMENT_ID)
        if not isinstance(pending, list):
            raise BloFinDemoExecutionBlocked("BTC-USDT pending-orders response is invalid.")
        if pending:
            raise BloFinDemoExecutionBlocked(
                "BTC-USDT has pending orders; smoke test stopped without changing them."
            )

        self._emit("PREFLIGHT_EQUITY", "در حال خواندن موجودی و Equity حساب Demo.")
        equity = Decimal(str(_equity_from_balance(
            client.get_futures_balance(product_type="USDT-FUTURES")
        )))
        min_size = _decimal(instrument.get("minSize"), "minSize")
        lot_size = _decimal(instrument.get("lotSize"), "lotSize")
        contract_value = _decimal(instrument.get("contractValue"), "contractValue")
        tick_size = _decimal(instrument.get("tickSize"), "tickSize")
        if min(min_size, lot_size, contract_value, tick_size, equity) <= 0:
            raise BloFinDemoExecutionBlocked("BTC-USDT contract minimum or Demo equity is invalid.")
        quantity = (min_size / lot_size).to_integral_value(rounding=ROUND_CEILING) * lot_size
        notional = quantity * contract_value * mark_price
        max_notional = equity * self.MAX_NOTIONAL_EQUITY_FRACTION
        if notional > max_notional:
            raise BloFinDemoExecutionBlocked(
                "The minimum BTC-USDT contract exceeds the smoke-test notional cap of 2% of Demo equity."
            )
        if instrument.get("maxMarketSize") not in (None, ""):
            if quantity > _decimal(instrument["maxMarketSize"], "maxMarketSize"):
                raise BloFinDemoExecutionBlocked("Minimum contract size exceeds maximum market-order size.")

        # The test direction is deliberately fixed LONG: no signal or indicator is consulted.
        position_side = "net" if position_mode == "net_mode" else "long"
        take_profit = self._round_up(mark_price * (Decimal("1") + self.BRACKET_DISTANCE), tick_size)
        stop_loss = self._round_down(mark_price * (Decimal("1") - self.BRACKET_DISTANCE), tick_size)
        if stop_loss <= 0 or take_profit <= mark_price or stop_loss >= mark_price:
            raise BloFinDemoExecutionBlocked("Could not construct valid Demo-only protective TP/SL prices.")

        entry_client_id = "SMK" + uuid.uuid4().hex[:24]
        close_client_id = "SMKC" + uuid.uuid4().hex[:24]
        self._emit(
            "SAFEGUARDS_PASSED",
            "کنترل‌های حساب عبور کردند؛ سفارش LONG با حداقل حجم، اهرم ایزوله 1x و TP/SL حفاظتی آماده است.",
            instrument_id=self.INSTRUMENT_ID,
            quantity=_format_decimal(quantity),
            notional_usdt=_format_decimal(notional),
            mark_price=_format_decimal(mark_price),
            leverage=self.LEVERAGE,
        )

        client.set_futures_leverage(
            self.INSTRUMENT_ID,
            self.LEVERAGE,
            margin_mode=self.MARGIN_MODE,
            position_side=None if position_mode == "net_mode" else position_side,
        )
        self._emit("ENTRY_SUBMITTING", "ربات سفارش آزمایشی LONG را مستقیماً به BloFin Demo می‌فرستد.")
        try:
            entry_response = client.place_futures_order(
                inst_id=self.INSTRUMENT_ID,
                side="buy",
                position_side=position_side,
                size=_format_decimal(quantity),
                client_order_id=entry_client_id,
                take_profit=_format_decimal(take_profit),
                stop_loss=_format_decimal(stop_loss),
                margin_mode=self.MARGIN_MODE,
            )
        except BloFinDemoExecutionUnknown:
            raise
        except Exception as exc:
            raise BloFinDemoExecutionUnknown(
                "Demo entry outcome is unknown; reconcile order and positions before retrying."
            ) from exc

        entry_receipt = self._first_row(entry_response, "Entry order receipt")
        exchange_order_id = str(entry_receipt.get("orderId") or "")
        if not exchange_order_id:
            raise BloFinDemoExecutionUnknown(
                "BloFin returned no entry order ID; do not submit another order."
            )

        self._emit(
            "ENTRY_ACCEPTED",
            "BloFin سفارش ورود را پذیرفت؛ ربات در حال راستی‌آزمایی Fill و پوزیشن ایجادشده است.",
            exchange_order_id=exchange_order_id,
            client_order_id=entry_client_id,
        )

        order_detail: dict[str, Any] = {}
        fills: list[dict[str, Any]] = []
        filled_size = Decimal("0")
        for attempt in range(self.poll_attempts):
            detail_value = client.get_order_detail(
                self.INSTRUMENT_ID, client_order_id=entry_client_id
            )
            order_detail = self._first_row(detail_value, "Entry order detail")
            fills_value = client.get_fills_history(
                inst_id=self.INSTRUMENT_ID, order_id=exchange_order_id, limit=100
            )
            fills = [dict(row) for row in fills_value if isinstance(row, Mapping)] if isinstance(fills_value, list) else []
            filled_size = self._filled_size(order_detail, fills)
            state = str(order_detail.get("state") or "").lower()
            if filled_size > 0 and state in {"filled", "partially_filled"}:
                break
            if state in {"canceled", "order_failed"}:
                raise BloFinDemoExecutionBlocked("BloFin did not fill the smoke-test entry order.")
            if attempt < self.poll_attempts - 1:
                self.sleep_fn(self.poll_interval_seconds)

        if filled_size <= 0:
            raise BloFinDemoExecutionUnknown(
                "Entry order was accepted but no confirmed fill was found; no second entry will be sent."
            )

        self._emit(
            "ENTRY_FILLED",
            "Fill واقعی ورود تأیید شد؛ اکنون فقط پوزیشن ایجادشده توسط همین سفارش شناسایی می‌شود.",
            filled_size=_format_decimal(filled_size),
            average_fill_price=order_detail.get("averagePrice"),
        )

        active_positions: list[dict[str, Any]] = []
        for attempt in range(4):
            active_positions = self._active_positions(
                client.get_positions(inst_id=self.INSTRUMENT_ID), self.INSTRUMENT_ID
            )
            if active_positions:
                break
            if attempt < 3:
                self.sleep_fn(self.poll_interval_seconds)
        if len(active_positions) != 1:
            raise BloFinDemoExecutionUnknown(
                "The filled test position could not be uniquely identified; no close request was guessed."
            )

        position = active_positions[0]
        actual_side = str(position.get("positionSide") or "").lower()
        actual_size_raw = _decimal(position.get("positions"), "position_size")
        actual_size = abs(actual_size_raw)
        side_is_long = actual_side == "long" or (actual_side == "net" and actual_size_raw > 0)
        if not side_is_long or actual_size != filled_size:
            raise BloFinDemoExecutionUnknown(
                "Open BTC-USDT position does not exactly match the test fill; automatic close is withheld."
            )
        position_id = str(position.get("positionId") or order_detail.get("positionId") or "")
        multi_position = str(mode.get("multiPosition", "false")).lower() in {"true", "1"}
        if multi_position and not position_id:
            raise BloFinDemoExecutionUnknown(
                "BloFin Multi-Position mode requires the exact positionId; no close-all request is allowed."
            )

        self._emit(
            "CLOSE_SUBMITTING",
            "پوزیشن با حجم و جهت Fill تطبیق داده شد؛ ربات درخواست بستن همان پوزیشن را می‌فرستد.",
            position_id=position_id or None,
            filled_size=_format_decimal(actual_size),
        )
        try:
            close_response = client.close_futures_position(
                inst_id=self.INSTRUMENT_ID,
                margin_mode=self.MARGIN_MODE,
                position_side=actual_side if actual_side in {"net", "long", "short"} else position_side,
                client_order_id=close_client_id,
                position_id=position_id or None,
            )
            close_receipt = self._first_row(close_response, "Close-position receipt")
        except Exception as exc:
            # A close POST is never blindly retried. Reconcile using READ-only positions.
            current = self._active_positions(
                client.get_positions(inst_id=self.INSTRUMENT_ID), self.INSTRUMENT_ID
            )
            if not current:
                close_receipt = {"reconciled": True, "clientOrderId": close_client_id}
            else:
                raise BloFinDemoExecutionUnknown(
                    "Close outcome is unknown and the test position may remain open; do not retry blindly."
                ) from exc

        self._emit("CLOSE_ACCEPTED", "درخواست بستن پذیرفته شد؛ ربات تخت‌شدن حساب Demo را تأیید می‌کند.")
        for attempt in range(self.poll_attempts):
            remaining = self._active_positions(
                client.get_positions(inst_id=self.INSTRUMENT_ID), self.INSTRUMENT_ID
            )
            if not remaining:
                history: list[dict[str, Any]] = []
                try:
                    hist = client.get_positions_history(
                        inst_id=self.INSTRUMENT_ID, state="closed", limit=20
                    )
                    history = [dict(row) for row in hist if isinstance(row, Mapping)] if isinstance(hist, list) else []
                except Exception:
                    history = []
                self._emit("CLOSED_VERIFIED", "پوزیشن BTC-USDT بسته شد و نبود پوزیشن باز تأیید شد.")
                return {
                    "status": "CLOSED_VERIFIED",
                    "test_name": self.TEST_NAME,
                    "environment": "BLOFIN_DEMO",
                    "instrument_id": self.INSTRUMENT_ID,
                    "direction": "LONG",
                    "entry_order_id": exchange_order_id,
                    "entry_client_order_id": entry_client_id,
                    "close_client_order_id": close_client_id,
                    "position_id": position_id or None,
                    "quantity_contracts": _format_decimal(filled_size),
                    "average_entry_price": order_detail.get("averagePrice"),
                    "leverage": self.LEVERAGE,
                    "margin_mode": self.MARGIN_MODE,
                    "protective_take_profit": _format_decimal(take_profit),
                    "protective_stop_loss": _format_decimal(stop_loss),
                    "close_receipt": close_receipt,
                    "open_position_count_after_close": 0,
                    "closed_position_history_count": len(history),
                    "orders_submitted": True,
                    "trade_executed": True,
                    "position_closed": True,
                }
            if attempt < self.poll_attempts - 1:
                self.sleep_fn(self.poll_interval_seconds)

        raise BloFinDemoExecutionUnknown(
            "BloFin accepted the close request, but a flat position could not be verified in time."
        )
