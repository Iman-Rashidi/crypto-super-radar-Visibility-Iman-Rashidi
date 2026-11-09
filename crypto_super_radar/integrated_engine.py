"""Connect the new whale/sentiment modules to the legacy nine-gate engine."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .cex_whales import CexWhaleScanner
from .config import AppConfig
from .engine import ConfirmationEngine
from .evm_security import screen_evm_token
from .evm_transfers import EvmTransferScanner
from .market_sentiment import fetch_market_sentiment
from .models import AssetSpec, GateResult, GateStatus, SafetyStatus, Signal
from .providers import MarketDataService, PublicHttpClient
from .solana_whales import SolanaWhaleScanner
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
    ):
        self.config = config or AppConfig()
        self.data = WhaleAwareMarketDataService(self.config)
        self.base = ConfirmationEngine(self.config, data_service=self.data)
        self.news_feed_urls = news_feed_urls or ["https://www.coindesk.com/arc/outboundfeeds/rss/"]
        self.social_feed_urls = social_feed_urls or ["https://www.reddit.com/r/CryptoCurrency/.rss"]
        self.sentiment_cache: dict = {}

    def analyze(
        self,
        asset_input: str | AssetSpec,
        headlines: Optional[Iterable[str]] = None,
        open_portfolio_risk_pct: float = 0.0,
        social_posts: Optional[Iterable[str]] = None,
    ):
        asset = asset_input if isinstance(asset_input, AssetSpec) else AssetSpec.parse(asset_input)
        supplied = list(headlines or [])
        sentiment = fetch_market_sentiment(
            self.news_feed_urls,
            social_feed_urls=self.social_feed_urls,
            social_text=list(social_posts or []),
            cache=self.sentiment_cache,
            limiter=self.data.http.limiter,
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
        whale = self.data.whale_evidence.get(asset.raw, {"status": "UNKNOWN"})
        whale_status = str(whale.get("status", "UNKNOWN")).upper()
        whale_flow_available = any(whale.get(key) is not None for key in (
            "net_exchange_flow_usd", "net_whale_transfer_usd", "net_whale_trade_flow_usd",
        ))
        whale_gate_status = (
            GateStatus.PASS if whale_status == "KNOWN" and whale_flow_available
            else GateStatus.FAIL if whale_status == "FAIL" else GateStatus.UNKNOWN
        )
        result.gates.append(GateResult(
            10, "Whale intelligence", "ردیابی رفتار نهنگ‌ها",
            whale_gate_status,
            whale.get("reason", whale.get("interpretation", "Whale flow is unavailable or incomplete.")),
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
        direction = result.gates[2].evidence.get("direction") if len(result.gates) >= 3 else None
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
        adjustment = 0.0
        whale_risk_adjustment = 0.0
        sentiment_risk_adjustment = 0.0
        if direction in {"LONG", "SHORT"} and whale_bias != "UNKNOWN":
            aligned = (whale_bias == "ACCUMULATION_PRESSURE" and direction == "LONG") or (
                whale_bias == "DISTRIBUTION_PRESSURE" and direction == "SHORT"
            )
            opposed = (whale_bias == "ACCUMULATION_PRESSURE" and direction == "SHORT") or (
                whale_bias == "DISTRIBUTION_PRESSURE" and direction == "LONG"
            )
            adjustment += 5.0 if aligned else -10.0 if opposed else 0.0
            whale_risk_adjustment = 0.0 if aligned else 10.0 if opposed else 3.0
        sentiment_score = sentiment.get("score")
        if direction in {"LONG", "SHORT"} and sentiment_score is not None:
            aligned_sentiment = (sentiment_score >= 25 and direction == "LONG") or (
                sentiment_score <= -25 and direction == "SHORT"
            )
            opposed_sentiment = (sentiment_score <= -25 and direction == "LONG") or (
                sentiment_score >= 25 and direction == "SHORT"
            )
            adjustment += 3.0 if aligned_sentiment else -3.0 if opposed_sentiment else 0.0
            sentiment_risk_adjustment = 0.0 if aligned_sentiment else 5.0 if opposed_sentiment else 1.0
        if result.confirmation_score is not None:
            result.confirmation_score = round(max(0.0, min(100.0, result.confirmation_score + adjustment)), 2)
            result.opportunity_rank = result.confirmation_score
        risk_adjustment = whale_risk_adjustment + sentiment_risk_adjustment
        if result.estimated_risk_score is not None:
            result.estimated_risk_score = round(min(100.0, result.estimated_risk_score + risk_adjustment), 2)
        passed = sum(g.status == GateStatus.PASS for g in result.gates)
        result.metrics.update({
            "whale_status": whale_status, "whale_bias": whale_bias,
            "whale_exchange_net_flow_usd": exchange_net,
            "whale_wallet_net_transfer_usd": wallet_net,
            "whale_cex_large_trade_net_usd": cex_net,
            "whale_sentiment_adjustment": adjustment,
            "whale_risk_adjustment": whale_risk_adjustment,
            "sentiment_risk_adjustment": sentiment_risk_adjustment,
            "combined_risk_adjustment": risk_adjustment,
            "market_sentiment_score": sentiment_score,
            "market_sentiment_label": sentiment.get("label"),
            "fear_greed_value": (sentiment.get("fear_greed") or {}).get("value"),
            "sentiment_coverage_status": sentiment_status,
            "mandatory_gate_count": len(result.gates),
            "evidence_confidence_pct": round(passed / len(result.gates) * 100.0, 2),
        })
        all_pass = all(g.status == GateStatus.PASS for g in result.gates)
        if result.signal != Signal.NO_TRADE and (
            not all_pass or result.confirmation_score is None or
            result.confirmation_score < self.config.thresholds.min_confirmation_score or
            result.estimated_risk_score is None or
            result.estimated_risk_score >= self.config.thresholds.max_estimated_risk_score
        ):
            result.signal = Signal.NO_TRADE
        if result.signal == Signal.NO_TRADE:
            result.entry_price = None
            result.take_profit = None
            result.stop_loss = None
            result.expected_holding_days = None
            result.position_notional_usd = None
            result.risk_budget_usd = None
            result.safety_status = (
                SafetyStatus.HIGH_RISK if any(g.status == GateStatus.FAIL for g in result.gates)
                or (result.estimated_risk_score is not None and result.estimated_risk_score >= self.config.thresholds.max_estimated_risk_score)
                else SafetyStatus.UNKNOWN
            )
            result.summary_en = "NO TRADE: all 11 mandatory gates must pass; whale and sentiment evidence are required."
            result.summary_fa = "عدم معامله: هر ۱۱ فیلتر اجباری باید تأیید شوند؛ تحلیل نهنگ‌ها و احساسات بازار الزامی است."
        else:
            result.summary_en = "All 11 mandatory gates passed. Paper/research plan only; no order was submitted."
            result.summary_fa = "هر ۱۱ فیلتر تأیید شده‌اند؛ فقط برنامه پژوهشی و آزمایشی است و سفارشی ارسال نشده است."
        return result
