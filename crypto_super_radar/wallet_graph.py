"""Evidence-based wallet graph analytics for token-launch research.

This module is deliberately advisory. It never labels a token safe and never
authorizes a trade. Missing or insufficient evidence is represented explicitly.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping


PUBLIC_FUNDING_TYPES = {
    "cex", "exchange", "centralized_exchange", "bridge", "router",
    "aggregator", "known_service", "unknown",
}
VALID_EVENT_TYPES = {"buy", "sell", "transfer", "funding", "swap", "mint", "other"}


@dataclass(frozen=True)
class WalletEvent:
    chain: str
    wallet: str
    token: str
    event_type: str
    timestamp: int
    amount_usd: float | None = None
    funding_source: str | None = None
    funding_source_type: str = "unknown"
    deployer_relation: bool = False
    tx_hash: str | None = None
    received_at: int | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "WalletEvent":
        chain = str(row.get("chain") or "").strip().lower()
        wallet = str(row.get("wallet") or "").strip().lower()
        token = str(row.get("token") or "").strip().lower()
        event_type = str(row.get("event_type") or "other").strip().lower()
        if not chain or not wallet or not token:
            raise ValueError("chain, wallet and token are required")
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {event_type}")
        timestamp = _nonnegative_int(row.get("timestamp"), "timestamp")
        received = row.get("received_at")
        received_at = None if received is None else _nonnegative_int(received, "received_at")
        amount = row.get("amount_usd")
        amount_usd = None if amount is None else _finite_nonnegative(amount, "amount_usd")
        funding = row.get("funding_source")
        funding_type = str(row.get("funding_source_type") or "unknown").strip().lower()
        return cls(
            chain=chain, wallet=wallet, token=token, event_type=event_type,
            timestamp=timestamp, amount_usd=amount_usd,
            funding_source=None if funding in (None, "") else str(funding).strip().lower(),
            funding_source_type=funding_type,
            deployer_relation=bool(row.get("deployer_relation", False)),
            tx_hash=None if row.get("tx_hash") in (None, "") else str(row["tx_hash"]).lower(),
            received_at=received_at,
        )


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    number = int(value)
    if number < 0 or number != float(value):
        raise ValueError(f"{name} must be a non-negative integer")
    return number


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and non-negative")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _amount_similarity(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if a == 0 and b == 0:
        return 1.0
    if min(a, b) <= 0:
        return 0.0
    return min(a, b) / max(a, b)


class WalletGraphAnalytics:
    """Bounded candidate generation and explainable pair-level evidence."""

    def __init__(
        self,
        *,
        temporal_tolerance_seconds: int = 30,
        min_wallets: int = 5,
        min_events: int = 10,
        max_candidate_pairs: int = 100_000,
        max_events: int = 100_000,
    ) -> None:
        self.temporal_tolerance_seconds = max(1, int(temporal_tolerance_seconds))
        self.min_wallets = max(2, int(min_wallets))
        self.min_events = max(1, int(min_events))
        self.max_candidate_pairs = max(1, int(max_candidate_pairs))
        self.max_events = max(1, int(max_events))

    def analyze_token_distribution(
        self,
        events: Iterable[WalletEvent | Mapping[str, Any]],
        *,
        chain: str,
        token: str,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> dict[str, Any]:
        """Analyze one chain/token/time scope; no anomaly result is not a safety pass."""
        target_chain = str(chain).strip().lower()
        target_token = str(token).strip().lower()
        if not target_chain or not target_token:
            raise ValueError("chain and token scope are required")
        start = None if start_timestamp is None else _nonnegative_int(start_timestamp, "start_timestamp")
        end = None if end_timestamp is None else _nonnegative_int(end_timestamp, "end_timestamp")
        if start is not None and end is not None and end < start:
            raise ValueError("end_timestamp must be >= start_timestamp")

        normalized: list[WalletEvent] = []
        seen: set[tuple[Any, ...]] = set()
        invalid_count = 0
        duplicate_count = 0
        input_truncated = False
        for raw in events:
            if len(normalized) >= self.max_events:
                input_truncated = True
                break
            try:
                event = raw if isinstance(raw, WalletEvent) else WalletEvent.from_mapping(raw)
                # Re-validate directly constructed dataclasses at the boundary.
                if not event.chain or not event.wallet or not event.token:
                    raise ValueError("missing identity")
                if event.chain.lower() != target_chain or event.token.lower() != target_token:
                    continue
                if start is not None and event.timestamp < start:
                    continue
                if end is not None and event.timestamp > end:
                    continue
                if event.timestamp < 0 or (event.received_at is not None and event.received_at < 0):
                    raise ValueError("negative timestamp")
                if event.amount_usd is not None and (
                    not math.isfinite(float(event.amount_usd)) or float(event.amount_usd) < 0
                ):
                    raise ValueError("invalid amount")
                identity = (
                    event.chain.lower(), event.token.lower(), event.wallet.lower(),
                    event.event_type, event.timestamp, event.tx_hash,
                    event.amount_usd, event.funding_source, event.funding_source_type,
                )
                if identity in seen:
                    duplicate_count += 1
                    continue
                seen.add(identity)
                normalized.append(event)
            except (TypeError, ValueError, OverflowError):
                invalid_count += 1

        wallets: dict[str, list[WalletEvent]] = defaultdict(list)
        for event in normalized:
            wallets[event.wallet.lower()].append(event)
        unique_wallets = sorted(wallets)
        deployer_wallets = sorted({
            wallet for wallet, rows in wallets.items()
            if any(row.deployer_relation for row in rows)
        })

        if len(unique_wallets) < self.min_wallets or len(normalized) < self.min_events:
            return {
                "status": "INSUFFICIENT_DATA",
                "chain": target_chain, "token": target_token,
                "event_count": len(normalized), "unique_wallet_count": len(unique_wallets),
                "invalid_event_count": invalid_count, "duplicate_events_removed": duplicate_count,
                "input_truncated": input_truncated,
                "deployer_linked_wallet_count": len(deployer_wallets),
                "candidate_pair_count": 0, "suspicious_pair_count": 0,
                "cluster_ratio": None, "confidence": "LOW",
                "evidence": [], "trade_authorization": "NO_TRADE",
                "reason": "Minimum event and unique-wallet coverage was not met.",
            }

        # Candidate pairs are generated only from close temporal activity or
        # suspicious/private funding links. A public CEX/bridge is not a Sybil signal.
        buckets: dict[int, set[str]] = defaultdict(set)
        for wallet, rows in wallets.items():
            for row in rows:
                buckets[row.timestamp // self.temporal_tolerance_seconds].add(wallet)

        candidates: set[tuple[str, str]] = set()
        for bucket, members in buckets.items():
            nearby = set(members)
            nearby.update(buckets.get(bucket - 1, set()))
            nearby.update(buckets.get(bucket + 1, set()))
            ordered = sorted(nearby)
            for i, left in enumerate(ordered):
                for right in ordered[i + 1:]:
                    candidates.add((left, right))
                    if len(candidates) >= self.max_candidate_pairs:
                        break
                if len(candidates) >= self.max_candidate_pairs:
                    break
            if len(candidates) >= self.max_candidate_pairs:
                break

        funding_wallets: dict[tuple[str, str], set[str]] = defaultdict(set)
        for wallet, rows in wallets.items():
            for row in rows:
                if row.funding_source and row.funding_source_type not in PUBLIC_FUNDING_TYPES:
                    funding_wallets[(row.funding_source_type, row.funding_source)].add(wallet)
        for linked in funding_wallets.values():
            ordered = sorted(linked)
            for i, left in enumerate(ordered):
                for right in ordered[i + 1:]:
                    candidates.add((left, right))
                    if len(candidates) >= self.max_candidate_pairs:
                        break
                if len(candidates) >= self.max_candidate_pairs:
                    break
            if len(candidates) >= self.max_candidate_pairs:
                break

        evidence: list[dict[str, Any]] = []
        suspicious_pairs = 0
        for left, right in sorted(candidates):
            left_rows, right_rows = wallets[left], wallets[right]
            temporal_matches = 0
            amount_scores: list[float] = []
            right_by_type: dict[str, list[WalletEvent]] = defaultdict(list)
            for row in right_rows:
                right_by_type[row.event_type].append(row)
            right_times: dict[str, list[int]] = {}
            for event_type, rows_for_type in right_by_type.items():
                rows_for_type.sort(key=lambda item: item.timestamp)
                right_times[event_type] = [item.timestamp for item in rows_for_type]
            for a in left_rows:
                matching = right_by_type.get(a.event_type, [])
                times = right_times.get(a.event_type, [])
                if not times:
                    continue
                position = bisect_left(times, a.timestamp)
                nearby_indices = []
                if position < len(times):
                    nearby_indices.append(position)
                if position > 0:
                    nearby_indices.append(position - 1)
                nearby = [
                    matching[index] for index in nearby_indices
                    if abs(matching[index].timestamp - a.timestamp) <= self.temporal_tolerance_seconds
                ]
                if nearby:
                    temporal_matches += 1
                    scores = [_amount_similarity(a.amount_usd, b.amount_usd) for b in nearby]
                    known_scores = [score for score in scores if score is not None]
                    if known_scores:
                        amount_scores.append(max(known_scores))

            temporal_rate = temporal_matches / max(1, len(left_rows))
            amount_similarity = (sum(amount_scores) / len(amount_scores)) if amount_scores else None
            shared_private_funder = any(
                a.funding_source and a.funding_source_type not in PUBLIC_FUNDING_TYPES
                and any(a.funding_source == b.funding_source and
                        a.funding_source_type == b.funding_source_type for b in right_rows)
                for a in left_rows
            )
            deployer_link = left in deployer_wallets or right in deployer_wallets
            reasons = []
            if temporal_rate >= 0.8 and amount_similarity is not None and amount_similarity >= 0.9:
                reasons.append("repeated_temporal_and_amount_similarity")
            if shared_private_funder:
                reasons.append("shared_non_public_funding_source")
            if deployer_link:
                reasons.append("deployer_linked_wallet")
            # Shared private funding alone is not enough: require temporal corroboration.
            is_suspicious = (
                "deployer_linked_wallet" in reasons
                or "repeated_temporal_and_amount_similarity" in reasons
                or ("shared_non_public_funding_source" in reasons and temporal_rate >= 0.5)
            )
            if is_suspicious:
                suspicious_pairs += 1
                evidence.append({
                    "wallet_a": left, "wallet_b": right,
                    "reasons": reasons, "temporal_match_rate": round(temporal_rate, 4),
                    "amount_similarity": None if amount_similarity is None else round(amount_similarity, 4),
                })

        possible_pairs = len(unique_wallets) * (len(unique_wallets) - 1) / 2
        ratio = suspicious_pairs / possible_pairs if possible_pairs else None
        if invalid_count or input_truncated:
            status = "ANALYSIS_INCOMPLETE"
        elif len(deployer_wallets) >= 2 or (ratio is not None and ratio >= 0.20):
            status = "SUSPICIOUS"
        elif suspicious_pairs or deployer_wallets:
            status = "WATCHLIST"
        else:
            status = "NO_ANOMALY_DETECTED"

        return {
            "status": status,
            "chain": target_chain, "token": target_token,
            "event_count": len(normalized), "unique_wallet_count": len(unique_wallets),
            "invalid_event_count": invalid_count,
            "duplicate_events_removed": duplicate_count,
            "input_truncated": input_truncated,
            "deployer_linked_wallet_count": len(deployer_wallets),
            "candidate_pair_count": len(candidates),
            "suspicious_pair_count": suspicious_pairs,
            "cluster_ratio": None if ratio is None else round(ratio, 6),
            "candidate_generation_truncated": len(candidates) >= self.max_candidate_pairs,
            "confidence": "LOW", "calibration_status": "NOT_CALIBRATED",
            "evidence": evidence[:500],
            "trade_authorization": "NO_TRADE",
            "reason": "Wallet graph is an evidence signal only; no status from this module authorizes trading.",
        }
