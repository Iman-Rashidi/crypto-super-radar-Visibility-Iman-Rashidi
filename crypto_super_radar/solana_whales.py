"""Best-effort Solana SPL token flow scanner with durable wallet event evidence.

The scanner samples largest token accounts and a bounded recent-signature window.
Coverage remains PARTIAL: transfers are not inferred to be buys/sells, and this
is not a complete token-lifetime indexer or beneficial-owner graph.
"""
from __future__ import annotations

import time
from typing import Any, Mapping, Optional

import requests

from .database import connect_database
from .rate_limiter import SharedRateLimiter
from .wallet_event_store import WalletEventStore
from .wallet_graph import WalletGraphAnalytics
from .whale_intelligence import analyze_whale_activity


class SolanaWhaleScanner:
    def __init__(
        self,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 15.0,
        max_accounts: int = 10,
        signatures_per_account: int = 20,
        limiter: Optional[SharedRateLimiter] = None,
        wallet_event_store: Optional[WalletEventStore] = None,
    ):
        self.rpc_url = rpc_url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_accounts = max(1, min(int(max_accounts), 20))
        self.signatures_per_account = max(1, min(int(signatures_per_account), 100))
        self.limiter = limiter or SharedRateLimiter()
        self._wallet_db = None
        if wallet_event_store is None:
            self._wallet_db = connect_database()
            self.wallet_event_store = WalletEventStore(self._wallet_db)
        else:
            self.wallet_event_store = wallet_event_store

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self.limiter.wait(self.rpc_url)
        response = self.session.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") or "result" not in payload:
            raise ValueError(f"Solana RPC error for {method}")
        return payload["result"]

    def scan_mint(
        self,
        mint: str,
        *,
        price_usd: Optional[float],
        address_labels: Optional[Mapping[str, str]] = None,
        whale_threshold_usd: float = 100_000.0,
    ) -> dict[str, Any]:
        if not isinstance(mint, str) or len(mint) < 32 or len(mint) > 44:
            return {"status": "FAIL", "reason": "Invalid Solana mint address."}
        if price_usd is not None and price_usd <= 0:
            return {"status": "UNKNOWN", "reason": "USD price must be positive or unavailable."}

        # Solana addresses are Base58 and case-sensitive. Do not lowercase them.
        labels = dict(address_labels or {})
        try:
            largest = self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
            accounts = largest.get("value", [])[:self.max_accounts]
            if not accounts:
                return {"status": "UNKNOWN", "reason": "No token accounts returned for this mint."}
            signatures: dict[str, int] = {}
            watchlist = []
            for account in accounts:
                try:
                    info = self._rpc("getAccountInfo", [
                        account["address"], {"encoding": "jsonParsed", "commitment": "confirmed"}
                    ])
                    parsed = (((info or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
                    owner = str((parsed.get("info") or {}).get("owner") or "")
                    ui_amount = account.get("uiAmount")
                    if ui_amount is None and account.get("uiAmountString") is not None:
                        ui_amount = float(account["uiAmountString"])
                    balance_usd = None if price_usd is None or ui_amount is None else float(ui_amount) * price_usd
                    if owner and balance_usd is not None:
                        watchlist.append({
                            "address": owner, "token_account": account.get("address"),
                            "balance_tokens": float(ui_amount), "balance_usd": balance_usd,
                        })
                        if balance_usd >= whale_threshold_usd and owner not in labels:
                            labels[owner] = "whale"
                except Exception:
                    # Keep the bounded scan going, but never invent the owner label.
                    continue

            for account in accounts:
                result = self._rpc("getSignaturesForAddress", [
                    account["address"],
                    {"limit": self.signatures_per_account, "commitment": "confirmed"},
                ])
                for item in result:
                    if item.get("signature") and item.get("err") is None:
                        signatures[item["signature"]] = int(item.get("blockTime") or 0)

            events: list[dict[str, Any]] = []
            wallet_rows: list[dict[str, Any]] = []
            timestamp_missing_count = 0
            for signature, block_time in signatures.items():
                tx = self._rpc("getTransaction", [
                    signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                "commitment": "confirmed"},
                ])
                if not tx:
                    continue
                meta = tx.get("meta") or {}
                pre = meta.get("preTokenBalances") or []
                post = meta.get("postTokenBalances") or []
                deltas: dict[str, float] = {}
                for balances, sign in ((pre, -1.0), (post, 1.0)):
                    for balance in balances:
                        if balance.get("mint") != mint or not balance.get("owner"):
                            continue
                        amount = (balance.get("uiTokenAmount") or {}).get("uiAmount")
                        if amount is None:
                            continue
                        owner = str(balance["owner"])
                        deltas[owner] = deltas.get(owner, 0.0) + sign * float(amount)

                timestamp_valid = block_time > 0
                if not timestamp_valid:
                    timestamp_missing_count += len([delta for delta in deltas.values() if abs(delta) > 1e-12])
                else:
                    event_time_ms = block_time * 1000
                    received_at_ms = int(time.time() * 1000)
                slot = tx.get("slot")
                owner_event_indices: dict[str, int] = {}

                senders = [[owner, -delta] for owner, delta in deltas.items() if delta < 0]
                receivers = [[owner, delta] for owner, delta in deltas.items() if delta > 0]
                si = ri = 0
                while si < len(senders) and ri < len(receivers):
                    source, out_amount = senders[si]
                    destination, in_amount = receivers[ri]
                    moved = min(out_amount, in_amount)
                    if moved > 0:
                        events.append({
                            "tx_hash": signature, "timestamp": block_time,
                            "token_address": mint, "from_address": source,
                            "to_address": destination, "amount": moved,
                            "amount_usd": None if price_usd is None else moved * price_usd,
                            "from_label": labels.get(source), "to_label": labels.get(destination),
                            "chain": "solana",
                        })
                        # Persist each matched transfer leg with its counterparty.
                        # It remains a transfer observation, never an inferred swap.
                        if timestamp_valid:
                            source_index = owner_event_indices.get(source, 0)
                            destination_index = owner_event_indices.get(destination, 0)
                            owner_event_indices[source] = source_index + 1
                            owner_event_indices[destination] = destination_index + 1
                            common = {
                                "chain": "solana",
                                "tx_signature": signature,
                                "token_address": mint,
                                "event_time_ms": event_time_ms,
                                "received_at_ms": received_at_ms,
                                "slot": slot if isinstance(slot, int) and not isinstance(slot, bool) else None,
                                "quote_delta": None,
                                "quote_symbol": None,
                                "source": "public_solana_json_rpc",
                                "evidence_status": "OBSERVED",
                            }
                            wallet_rows.extend([
                                {
                                    **common, "wallet_address": source,
                                    "counterparty_address": destination,
                                    "event_type": "TRANSFER_OUT", "event_index": source_index,
                                    "token_delta": -moved,
                                },
                                {
                                    **common, "wallet_address": destination,
                                    "counterparty_address": source,
                                    "event_type": "TRANSFER_IN", "event_index": destination_index,
                                    "token_delta": moved,
                                },
                            ])
                    senders[si][1] -= moved
                    receivers[ri][1] -= moved
                    if senders[si][1] <= 1e-12:
                        si += 1
                    if receivers[ri][1] <= 1e-12:
                        ri += 1

            persistence = {"status": "NOT_CONFIGURED", "inserted": 0, "duplicates": 0}
            if wallet_rows:
                try:
                    persistence = {
                        "status": "COMPLETE",
                        **self.wallet_event_store.append_many(wallet_rows),
                    }
                except Exception as exc:
                    persistence = {
                        "status": "FAILED",
                        "inserted": 0,
                        "duplicates": 0,
                        "reason": type(exc).__name__,
                    }

            summary = analyze_whale_activity(events, whale_threshold_usd)
            wallet_graph_events = [
                {
                    "chain": row["chain"],
                    "wallet": row["wallet_address"],
                    "token": row["token_address"],
                    "event_type": "transfer",
                    "timestamp": row["event_time_ms"] // 1000,
                    "amount_usd": (
                        None if price_usd is None or row.get("token_delta") is None
                        else abs(float(row["token_delta"]) * price_usd)
                    ),
                    "counterparty_wallet": row.get("counterparty_address"),
                    "tx_hash": row["tx_signature"],
                    "received_at": row["received_at_ms"] // 1000,
                    "instruction_index": row["event_index"],
                }
                for row in wallet_rows
            ]
            if len(wallet_graph_events) >= 1:
                wallet_graph = WalletGraphAnalytics().analyze_token_distribution(
                    wallet_graph_events, chain="solana", token=mint,
                )
            else:
                wallet_graph = {
                    "status": "INSUFFICIENT_DATA",
                    "event_count": 0,
                    "unique_wallet_count": 0,
                    "trade_authorization": "NO_TRADE",
                    "reason": "No timestamped wallet transfer observations were available for graph analysis.",
                }
            summary.update({
                "chain": "solana", "mint": mint, "sampled_token_accounts": len(accounts),
                "sampled_transactions": len(signatures), "whale_watchlist": watchlist,
                "coverage": "PARTIAL_RECENT_SAMPLE",
                "source": "public_solana_json_rpc",
                "wallet_event_persistence": persistence,
                "wallet_events_observed": len(wallet_rows),
                "wallet_event_timestamps_missing": timestamp_missing_count,
                "wallet_graph_analysis": wallet_graph,
                "caveat": (
                    "Recent transactions for largest token accounts only; no complete history, "
                    "beneficial-owner clustering, or proof of exchange classification. "
                    "Observed token movements are transfers, not inferred buys or sells."
                ),
            })
            # This scanner is explicitly partial regardless of successful persistence.
            summary["status"] = "PARTIAL"
            if persistence["status"] == "FAILED":
                summary["persistence_warning"] = "Wallet observations were not durably saved."
            if price_usd is None:
                summary["interpretation"] = "Recent token-account movements observed, but USD valuation is unavailable."
            return summary
        except Exception as exc:
            return {
                "status": "UNKNOWN",
                "reason": f"Solana public RPC scan failed: {type(exc).__name__}",
                "chain": "solana", "mint": mint,
                "trade_authorization": "NO_TRADE",
            }

    def close(self) -> None:
        if self._wallet_db is not None:
            self._wallet_db.close()
            self._wallet_db = None
