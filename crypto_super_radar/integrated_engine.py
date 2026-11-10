"""Connect the new whale/sentiment modules to the legacy nine-gate engine."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

import pandas as pd

from .cex_whales import CexWhaleScanner
from .config import AppConfig
from .engine import ConfirmationEngine
from .evm_security import screen_evm_token
from .elliott_waves import monitor_elliott_waves
from .chart_patterns import monitor_chart_patterns, contradicts_confirmed_breakout
from .candle_quality import completed_daily_candles
from .evm_transfers import EvmTransferScanner
from .market_sentiment import fetch_market_sentiment
from .market_intelligence import fetch_market_intelligence
from .market_behavior import analyze_market_behavior
from .models import AssetSpec, GateResult, GateStatus, SafetyStatus, Signal
from .providers import MarketDataService, PublicHttpClient
from .solana_whales import SolanaWhaleScanner
from .walk_forward import WalkForwardConfig, walk_forward_validate
from .whale_store import WhaleStore
from .whale_intelligence import (
    build_wallet_label_map,
    extract_goplus_whale_watchlist,
)


class WhaleAwareMarketDataService(MarketDataService):
    """MarketDataService that records per-asset whale evidence and screens EVM."""

    def __init__(self, config: Optional[AppConfig] = None):
        super().__init__(config)
        self.http = PublicHttpClient(self.config)
        self.snapshots: dict[str, Any] = {}
        self.whale_evidence: dict[str, dict[str, Any]] = {}
        self.candle_frames: dict[str, pd.DataFrame] = {}
        self.funding_frames: dict[str, pd.Series] = {}

    def candles(self, asset: AssetSpec, snapshot):
        frame = completed_daily_candles(super().candles(asset, snapshot))
        self.candle_frames[asset.raw] = frame.copy()
        return frame

    def benchmark_candles(self):
        return completed_daily_candles(super().benchmark_candles())

    def funding_history(self, asset: AssetSpec):
        series = super().funding_history(asset)
        self.funding_frames[asset.raw] = series.copy()
        return series

    def snapshot(self, asset: AssetSpec):
        snapshot = super().snapshot(asset)
        self.snapshots[asset.raw] = snapshot
        return snapshot

    def security(self, asset: AssetSpec) -> dict[str, Any]:
        snapshot = self.snapshots.get(asset.raw)
        if asset.market_type == "dex" and asset.chain in {"eth", "base"}:
            try:
                security = screen_evm_token(
                    asset.chain, asset.address or "",
                    timeout=self.config.request_timeout_seconds,
                    limiter=self.http.limiter,
                )
                if security.status != "PASS":
                    self.whale_evidence[asset.raw] = {
                        "status": "UNKNOWN", "reason": "EVM contract security did not PASS.",
                    }
                    return {
                        "status": security.status,
                        "reason": "; ".join(security.reasons),
                        "evm_security": security.evidence,
                    }
                if snapshot is None:
                    raise ValueError("Validated market snapshot is unavailable for whale valuation")
                token_data = security.evidence.get("goplus", {})
                watchlist = extract_goplus_whale_watchlist(
                    token_data, token_price_usd=snapshot.price_usd,
                )
                labels = build_wallet_label_map(watchlist.get("watchlist", []))
                chain_id = 1 if asset.chain == "eth" else 8453
                scanner = EvmTransferScanner(
                    chain_id, session=None,
                    timeout=self.config.request_timeout_seconds,
                    max_block_range=1500,
                    limiter=self.http.limiter,
                )
                latest = scanner.latest_block_number()
                first = max(0, latest - 1499)
                decimals = int(token_data["decimals"])
                whale = scanner.scan_erc20_transfers(
                    asset.address or "", first, latest,
                    decimals=decimals, price_usd=snapshot.price_usd,
                    address_labels=labels,
                )
                whale["watchlist"] = watchlist
                self.whale_evidence[asset.raw] = whale
                return {
                    "status": "PASS",
                    "reason": "GoPlus risk fields and exact Sourcify source match passed minimum screening; not an audit.",
                    "evm_security": security.evidence,
                    "whale_watchlist": watchlist,
                }
            except Exception as exc:
                self.whale_evidence[asset.raw] = {
                    "status": "UNKNOWN",
                    "reason": f"EVM whale/security pipeline unavailable: {type(exc).__name__}",
                }
                return {"status": "UNKNOWN", "reason": f"EVM security/whale evidence unavailable: {type(exc).__name__}"}
        if asset.market_type == "dex" and asset.chain == "solana":
            base = super().security(asset)
            try:
                if snapshot is None:
                    raise ValueError("Validated market snapshot is unavailable for whale valuation")
                whale = SolanaWhaleScanner(
                    timeout=self.config.request_timeout_seconds,
                    limiter=self.http.limiter,
                ).scan_mint(asset.address or "", price_usd=snapshot.price_usd)
                self.whale_evidence[asset.raw] = whale
            except Exception as exc:
                self.whale_evidence[asset.raw] = {
                    "status": "UNKNOWN", "reason": f"Solana whale scan unavailable: {type(exc).__name__}",
                }
            return base
        if asset.market_type in {"cex", "perp"}:
            base = super().security(asset)
            try:
                self.whale_evidence[asset.raw] = CexWhaleScanner(
                    self.config,
                ).scan(asset.symbol or "", asset.market_type)
            except Exception as exc:
                self.whale_evidence[asset.raw] = {
                    "status": "UNKNOWN", "reason": f"CEX whale scan unavailable: {type(exc).__name__}",
                }
            return base
        self.whale_evidence[asset.raw] = {"status": "UNKNOWN", "reason": "Whale source is not supported for this asset."}
        return super().security(asset)


class WhaleAwareConfirmationEngine:
    """Legacy engine plus mandatory whale and full-coverage sentiment gates."""

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        *,
        news_feed_urls: Optional[list[str]] = None,
        social_feed_urls: Optional[list[str]] = None,
        data_service: Optional[Any] = None,
        sentiment_fetcher: Optional[Callable[..., dict]] = None,
        whale_store: Optional[WhaleStore] = None,
    ):
        self.config = config or AppConfig()
        self.data = data_service or WhaleAwareMarketDataService(self.config)
        self.base = ConfirmationEngine(self.config, data_service=self.data)
        self.sentiment_fetcher = sentiment_fetcher or fetch_market_sentiment
        self.whale_store = whale_store or WhaleStore()
        self.news_feed_urls = news_feed_urls or ["https://www.coindesk.com/arc/outboundfeeds/rss/"]
        self.social_feed_urls = social_feed_urls or ["https://www.reddit.com/r/CryptoCurrency/.rss"]
        self.sentiment_cache: dict = {}


    def _multi_fold_validation(self, asset: AssetSpec) -> dict[str, Any]:
        candles = getattr(self.data, "candle_frames", {}).get(asset.raw)
        if candles is None or not isinstance(candles.index, pd.DatetimeIndex):
            return {"status": "UNKNOWN", "reason": "Validated candle history is unavailable for multi-fold validation."}
        if len(candles) < 315 or "close" not in candles:
            return {"status": "UNKNOWN", "reason": "At least 315 daily candles are required for three non-overlapping out-of-sample folds."}
        close = pd.to_numeric(candles["close"], errors="coerce")
        if close.isna().any() or (close <= 0).any():
            return {"status": "UNKNOWN", "reason": "Missing or invalid close prices block multi-fold validation."}
        fast = close.ewm(span=20, adjust=False).mean()
        slow = close.ewm(span=50, adjust=False).mean()
        if asset.market_type == "perp":
            position = pd.Series(0.0, index=close.index)
            position.loc[fast > slow] = 1.0
            position.loc[fast < slow] = -1.0
        else:
            position = (fast > slow).astype(float)
        held_position = position.shift(1).fillna(0.0)
        asset_returns = close.pct_change().fillna(0.0)
        turnover = held_position.diff().abs().fillna(held_position.abs())
        one_way_cost = (self.config.risk.fee_bps_per_side + self.config.risk.slippage_bps_per_side) / 10000.0
        strategy_returns = held_position * asset_returns - turnover * one_way_cost
        funding_daily = None
        if asset.market_type == "perp":
            funding = getattr(self.data, "funding_frames", {}).get(asset.raw)
            if funding is None or funding.empty:
                return {"status": "UNKNOWN", "reason": "Historical funding is mandatory for perpetual multi-fold validation."}
            funding_daily = pd.to_numeric(funding, errors="coerce").groupby(funding.index.floor("D")).sum(min_count=1)
            aligned_funding = funding_daily.reindex(strategy_returns.index)
            if aligned_funding.isna().any():
                return {"status": "UNKNOWN", "reason": "Historical funding does not cover every daily observation; missing funding is never assumed to be zero."}
            strategy_returns = strategy_returns - held_position * aligned_funding
        cfg = WalkForwardConfig(
            train_bars=180, test_bars=45, step_bars=45, embargo_bars=0,
            min_folds=3, fee_bps_per_side=0.0, slippage_bps_per_side=0.0,
        )
        validation = walk_forward_validate(
            strategy_returns, cfg, funding_returns=None, perpetual=False,
        )
        if validation.get("status") != "KNOWN":
            return {"status": "UNKNOWN", "reason": validation.get("reason", "Multi-fold validation is unknown."),
                    "fold_count": validation.get("fold_count"), "folds": validation.get("folds", [])}
        aggregate = validation.get("aggregate_oos") or {}
        sharpe = aggregate.get("sharpe")
        profit_factor = aggregate.get("profit_factor")
        drawdown = aggregate.get("max_drawdown_pct")
        worst = validation.get("worst_fold_drawdown_pct")
        if any(value is None for value in (sharpe, profit_factor, drawdown, worst)):
            return {"status": "UNKNOWN", "reason": "One or more multi-fold performance metrics are undefined.",
                    "fold_count": validation.get("fold_count"), "aggregate_oos": aggregate}
        passed = (
            sharpe >= self.config.thresholds.min_oos_sharpe
            and profit_factor >= self.config.thresholds.min_oos_profit_factor
            and drawdown < self.config.thresholds.max_backtest_drawdown_pct
            and worst < self.config.thresholds.max_backtest_drawdown_pct
        )
        return {
            "status": "PASS" if passed else "FAIL",
            "reason": "Multi-fold OOS metrics pass stability thresholds." if passed else "Multi-fold OOS metrics fail Sharpe, profit-factor or drawdown thresholds.",
            "fold_count": validation.get("fold_count"),
            "aggregate_oos": aggregate,
            "worst_fold_drawdown_pct": worst,
            "folds": validation.get("folds", []),
            "costs_included": True,
            "strategy": "Fixed EMA20/EMA50; chronological non-overlapping test folds; no parameter optimization.",
        }

    def analyze(
        self,
        asset_input: str | AssetSpec,
        headlines: Optional[Iterable[str]] = None,
        open_portfolio_risk_pct: float = 0.0,
        social_posts: Optional[Iterable[str]] = None,
    ):
        asset = asset_input if isinstance(asset_input, AssetSpec) else AssetSpec.parse(asset_input)
        supplied = list(headlines or [])
        sentiment = self.sentiment_fetcher(
            self.news_feed_urls,
            social_feed_urls=self.social_feed_urls,
            social_text=list(social_posts or []),
            cache=self.sentiment_cache,
            limiter=getattr(getattr(self.data, "http", None), "limiter", None),
        )
        news_items = [
            " ".join(x for x in (item.get("title", ""), item.get("summary", "")) if x)
            for item in sentiment.get("news", {}).get("headlines", [])
        ]
        social_items = [
            " ".join(x for x in (item.get("title", ""), item.get("summary", "")) if x)
            for item in sentiment.get("social", {}).get("headlines", [])
        ]
        result = self.base.analyze(
            asset, supplied + news_items + social_items, open_portfolio_risk_pct,
        )
        candle_frames = getattr(self.data, "candle_frames", {})
        wave_candles = candle_frames.get(asset.raw) if isinstance(candle_frames, dict) else None
        # Elliott output is descriptive only; it does not gate or change decisions.
        result.metrics["elliott_wave"] = monitor_elliott_waves(wave_candles)
        # Geometric pattern evidence is descriptive and never changes a gate or signal.
        pattern_candles = wave_candles.tail(120) if isinstance(wave_candles, pd.DataFrame) else wave_candles
        result.metrics["chart_pattern"] = monitor_chart_patterns(pattern_candles)
        whale = self.data.whale_evidence.get(asset.raw, {"status": "UNKNOWN"})
        whale_status = str(whale.get("status", "UNKNOWN")).upper()
        whale_flow_available = any(whale.get(key) is not None for key in (
            "net_exchange_flow_usd", "net_whale_transfer_usd", "net_whale_trade_flow_usd",
        ))
        whale_gate_status = (
            GateStatus.PASS if whale_status == "KNOWN" and whale_flow_available
            else GateStatus.FAIL if whale_status == "FAIL" else GateStatus.UNKNOWN
        )
        whale_reason = whale.get("reason") or whale.get("interpretation")
        if not whale_reason:
            whale_reason = (
                "Directional whale-flow evidence is present; no wallet identity is inferred."
                if whale_gate_status == GateStatus.PASS else
                "Whale source reported a hard failure."
                if whale_gate_status == GateStatus.FAIL else
                "Whale-flow evidence is missing, partial, or not attributable."
            )
        result.gates.append(GateResult(
            10, "Whale intelligence", "ردیابی رفتار نهنگ‌ها",
            whale_gate_status,
            whale_reason,
            "جریان نهنگ‌ها فقط با داده معتبر و پوشش کافی تأیید می‌شود؛ انتقال بزرگ به‌تنهایی خرید یا فروش نیست.",
            {k: v for k, v in whale.items() if k not in {"reason", "interpretation"}},
        ))
        sentiment_status = str(sentiment.get("status", "UNKNOWN")).upper()
        sentiment_gate_status = GateStatus.PASS if sentiment_status == "KNOWN" and sentiment.get("score") is not None else GateStatus.UNKNOWN
        result.gates.append(GateResult(
            11, "Full market sentiment coverage", "پوشش کامل احساسات بازار",
            sentiment_gate_status,
            "News, social and Fear & Greed sources are available." if sentiment_gate_status == GateStatus.PASS else "News/social/Fear & Greed coverage is partial or unavailable.",
            "منابع خبر، اجتماع و شاخص ترس و طمع باید همگی معتبر باشند؛ پوشش ناقص تأیید نمی‌شود.",
            {
                "sentiment_score": sentiment.get("score"),
                "sentiment_label": sentiment.get("label"),
                "news_status": sentiment.get("news", {}).get("status"),
                "social_status": sentiment.get("social", {}).get("status"),
                "fear_greed": sentiment.get("fear_greed"),
                "social_coverage": sentiment.get("social_coverage"),
            },
        ))
        walk = self._multi_fold_validation(asset)
        snapshot = getattr(self.data, "snapshots", {}).get(asset.raw)
        if snapshot is None:
            snapshot_reader = getattr(self.data, "snapshot", None)
            if callable(snapshot_reader):
                try:
                    snapshot = snapshot_reader(asset)
                except Exception:
                    snapshot = None
        signal_side = (
            result.gates[2].evidence.get("direction", "NO TRADE")
            if len(result.gates) > 2 else "NO TRADE"
        )
        behavior = analyze_market_behavior(
            wave_candles,
            getattr(snapshot, "price_usd", None),
            side=signal_side,
            horizon_days=result.expected_holding_days or 5,
            fee_bps_per_side=self.config.risk.fee_bps_per_side,
            slippage_bps_per_side=self.config.risk.slippage_bps_per_side,
        )
        behavior["wave_context"] = {
            "elliott_wave": result.metrics.get("elliott_wave"),
            "chart_pattern": result.metrics.get("chart_pattern"),
            "used_as_confirmation": False,
        }
        result.metrics["market_behavior"] = behavior
        result.metrics["risk_before_entry"] = behavior.get("risk_before_entry")
        # Existing walk-forward folds and market-history checks are one mandatory gate.
        if walk.get("status") == "FAIL" or behavior.get("status") == "FAIL":
            walk_status = GateStatus.FAIL
        elif walk.get("status") == "PASS" and behavior.get("status") == "PASS":
            walk_status = GateStatus.PASS
            structural_levels = behavior.get("levels", {})
            result.stop_loss = structural_levels.get("stop_loss")
            result.take_profit = structural_levels.get("take_profit")
            result.metrics["execution_levels"] = structural_levels
        else:
            walk_status = GateStatus.UNKNOWN
        combined_reason = (
            f"Walk-forward: {walk.get('reason', 'unknown')}; "
            f"market behavior: {behavior.get('reason', 'unknown')}."
        )
        result.gates.append(GateResult(
            12, "Walk-forward, Fibonacci and historical analog risk",
            "اعتبارسنجی زمانی، فیبوناچی و ریسکِ الگوهای تاریخی",
            walk_status,
            combined_reason,
            "ورود فقط پس از پایداری خارج از نمونه، ساختار سقف/کف همسو، هم‌گرایی فیبوناچی، "
            "برآورد خطای تاریخی با بازه اطمینان، حدضرر ساختاری و نسبت سود به ریسک خالص مجاز است.",
            {"walk_forward": {k: v for k, v in walk.items() if k not in {"status", "reason"}},
             "market_behavior": behavior},
        ))
        direction = result.gates[2].evidence.get("direction") if len(result.gates) >= 3 else None
        market_intelligence = fetch_market_intelligence(
            asset,
            direction or "NONE",
            snapshot=snapshot,
            http=getattr(self.data, "http", None),
            min_dex_liquidity_usd=self.config.thresholds.min_dex_liquidity_usd,
        )
        market_intelligence_scoring = market_intelligence.get("scoring") or {}
        market_intelligence_confirmation_adjustment = float(
            market_intelligence_scoring.get("confirmation_adjustment") or 0.0
        )
        market_intelligence_risk_adjustment = float(
            market_intelligence_scoring.get("risk_adjustment") or 0.0
        )
        market_intelligence_opportunity_adjustment = float(
            market_intelligence_scoring.get("opportunity_adjustment") or 0.0
        )
        market_intelligence_confidence_penalty = float(
            market_intelligence_scoring.get("confidence_penalty") or 0.0
        )
        exchange_net = whale.get("net_exchange_flow_usd")
        wallet_net = whale.get("net_whale_transfer_usd")
        cex_net = whale.get("net_whale_trade_flow_usd")
        bias_points = []
        if exchange_net is not None:
            bias_points.append(-1 if exchange_net > 0 else 1 if exchange_net < 0 else 0)
        if wallet_net is not None:
            bias_points.append(1 if wallet_net > 0 else -1 if wallet_net < 0 else 0)
        if cex_net is not None:
            bias_points.append(1 if cex_net > 0 else -1 if cex_net < 0 else 0)
        bias_total = sum(bias_points)
        whale_bias = "UNKNOWN" if not bias_points else (
            "ACCUMULATION_PRESSURE" if bias_total > 0 else
            "DISTRIBUTION_PRESSURE" if bias_total < 0 else "MIXED_OR_BALANCED"
        )
        scoring = self.config.scoring
        adjustment = market_intelligence_confirmation_adjustment
        whale_risk_adjustment = 0.0
        sentiment_risk_adjustment = 0.0
        if direction in {"LONG", "SHORT"} and whale_bias != "UNKNOWN":
            aligned = (whale_bias == "ACCUMULATION_PRESSURE" and direction == "LONG") or (
                whale_bias == "DISTRIBUTION_PRESSURE" and direction == "SHORT"
            )
            opposed = (whale_bias == "ACCUMULATION_PRESSURE" and direction == "SHORT") or (
                whale_bias == "DISTRIBUTION_PRESSURE" and direction == "LONG"
            )
            adjustment += (
                scoring.whale_confirmation_aligned if aligned else
                scoring.whale_confirmation_opposed if opposed else 0.0
            )
            whale_risk_adjustment = (
                0.0 if aligned else
                scoring.whale_risk_opposed if opposed else
                scoring.whale_risk_neutral
            )
        sentiment_score = sentiment.get("score")
        if direction in {"LONG", "SHORT"} and sentiment_score is not None:
            aligned_sentiment = (sentiment_score >= 25 and direction == "LONG") or (
                sentiment_score <= -25 and direction == "SHORT"
            )
            opposed_sentiment = (sentiment_score <= -25 and direction == "LONG") or (
                sentiment_score >= 25 and direction == "SHORT"
            )
            adjustment += (
                scoring.sentiment_confirmation_aligned if aligned_sentiment else
                scoring.sentiment_confirmation_opposed if opposed_sentiment else 0.0
            )
            sentiment_risk_adjustment = (
                0.0 if aligned_sentiment else
                scoring.sentiment_risk_opposed if opposed_sentiment else
                scoring.sentiment_risk_neutral
            )
        if result.confirmation_score is not None:
            result.confirmation_score = round(max(0.0, min(100.0, result.confirmation_score + adjustment)), 2)
        risk_adjustment = (
            market_intelligence_risk_adjustment
            + whale_risk_adjustment
            + sentiment_risk_adjustment
        )
        self.whale_store.record(asset.raw, whale, whale_bias)
        whale_history = self.whale_store.summary(asset.raw)
        if result.estimated_risk_score is not None:
            result.estimated_risk_score = round(
                max(0.0, min(100.0, result.estimated_risk_score + risk_adjustment)), 2
            )
        passed = sum(g.status == GateStatus.PASS for g in result.gates)
        evidence_confidence = round(passed / len(result.gates) * 100.0, 2) if result.gates else 0.0
        whale_alignment = "UNKNOWN"
        if direction in {"LONG", "SHORT"} and whale_bias != "UNKNOWN":
            if whale_bias == "MIXED_OR_BALANCED":
                whale_alignment = "NEUTRAL"
            elif (whale_bias == "ACCUMULATION_PRESSURE" and direction == "LONG") or (
                whale_bias == "DISTRIBUTION_PRESSURE" and direction == "SHORT"
            ):
                whale_alignment = "ALIGNED"
            else:
                whale_alignment = "OPPOSED"
        sentiment_alignment = "UNKNOWN"
        if direction in {"LONG", "SHORT"} and sentiment_score is not None:
            if abs(float(sentiment_score)) < 25:
                sentiment_alignment = "NEUTRAL"
            elif (float(sentiment_score) >= 25 and direction == "LONG") or (
                float(sentiment_score) <= -25 and direction == "SHORT"
            ):
                sentiment_alignment = "ALIGNED"
            else:
                sentiment_alignment = "OPPOSED"
        confidence_penalty = (
            market_intelligence_confidence_penalty
            + (
                scoring.confidence_whale_opposed if whale_alignment == "OPPOSED" else
                scoring.confidence_whale_neutral if whale_alignment == "NEUTRAL" else 0.0
            )
            + (
                scoring.confidence_sentiment_opposed
                if sentiment_alignment == "OPPOSED" else 0.0
            )
        )
        if (
            whale_alignment == "OPPOSED" and sentiment_alignment == "ALIGNED"
        ) or (
            whale_alignment == "ALIGNED" and sentiment_alignment == "OPPOSED"
        ):
            confidence_penalty += scoring.confidence_cross_divergence
        confidence_level = round(max(0.0, evidence_confidence - confidence_penalty), 2)

        # Opportunity is adjusted on its own scale; it is never copied from confirmation.
        opportunity_whale_adjustment = (
            scoring.opportunity_whale_aligned if whale_alignment == "ALIGNED" else
            scoring.opportunity_whale_opposed if whale_alignment == "OPPOSED" else 0.0
        )
        opportunity_sentiment_adjustment = (
            scoring.opportunity_sentiment_aligned if sentiment_alignment == "ALIGNED" else
            scoring.opportunity_sentiment_opposed if sentiment_alignment == "OPPOSED" else 0.0
        )
        opportunity_adjustment = (
            opportunity_whale_adjustment
            + opportunity_sentiment_adjustment
            + market_intelligence_opportunity_adjustment
        )
        if result.opportunity_rank is not None:
            result.opportunity_rank = round(
                max(0.0, min(100.0, float(result.opportunity_rank) + opportunity_adjustment)), 2
            )

        result.metrics.update({
            "whale_status": whale_status, "whale_bias": whale_bias,
            "whale_history_count": whale_history["observation_count"],
            "whale_recent_history_bias": whale_history["recent_bias"],
            "whale_exchange_net_flow_usd": exchange_net,
            "whale_wallet_net_transfer_usd": wallet_net,
            "whale_cex_large_trade_net_usd": cex_net,
            "whale_sentiment_adjustment": adjustment - market_intelligence_confirmation_adjustment,
            "market_intelligence": market_intelligence,
            "market_intelligence_confirmation_adjustment": market_intelligence_confirmation_adjustment,
            "market_intelligence_risk_adjustment": market_intelligence_risk_adjustment,
            "market_intelligence_opportunity_adjustment": market_intelligence_opportunity_adjustment,
            "market_intelligence_confidence_penalty": market_intelligence_confidence_penalty,
            "whale_risk_adjustment": whale_risk_adjustment,
            "sentiment_risk_adjustment": sentiment_risk_adjustment,
            "combined_risk_adjustment": risk_adjustment,
            "market_sentiment_score": sentiment_score,
            "market_sentiment_label": sentiment.get("label"),
            "fear_greed_value": (sentiment.get("fear_greed") or {}).get("value"),
            "fear_greed_attribution": (sentiment.get("fear_greed") or {}).get("attribution"),
            "fear_greed_scope": (sentiment.get("fear_greed") or {}).get("scope"),
            "sentiment_coverage_status": sentiment_status,
            "mandatory_gate_count": len(result.gates),
            "risk_score": result.estimated_risk_score,
            "opportunity_rank": result.opportunity_rank,
            "opportunity_whale_adjustment": opportunity_whale_adjustment,
            "opportunity_sentiment_adjustment": opportunity_sentiment_adjustment,
            "opportunity_total_adjustment": opportunity_adjustment,
            "confidence_level_pct": confidence_level,
            "confidence_level_semantics": "Evidence coverage adjusted for whale/market/sentiment disagreement; not a probability of profit.",
            "whale_alignment": whale_alignment,
            "sentiment_alignment": sentiment_alignment,
            "confidence_penalty_pct": confidence_penalty,
            "walk_forward_status": walk.get("status"),
            "walk_forward_fold_count": walk.get("fold_count"),
            "walk_forward_worst_fold_drawdown_pct": walk.get("worst_fold_drawdown_pct"),
            "evidence_confidence_pct": evidence_confidence,
        })
        all_pass = all(g.status == GateStatus.PASS for g in result.gates)
        failed_gates = [g for g in result.gates if g.status == GateStatus.FAIL]
        unknown_gates = [g for g in result.gates if g.status == GateStatus.UNKNOWN]
        confirmation_blocked = (
            result.confirmation_score is None
            or result.confirmation_score < self.config.thresholds.min_confirmation_score
        )
        chart_pattern = result.metrics.get("chart_pattern") or {}
        structure_conflict = contradicts_confirmed_breakout(direction, chart_pattern)
        result.metrics["chart_structure_conflict"] = structure_conflict
        risk_missing = result.estimated_risk_score is None
        risk_too_high = (
            result.estimated_risk_score is not None
            and result.estimated_risk_score >= self.config.thresholds.max_estimated_risk_score
        )
        result.metrics["decision_blockers"] = {
            "failed_filters": [{"id": g.gate_id, "name": g.name_fa} for g in failed_gates],
            "unknown_filters": [{"id": g.gate_id, "name": g.name_fa} for g in unknown_gates],
            "confirmation_below_threshold": confirmation_blocked,
            "risk_score_missing": risk_missing,
            "risk_score_at_or_above_limit": risk_too_high,
            "confirmed_structure_breakout_opposes_direction": structure_conflict,
            "base_engine_returned_no_trade": result.signal == Signal.NO_TRADE,
        }
        if result.signal != Signal.NO_TRADE and (
            not all_pass or confirmation_blocked or risk_missing or risk_too_high or structure_conflict
        ):
            result.signal = Signal.NO_TRADE
        if result.signal == Signal.NO_TRADE:
            result.entry_price = None
            result.take_profit = None
            result.stop_loss = None
            result.expected_holding_days = None
            result.position_notional_usd = None
            result.risk_budget_usd = None
            if failed_gates or risk_too_high or structure_conflict:
                result.safety_status = SafetyStatus.HIGH_RISK
            elif unknown_gates or risk_missing:
                result.safety_status = SafetyStatus.UNKNOWN
            else:
                # A missed confirmation threshold is not the same as unknown risk.
                result.safety_status = SafetyStatus.LOW_RISK
            if failed_gates:
                result.summary_en = "NO TRADE: one or more mandatory filters failed."
                result.summary_fa = "معامله مسدود شد: حداقل یکی از ۱۲ فیلتر اجباری با وضعیت FAIL رد شده است."
            elif unknown_gates:
                result.summary_en = "NO TRADE: one or more mandatory filters are unknown."
                result.summary_fa = "معامله مسدود شد: وضعیت حداقل یکی از ۱۲ فیلتر اجباری نامشخص است."
            elif risk_missing:
                result.summary_en = "NO TRADE: risk score is unavailable."
                result.summary_fa = "هر ۱۲ فیلتر تأیید شده‌اند، اما Risk Score قابل محاسبه نیست؛ معامله انجام نمی‌شود."
            elif risk_too_high:
                result.summary_en = "NO TRADE: estimated risk is at or above the configured limit."
                result.summary_fa = "هر ۱۲ فیلتر تأیید شده‌اند، اما Risk Score به سقف مجاز رسیده یا از آن عبور کرده است."
            elif confirmation_blocked:
                result.summary_en = "NO TRADE: confirmation score is below the required threshold."
                result.summary_fa = "هر ۱۲ فیلتر تأیید شده‌اند، اما امتیاز تأیید به حداقل لازم نرسیده است."
            elif structure_conflict:
                result.summary_en = "NO TRADE: a confirmed triangle breakout opposes the market direction."
                result.summary_fa = "شکست تأییدشدهٔ الگوی مثلث خلاف جهت بازار است؛ سیگنال مسدود شد."
            else:
                result.summary_en = "NO TRADE: the base engine did not confirm a trade direction."
                result.summary_fa = "هر ۱۲ فیلتر تأیید شده‌اند، اما موتور جهت معاملاتی قابل تأییدی پیدا نکرده است."
        else:
            result.summary_en = "All 12 mandatory gates passed. Paper/research plan only; no order was submitted."
            result.summary_fa = "هر ۱۲ فیلتر تأیید شده‌اند؛ خروجی فقط برنامه پژوهشی و آزمایشی است و هیچ سفارشی ارسال نشده است."
        return result
