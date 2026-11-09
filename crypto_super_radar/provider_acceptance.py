"""Read-only live acceptance checks for public market data providers."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from .models import AssetSpec
from .providers import MarketDataService, relative_difference_pct


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    try:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return stamp.tz_convert("UTC").to_pydatetime()
    except (TypeError, ValueError, OverflowError):
        return None


def _timed_source(name: str, fetch: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        result = fetch()
        received = _utc_now()
        price = float(result["price"])
        if not pd.notna(price) or price <= 0:
            raise ValueError("Provider returned a missing or non-positive price")
        return {
            "provider": name,
            "status": "PASS",
            "received_at_utc": received.isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "price_usd": price,
            "_pool_address": result.get("pool_address"),
        }
    except Exception as exc:
        return {
            "provider": name,
            "status": "FAIL",
            "received_at_utc": _utc_now().isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def _source_calls(service: MarketDataService, asset: AssetSpec):
    if asset.market_type == "dex":
        return [
            ("GeckoTerminal", lambda: service.gecko.token_pool(asset.chain, asset.address)),
            ("DexScreener", lambda: service.dexscreener.token_pair(asset.chain, asset.address)),
        ]
    if asset.market_type == "perp":
        return [
            ("Binance USD-M", lambda: service.perpetual.binance_ticker(asset.symbol)),
            ("Bybit Linear", lambda: service.perpetual.bybit_ticker(asset.symbol)),
        ]
    return [
        ("Binance Spot", lambda: service.binance.ticker(asset.symbol)),
        ("Bybit Spot", lambda: service.bybit.ticker(asset.symbol)),
    ]


def _freshness_check(
    service: MarketDataService,
    asset: AssetSpec,
    sources: List[Dict[str, Any]],
    max_candle_age_hours: float,
) -> Dict[str, Any]:
    try:
        if asset.market_type == "dex":
            pool = sources[0].get("_pool_address") if sources and sources[0]["status"] == "PASS" else None
            if not pool:
                return {"status": "UNKNOWN", "reason": "GeckoTerminal pool unavailable; candle pool cannot be verified."}
            frame = service.gecko.candles(asset.chain, pool, token_address=asset.address, timeframe="day", aggregate=1, limit=3)
        elif asset.market_type == "perp":
            frame = service.perpetual.binance_candles(asset.symbol, limit=3)
        else:
            frame = service.binance.candles(asset.symbol, interval="1d", limit=3)
        if frame.empty:
            return {"status": "UNKNOWN", "reason": "Provider returned no valid daily candles."}
        latest = _as_utc(frame.index.max())
        if latest is None:
            return {"status": "UNKNOWN", "reason": "Latest candle timestamp is unreadable."}
        age_seconds = max(0.0, (_utc_now() - latest).total_seconds())
        return {
            "status": "PASS" if age_seconds <= max_candle_age_hours * 3600 else "FAIL",
            "provider": "GeckoTerminal" if asset.market_type == "dex" else "Binance",
            "latest_candle_at_utc": latest.isoformat(),
            "age_seconds": round(age_seconds, 2),
            "max_age_hours": max_candle_age_hours,
        }
    except Exception as exc:
        return {"status": "UNKNOWN", "reason": f"{type(exc).__name__}: {str(exc)[:240]}"}



def _solana_security_check(service: MarketDataService, asset: AssetSpec) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        result = service.solana_rpc.token_security(asset.address)
        return {
            "status": "PASS",
            "provider": "Solana RPC",
            "received_at_utc": _utc_now().isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "mint_authority_active": result["mint_authority_active"],
            "freeze_authority_active": result["freeze_authority_active"],
            "top10_token_account_concentration_pct": result["top10_token_account_concentration_pct"],
        }
    except Exception as exc:
        return {
            "status": "FAIL",
            "provider": "Solana RPC",
            "received_at_utc": _utc_now().isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def check_asset(
    asset: AssetSpec,
    service: Optional[MarketDataService] = None,
    max_candle_age_hours: float = 48.0,
) -> Dict[str, Any]:
    """Query only public market-data endpoints; never calls order or wallet APIs."""
    service = service or MarketDataService()
    sources = [_timed_source(name, fetch) for name, fetch in _source_calls(service, asset)]
    valid_prices = [row["price_usd"] for row in sources if row["status"] == "PASS"]
    discrepancy: Dict[str, Any] = {"status": "UNKNOWN", "reason": "Both independent source prices are required."}
    if len(valid_prices) == 2:
        value = relative_difference_pct(valid_prices[0], valid_prices[1])
        threshold = float(service.config.thresholds.provider_discrepancy_pct)
        discrepancy = {
            "status": "PASS" if value <= threshold else "FAIL",
            "price_discrepancy_pct": round(value, 6),
            "max_discrepancy_pct": threshold,
        }
    freshness = _freshness_check(service, asset, sources, max_candle_age_hours)
    statuses = [row["status"] for row in sources] + [discrepancy["status"], freshness["status"]]
    security = _solana_security_check(service, asset) if asset.chain == "solana" else None
    if security:
        statuses.append(security["status"])
    overall = "FAIL" if "FAIL" in statuses else ("UNKNOWN" if "UNKNOWN" in statuses else "PASS")
    for row in sources:
        row.pop("_pool_address", None)
    return {
        "asset": asset.raw,
        "checked_at_utc": _utc_now().isoformat(),
        "read_only": True,
        "status": overall,
        "sources": sources,
        "price_comparison": discrepancy,
        "freshness": freshness,
        "on_chain_security": security,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-super-radar-provider-acceptance",
        description="Read-only live acceptance check for public market-data providers.",
    )
    parser.add_argument("--asset", action="append", required=True, metavar="MARKET:IDENTIFIER",
                        help="Repeatable: cex:BTCUSDT, perp:BTCUSDT, solana:MINT, base:ADDRESS, eth:ADDRESS")
    parser.add_argument("--max-candle-age-hours", type=float, default=48.0)
    parser.add_argument("--output", default="", help="Optional JSON report path")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_candle_age_hours <= 0:
        parser.error("--max-candle-age-hours must be positive")
    try:
        assets = [AssetSpec.parse(value) for value in args.asset]
    except ValueError as exc:
        parser.error(str(exc))
    service = MarketDataService()
    report = {
        "generated_at_utc": _utc_now().isoformat(),
        "mode": "live_read_only",
        "results": [check_asset(asset, service, args.max_candle_age_hours) for asset in assets],
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if all(row["status"] == "PASS" for row in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
