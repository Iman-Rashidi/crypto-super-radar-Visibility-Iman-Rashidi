"""Strict public BUY qualification gate for early Gem Hunter candidates.

A discovery candidate is never a trade signal. Only a complete, versioned
qualification record can pass this gate. Missing or stale evidence fails closed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional
import re


SCHEMA_VERSION = "gem-hunter-v2"
MIN_PASSED_FILTERS = 9
TOTAL_FILTERS = 12
MAX_DATA_AGE_SECONDS = 300.0
MAX_RISK_SCORE = 45.0
MAX_SLIPPAGE_BPS = 100.0

FILTER_IDS = (
    "trend",
    "momentum",
    "relative_strength",
    "volume_confirmation",
    "buy_sell_pressure",
    "capital_flow",
    "whale_activity",
    "liquidity_depth",
    "market_integrity",
    "contract_security",
    "holder_distribution",
    "market_regime_validation",
)
CRITICAL_GATES = ("security", "liquidity", "data_quality", "exit")


def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _valid_token_identity(chain: str, address: Any) -> bool:
    if not isinstance(address, str):
        return False
    if chain == "base":
        return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", address))
    if chain == "solana":
        return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address))
    return False


def _same_chain_address(chain: str, left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left == right if chain == "solana" else left.lower() == right.lower()


def _valid_wallet(chain: str, address: Any) -> bool:
    return _valid_token_identity(chain, address)


def _valid_tx_hash(chain: str, tx_hash: Any) -> bool:
    if not isinstance(tx_hash, str):
        return False
    if chain == "base":
        return bool(re.fullmatch(r"0x[a-fA-F0-9]{64}", tx_hash))
    if chain == "solana":
        return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,100}", tx_hash))
    return False


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_whale_evidence(chain: str, evidence: Any) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("status") != "PASS":
        return False
    if evidence.get("coverage_status") != "COMPLETE_FOR_TOKEN_LIFETIME":
        return False
    events = evidence.get("events")
    if not isinstance(events, list) or not events:
        return False
    buyers = set()
    valid_events = 0
    for event in events:
        if not isinstance(event, Mapping):
            return False
        wallet = event.get("wallet_address")
        tx_hash = event.get("tx_hash")
        kind = event.get("event_type")
        timestamp = _parse_timestamp(event.get("timestamp_utc"))
        amount_usd = _finite_number(event.get("amount_usd"))
        if not _valid_wallet(chain, wallet) or not _valid_tx_hash(chain, tx_hash):
            return False
        if kind not in {"SWAP_BUY", "SWAP_SELL"} or timestamp is None:
            return False
        if amount_usd is None or amount_usd <= 0:
            return False
        valid_events += 1
        if kind == "SWAP_BUY":
            buyers.add(wallet.lower())
    required_buyers = _finite_number(evidence.get("independent_buy_wallets"))
    if required_buyers is None or required_buyers < 2:
        return False
    return len(buyers) >= 2 and valid_events >= 2


def is_public_buy_candidate(candidate: Any, *, now: Optional[datetime] = None) -> bool:
    """True only for a schema-valid, evidence-complete spot BUY candidate."""
    if not isinstance(candidate, Mapping):
        return False
    chain = str(candidate.get("chain") or "").lower()
    if not _valid_token_identity(chain, candidate.get("token_address")):
        return False
    if not isinstance(candidate.get("pool_address"), str) or not candidate["pool_address"].strip():
        return False
    if not isinstance(candidate.get("dex"), str) or not candidate["dex"].strip():
        return False
    if candidate.get("trade_authorization") != "BUY":
        return False
    if candidate.get("candidate_status") != "BUY":
        return False

    # v2 requires fresh, exact-pool cross-source evidence and an explicit
    # contract-security PASS. Legacy qualification payloads cannot bypass it.
    reconciliation = candidate.get("market_reconciliation")
    if not isinstance(reconciliation, Mapping) or reconciliation.get("status") != "PASS":
        return False
    if not _valid_token_identity(chain, reconciliation.get("token_address")):
        return False
    if not _same_chain_address(chain, reconciliation.get("token_address"), candidate.get("token_address")):
        return False
    if not _same_chain_address(chain, reconciliation.get("pool_address"), candidate.get("pool_address")):
        return False
    source_count = _finite_number(reconciliation.get("source_count"))
    if source_count is None or source_count < 2:
        return False
    reconciliation_sources = reconciliation.get("sources")
    providers = {
        str(row.get("provider")) for row in reconciliation_sources
        if isinstance(row, Mapping) and row.get("provider")
    } if isinstance(reconciliation_sources, list) else set()
    if not {"DEXSCREENER", "GECKOTERMINAL"}.issubset(providers):
        return False
    verified_liquidity = _finite_number(reconciliation.get("verified_liquidity_usd"))
    verified_volume = _finite_number(reconciliation.get("verified_volume_24h_usd"))
    if verified_liquidity is None or verified_liquidity < 50_000:
        return False
    if verified_volume is None or verified_volume < 100_000:
        return False

    security_evidence = candidate.get("security_evidence")
    if not isinstance(security_evidence, Mapping) or security_evidence.get("status") != "PASS":
        return False

    qualification = candidate.get("qualification")
    if not isinstance(qualification, Mapping):
        return False
    if qualification.get("schema_version") != SCHEMA_VERSION:
        return False
    if qualification.get("decision") != "BUY" or qualification.get("market_type") != "SPOT":
        return False

    filters = qualification.get("filters")
    if not isinstance(filters, Mapping) or set(filters.keys()) != set(FILTER_IDS):
        return False
    if any(filters.get(filter_id) not in {"PASS", "FAIL", "UNKNOWN"} for filter_id in FILTER_IDS):
        return False
    if sum(filters[filter_id] == "PASS" for filter_id in FILTER_IDS) < MIN_PASSED_FILTERS:
        return False
    if filters["whale_activity"] != "PASS" or filters["contract_security"] != "PASS":
        return False

    critical = qualification.get("critical_gates")
    if not isinstance(critical, Mapping) or any(critical.get(gate) != "PASS" for gate in CRITICAL_GATES):
        return False

    opportunity = _finite_number(qualification.get("opportunity_score"))
    risk = _finite_number(qualification.get("risk_score"))
    if opportunity is None or opportunity < 80.0 or risk is None or risk >= MAX_RISK_SCORE:
        return False

    metrics = qualification.get("market_metrics")
    if not isinstance(metrics, Mapping):
        return False
    checks = (
        ("price_usd", lambda v: v > 0),
        ("liquidity_usd", lambda v: v >= 50_000),
        ("volume_24h_usd", lambda v: v >= 100_000),
        ("volume_to_liquidity_24h", lambda v: 1.0 <= v <= 10.0),
        ("holder_count", lambda v: v >= 300),
        ("unique_traders_24h", lambda v: v >= 100),
        ("buy_volume_share_pct", lambda v: v >= 55.0),
        ("transactions_24h", lambda v: v >= 200),
        ("pool_age_hours", lambda v: v >= 24.0),
        ("top1_independent_holder_pct", lambda v: v <= 5.0),
        ("top10_independent_holders_pct", lambda v: v <= 30.0),
        ("related_wallet_cluster_pct", lambda v: v <= 20.0),
        ("estimated_slippage_bps", lambda v: 0 <= v <= MAX_SLIPPAGE_BPS),
    )
    for key, predicate in checks:
        value = _finite_number(metrics.get(key))
        if value is None or not predicate(value):
            return False

    sources = qualification.get("market_data_sources")
    if not isinstance(sources, list) or len({str(x).strip() for x in sources if str(x).strip()}) < 2:
        return False

    as_of = _parse_timestamp(qualification.get("data_as_of_utc"))
    if as_of is None:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_seconds = (now.astimezone(timezone.utc) - as_of).total_seconds()
    if age_seconds < -30 or age_seconds > MAX_DATA_AGE_SECONDS:
        return False

    # Cross-source pool and contract-security evidence must also be fresh.
    for evidence in (reconciliation, security_evidence):
        checked_at = _parse_timestamp(evidence.get("checked_at_utc"))
        if checked_at is None:
            return False
        evidence_age = (now.astimezone(timezone.utc) - checked_at).total_seconds()
        if evidence_age < -30 or evidence_age > MAX_DATA_AGE_SECONDS:
            return False

    return _valid_whale_evidence(chain, qualification.get("whale_evidence"))


def public_buy_candidates(candidates: Any, *, limit: int = 20,
                          now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Return only complete, independently qualified BUY candidates."""
    if not isinstance(candidates, list):
        return []
    limit = max(1, min(int(limit), 50))
    qualified = [dict(item) for item in candidates
                 if is_public_buy_candidate(item, now=now)]
    qualified.sort(
        key=lambda item: (
            float(item["qualification"]["opportunity_score"]),
            -float(item["qualification"]["risk_score"]),
        ),
        reverse=True,
    )
    return qualified[:limit]
