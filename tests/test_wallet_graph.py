"""Deterministic tests for WalletGraphAnalytics."""
import math

import pytest

from crypto_super_radar.wallet_graph import WalletEvent, WalletGraphAnalytics


def _rows(wallet_count=10, *, chain="solana", token="mint-a", start=1_700_000_000):
    rows = []
    for index in range(wallet_count):
        rows.append({
            "chain": chain, "wallet": f"wallet-{index}", "token": token,
            "event_type": "buy", "timestamp": start + index * 137,
            "amount_usd": 80.0 + index * 17,
            "funding_source": f"funder-{index}",
            "funding_source_type": "private",
        })
    return rows


def test_insufficient_data_never_passes():
    result = WalletGraphAnalytics().analyze_token_distribution(
        _rows(1), chain="solana", token="mint-a"
    )
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["trade_authorization"] == "NO_TRADE"


def test_unrelated_scope_is_excluded():
    rows = _rows()
    rows += _rows(chain="base", token="mint-a")
    rows += _rows(chain="solana", token="mint-b")
    result = WalletGraphAnalytics().analyze_token_distribution(
        rows, chain="solana", token="mint-a"
    )
    assert result["unique_wallet_count"] == 10
    assert result["chain"] == "solana"
    assert result["token"] == "mint-a"


def test_duplicate_events_are_removed():
    rows = _rows()
    rows.append(dict(rows[0]))
    result = WalletGraphAnalytics().analyze_token_distribution(
        rows, chain="solana", token="mint-a"
    )
    assert result["event_count"] == 10
    assert result["duplicate_events_removed"] == 1


def test_public_cex_funder_alone_is_not_sybil_evidence():
    rows = _rows()
    for row in rows:
        row["funding_source"] = "exchange-hot-wallet"
        row["funding_source_type"] = "cex"
    result = WalletGraphAnalytics().analyze_token_distribution(
        rows, chain="solana", token="mint-a"
    )
    assert result["status"] == "NO_ANOMALY_DETECTED"
    assert result["trade_authorization"] == "NO_TRADE"


def test_temporal_and_amount_pattern_is_flagged():
    rows = []
    for wallet_index in range(8):
        for event_index in range(2):
            rows.append({
                "chain": "solana", "wallet": f"wallet-{wallet_index}",
                "token": "mint-a", "event_type": "buy",
                "timestamp": 1_700_000_000 + event_index * 300 + wallet_index,
                "amount_usd": 125.0,
                "funding_source": f"private-{wallet_index}",
                "funding_source_type": "private",
            })
    result = WalletGraphAnalytics().analyze_token_distribution(
        rows, chain="solana", token="mint-a"
    )
    assert result["status"] in {"WATCHLIST", "SUSPICIOUS"}
    assert result["suspicious_pair_count"] > 0


def test_deployer_wallets_are_counted_uniquely():
    rows = _rows()
    for row in rows[:2]:
        row["deployer_relation"] = True
    rows.extend([dict(rows[0], timestamp=1_700_001_000), dict(rows[0], timestamp=1_700_002_000)])
    result = WalletGraphAnalytics().analyze_token_distribution(
        rows, chain="solana", token="mint-a"
    )
    assert result["deployer_linked_wallet_count"] == 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_invalid_amount_is_rejected_at_normalization(bad):
    with pytest.raises(ValueError):
        WalletEvent.from_mapping({
            "chain": "solana", "wallet": "w", "token": "t",
            "event_type": "buy", "timestamp": 1, "amount_usd": bad,
        })


def test_missing_or_insufficient_evidence_never_authorizes_trade():
    rows = _rows()
    result = WalletGraphAnalytics().analyze_token_distribution(
        rows, chain="solana", token="mint-a"
    )
    assert result["status"] == "NO_ANOMALY_DETECTED"
    assert result["trade_authorization"] == "NO_TRADE"
