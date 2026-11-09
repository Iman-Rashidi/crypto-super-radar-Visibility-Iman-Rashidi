"""Best-effort Solana SPL token flow scanner using public Solana JSON-RPC.

It inspects a bounded sample of largest token accounts and recent signatures.
Coverage is explicitly PARTIAL: this is not a complete historical indexer and
account-level changes are not proof of beneficial ownership or exchange trading.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import requests

from .rate_limiter import SharedRateLimiter
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
    ):
        self.rpc_url = rpc_url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_accounts = max(1, min(int(max_accounts), 20))
        self.signatures_per_account = max(1, min(int(signatures_per_account), 100))
        self.limiter = limiter or SharedRateLimiter()

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
        labels = {k.lower(): v for k, v in (address_labels or {}).items()}
        try:
            largest = self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
            accounts = largest.get("value", [])[:self.max_accounts]
            if not accounts:
                return {"status": "UNKNOWN", "reason": "No token accounts returned for this mint."}
            signatures: dict[str, int] = {}
            watchlist = []
            labels = dict(labels)
            for account in accounts:
                try:
                    info = self._rpc("getAccountInfo", [
                        account["address"], {"encoding": "jsonParsed", "commitment": "confirmed"}
                    ])
                    parsed = (((info or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
                    owner = str((parsed.get("info") or {}).get("owner") or "").lower()
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
                    # Keep scanning signatures, but do not invent the owner label.
                    continue
            for account in accounts:
                result = self._rpc("getSignaturesForAddress", [
                    account["address"],
                    {"limit": self.signatures_per_account, "commitment": "confirmed"},
                ])
                for item in result:
                    if item.get("signature") and item.get("err") is None:
                        signatures[item["signature"]] = int(item.get("blockTime") or 0)
            events = []
            for signature, block_time in signatures.items():
                tx = self._rpc("getTransaction", [
                    signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                "commitment": "confirmed"},
                ])
                if not tx:
                    continue
                meta = tx.get("meta") or {}
                pre, post = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []
                deltas: dict[str, float] = {}
                for balances, sign in ((pre, -1.0), (post, 1.0)):
                    for balance in balances:
                        if balance.get("mint") != mint or not balance.get("owner"):
                            continue
                        amount = (balance.get("uiTokenAmount") or {}).get("uiAmount")
                        if amount is None:
                            continue
                        owner = str(balance["owner"]).lower()
                        deltas[owner] = deltas.get(owner, 0.0) + sign * float(amount)
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
                    senders[si][1] -= moved
                    receivers[ri][1] -= moved
                    if senders[si][1] <= 1e-12:
                        si += 1
                    if receivers[ri][1] <= 1e-12:
                        ri += 1
            summary = analyze_whale_activity(events, whale_threshold_usd)
            summary.update({
                "chain": "solana", "mint": mint, "sampled_token_accounts": len(accounts),
                "sampled_transactions": len(signatures), "whale_watchlist": watchlist,
                "coverage": "PARTIAL_RECENT_SAMPLE",
                "source": "public_solana_json_rpc",
                "caveat": "Recent transactions for largest token accounts only; no complete history, beneficial-owner clustering, or proof of exchange classification.",
            })
            if price_usd is None:
                summary["status"] = "PARTIAL"
                summary["interpretation"] = "Recent token-account movements observed, but USD valuation is unavailable."
            else:
                summary["status"] = "PARTIAL"
            return summary
        except Exception as exc:
            return {"status": "UNKNOWN", "reason": f"Solana public RPC scan failed: {type(exc).__name__}",
                    "chain": "solana", "mint": mint}
