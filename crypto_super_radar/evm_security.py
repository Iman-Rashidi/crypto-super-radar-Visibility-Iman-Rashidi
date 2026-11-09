"""Minimum EVM token-security screen using public GoPlus and Sourcify APIs.

This is not an audit. Unknown API fields or provider failures return UNKNOWN.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

CHAIN_IDS = {"eth": 1, "ethereum": 1, "base": 8453}
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
SOURCIFY_URL = "https://sourcify.dev/server/v2/contract/{chain_id}/{address}"


@dataclass
class EvmSecurityResult:
    status: str
    chain_id: int
    address: str
    source_verified: Optional[bool]
    flags: dict[str, Optional[bool]]
    buy_tax_pct: Optional[float]
    sell_tax_pct: Optional[float]
    top10_holder_pct: Optional[float]
    reasons: list[str]
    evidence: dict[str, Any]


def _tri(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if str(value) == "1":
        return True
    if str(value) == "0":
        return False
    return None


def _pct(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value) * 100.0
    except (TypeError, ValueError):
        return None
    if not 0 <= number <= 100:
        return None
    return number


def screen_evm_token(
    chain: str,
    address: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: float = 12.0,
    cache: Optional[dict] = None,
    cache_ttl_seconds: int = 3600,
    max_cache_entries: int = 512,
    max_tax_pct: float = 5.0,
    max_top10_holder_pct: float = 60.0,
    now: Optional[float] = None,
) -> EvmSecurityResult:
    normalized_chain = chain.lower()
    if normalized_chain not in CHAIN_IDS or not ADDRESS_RE.fullmatch(address or ""):
        return EvmSecurityResult("FAIL", CHAIN_IDS.get(normalized_chain, 0), address or "",
                                 None, {}, None, None, None,
                                 ["Unsupported chain or invalid EVM address."], {})
    chain_id = CHAIN_IDS[normalized_chain]
    address = address.lower()
    cache = cache if cache is not None else {}
    now = time.time() if now is None else now
    key = f"{chain_id}:{address}"
    cached = cache.get(key)
    if cached and now - cached[0] <= cache_ttl_seconds:
        return cached[1]
    client = session or requests.Session()
    reasons: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        gp = client.get(GOPLUS_URL.format(chain_id=chain_id),
                        params={"contract_addresses": address}, timeout=timeout)
        gp.raise_for_status()
        payload = gp.json()
        if str(payload.get("code")) != "1":
            raise ValueError("GoPlus returned a non-success response")
        token = payload.get("result", {}).get(address)
        if not isinstance(token, dict):
            raise ValueError("GoPlus token result is missing")
        evidence["goplus"] = token
    except Exception as exc:
        return EvmSecurityResult("UNKNOWN", chain_id, address, None, {}, None, None, None,
                                 [f"GoPlus unavailable or unreadable: {type(exc).__name__}"], evidence)
    flags = {name: _tri(token.get(name)) for name in (
        "is_honeypot", "is_open_source", "is_proxy", "is_mintable",
        "owner_change_balance", "can_take_back_ownership", "hidden_owner",
        "selfdestruct", "transfer_pausable", "is_blacklisted",
    )}
    buy_tax, sell_tax = _pct(token.get("buy_tax")), _pct(token.get("sell_tax"))
    holders = token.get("holders")
    top10 = None
    if isinstance(holders, list) and holders:
        try:
            percentages = [float(item["percent"]) * 100.0 for item in holders[:10]]
            if all(0 <= value <= 100 for value in percentages):
                top10 = sum(percentages)
        except (KeyError, TypeError, ValueError):
            top10 = None
    try:
        sourcify = client.get(SOURCIFY_URL.format(chain_id=chain_id, address=address),
                              params={}, timeout=timeout)
        if sourcify.status_code == 404:
            source_verified = False
        else:
            sourcify.raise_for_status()
            source_payload = sourcify.json()
            source_verified = source_payload.get("match") == "exact_match"
            evidence["sourcify"] = {
                "match": source_payload.get("match"),
                "chainId": source_payload.get("chainId"),
                "address": source_payload.get("address"),
            }
    except Exception as exc:
        source_verified = None
        reasons.append(f"Sourcify unavailable or unreadable: {type(exc).__name__}")
    if flags["is_honeypot"] is True:
        reasons.append("GoPlus flags a honeypot.")
    for flag in ("hidden_owner", "selfdestruct", "owner_change_balance",
                 "can_take_back_ownership", "is_blacklisted"):
        if flags[flag] is True:
            reasons.append(f"GoPlus risk flag: {flag}.")
    if flags["is_open_source"] is False:
        reasons.append("Contract source is not open source.")
    if flags["is_mintable"] is True or flags["transfer_pausable"] is True:
        reasons.append("Privileged token control detected (mint or pause).")
    if flags["is_proxy"] is True:
        reasons.append("Proxy contract detected; implementation/admin risk requires review.")
    if buy_tax is not None and buy_tax > max_tax_pct:
        reasons.append("Buy tax exceeds configured threshold.")
    if sell_tax is not None and sell_tax > max_tax_pct:
        reasons.append("Sell tax exceeds configured threshold.")
    if top10 is not None and top10 > max_top10_holder_pct:
        reasons.append("Top-10 holder concentration exceeds configured threshold.")
    required = [flags[k] for k in ("is_honeypot", "is_open_source", "is_mintable",
                "transfer_pausable", "is_blacklisted", "hidden_owner")]
    unknown = any(v is None for v in required) or buy_tax is None or sell_tax is None or top10 is None or source_verified is None
    if source_verified is False:
        reasons.append("Contract is not verified on Sourcify.")
    if reasons and any("exceeds" in r or "flags" in r or "not open" in r or
                       "not verified" in r or "Proxy contract" in r or "Privileged" in r or
                       "honeypot" in r for r in reasons):
        status = "FAIL"
    elif unknown:
        status = "UNKNOWN"
        reasons.append("One or more mandatory security fields are unavailable.")
    else:
        status = "PASS"
        reasons.append("Minimum screening passed; this is not a security audit.")
    result = EvmSecurityResult(status, chain_id, address, source_verified, flags,
                               buy_tax, sell_tax, top10, reasons, evidence)
    if len(cache) >= max_cache_entries:
        oldest = min(cache, key=lambda k: cache[k][0])
        cache.pop(oldest, None)
    cache[key] = (now, result)
    return result
