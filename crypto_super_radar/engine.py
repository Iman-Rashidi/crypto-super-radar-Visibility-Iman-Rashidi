"""Nine-gate decision engine: trades are allowed only when every gate passes."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from .backtesting import walk_forward_backtest
from .config import AppConfig
from .filters import evaluate_filters, gate
from .models import AnalysisResult, AssetSpec, GateResult, GateStatus, MarketSnapshot, SafetyStatus, Signal
from .providers import DataProviderError, MarketDataService


class ConfirmationEngine:
    """Fail-closed multi-stage quantitative confirmation engine.

    No exchange orders are sent. Signals are research outputs only.
    """

    def __init__(self, config: Optional[AppConfig] = None, data_service: Optional[Any] = None):
        self.config = config or AppConfig()
        self.data = data_service or MarketDataService(self.config)

    @staticmethod
    def _unknown_gate_set(reason: str) -> List[GateResult]:
        names = [
            (1, "Data integrity", "اعتبار داده"),
            (2, "Market regime", "وضعیت کلی بازار"),
            (3, "Trend and structure", "روند و ساختار قیمت"),
            (4, "Momentum and relative strength", "مومنتوم و قدرت نسبی"),
            (5, "Capital flow and liquidity", "جریان سرمایه و نقدینگی"),
            (6, "On-chain and contract risk", "امنیت آن‌چین و قرارداد"),
            (7, "News and sentiment", "اخبار و احساسات"),
            (8, "Out-of-sample strategy validation", "اعتبارسنجی آماری استراتژی"),
            (9, "Portfolio and trade risk", "ریسک پرتفوی و معامله"),
        ]
        return [
            gate(i, en, fa, GateStatus.UNKNOWN, reason,
                 "به علت نبود داده معتبر، این مرحله قابل تأیید نیست.")
            for i, en, fa in names
        ]

    def analyze(
        self,
        asset_input: str | AssetSpec,
        headlines: Optional[Iterable[str]] = None,
        open_portfolio_risk_pct: float = 0.0,
    ) -> AnalysisResult:
        asset = asset_input if isinstance(asset_input, AssetSpec) else AssetSpec.parse(asset_input)
        headlines = list(headlines or [])
        try:
            snapshot: MarketSnapshot = self.data.snapshot(asset)
            candles = self.data.candles(asset, snapshot)
            benchmark = self.data.benchmark_candles()
        except Exception as exc:
            return AnalysisResult(
                asset=asset.raw,
                signal=Signal.NO_TRADE,
                safety_status=SafetyStatus.UNKNOWN,
                confirmation_score=None,
                estimated_risk_score=None,
                opportunity_rank=None,
                entry_price=None,
                take_profit=None,
                stop_loss=None,
                expected_holding_days=None,
                position_notional_usd=None,
                risk_budget_usd=None,
                gates=self._unknown_gate_set(f"Data unavailable: {type(exc).__name__}: {exc}"),
                summary_en="No trade: required public data could not be validated.",
                summary_fa="عدم معامله: داده‌های ضروری دریافت یا اعتبارسنجی نشدند؛ هیچ عددی جایگزین دادهٔ گمشده نشده است.",
                metrics={"error_type": type(exc).__name__, "error": str(exc)},
            )

        # A security-RPC failure invalidates only the security gate; it must not
        # erase otherwise useful diagnostics or be converted into a clean bill.
        try:
            security = self.data.security(asset)
        except Exception as exc:
            security = {"status": "UNKNOWN", "reason": f"Security evidence unavailable: {type(exc).__name__}: {exc}"}

        core = evaluate_filters(asset, snapshot, candles, benchmark, security, headlines, self.config)
        gates: List[GateResult] = list(core["gates"])
        direction = core["technical"].get("direction", "NONE")

        # Gate 8 — backtest. Perpetuals require funding history from both venues.
        funding = None
        funding_error = None
        if asset.market_type == "perp":
            try:
                funding = self.data.funding_history(asset)
            except Exception as exc:
                funding_error = f"Funding history unavailable: {type(exc).__name__}: {exc}"
        if asset.market_type == "perp" and funding is None:
            backtest = {"status": "UNKNOWN", "reason": funding_error or "Funding history is required for perpetual backtesting."}
        else:
            # A 200-bar sample gives the out-of-sample funding window a chance
            # to overlap public funding-history retention on both venues.
            bt_candles = candles.tail(200) if asset.market_type == "perp" else candles
            backtest = walk_forward_backtest(
                bt_candles,
                self.config,
                allow_short=(asset.market_type == "perp"),
                funding_rates=funding,
            )
        if backtest.get("status") == "PASS":
            g8_status = GateStatus.PASS
        elif backtest.get("status") == "FAIL":
            g8_status = GateStatus.FAIL
        else:
            g8_status = GateStatus.UNKNOWN
        gates.append(gate(
            8,
            "Out-of-sample strategy validation",
            "اعتبارسنجی آماری خارج از نمونه",
            g8_status,
            backtest.get("reason", "Backtest status unavailable."),
            "استراتژی با داده تاریخی به‌صورت زمانی و خارج از نمونه ارزیابی شده است." if g8_status == GateStatus.PASS else "آزمون آماری خارج از نمونه کافی نیست یا شرایط لازم را پاس نکرده است.",
            **{k: v for k, v in backtest.items() if k not in {"status", "reason"}},
        ))

        # Candidate entry, SL/TP and size are calculated before gate 9.
        metrics = {
            "market_regime": core["regime"].get("regime"),
            "rsi14": core["technical"].get("rsi14"),
            "relative_strength_30d_pct": core["technical"].get("relative_strength_30d_pct"),
            "atr14": core["technical"].get("atr14"),
            "volume_to_liquidity": core.get("volume_to_liquidity"),
            "price_discrepancy_pct": snapshot.price_discrepancy_pct,
            "volume_discrepancy_pct": snapshot.volume_discrepancy_pct,
            "sentiment_score": core["sentiment"].get("score"),
            "hype_score": core["sentiment"].get("hype_score"),
            "leader_mentions": core["sentiment"].get("leader_mentions"),
            "oos_sharpe": backtest.get("oos_sharpe"),
            "oos_sortino": backtest.get("oos_sortino"),
            "oos_profit_factor": backtest.get("oos_profit_factor"),
            "oos_max_drawdown_pct": backtest.get("oos_max_drawdown_pct"),
            "oos_net_return_pct": backtest.get("oos_net_return_pct"),
            "pool_created_at": snapshot.pool_created_at,
            "provider_a": snapshot.provider_a,
            "provider_b": snapshot.provider_b,
        }

        risk_score = self._estimated_risk(core, snapshot, security, candles)
        trade_plan = self._build_trade_plan(
            asset=asset,
            snapshot=snapshot,
            candles=candles,
            direction=direction,
            security=security,
            open_portfolio_risk_pct=open_portfolio_risk_pct,
            estimated_risk_score=risk_score,
        )
        if trade_plan["status"] == "PASS":
            g9_status = GateStatus.PASS
        elif trade_plan["status"] == "FAIL":
            g9_status = GateStatus.FAIL
        else:
            g9_status = GateStatus.UNKNOWN
        gates.append(gate(
            9,
            "Portfolio and trade risk",
            "ریسک پرتفوی و معامله",
            g9_status,
            trade_plan["reason_en"],
            trade_plan["reason_fa"],
            **{k: v for k, v in trade_plan.items() if k not in {"status", "reason_en", "reason_fa"}},
        ))

        # Score only measured dimensions. The score cannot override hard gates.
        score = self._confirmation_score(core, snapshot, security)
        all_pass = len(gates) == 9 and all(item.status == GateStatus.PASS for item in gates)
        if all_pass and score is not None and score >= self.config.thresholds.min_confirmation_score and risk_score is not None and risk_score < 45:
            signal = Signal(direction)
            safety = SafetyStatus.LOW_RISK if risk_score <= 35 else SafetyStatus.UNKNOWN
            if safety != SafetyStatus.LOW_RISK:
                signal = Signal.NO_TRADE
        else:
            signal = Signal.NO_TRADE
            if any(item.status == GateStatus.FAIL for item in gates) or (risk_score is not None and risk_score >= 55):
                safety = SafetyStatus.HIGH_RISK
            else:
                safety = SafetyStatus.UNKNOWN
        if risk_score is not None and risk_score >= 55:
            safety = SafetyStatus.HIGH_RISK
        elif risk_score is not None and risk_score <= 35 and gates[0].status == GateStatus.PASS and gates[5].status == GateStatus.PASS:
            safety = SafetyStatus.LOW_RISK

        if signal == Signal.NO_TRADE:
            entry = tp = sl = notional = risk_budget = None
            holding = None
            summary_en = "NO TRADE: one or more mandatory gates did not pass; the score cannot override a failed or unknown gate."
            failed = [f"{g.gate_id}:{g.name_en}={g.status.value}" for g in gates if g.status != GateStatus.PASS]
            summary_fa = "فعلاً معامله نکن؛ " + ("فیلترهای زیر تأیید نشده‌اند: " + "، ".join(f"مرحله {g.gate_id} ({g.status.value})" for g in gates if g.status != GateStatus.PASS) if failed else "امتیاز یا وضعیت ریسک به حد مجاز نرسیده است.")
        else:
            entry = trade_plan["entry"]
            tp = trade_plan["take_profit"]
            sl = trade_plan["stop_loss"]
            notional = trade_plan["position_notional_usd"]
            risk_budget = trade_plan["risk_budget_usd"]
            holding = 10
            summary_en = f"{signal.value}: all nine mandatory gates passed; paper-only plan, not an order."
            summary_fa = f"{signal.value}: هر ۹ فیلتر تأیید شده‌اند؛ این فقط برنامه معاملاتی آزمایشی است و سفارش واقعی ارسال نمی‌شود."

        metrics.update({
            "confirmation_score": score,
            "estimated_risk_score": risk_score,
            "security_status": security.get("status", "UNKNOWN"),
            "funding_rate_8h": snapshot.raw.get("funding_rate_8h"),
            "open_interest_binance": snapshot.raw.get("open_interest_a"),
            "open_interest_bybit": snapshot.raw.get("open_interest_b"),
            "backtest_status": backtest.get("status"),
        })
        return AnalysisResult(
            asset=asset.raw,
            signal=signal,
            safety_status=safety,
            confirmation_score=score,
            estimated_risk_score=risk_score,
            opportunity_rank=score,
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            expected_holding_days=holding,
            position_notional_usd=notional,
            risk_budget_usd=risk_budget,
            gates=gates,
            summary_en=summary_en,
            summary_fa=summary_fa,
            metrics=metrics,
        )

    def _build_trade_plan(
        self,
        asset: AssetSpec,
        snapshot: MarketSnapshot,
        candles: pd.DataFrame,
        direction: str,
        security: Dict[str, Any],
        open_portfolio_risk_pct: float,
        estimated_risk_score: Optional[float],
    ) -> Dict[str, Any]:
        risk = self.config.risk
        thresholds = self.config.thresholds
        if direction == "NONE":
            return {"status": "FAIL", "reason_en": "No trend direction exists.", "reason_fa": "جهت قابل معامله‌ای وجود ندارد."}
        if estimated_risk_score is None:
            return {"status": "UNKNOWN", "reason_en": "Estimated risk cannot be calculated.", "reason_fa": "ریسک تخمینی محاسبه نشده است."}
        if estimated_risk_score >= self.config.thresholds.max_estimated_risk_score:
            ceiling = self.config.thresholds.max_estimated_risk_score
            return {"status": "FAIL", "reason_en": f"Estimated risk score {estimated_risk_score:.1f} is above the configured entry ceiling ({ceiling:.1f}).", "reason_fa": f"امتیاز ریسک {estimated_risk_score:.1f} از سقف مجاز ورود ({ceiling:.1f}) بیشتر است."}
        if direction == "SHORT" and asset.market_type != "perp":
            return {"status": "FAIL", "reason_en": "Short selling is unavailable for this spot/DEX instrument.", "reason_fa": "این ابزار Spot یا DEX است؛ فروش استقراضی بدون بازار مشتقه مجاز نیست."}
        if direction == "SHORT" and asset.market_type == "perp" and snapshot.raw.get("funding_rate_8h") is None:
            return {"status": "UNKNOWN", "reason_en": "Perpetual funding evidence is unavailable.", "reason_fa": "داده Funding برای کنترل هزینه و ریسک فروش استقراضی موجود نیست."}
        if open_portfolio_risk_pct > risk.max_open_portfolio_risk_pct:
            return {"status": "FAIL", "reason_en": "Existing portfolio risk exceeds the configured limit.", "reason_fa": "ریسک معاملات باز از سقف مجاز پرتفوی بیشتر است."}
        # ATR is computed from actual OHLC candles; no missing values are replaced.
        from .indicators import atr
        atr_value = atr(candles)
        entry = self._number(candles["close"].iloc[-1]) if not candles.empty else None
        if atr_value is None or entry is None or entry <= 0:
            return {"status": "UNKNOWN", "reason_en": "ATR or entry price is unavailable.", "reason_fa": "قیمت ورود یا ATR مشخص نیست؛ حد ضرر معتبر محاسبه نمی‌شود."}
        stop_distance = risk.atr_stop_multiple * atr_value
        target_distance = risk.atr_target_multiple * atr_value
        if stop_distance <= 0 or target_distance / stop_distance < thresholds.min_risk_reward:
            return {"status": "FAIL", "reason_en": "Risk/reward ratio is below the required minimum.", "reason_fa": "نسبت سود احتمالی به زیان کمتر از حداقل تعیین‌شده است."}
        if direction == "LONG":
            stop = entry - stop_distance
            target = entry + target_distance
        else:
            stop = entry + stop_distance
            target = entry - target_distance
        if stop <= 0 or target <= 0:
            return {"status": "FAIL", "reason_en": "Computed stop or target is not a valid positive price.", "reason_fa": "حد سود یا حد ضرر محاسبه‌شده معتبر نیست."}
        equity = risk.paper_equity_usd
        risk_budget = equity * risk.risk_per_trade_pct / 100.0
        risk_fraction = stop_distance / entry
        if risk_fraction <= 0:
            return {"status": "UNKNOWN", "reason_en": "Invalid stop distance.", "reason_fa": "فاصله حد ضرر معتبر نیست."}
        risk_based_notional = risk_budget / risk_fraction
        max_notional = equity * risk.max_position_notional_pct / 100.0
        if snapshot.liquidity_usd is not None:
            liquidity_cap = snapshot.liquidity_usd * risk.max_pool_liquidity_participation_pct / 100.0
        else:
            # Without order-book depth, use only a very small quote-volume proxy.
            liquidity_cap = snapshot.volume_24h_usd * 0.0005
        notional = min(risk_based_notional, max_notional, liquidity_cap)
        if notional < 100.0:
            return {"status": "FAIL", "reason_en": "Liquidity-constrained position size is below $100.", "reason_fa": "با رعایت سقف مشارکت در نقدینگی، اندازه موقعیت کمتر از ۱۰۰ دلار می‌شود."}
        required_risk_pct = (notional * risk_fraction / equity) * 100.0
        if open_portfolio_risk_pct + required_risk_pct > risk.max_open_portfolio_risk_pct:
            return {"status": "FAIL", "reason_en": "Adding this position would exceed the portfolio risk budget.", "reason_fa": "افزودن این موقعیت سقف ریسک کل پرتفوی را نقض می‌کند."}
        return {
            "status": "PASS",
            "reason_en": "Position size is capped by risk budget, liquidity participation and portfolio limits.",
            "reason_fa": "اندازه موقعیت با بودجه ریسک، سقف مشارکت در نقدینگی و محدودیت پرتفوی کنترل شده است.",
            "entry": entry,
            "take_profit": target,
            "stop_loss": stop,
            "position_notional_usd": notional,
            "risk_budget_usd": risk_budget,
            "estimated_position_risk_pct_equity": required_risk_pct,
            "risk_reward": target_distance / stop_distance,
            "atr14": atr_value,
        }

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            result = float(value)
            return result if np.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    def _confirmation_score(self, core: Dict[str, Any], snapshot: MarketSnapshot, security: Dict[str, Any]) -> Optional[float]:
        tech = core["technical"]
        sentiment = core["sentiment"].get("score")
        if tech.get("status") != "KNOWN" or sentiment is None:
            return None
        direction = tech.get("direction")
        trend = 90.0 if direction in {"LONG", "SHORT"} else 20.0
        rs = abs(float(tech.get("relative_strength_30d_pct", 0.0)))
        momentum = min(100.0, 55.0 + rs * 1.5)
        if direction == "LONG" and not 50 <= tech["rsi14"] <= 70:
            momentum *= 0.6
        if direction == "SHORT" and not 30 <= tech["rsi14"] <= 50:
            momentum *= 0.6
        if snapshot.liquidity_usd:
            capital = min(100.0, 55.0 + snapshot.liquidity_usd / 25_000.0)
        else:
            capital = min(100.0, 55.0 + snapshot.volume_24h_usd / 100_000.0)
        security_score = 85.0 if security.get("status") in {"KNOWN", "PASS"} else 0.0
        sentiment_alignment = sentiment if direction == "LONG" else 100.0 - sentiment
        score = 0.25 * trend + 0.20 * momentum + 0.25 * capital + 0.20 * security_score + 0.10 * sentiment_alignment
        return round(float(np.clip(score, 0.0, 100.0)), 2)

    def _estimated_risk(self, core: Dict[str, Any], snapshot: MarketSnapshot, security: Dict[str, Any], candles: pd.DataFrame) -> Optional[float]:
        tech = core["technical"]
        if tech.get("status") != "KNOWN":
            return None
        atr_pct = tech["atr14"] / tech["close"] * 100.0
        volatility_component = min(35.0, atr_pct * 4.0)
        if snapshot.liquidity_usd:
            liquidity_component = max(0.0, 30.0 - min(30.0, snapshot.liquidity_usd / 20_000.0))
            turnover = snapshot.volume_24h_usd / snapshot.liquidity_usd
            turnover_component = min(20.0, max(0.0, turnover - 1.0) * 5.0)
        else:
            liquidity_component = max(0.0, 30.0 - min(30.0, snapshot.volume_24h_usd / 100_000.0))
            turnover_component = 10.0
        security_component = 0.0 if security.get("status") in {"KNOWN", "PASS"} else 25.0
        score = 10.0 + volatility_component + liquidity_component + turnover_component + security_component
        return round(float(np.clip(score, 0.0, 100.0)), 2)
