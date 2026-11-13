"""Public EVM ERC-20 Transfer-log scanner for whale-flow research.

The caller supplies a verified USD price and optional curated address labels.
Unlabelled transfers stay unattributed; they are never interpreted as exchange flows.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional
import re

import requests

from .rate_limiter import SharedRateLimiter
from .whale_intelligence import analyze_whale_activity

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEFAULT_RPC = {1: "https://ethereum-rpc.publicnode.com", 8453: "https://mainnet.base.org"}


def _hex_int(value: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("Invalid hex integer in RPC response")
    return int(value, 16)


def _topic_address(topic: str) -> str:
    if not isinstance(topic, str) or len(topic) != 66:
        raise ValueError("Malformed indexed address topic")
    return "0x" + topic[-40:].lower()


class EvmTransferScanner:
    def __init__(
        self,
        chain_id: int,
        *,
        rpc_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 15.0,
        max_block_range: int = 1500,
        max_logs: int = 5000,
        limiter: Optional[SharedRateLimiter] = None,
    ):
        if chain_id not in DEFAULT_RPC and not rpc_url:
            raise ValueError("Provide an RPC URL for this chain")
        self.chain_id = int(chain_id)
        self.rpc_url = rpc_url or DEFAULT_RPC[chain_id]
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_block_range = max(1, int(max_block_range))
        self.max_logs = max(1, int(max_logs))
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
            raise ValueError(f"RPC error for {method}")
        return payload["result"]

    def _rpc_batch(self, method: str, params_list: list[list[Any]]) -> list[Any]:
        """Fetch block headers in bounded JSON-RPC batches to avoid one call per log."""
        results = []
        for offset in range(0, len(params_list), 100):
            chunk = params_list[offset:offset + 100]
            payload = [
                {"jsonrpc": "2.0", "id": offset + index + 1,
                 "method": method, "params": params}
                for index, params in enumerate(chunk)
            ]
            self.limiter.wait(self.rpc_url)
            response = self.session.post(self.rpc_url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, list):
                raise ValueError("RPC provider does not support JSON-RPC batch responses")
            by_id = {item.get("id"): item for item in body}
            for index in range(len(chunk)):
                item = by_id.get(offset + index + 1)
                if not item or item.get("error") or "result" not in item:
                    raise ValueError(f"RPC batch failed for {method}")
                results.append(item["result"])
        return results

    def latest_block_number(self) -> int:
        """Return the latest canonical block number from the configured public RPC."""
        return _hex_int(self._rpc("eth_blockNumber", []))

    def scan_erc20_transfers(
        self,
        token_address: str,
        from_block: int,
        to_block: int,
        *,
        decimals: int,
        price_usd: Optional[float],
        address_labels: Optional[Mapping[str, str]] = None,
        whale_threshold_usd: float = 100_000.0,
    ) -> dict[str, Any]:
        """Scan a bounded block range and return normalized whale-flow evidence."""
        if not (isinstance(token_address, str) and re.fullmatch(r"0x[a-fA-F0-9]{40}", token_address)):
            return {"status": "FAIL", "reason": "Invalid token contract address."}
        if from_block < 0 or to_block < from_block:
            return {"status": "FAIL", "reason": "Invalid block range."}
        if to_block - from_block + 1 > self.max_block_range:
            return {"status": "FAIL", "reason": "Requested block range exceeds configured safety cap."}
        if not isinstance(decimals, int) or not 0 <= decimals <= 36:
            return {"status": "UNKNOWN", "reason": "Token decimals are missing or invalid."}
        if price_usd is not None and price_usd <= 0:
            return {"status": "UNKNOWN", "reason": "USD price must be positive or unavailable."}
        labels = {k.lower(): v for k, v in (address_labels or {}).items()}
        try:
            logs = self._rpc("eth_getLogs", [{
                "fromBlock": hex(from_block), "toBlock": hex(to_block),
                "address": token_address.lower(),
                "topics": [TRANSFER_TOPIC],
            }])
            if not isinstance(logs, list) or len(logs) > self.max_logs:
                return {"status": "UNKNOWN", "reason": "RPC log response is invalid or exceeds configured cap."}
            block_hashes = []
            for log in logs:
                if len(log.get("topics", [])) >= 3:
                    block_hash = log.get("blockHash")
                    if not block_hash or not log.get("blockNumber"):
                        raise ValueError("Transfer log is missing block identity")
                    if block_hash not in block_hashes:
                        block_hashes.append(block_hash)
            block_times: dict[str, int] = {}
            if block_hashes:
                blocks = self._rpc_batch("eth_getBlockByHash", [[block_hash, False] for block_hash in block_hashes])
                block_times = {block_hash: _hex_int(block["timestamp"])
                               for block_hash, block in zip(block_hashes, blocks)}
            rows = []
            for log in logs:
                topics = log.get("topics", [])
                if len(topics) < 3:
                    continue
                block_hash = log["blockHash"]
                raw_amount = _hex_int(log["data"])
                amount = raw_amount / (10 ** decimals)
                amount_usd = None if price_usd is None else amount * price_usd
                source = _topic_address(topics[1])
                destination = _topic_address(topics[2])
                rows.append({
                    "tx_hash": log.get("transactionHash", ""),
                    "timestamp": block_times[block_hash],
                    "token_address": token_address.lower(),
                    "from_address": source, "to_address": destination,
                    "amount": amount, "amount_usd": amount_usd,
                    "from_label": labels.get(source), "to_label": labels.get(destination),
                    "chain": str(self.chain_id),
                })
            summary = analyze_whale_activity(rows, whale_threshold_usd)
            summary.update({
                "chain_id": self.chain_id, "token_address": token_address.lower(),
                "from_block": from_block, "to_block": to_block,
                "source": "public_evm_json_rpc_transfer_logs",
                "price_usd_used": price_usd,
                "labelled_wallets": len(labels),
            })
            if price_usd is None and rows:
                summary["status"] = "PARTIAL"
                summary["interpretation"] = "Transfer events observed, but USD valuation is unavailable; whale threshold and USD exchange flow are UNKNOWN."
            return summary
        except Exception as exc:
            return {"status": "UNKNOWN", "reason": f"Public RPC scan failed: {type(exc).__name__}",
                    "chain_id": self.chain_id, "token_address": token_address.lower(),
                    "from_block": from_block, "to_block": to_block}
