"""Read-only contract-risk checks for discovered Solana and Base tokens.

EVM checks reuse the repository's GoPlus + Sourcify minimum screen. Solana
checks inspect mint/freeze authorities and token-account concentration but
intentionally remain UNKNOWN when they cannot establish a full security verdict.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .evm_security import screen_evm_token

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 300.0
_CACHE_LOCK = threading.RLock()


def _cached(key: str):
    with _CACHE_LOCK:
        row = _CACHE.get(key)
        if row and row[0] > time.monotonic():
            return dict(row[1])
    return None


def _store(key: str, value: dict[str, Any]) -> dict[str, Any]:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, dict(value))
    return value


def screen_discovery_candidate(candidate: dict, data_service) -> dict[str, Any]:
    chain = str(candidate.get("chain") or "").lower()
    address = candidate.get("token_address")
    key = f"{chain}:{str(address or '').lower()}"
    cached = _cached(key)
    if cached is not None:
        return cached
    if not isinstance(address, str) or not data_service:
        return {"status": "UNKNOWN", "provider": "UNAVAILABLE",
                "reason_fa": ["آدرس توکن یا سرویس داده برای بررسی قرارداد موجود نیست."]}

    if chain == "base":
        http = getattr(data_service, "http", None)
        if http is None:
            return {"status": "UNKNOWN", "provider": "GOPLUS_SOURCIFY",
                    "reason_fa": ["اتصال بررسی امنیت EVM در دسترس نیست."]}
        try:
            config = getattr(http, "config", None)
            result = screen_evm_token(
                "base", address,
                timeout=float(getattr(config, "request_timeout_seconds", 12.0)),
                limiter=getattr(http, "limiter", None),
                cache={},
            )
            status = result.status
            reasons = list(result.reasons or [])
            return _store(key, {
                "status": status,
                "provider": "GOPLUS_SOURCIFY",
                "source_verified": result.source_verified,
                "flags": dict(result.flags or {}),
                "buy_tax_pct": result.buy_tax_pct,
                "sell_tax_pct": result.sell_tax_pct,
                "top10_holder_pct": result.top10_holder_pct,
                "reason_fa": (
                    ["بررسی حداقلی GoPlus و تطبیق منبع قرارداد در Sourcify عبور کرد؛ این ممیزی امنیتی کامل نیست."]
                    if status == "PASS" else
                    ["منبع امنیت قرارداد یک یا چند ریسک را گزارش کرده است: " + "؛ ".join(reasons[:5])]
                    if status == "FAIL" else
                    ["دادهٔ الزامی امنیت قرارداد یا تطبیق Sourcify ناقص است؛ وضعیت UNKNOWN باقی می‌ماند."]
                ),
                "reason_codes": reasons[:12],
                "coverage_note": "GoPlus token-security + Sourcify exact source match; not an audit.",
            })
        except Exception as exc:
            return _store(key, {
                "status": "UNKNOWN", "provider": "GOPLUS_SOURCIFY",
                "reason_fa": ["بررسی قرارداد EVM به خطا خورد؛ هیچ فرض ایمنی ثبت نشد."],
                "reason_codes": [type(exc).__name__],
            })

    if chain == "solana":
        rpc = getattr(data_service, "solana_rpc", None)
        if rpc is None:
            return {"status": "UNKNOWN", "provider": "SOLANA_RPC",
                    "reason_fa": ["RPC سولانا برای بررسی Mint در دسترس نیست."]}
        try:
            result = rpc.token_security(address)
            mint_active = result.get("mint_authority_active")
            freeze_active = result.get("freeze_authority_active")
            concentration = result.get("top10_token_account_concentration_pct")
            active = mint_active is True or freeze_active is True
            # Revoked authorities alone cannot prove honeypot/transfer safety.
            status = "FAIL" if active else "UNKNOWN"
            reasons = []
            if mint_active is True:
                reasons.append("اختیار ضرب توکن هنوز فعال است.")
            if freeze_active is True:
                reasons.append("اختیار مسدودسازی حساب‌ها هنوز فعال است.")
            if concentration is not None:
                reasons.append(f"تمرکز ۱۰ حساب برتر توکن {concentration:.2f}% است؛ این معادل مالکیت ۱۰ کیف‌پول مستقل نیست.")
            if not active:
                reasons.append("اختیارات Mint و Freeze غیرفعال‌اند، اما Honeypot و امنیت انتقال با این داده‌ها اثبات نمی‌شوند.")
            return _store(key, {
                "status": status,
                "provider": "SOLANA_RPC",
                "mint_authority_active": mint_active,
                "freeze_authority_active": freeze_active,
                "top10_token_account_concentration_pct": concentration,
                "reason_fa": reasons,
                "coverage_note": result.get("scope_note"),
            })
        except Exception as exc:
            return _store(key, {
                "status": "UNKNOWN", "provider": "SOLANA_RPC",
                "reason_fa": ["بررسی Mint در RPC سولانا کامل نشد؛ وضعیت UNKNOWN باقی می‌ماند."],
                "reason_codes": [type(exc).__name__],
            })

    return {"status": "UNKNOWN", "provider": "UNSUPPORTED",
            "reason_fa": ["شبکه برای بررسی امنیت قرارداد پشتیبانی نمی‌شود."]}


def screen_discovery_candidates(candidates: Any, data_service, *, limit: int = 10) -> list[dict]:
    if not isinstance(candidates, list):
        return []
    valid = [dict(row) for row in candidates if isinstance(row, dict)]
    bounded = max(0, min(int(limit), 10))
    for index, row in enumerate(valid):
        if index < bounded:
            row["security_evidence"] = screen_discovery_candidate(row, data_service)
        else:
            row["security_evidence"] = {
                "status": "NOT_CHECKED_BUDGET",
                "provider": "NOT_CHECKED",
                "reason_fa": ["بررسی قرارداد در این نوبت به‌دلیل محدودیت درخواست API انجام نشد."],
            }
    return valid
