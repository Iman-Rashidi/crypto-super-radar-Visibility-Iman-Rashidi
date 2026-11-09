"""Typed domain models used by filters and the decision engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class GateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class Signal(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO TRADE"


class SafetyStatus(str, Enum):
    LOW_RISK = "LOW_RISK"
    HIGH_RISK = "HIGH_RISK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AssetSpec:
    """An asset identifier: solana:address, base:address, eth:address, cex:BTCUSDT."""

    raw: str
    market_type: str
    chain: Optional[str] = None
    address: Optional[str] = None
    symbol: Optional[str] = None

    @classmethod
    def parse(cls, value: str) -> "AssetSpec":
        if not isinstance(value, str) or ":" not in value:
            raise ValueError("Asset must use chain:address or cex:SYMBOL format")
        prefix, identifier = value.split(":", 1)
        prefix = prefix.strip().lower()
        identifier = identifier.strip()
        if not identifier:
            raise ValueError("Asset identifier cannot be empty")
        if prefix in {"cex", "perp"}:
            symbol = identifier.upper()
            if not symbol.isalnum():
                raise ValueError("Market symbol must contain only letters and numbers")
            return cls(value, "cex" if prefix == "cex" else "perp", symbol=symbol)
        aliases = {"ethereum": "eth", "eth": "eth", "solana": "solana", "base": "base"}
        if prefix not in aliases:
            raise ValueError(f"Unsupported chain '{prefix}'. Use solana, base, eth or cex")
        if len(identifier) < 10:
            raise ValueError("Token address appears too short")
        return cls(value, "dex", chain=aliases[prefix], address=identifier)


@dataclass
class GateResult:
    gate_id: int
    name_en: str
    name_fa: str
    status: GateStatus
    reason_en: str
    reason_fa: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass
class MarketSnapshot:
    asset: AssetSpec
    price_usd: float
    volume_24h_usd: float
    liquidity_usd: Optional[float]
    price_discrepancy_pct: float
    volume_discrepancy_pct: Optional[float]
    provider_a: str
    provider_b: str
    pool_address: Optional[str] = None
    pool_created_at: Optional[str] = None
    pair_address: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    asset: str
    signal: Signal
    safety_status: SafetyStatus
    confirmation_score: Optional[float]
    estimated_risk_score: Optional[float]
    opportunity_rank: Optional[float]
    entry_price: Optional[float]
    take_profit: Optional[float]
    stop_loss: Optional[float]
    expected_holding_days: Optional[int]
    position_notional_usd: Optional[float]
    risk_budget_usd: Optional[float]
    gates: List[GateResult]
    summary_en: str
    summary_fa: str
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> Dict[str, Any]:
        return {
            "asset": self.asset,
            "signal": self.signal.value,
            "safety_status": self.safety_status.value,
            "confirmation_score": self.confirmation_score,
            "estimated_risk_score": self.estimated_risk_score,
            "opportunity_rank": self.opportunity_rank,
            "entry_price_usd": self.entry_price,
            "take_profit_usd": self.take_profit,
            "stop_loss_usd": self.stop_loss,
            "expected_holding_days": self.expected_holding_days,
            "position_notional_usd": self.position_notional_usd,
            "risk_budget_usd": self.risk_budget_usd,
            "gate_pass_count": sum(g.status == GateStatus.PASS for g in self.gates),
            "gate_fail_count": sum(g.status == GateStatus.FAIL for g in self.gates),
            "gate_unknown_count": sum(g.status == GateStatus.UNKNOWN for g in self.gates),
            "gates": [g.to_dict() for g in self.gates],
            "summary_en": self.summary_en,
            "summary_fa": self.summary_fa,
            **self.metrics,
        }
