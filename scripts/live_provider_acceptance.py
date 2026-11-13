#!/usr/bin/env python3
"""Read-only live acceptance checks for CRYPTO SUPER RADAR public providers.

The runner only issues public GET requests and read-only JSON-RPC calls. It
never creates orders, authenticates to an exchange, or uses wallet credentials.
"""
from __future__ import annotations

import argparse
import email.utils
import json
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

from crypto_super_radar.config import AppConfig
from crypto_super_radar.rate_limiter import SharedRateLimiter


BINANCE_SPOT = "https://data-api.binance.vision"
BYBIT = "https://api.bybit.com"
BINANCE_FUTURES = "https://fapi.binance.com"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
EVM_RPCS = (
    ("Ethereum PublicNode RPC", "https://ethereum-rpc.publicnode.com", 1),
    ("Base public RPC", "https://mainnet.base.org", 8453),
)
NEWS_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
SOCIAL_RSS = "https://www.reddit.com/r/CryptoCurrency/.rss"
FEAR_GREED = "https://api.alternative.me/fng/"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def epoch_datetime(value: Any) -> Optional[datetime]:
    """Parse a Unix timestamp in seconds or milliseconds, rejecting booleans."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= 1_000_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_feed_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def positive_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def relative_difference_pct(a: float, b: float) -> float:
    denominator = (abs(a) + abs(b)) / 2.0
    return 0.0 if denominator == 0 else abs(a - b) / denominator * 100.0


def classify_observation(
    *,
    latency_ms: float,
    source_timestamp: Optional[datetime],
    received_at: datetime,
    max_latency_ms: float,
    max_age_seconds: float,
    error: Optional[str] = None,
) -> tuple[str, Optional[float], Optional[str]]:
    """Return PASS/FAIL/INCONCLUSIVE, age, and a concise reason."""
    if error:
        return "FAIL", None, error
    if latency_ms > max_latency_ms:
        return "FAIL", None, f"Latency {latency_ms:.0f}ms exceeds {max_latency_ms:.0f}ms."
    if source_timestamp is None:
        return "INCONCLUSIVE", None, "Provider responded, but no trustworthy source timestamp was available."
    age = (received_at - source_timestamp).total_seconds()
    if age < -60:
        return "FAIL", age, "Provider timestamp is more than 60 seconds in the future."
    age = max(0.0, age)
    if age > max_age_seconds:
        return "FAIL", age, f"Data age {age:.0f}s exceeds {max_age_seconds:.0f}s."
    return "PASS", age, None


class AcceptanceRunner:
    def __init__(
        self,
        *,
        symbol: str = "BTCUSDT",
        timeout_seconds: float = 12.0,
        max_latency_ms: float = 5000.0,
        max_data_age_seconds: float = 180.0,
        price_discrepancy_limit_pct: float = 5.0,
        funding_discrepancy_limit: float = 0.0002,
        session: Optional[requests.Session] = None,
        limiter: Optional[SharedRateLimiter] = None,
    ):
        if not re.fullmatch(r"[A-Z0-9]{3,30}", symbol):
            raise ValueError("Symbol must contain 3–30 letters or digits.")
        self.symbol = symbol
        self.timeout_seconds = timeout_seconds
        self.max_latency_ms = max_latency_ms
        self.max_data_age_seconds = max_data_age_seconds
        self.price_discrepancy_limit_pct = price_discrepancy_limit_pct
        self.funding_discrepancy_limit = funding_discrepancy_limit
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "CRYPTO-SUPER-RADAR-live-acceptance/1.0"})
        self.limiter = limiter or SharedRateLimiter(AppConfig().rate_limits.host_intervals)
        self.results: list[dict[str, Any]] = []
        self._values: dict[str, dict[str, Any]] = {}

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> tuple[Any, float, datetime]:
        self.limiter.wait(url)
        started = time.monotonic()
        response = getattr(self.session, method.lower())(
            url,
            params=params,
            json=body,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        latency_ms = (time.monotonic() - started) * 1000.0
        return payload, latency_ms, utc_now()

    def _rpc(self, url: str, method: str, params: list[Any]) -> tuple[Any, float, datetime]:
        payload, latency, received = self._request_json(
            "post", url, body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if not isinstance(payload, dict) or payload.get("id") != 1:
            raise ValueError(f"{method}: malformed JSON-RPC envelope")
        if payload.get("error"):
            error_payload = payload.get("error")
            code = error_payload.get("code") if isinstance(error_payload, dict) else None
            message = error_payload.get("message") if isinstance(error_payload, dict) else str(error_payload)
            raise ValueError(f"{method}: JSON-RPC error code={code}, message={str(message)[:120]}")
        if "result" not in payload:
            raise ValueError(f"{method}: JSON-RPC response omitted result")
        return payload["result"], latency, received

    def _probe(
        self,
        name: str,
        provider: str,
        operation: Callable[[], tuple[dict[str, Any], Optional[datetime]]],
        *,
        max_age_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        received = utc_now()
        details: dict[str, Any] = {}
        source_timestamp = None
        error = None
        inconclusive_reason = None
        try:
            details, source_timestamp = operation()
        except requests.HTTPError as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code == 403:
                inconclusive_reason = (
                    "HTTP 403: provider access was denied by its access-control/WAF policy; "
                    "verify the runner's network and regional eligibility before retrying."
                )
            elif status_code == 429:
                inconclusive_reason = (
                    "HTTP 429: provider rate limit was reached; retry only after the "
                    "provider's required backoff interval."
                )
            elif status_code == 451:
                inconclusive_reason = (
                    "HTTP 451: provider marks this endpoint unavailable for legal reasons "
                    "from the current request context; repeat only from an authorized location."
                )
            else:
                error = f"{type(exc).__name__}: {str(exc)[:180]}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:180]}"
        latency_ms = (time.monotonic() - started) * 1000.0
        received = utc_now()
        if inconclusive_reason:
            status, age, reason = "INCONCLUSIVE", None, inconclusive_reason
        else:
            status, age, reason = classify_observation(
                latency_ms=latency_ms,
                source_timestamp=source_timestamp,
                received_at=received,
                max_latency_ms=self.max_latency_ms,
                max_age_seconds=max_age_seconds or self.max_data_age_seconds,
                error=error,
            )
        result = {
            "check": name,
            "provider": provider,
            "status": status,
            "latency_ms": round(latency_ms, 2),
            "received_at_utc": iso_utc(received),
            "source_timestamp_utc": iso_utc(source_timestamp),
            "data_age_seconds": round(age, 2) if age is not None else None,
            "details": details,
        }
        if reason:
            result["reason"] = reason
        self.results.append(result)
        self._values[name] = details
        return result

    def _evaluate_derived(
        self,
        name: str,
        provider: str,
        details: dict[str, Any],
        source_timestamps: list[Optional[datetime]],
        *,
        error: Optional[str] = None,
        inconclusive_reason: Optional[str] = None,
    ) -> dict[str, Any]:
        timestamps = [item for item in source_timestamps if item is not None]
        conservative_timestamp = min(timestamps) if timestamps else None
        received = utc_now()
        if inconclusive_reason and not error:
            status, age, reason = "INCONCLUSIVE", None, inconclusive_reason
        else:
            status, age, reason = classify_observation(
                latency_ms=0.0,
                source_timestamp=conservative_timestamp,
                received_at=received,
                max_latency_ms=self.max_latency_ms,
                max_age_seconds=self.max_data_age_seconds,
                error=error,
            )
        result = {
            "check": name,
            "provider": provider,
            "status": status,
            "latency_ms": 0.0,
            "received_at_utc": iso_utc(received),
            "source_timestamp_utc": iso_utc(conservative_timestamp),
            "data_age_seconds": round(age, 2) if age is not None else None,
            "details": details,
        }
        if reason:
            result["reason"] = reason
        self.results.append(result)
        self._values[name] = details
        return result

    def check_binance_spot(self) -> dict[str, Any]:
        def operation():
            payload, _, _ = self._request_json(
                "get", f"{BINANCE_SPOT}/api/v3/ticker/24hr", params={"symbol": self.symbol}
            )
            if not isinstance(payload, dict) or payload.get("symbol") != self.symbol:
                raise ValueError("Binance Spot returned an invalid ticker schema")
            price = positive_number(payload.get("lastPrice"))
            volume = positive_number(payload.get("quoteVolume"))
            source = epoch_datetime(payload.get("closeTime"))
            if price is None or volume is None:
                raise ValueError("Binance Spot price or quote volume is missing/invalid")
            return {"symbol": self.symbol, "price": price, "quote_volume_24h": volume}, source
        return self._probe("binance_spot", "Binance Spot", operation)

    def check_bybit_spot(self) -> dict[str, Any]:
        def operation():
            payload, _, _ = self._request_json(
                "get", f"{BYBIT}/v5/market/tickers",
                params={"category": "spot", "symbol": self.symbol},
            )
            if not isinstance(payload, dict) or payload.get("retCode") not in (0, "0"):
                raise ValueError("Bybit Spot returned an API error or invalid envelope")
            items = ((payload.get("result") or {}).get("list") or [])
            if not isinstance(items, list) or len(items) != 1:
                raise ValueError("Bybit Spot did not return exactly one instrument")
            price = positive_number(items[0].get("lastPrice"))
            if price is None:
                raise ValueError("Bybit Spot lastPrice is missing/invalid")
            return {"symbol": self.symbol, "price": price, "timestamp_basis": "Bybit REST response time; not an individual last-trade timestamp"}, epoch_datetime(payload.get("time"))
        return self._probe("bybit_spot", "Bybit Spot", operation)

    def check_spot_price_agreement(self) -> dict[str, Any]:
        a = self._values.get("binance_spot", {})
        b = self._values.get("bybit_spot", {})
        if not a.get("price") or not b.get("price"):
            return self._evaluate_derived(
                "spot_price_agreement", "Binance Spot + Bybit Spot",
                {}, [], inconclusive_reason="Spot price comparison is unavailable until both providers return valid observations.",
            )
        difference = relative_difference_pct(a["price"], b["price"])
        details = {
            "symbol": self.symbol, "binance_price": a["price"], "bybit_price": b["price"],
            "difference_pct": round(difference, 6),
            "limit_pct": self.price_discrepancy_limit_pct,
        }
        error = None if difference <= self.price_discrepancy_limit_pct else "Spot prices disagree beyond the configured tolerance."
        return self._evaluate_derived(
            "spot_price_agreement", "Binance Spot + Bybit Spot", details,
            [self._source("binance_spot"), self._source("bybit_spot")], error=error,
        )

    def check_binance_perpetual(self) -> dict[str, Any]:
        def operation():
            payload, _, _ = self._request_json(
                "get", f"{BINANCE_FUTURES}/fapi/v1/premiumIndex",
                params={"symbol": self.symbol},
            )
            if not isinstance(payload, dict) or payload.get("symbol") != self.symbol:
                raise ValueError("Binance Futures returned an invalid premium-index schema")
            mark = positive_number(payload.get("markPrice"))
            funding = finite_number(payload.get("lastFundingRate"))
            if mark is None or funding is None:
                raise ValueError("Binance Futures mark price or funding rate is missing/invalid")
            info, _, _ = self._request_json("get", f"{BINANCE_FUTURES}/fapi/v1/fundingInfo")
            if not isinstance(info, list):
                raise ValueError("Binance fundingInfo returned an invalid schema")
            matches = [row for row in info if isinstance(row, dict) and row.get("symbol") == self.symbol]
            if len(matches) > 1:
                raise ValueError("Binance returned duplicate funding interval metadata")
            interval = 8.0 if not matches else positive_number(matches[0].get("fundingIntervalHours"))
            if interval is None:
                raise ValueError("Binance adjusted funding interval is missing/invalid")
            return {
                "symbol": self.symbol, "mark_price": mark, "funding_rate": funding,
                "funding_interval_hours": interval,
                "next_funding_time_utc": iso_utc(epoch_datetime(payload.get("nextFundingTime"))),
            }, epoch_datetime(payload.get("time"))
        return self._probe("binance_perpetual", "Binance USD-M Futures", operation)

    def check_bybit_perpetual(self) -> dict[str, Any]:
        def operation():
            payload, _, _ = self._request_json(
                "get", f"{BYBIT}/v5/market/tickers",
                params={"category": "linear", "symbol": self.symbol},
            )
            if not isinstance(payload, dict) or payload.get("retCode") not in (0, "0"):
                raise ValueError("Bybit Linear ticker returned an API error or invalid envelope")
            items = ((payload.get("result") or {}).get("list") or [])
            if not isinstance(items, list) or len(items) != 1:
                raise ValueError("Bybit Linear did not return exactly one instrument")
            item = items[0]
            price = positive_number(item.get("markPrice") or item.get("lastPrice"))
            funding = finite_number(item.get("fundingRate"))
            if price is None or funding is None:
                raise ValueError("Bybit Linear mark price or funding rate is missing/invalid")
            metadata, _, _ = self._request_json(
                "get", f"{BYBIT}/v5/market/instruments-info",
                params={"category": "linear", "symbol": self.symbol},
            )
            if not isinstance(metadata, dict) or metadata.get("retCode") not in (0, "0"):
                raise ValueError("Bybit instrument metadata returned an API error")
            instruments = ((metadata.get("result") or {}).get("list") or [])
            if not isinstance(instruments, list) or len(instruments) != 1:
                raise ValueError("Bybit did not return exactly one matching linear instrument")
            interval_minutes = positive_number(instruments[0].get("fundingInterval"))
            if interval_minutes is None or not interval_minutes.is_integer():
                raise ValueError("Bybit funding interval is missing/invalid")
            return {
                "symbol": self.symbol, "mark_price": price, "funding_rate": funding,
                "funding_interval_hours": interval_minutes / 60.0,
                "next_funding_time_utc": iso_utc(epoch_datetime(item.get("nextFundingTime"))),
                "timestamp_basis": "Bybit REST response time; not an individual last-trade timestamp",
            }, epoch_datetime(payload.get("time"))
        return self._probe("bybit_perpetual", "Bybit Linear", operation)

    def check_perpetual_agreement(self) -> None:
        a = self._values.get("binance_perpetual", {})
        b = self._values.get("bybit_perpetual", {})
        valid = bool(a.get("mark_price")) and bool(b.get("mark_price"))
        if not valid:
            self._evaluate_derived(
                "perpetual_price_agreement", "Binance USD-M + Bybit Linear", {}, [],
                inconclusive_reason="Perpetual price comparison is unavailable until both providers return valid mark prices.",
            )
            self._evaluate_derived(
                "funding_interval_agreement", "Binance USD-M + Bybit Linear", {}, [],
                inconclusive_reason="Funding interval comparison is unavailable until both providers return instrument metadata.",
            )
            self._evaluate_derived(
                "funding_rate_agreement", "Binance USD-M + Bybit Linear", {}, [],
                inconclusive_reason="Funding rate comparison is unavailable until both providers return valid funding rates.",
            )
            return
        timestamps = [self._source("binance_perpetual"), self._source("bybit_perpetual")]
        price_diff = relative_difference_pct(a["mark_price"], b["mark_price"])
        self._evaluate_derived(
            "perpetual_price_agreement", "Binance USD-M + Bybit Linear",
            {"symbol": self.symbol, "binance_mark_price": a["mark_price"],
             "bybit_mark_price": b["mark_price"], "difference_pct": round(price_diff, 6),
             "limit_pct": self.price_discrepancy_limit_pct},
            timestamps,
            error=None if price_diff <= self.price_discrepancy_limit_pct else "Perpetual mark prices disagree beyond tolerance.",
        )
        interval_a = a.get("funding_interval_hours")
        interval_b = b.get("funding_interval_hours")
        interval_error = None
        if interval_a is None or interval_b is None:
            interval_error = "Funding interval is missing."
        elif abs(interval_a - interval_b) > 1e-9:
            interval_error = "Funding settlement intervals differ between venues."
        self._evaluate_derived(
            "funding_interval_agreement", "Binance USD-M + Bybit Linear",
            {"binance_interval_hours": interval_a, "bybit_interval_hours": interval_b},
            timestamps, error=interval_error,
        )
        rate_diff = abs(a["funding_rate"] - b["funding_rate"])
        self._evaluate_derived(
            "funding_rate_agreement", "Binance USD-M + Bybit Linear",
            {"binance_funding_rate": a["funding_rate"], "bybit_funding_rate": b["funding_rate"],
             "absolute_difference": rate_diff, "limit": self.funding_discrepancy_limit},
            timestamps,
            error=None if rate_diff <= self.funding_discrepancy_limit else "Funding rates disagree beyond tolerance.",
        )

    def _source(self, name: str) -> Optional[datetime]:
        record = next((item for item in self.results if item["check"] == name), None)
        raw = record.get("source_timestamp_utc") if record else None
        return parse_feed_datetime(raw)

    def check_evm_rpc(self, name: str, url: str, expected_chain_id: int) -> dict[str, Any]:
        def operation():
            chain_hex, _, _ = self._rpc(url, "eth_chainId", [])
            block_hex, _, _ = self._rpc(url, "eth_blockNumber", [])
            if not isinstance(chain_hex, str) or not isinstance(block_hex, str):
                raise ValueError("EVM RPC returned invalid chain/block identifiers")
            chain_id, block_number = int(chain_hex, 16), int(block_hex, 16)
            if chain_id != expected_chain_id or block_number <= 0:
                raise ValueError(f"Wrong chain ID or invalid block height: chain_id={chain_id}")
            block, _, _ = self._rpc(url, "eth_getBlockByNumber", [block_hex, False])
            if not isinstance(block, dict) or block.get("number") != block_hex:
                raise ValueError("EVM RPC returned an invalid latest block")
            timestamp = epoch_datetime(int(block.get("timestamp", "0x0"), 16))
            return {"chain_id": chain_id, "latest_block": block_number}, timestamp
        return self._probe(name, urlparse(url).netloc, operation)

    def check_solana_rpc(self) -> dict[str, Any]:
        def operation():
            health, _, _ = self._rpc(SOLANA_RPC, "getHealth", [])
            slot, _, _ = self._rpc(SOLANA_RPC, "getSlot", [{"commitment": "confirmed"}])
            if health != "ok" or isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
                raise ValueError("Solana RPC health or confirmed slot is invalid")
            block_time, _, _ = self._rpc(SOLANA_RPC, "getBlockTime", [slot])
            source = epoch_datetime(block_time)
            return {"health": health, "confirmed_slot": slot}, source
        return self._probe("solana_rpc", "Solana Mainnet JSON-RPC", operation)

    def check_fear_greed(self) -> dict[str, Any]:
        def operation():
            payload, _, _ = self._request_json("get", FEAR_GREED, params={"limit": 1, "format": "json"})
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                raise ValueError("Fear & Greed returned no observations")
            value = positive_or_zero(rows[0].get("value"))
            if value is None or value > 100:
                raise ValueError("Fear & Greed value is outside 0–100")
            stamp = epoch_datetime(rows[0].get("timestamp"))
            return {"value": value, "classification": rows[0].get("value_classification")}, stamp
        return self._probe("fear_greed", "Alternative.me Fear & Greed", operation, max_age_seconds=259200)

    def check_rss(self, name: str, url: str) -> dict[str, Any]:
        def operation():
            self.limiter.wait(url)
            started = time.monotonic()
            response = self.session.get(url, timeout=self.timeout_seconds)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            latency_ms = (time.monotonic() - started) * 1000.0
            # Keep measured request latency in the details; _probe measures total too.
            entries = [
                node for node in root.iter()
                if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}
            ]
            headlines = 0
            timestamps = []
            for entry in entries:
                fields = {}
                for child in list(entry):
                    key = child.tag.rsplit("}", 1)[-1].lower()
                    if key in {"title", "pubdate", "published", "updated", "date"}:
                        fields[key] = " ".join("".join(child.itertext()).split())
                if fields.get("title"):
                    headlines += 1
                for key in ("pubdate", "published", "updated", "date"):
                    stamp = parse_feed_datetime(fields.get(key))
                    if stamp:
                        timestamps.append(stamp)
                        break
            if headlines == 0:
                raise ValueError("RSS/Atom feed contained no usable headline entries")
            newest = max(timestamps) if timestamps else None
            return {"headline_count": headlines, "request_latency_ms": round(latency_ms, 2)}, newest
        return self._probe(name, urlparse(url).netloc, operation, max_age_seconds=259200)

    def run(self) -> dict[str, Any]:
        self.check_binance_spot()
        self.check_bybit_spot()
        self.check_spot_price_agreement()
        self.check_binance_perpetual()
        self.check_bybit_perpetual()
        self.check_perpetual_agreement()
        for name, url, chain_id in EVM_RPCS:
            check_name = "ethereum_rpc" if chain_id == 1 else "base_rpc"
            self.check_evm_rpc(check_name, url, chain_id)
        self.check_solana_rpc()
        self.check_fear_greed()
        self.check_rss("news_rss", NEWS_RSS)
        self.check_rss("social_rss", SOCIAL_RSS)
        statuses = [item["status"] for item in self.results]
        overall = "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
        return {
            "project": "CRYPTO SUPER RADAR",
            "test_type": "LIVE_READ_ONLY_PROVIDER_ACCEPTANCE",
            "generated_at_utc": iso_utc(utc_now()),
            "symbol": self.symbol,
            "overall_status": overall,
            "check_count": len(self.results),
            "pass_count": statuses.count("PASS"),
            "fail_count": statuses.count("FAIL"),
            "inconclusive_count": statuses.count("INCONCLUSIVE"),
            "safety": {
                "read_only": True,
                "orders_submitted": False,
                "wallet_credentials_used": False,
                "private_exchange_credentials_used": False,
            },
            "checks": self.results,
        }


def finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def positive_or_zero(value: Any) -> Optional[float]:
    number = finite_number(value)
    return number if number is not None and number >= 0 else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT", help="CEX symbol to validate (default: BTCUSDT)")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    parser.add_argument("--timeout", type=float, default=12.0, help="Per-request timeout in seconds")
    parser.add_argument("--max-latency-ms", type=float, default=5000.0)
    parser.add_argument("--max-data-age-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)

    runner = AcceptanceRunner(
        symbol=args.symbol,
        timeout_seconds=args.timeout,
        max_latency_ms=args.max_latency_ms,
        max_data_age_seconds=args.max_data_age_seconds,
    )
    report = runner.run()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[report["overall_status"]]


if __name__ == "__main__":
    raise SystemExit(main())
