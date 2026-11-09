"""Central configuration with documented, conservative defaults."""

from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class RateLimitConfig:
    """Minimum interval per public API host, in seconds."""

    host_intervals: Dict[str, float] = field(default_factory=lambda: {
        # GeckoTerminal's public OpenAPI page currently states ~10 calls/min.
        # We intentionally use that conservative limit despite a newer support
        # page mentioning 30 calls/min; the API documentation may lag.
        "api.geckoterminal.com": 6.0,
        "api.dexscreener.com": 0.21,
        "api.binance.com": 0.25,
        "data-api.binance.vision": 0.25,
        "api.bybit.com": 0.25,
        "api.mainnet-beta.solana.com": 1.0,
        "ethereum-rpc.publicnode.com": 1.0,
        "mainnet.base.org": 1.0,
    })


@dataclass(frozen=True)
class ThresholdConfig:
    """Initial screening thresholds; calibrate only with out-of-sample tests."""

    provider_discrepancy_pct: float = 5.0
    max_candle_age_hours: float = 48.0
    min_dex_liquidity_usd: float = 250_000.0
    min_dex_volume_24h_usd: float = 50_000.0
    min_cex_quote_volume_24h_usd: float = 1_000_000.0
    max_volume_to_liquidity: float = 5.0
    min_relative_strength_30d_pct: float = 0.0
    min_confirmation_score: float = 75.0
    max_estimated_risk_score: float = 45.0
    max_backtest_drawdown_pct: float = 25.0
    min_oos_sharpe: float = 0.25
    min_oos_profit_factor: float = 1.05
    min_oos_bars: int = 45
    min_history_bars: int = 180
    max_sol_top10_account_concentration_pct: float = 60.0
    min_risk_reward: float = 2.0


@dataclass(frozen=True)
class RiskConfig:
    """Paper-only sizing defaults; no exchange orders are ever submitted."""

    paper_equity_usd: float = 10_000.0
    risk_per_trade_pct: float = 0.50
    max_position_notional_pct: float = 10.0
    max_pool_liquidity_participation_pct: float = 1.0
    max_open_portfolio_risk_pct: float = 2.0
    fee_bps_per_side: float = 8.0
    slippage_bps_per_side: float = 10.0
    atr_stop_multiple: float = 2.0
    atr_target_multiple: float = 4.0


@dataclass(frozen=True)
class IntelligenceScoringConfig:
    """Explicit heuristic weights; calibrate with walk-forward evidence, not intuition alone."""

    whale_confirmation_aligned: float = 5.0
    whale_confirmation_opposed: float = -10.0
    sentiment_confirmation_aligned: float = 3.0
    sentiment_confirmation_opposed: float = -3.0

    whale_risk_opposed: float = 10.0
    whale_risk_neutral: float = 3.0
    sentiment_risk_opposed: float = 5.0
    sentiment_risk_neutral: float = 1.0

    opportunity_whale_aligned: float = 4.0
    opportunity_whale_opposed: float = -6.0
    opportunity_sentiment_aligned: float = 2.0
    opportunity_sentiment_opposed: float = -3.0

    confidence_whale_opposed: float = 15.0
    confidence_whale_neutral: float = 5.0
    confidence_sentiment_opposed: float = 10.0
    confidence_cross_divergence: float = 5.0


@dataclass(frozen=True)
class AppConfig:
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scoring: IntelligenceScoringConfig = field(default_factory=IntelligenceScoringConfig)
    request_timeout_seconds: int = 15
    max_http_retries: int = 3
    user_agent: str = "CRYPTO-SUPER-RADAR/0.1 public-data-research)"
