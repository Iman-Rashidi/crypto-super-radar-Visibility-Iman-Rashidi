"""Whale-flow intelligence from normalized public on-chain transfer events.

This module classifies observed transfers; it never equates a large transfer with
a buy or sell. Wallet labels are evidence supplied by the upstream data source.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Optional


@dataclass(frozen=True)
class WhaleEvent:
    tx_hash: str
    timestamp: int
    token_address: str
    from_address: str
    to_address: str
    amount: float
    amount_usd: Optional[float]
    from_label: Optional[str] = None
    to_label: Optional[str] = None
    chain: Optional[str] = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "WhaleEvent":
        required = ("tx_hash", "timestamp", "token_address", "from_address",
                    "to_address", "amount")
        if any(row.get(key) in (None, "") for key in required):
            raise ValueError("Transfer event is missing required identity fields")
        amount = float(row["amount"])
        if amount <= 0:
            raise ValueError("Transfer amount must be positive")
        usd = row.get("amount_usd")
        usd_value = None if usd is None else float(usd)
        if usd_value is not None and usd_value < 0:
            raise ValueError("USD value cannot be negative")
        return cls(
            tx_hash=str(row["tx_hash"]), timestamp=int(row["timestamp"]),
            token_address=str(row["token_address"]).lower(),
            from_address=str(row["from_address"]).lower(),
            to_address=str(row["to_address"]).lower(), amount=amount,
            amount_usd=usd_value,
            from_label=_label(row.get("from_label")),
            to_label=_label(row.get("to_label")),
            chain=row.get("chain"),
        )


def _label(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def classify_transfer(event: WhaleEvent, whale_threshold_usd: float = 100_000.0) -> str:
    """Return a conservative flow label, not a trade-direction prediction."""
    if event.amount_usd is None:
        return "UNKNOWN_VALUE"
    if event.amount_usd < whale_threshold_usd:
        return "BELOW_WHALE_THRESHOLD"
    source = event.from_label or ""
    destination = event.to_label or ""
    exchange_names = {"exchange", "cex", "centralized_exchange"}
    if source in exchange_names and destination not in exchange_names:
        return "EXCHANGE_OUTFLOW"
    if destination in exchange_names and source not in exchange_names:
        return "EXCHANGE_INFLOW"
    if source in {"burn", "dead"} or destination in {"burn", "dead"}:
        return "BURN_OR_DEAD_WALLET"
    if source and destination and source == destination:
        return "SAME_ENTITY_INTERNAL_TRANSFER"
    if source in {"whale", "smart_money", "fund", "market_maker"} or destination in {
        "whale", "smart_money", "fund", "market_maker"
    }:
        return "LARGE_ENTITY_TRANSFER"
    return "LARGE_UNATTRIBUTED_TRANSFER"


def analyze_whale_activity(
    rows: Iterable[Mapping[str, Any] | WhaleEvent],
    whale_threshold_usd: float = 100_000.0,
) -> dict[str, Any]:
    """Aggregate known events; missing USD values remain unknown, never zero."""
    events = [r if isinstance(r, WhaleEvent) else WhaleEvent.from_mapping(r) for r in rows]
    if not events:
        return {
            "status": "UNKNOWN", "event_count": 0, "whale_event_count": None,
            "exchange_inflow_usd": None, "exchange_outflow_usd": None,
            "net_exchange_flow_usd": None, "net_exchange_flow_pct": None,
            "whale_inflow_usd": None, "whale_outflow_usd": None,
            "net_whale_transfer_usd": None, "whale_flow_event_count": 0,
            "classification_counts": {}, "unique_wallet_count": 0,
            "interpretation": "No transfer events supplied; whale activity is unknown.",
        }
    classified = [(event, classify_transfer(event, whale_threshold_usd)) for event in events]
    counts: dict[str, int] = {}
    for _, label in classified:
        counts[label] = counts.get(label, 0) + 1
    inflow_values = [e.amount_usd for e, label in classified if label == "EXCHANGE_INFLOW"]
    outflow_values = [e.amount_usd for e, label in classified if label == "EXCHANGE_OUTFLOW"]
    whale_entities = {"whale", "smart_money", "fund", "market_maker"}
    whale_in = [e.amount_usd for e in events
                if (e.to_label or "") in whale_entities and (e.from_label or "") not in whale_entities]
    whale_out = [e.amount_usd for e in events
                 if (e.from_label or "") in whale_entities and (e.to_label or "") not in whale_entities]
    inflow = sum(v for v in inflow_values if v is not None) if inflow_values else 0.0
    outflow = sum(v for v in outflow_values if v is not None) if outflow_values else 0.0
    known_exchange = len(inflow_values) + len(outflow_values)
    known_values = sum(v is not None for v in inflow_values + outflow_values)
    net = None if known_exchange == 0 or known_values != known_exchange else inflow - outflow
    whale_count_known = len(whale_in) + len(whale_out)
    whale_values_known = sum(v is not None for v in whale_in + whale_out)
    whale_inflow = sum(v for v in whale_in if v is not None) if whale_in else None
    whale_outflow = sum(v for v in whale_out if v is not None) if whale_out else None
    net_whale = None if whale_count_known == 0 or whale_values_known != whale_count_known else (
        sum(whale_in) - sum(whale_out)
    )
    unique_wallets = {e.from_address for e in events} | {e.to_address for e in events}
    whale_count = sum(label not in {"BELOW_WHALE_THRESHOLD", "UNKNOWN_VALUE"} for _, label in classified)
    interpretations = []
    if net is None:
        interpretations.append("Exchange-flow direction is UNKNOWN: exchange labels or USD values are incomplete.")
    elif net > 0:
        interpretations.append("Net exchange inflow observed; this can increase potential sell-side supply, but is not proof of selling.")
    elif net < 0:
        interpretations.append("Net exchange outflow observed; this can indicate custody or accumulation, but is not proof of buying.")
    else:
        interpretations.append("Exchange inflow and outflow are balanced in the observed sample.")
    if net_whale is not None and net_whale > 0:
        interpretations.append("Net token transfer into labelled whale wallets observed; this may indicate accumulation, not proof of spot buying.")
    elif net_whale is not None and net_whale < 0:
        interpretations.append("Net token transfer out of labelled whale wallets observed; this may indicate distribution, not proof of selling.")
    elif net_whale == 0:
        interpretations.append("Labelled whale-wallet inflows and outflows are balanced in the observed sample.")
    interpretation = " ".join(interpretations)
    return {
        "status": "KNOWN" if all(e.amount_usd is not None for e in events) else "PARTIAL",
        "event_count": len(events), "whale_event_count": whale_count,
        "exchange_inflow_usd": inflow if inflow_values else None,
        "exchange_outflow_usd": outflow if outflow_values else None,
        "net_exchange_flow_usd": net,
        "net_exchange_flow_pct": None,
        "whale_inflow_usd": whale_inflow,
        "whale_outflow_usd": whale_outflow,
        "net_whale_transfer_usd": net_whale,
        "whale_flow_event_count": whale_count_known,
        "classification_counts": counts,
        "unique_wallet_count": len(unique_wallets),
        "interpretation": interpretation,
        "caveat": "Transfers are not trades. Labels can be incomplete; wallet clusters and beneficial ownership are not inferred.",
    }



def extract_goplus_whale_watchlist(
    token_security: Mapping[str, Any],
    *,
    token_price_usd: Optional[float],
    min_balance_usd: float = 100_000.0,
) -> dict[str, Any]:
    """Build a top-holder watchlist from GoPlus supply/holder evidence.

    The list is PARTIAL by design: GoPlus supplies a top-holder sample, not all
    holders, and token accounts/contracts are not guaranteed beneficial owners.
    """
    if token_price_usd is None or token_price_usd <= 0:
        return {"status": "UNKNOWN", "watchlist": [],
                "reason": "A valid USD price is required to value holder balances."}
    try:
        supply_raw = float(token_security["total_supply"])
        decimals = int(token_security["decimals"])
        holders = token_security["holders"]
        if supply_raw <= 0 or not 0 <= decimals <= 36 or not isinstance(holders, list):
            raise ValueError("invalid supply metadata")
    except (KeyError, TypeError, ValueError):
        return {"status": "UNKNOWN", "watchlist": [],
                "reason": "GoPlus total supply, decimals or holder list is missing."}
    total_supply = supply_raw / (10 ** decimals)
    watchlist = []
    for holder in holders[:10]:
        try:
            address = str(holder["address"]).lower()
            percent = float(holder["percent"])
            if not re.fullmatch(r"0x[a-f0-9]{40}", address) or not 0 <= percent <= 1:
                continue
            balance_tokens = total_supply * percent
            balance_usd = balance_tokens * float(token_price_usd)
            if balance_usd >= min_balance_usd:
                watchlist.append({
                    "address": address, "label": "whale",
                    "tag": holder.get("tag"),
                    "balance_tokens": balance_tokens,
                    "balance_usd": balance_usd,
                    "supply_pct": percent * 100.0,
                    "is_contract": holder.get("is_contract"),
                    "is_locked": holder.get("is_locked"),
                })
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "status": "PARTIAL", "watchlist": watchlist,
        "sampled_holders": min(10, len(holders)),
        "reason": "Top-holder sample only; no complete holder universe or beneficial-owner clustering.",
    }
