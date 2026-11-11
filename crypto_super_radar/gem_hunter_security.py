"""Read-only contract-risk checks for discovered Solana and Base tokens.

EVM checks reuse the repository's GoPlus + Sourcify minimum screen. Solana
checks inspect mint/freeze authorities and token-account concentration but
intentionally remain UNKNOWN when they cannot establish a full security verdict.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from .evm_security import screen_evm_token

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 300.0
_CACHE_LOCK = threading.RLock()
_SCAN_OFFSET = 0


def _cached(key: str):
    with _CACHE_LOCK:
        row = _CACHE.get(key)
        if row and row[0] > time.monotonic():
            return dict(row[1])
    return None


def _store(key: str, value: dict[str, Any]) -> dict[str, Any]:
    value = dict(value)
    value.setdefault("checked_at_utc", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    with _CACHE_LOCK:
        if len(_CACHE) >= 512 and key not in _CACHE:
            oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
            _CACHE.pop(oldest, None)
        _CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, dict(value))
    return value


def _find_named(value: Any, names: set[str], depth: int = 0):
    if depth > 5:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized in names:
                return item
        for item in value.values():
            found = _find_named(item, names, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value[:100]:
            found = _find_named(item, names, depth + 1)
            if found is not None:
                return found
    return None


def _explicit_bool(value: Any):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _rugcheck_evidence(http, address: str) -> dict[str, Any]:
    """Read RugCheck's summary report; missing/ambiguous fields remain UNKNOWN."""
    if http is None:
        return {"status": "UNKNOWN", "provider": "RUGCHECK", "reason_codes": ["HTTP_UNAVAILABLE"]}
    try:
        report = http.get(
            f"https://api.rugcheck.xyz/v1/tokens/{address}/report/summary"
        )
        if not isinstance(report, dict) or not report:
            raise ValueError("RugCheck summary is not an object")
        risks = report.get("risks")
        risks = risks if isinstance(risks, list) else []
        risk_rows = []
        high_risk = False
        for row in risks[:50]:
            if not isinstance(row, dict):
                continue
            level = str(row.get("level") or row.get("severity") or "").strip().lower()
            name = str(row.get("name") or row.get("description") or "RugCheck risk")
            if level in {"high", "critical", "danger", "severe"}:
                high_risk = True
            risk_rows.append({"name": name[:180], "level": level[:32]})

        honeypot = _explicit_bool(_find_named(
            report, {"ishoneypot", "honeypot", "is_honeypot"}
        ))
        cannot_buy = _explicit_bool(_find_named(report, {"cannotbuy", "cannot_buy"}))
        cannot_sell = _explicit_bool(_find_named(
            report, {"cannotsell", "cannotsellall", "cannot_sell", "cannot_sell_all"}
        ))
        lp_locked = _explicit_bool(_find_named(
            report, {"lplocked", "lp_locked", "islocked", "is_locked"}
        ))
        lp_burned = _explicit_bool(_find_named(
            report, {"lpburned", "lp_burned", "isb urned".replace(" ", ""), "is_burned"}
        ))
        holders = _find_named(report, {"top_holders", "topholders", "holders"})
        top10_pct = None
        if isinstance(holders, list) and holders:
            percentages = []
            for holder in holders[:10]:
                if not isinstance(holder, dict):
                    continue
                raw = next((holder.get(key) for key in (
                    "pct", "percent", "percentage", "ownershipPercentage",
                    "ownership_percentage",
                ) if holder.get(key) is not None), None)
                try:
                    number = float(raw)
                except (TypeError, ValueError, OverflowError):
                    percentages = []
                    break
                if not 0 <= number <= 100:
                    percentages = []
                    break
                percentages.append(number)
            if percentages:
                top10_pct = sum(percentages)
                if max(percentages) <= 1.0 and top10_pct <= 1.0:
                    top10_pct *= 100.0

        score = _find_named(report, {"score", "score_normalised", "scorenormalised"})
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError, OverflowError):
            score = None
        explicit_unprotected_lp = lp_locked is False and lp_burned is False
        dangerous = (
            honeypot is True or cannot_buy is True or cannot_sell is True
            or high_risk or explicit_unprotected_lp
        )
        reasons = []
        if honeypot is True:
            reasons.append("RugCheck گزارش Honeypot داده است.")
        if cannot_buy is True:
            reasons.append("RugCheck امکان خرید توکن را پرریسک/غیرفعال گزارش کرده است.")
        if cannot_sell is True:
            reasons.append("RugCheck امکان فروش توکن را پرریسک/غیرفعال گزارش کرده است.")
        if high_risk:
            reasons.append("RugCheck حداقل یک ریسک با شدت High/Critical گزارش کرده است.")
        if explicit_unprotected_lp:
            reasons.append("در گزارش RugCheck نه قفل و نه سوزاندن LP تأیید شده است.")
        if not dangerous:
            reasons.append("گزارش RugCheck ریسک قطعی High/Critical نشان نداد؛ این داده به‌تنهایی PASS امنیتی صادر نمی‌کند.")
        return _store("rugcheck:" + address.lower(), {
            "status": "FAIL" if dangerous else "UNKNOWN",
            "provider": "RUGCHECK",
            "risk_score": score,
            "honeypot": honeypot,
            "cannot_buy": cannot_buy,
            "cannot_sell": cannot_sell,
            "lp_locked": lp_locked,
            "lp_burned": lp_burned,
            "lp_protected": True if lp_locked is True or lp_burned is True else (
                False if explicit_unprotected_lp else None
            ),
            "top10_holder_pct": round(top10_pct, 4) if top10_pct is not None else None,
            "risks": risk_rows,
            "reason_fa": reasons,
            "coverage_note": "RugCheck summary; unrecognized or missing fields remain UNKNOWN.",
        })
    except Exception as exc:
        return _store("rugcheck:" + address.lower(), {
            "status": "UNKNOWN", "provider": "RUGCHECK",
            "reason_fa": ["گزارش RugCheck دریافت یا تفسیر نشد؛ وضعیت UNKNOWN باقی می‌ماند."],
            "reason_codes": [type(exc).__name__],
        })


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
        http = getattr(data_service, "http", None)
        rpc_result = {}
        rpc_error = None
        if rpc is not None:
            try:
                response = rpc.token_security(address)
                rpc_result = response if isinstance(response, dict) else {}
            except Exception as exc:
                rpc_error = type(exc).__name__

        rugcheck = _rugcheck_evidence(http, address)
        mint_active = rpc_result.get("mint_authority_active")
        freeze_active = rpc_result.get("freeze_authority_active")
        rpc_top10 = rpc_result.get("top10_token_account_concentration_pct")
        top10 = rpc_top10 if rpc_top10 is not None else rugcheck.get("top10_holder_pct")
        active_authority = mint_active is True or freeze_active is True
        status = "FAIL" if active_authority or rugcheck.get("status") == "FAIL" else "UNKNOWN"

        reasons = []
        if mint_active is True:
            reasons.append("اختیار ضرب توکن هنوز فعال است.")
        if freeze_active is True:
            reasons.append("اختیار مسدودسازی حساب‌ها هنوز فعال است.")
        if top10 is not None:
            reasons.append(
                f"تمرکز ۱۰ هولدر گزارش‌شده {float(top10):.2f}% است؛ "
                "این عدد لزوماً مالکیت ۱۰ کیف‌پول مستقل را نشان نمی‌دهد."
            )
        reasons.extend(rugcheck.get("reason_fa") or [])
        if rpc_error:
            reasons.append("دادهٔ RPC سولانا دریافت نشد؛ بررسی باقیمانده ناقص است.")
        if status != "FAIL":
            reasons.append("اطلاعات موجود برای PASS امنیتی کامل کافی نیست؛ وضعیت UNKNOWN حفظ شد.")

        return _store(key, {
            "status": status,
            "provider": "SOLANA_RPC+RUGCHECK" if http is not None else "SOLANA_RPC",
            "mint_authority_active": mint_active,
            "freeze_authority_active": freeze_active,
            "top10_token_account_concentration_pct": top10,
            "lp_locked": rugcheck.get("lp_locked"),
            "lp_burned": rugcheck.get("lp_burned"),
            "lp_protected": rugcheck.get("lp_protected"),
            "rugcheck": rugcheck,
            "reason_fa": reasons,
            "reason_codes": ([rpc_error] if rpc_error else []),
            "coverage_note": (
                "Solana RPC mint/freeze authority checks plus RugCheck summary; "
                "not a full contract or transfer-simulation audit."
            ),
        })

    return {"status": "UNKNOWN", "provider": "UNSUPPORTED",
            "reason_fa": ["شبکه برای بررسی امنیت قرارداد پشتیبانی نمی‌شود."]}


def screen_discovery_candidates(candidates: Any, data_service, *, limit: int = 5) -> list[dict]:
    if not isinstance(candidates, list):
        return []
    valid = [dict(row) for row in candidates if isinstance(row, dict)]
    bounded = max(0, min(int(limit), 5, len(valid)))
    global _SCAN_OFFSET
    with _CACHE_LOCK:
        start = (_SCAN_OFFSET % len(valid)) if valid and bounded else 0
        if valid and bounded:
            _SCAN_OFFSET = (start + bounded) % len(valid)
    selected = {(start + offset) % len(valid) for offset in range(bounded)} if valid else set()
    for index, row in enumerate(valid):
        if index in selected:
            row["security_evidence"] = screen_discovery_candidate(row, data_service)
        else:
            row["security_evidence"] = {
                "status": "NOT_CHECKED_BUDGET",
                "provider": "NOT_CHECKED",
                "reason_fa": ["بررسی قرارداد در این نوبت به‌دلیل محدودیت درخواست API در صف بررسی بعدی قرار گرفت."],
            }
    return valid
