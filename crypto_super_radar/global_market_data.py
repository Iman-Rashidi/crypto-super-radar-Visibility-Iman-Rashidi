"""Global market data connector for US/EU equities, ETFs, FX and commodities.

This module is deliberately research-only. A quote from one vendor is not a
cross-validated executable price and must never authorize a paper or live order.
"""
from __future__ import annotations

import copy
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from .providers import DataProviderError, PublicHttpClient


TWELVE_DATA_BASE = "https://api.twelvedata.com"


@dataclass(frozen=True)
class GlobalInstrument:
    """A curated cross-market instrument with explicit vendor routing."""

    instrument_id: str
    asset_class: str
    region: str
    symbol: str
    display_name: str
    mic_code: Optional[str]
    currency_hint: str
    minimum_tier: str
    data_mode: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "asset_class": self.asset_class,
            "region": self.region,
            "symbol": self.symbol,
            "display_name": self.display_name,
            "mic_code": self.mic_code,
            "currency_hint": self.currency_hint,
            "minimum_tier": self.minimum_tier,
            "data_mode": self.data_mode,
        }


# Curated starting universe, not an assertion that these are the world's
# complete or highest-ranked securities. Extend through reviewed catalog PRs.
GLOBAL_MARKET_UNIVERSE: tuple[GlobalInstrument, ...] = (
    GlobalInstrument("us:aapl", "US_STOCK", "US", "AAPL", "Apple", "XNAS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:msft", "US_STOCK", "US", "MSFT", "Microsoft", "XNAS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:nvda", "US_STOCK", "US", "NVDA", "NVIDIA", "XNAS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:amzn", "US_STOCK", "US", "AMZN", "Amazon", "XNAS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:googl", "US_STOCK", "US", "GOOGL", "Alphabet", "XNAS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:meta", "US_STOCK", "US", "META", "Meta Platforms", "XNAS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:tsla", "US_STOCK", "US", "TSLA", "Tesla", "XNAS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:jpm", "US_STOCK", "US", "JPM", "JPMorgan Chase", "XNYS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:spy", "US_ETF", "US", "SPY", "SPDR S&P 500 ETF", "ARCX", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:qqq", "US_ETF", "US", "QQQ", "Invesco QQQ ETF", "XNAS", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:dia", "US_ETF", "US", "DIA", "SPDR Dow Jones Industrial Average ETF", "ARCX", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("us:iwm", "US_ETF", "US", "IWM", "iShares Russell 2000 ETF", "ARCX", "USD", "BASIC", "REALTIME"),
    GlobalInstrument("eu:asml", "EU_STOCK", "EU", "ASML", "ASML", "XAMS", "EUR", "GROW_EOD", "EOD"),
    GlobalInstrument("eu:sap", "EU_STOCK", "EU", "SAP", "SAP", "XETR", "EUR", "GROW_EOD", "EOD"),
    GlobalInstrument("eu:sie", "EU_STOCK", "EU", "SIE", "Siemens", "XETR", "EUR", "GROW_EOD", "EOD"),
    GlobalInstrument("eu:azn", "EU_STOCK", "EU", "AZN", "AstraZeneca", "XLON", "GBP", "GROW_EOD", "EOD"),
    GlobalInstrument("eu:mc", "EU_STOCK", "EU", "MC", "LVMH", "XPAR", "EUR", "GROW_EOD", "EOD"),
    GlobalInstrument("eu:nesn", "EU_STOCK", "EU", "NESN", "Nestlé", "XSWX", "CHF", "GROW_EOD", "EOD"),
    GlobalInstrument("fx:eurusd", "FX", "GLOBAL", "EUR/USD", "EUR / USD", None, "USD", "BASIC", "REALTIME"),
    GlobalInstrument("fx:gbpusd", "FX", "GLOBAL", "GBP/USD", "GBP / USD", None, "USD", "BASIC", "REALTIME"),
    GlobalInstrument("fx:usdjpy", "FX", "GLOBAL", "USD/JPY", "USD / JPY", None, "JPY", "BASIC", "REALTIME"),
    GlobalInstrument("fx:usdchf", "FX", "GLOBAL", "USD/CHF", "USD / CHF", None, "CHF", "BASIC", "REALTIME"),
    GlobalInstrument("fx:eurgbp", "FX", "GLOBAL", "EUR/GBP", "EUR / GBP", None, "GBP", "BASIC", "REALTIME"),
    GlobalInstrument("metal:xauusd", "PRECIOUS_METAL", "GLOBAL", "XAU/USD", "Gold / USD", None, "USD", "GROW", "PROVIDER_TIER_DEPENDENT"),
    GlobalInstrument("metal:xagusd", "PRECIOUS_METAL", "GLOBAL", "XAG/USD", "Silver / USD", None, "USD", "GROW", "PROVIDER_TIER_DEPENDENT"),
)


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _utc_timestamp(payload: dict[str, Any]) -> Optional[datetime]:
    raw = payload.get("timestamp")
    numeric = _finite_number(raw)
    if numeric is not None and numeric > 0:
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw_datetime = payload.get("datetime")
    if isinstance(raw_datetime, str) and raw_datetime.strip():
        try:
            parsed = datetime.fromisoformat(raw_datetime.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        # A naive exchange-local datetime is not silently mislabeled as UTC.
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    return None


def _is_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


class TwelveDataProvider:
    """Twelve Data adapter using an HTTP Authorization header for the API key."""

    def __init__(self, http: PublicHttpClient, api_key: Optional[str] = None):
        self.http = http
        self.api_key = (api_key or os.getenv("TWELVEDATA_API_KEY") or os.getenv("TWELVE_DATA_API_KEY") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise DataProviderError("TWELVEDATA_API_KEY is not configured")
        payload = self.http.request_json(
            "GET",
            f"{TWELVE_DATA_BASE}/{endpoint.lstrip('/')}",
            params=params,
            headers={"Authorization": f"apikey {self.api_key}"},
        )
        if not isinstance(payload, dict):
            raise DataProviderError("Twelve Data returned an invalid JSON document")
        if str(payload.get("status", "")).lower() == "error" or payload.get("code") in {401, 403, 404, 429}:
            code = payload.get("code")
            message = str(payload.get("message") or payload.get("error") or "provider rejected the request")[:240]
            # Never include request headers or API-key material in diagnostics.
            raise DataProviderError(f"Twelve Data error {code}: {message}")
        return payload

    @staticmethod
    def _instrument_params(instrument: GlobalInstrument) -> dict[str, str]:
        params = {"symbol": instrument.symbol}
        if instrument.mic_code:
            params["mic_code"] = instrument.mic_code
        return params

    def quote(self, instrument: GlobalInstrument) -> dict[str, Any]:
        """Fetch one source-attributed quote; never promotes it to validated trade data."""
        payload = self._get("quote", self._instrument_params(instrument))
        price = _finite_number(payload.get("price"))
        if price is None:
            price = _finite_number(payload.get("close"))
        if price is None or price <= 0:
            raise DataProviderError("Twelve Data quote omitted a positive finite price")

        timestamp = _utc_timestamp(payload)
        if timestamp is None:
            raise DataProviderError("Twelve Data quote omitted an unambiguous timestamp")

        now = datetime.now(timezone.utc)
        age_seconds = (now - timestamp).total_seconds()
        if age_seconds < -300:
            freshness = "FUTURE_TIMESTAMP"
        else:
            max_age = 2 * 60 * 60 if instrument.asset_class == "FX" else 72 * 60 * 60
            freshness = "WITHIN_LAST_SESSION" if age_seconds <= max_age else "STALE"

        provider_symbol = str(payload.get("symbol") or instrument.symbol).strip()
        provider_currency = str(payload.get("currency") or instrument.currency_hint).strip().upper()
        if provider_symbol.upper() != instrument.symbol.upper():
            raise DataProviderError("Twelve Data quote symbol did not match the requested instrument")

        return {
            **instrument.to_public_dict(),
            "status": "OBSERVED_SINGLE_SOURCE" if freshness == "WITHIN_LAST_SESSION" else "UNKNOWN",
            "freshness": freshness,
            "price": price,
            "currency": provider_currency,
            "volume": _finite_number(payload.get("volume")),
            "change_pct": _finite_number(payload.get("percent_change")),
            "market_open": _is_bool(payload.get("is_market_open")),
            "observed_at_utc": timestamp.isoformat(),
            "age_seconds": round(max(age_seconds, 0.0), 1),
            "provider": "Twelve Data",
            "validation_status": "SINGLE_SOURCE_ONLY",
            "trade_eligible": False,
            "orders_submitted": False,
            "source_note": (
                "US real-time feed is not a consolidated SIP feed; verify venue coverage and quote conditions."
                if instrument.region == "US"
                else "Global quote entitlement and delay depend on the subscribed Twelve Data plan."
            ),
        }

    def time_series(
        self,
        instrument: GlobalInstrument,
        *,
        interval: str = "1day",
        outputsize: int = 500,
    ) -> pd.DataFrame:
        """Fetch normalized OHLCV history; missing volume remains NaN, never zero-filled."""
        if not 1 <= int(outputsize) <= 5000:
            raise ValueError("outputsize must be between 1 and 5000")
        params = {
            **self._instrument_params(instrument),
            "interval": interval,
            "outputsize": int(outputsize),
            "format": "JSON",
        }
        payload = self._get("time_series", params)
        values = payload.get("values")
        if not isinstance(values, list):
            raise DataProviderError("Twelve Data time_series omitted its values array")

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        tz_name = meta.get("exchange_timezone") or meta.get("timezone") or (
            "UTC" if instrument.asset_class == "FX" else None
        )
        if not isinstance(tz_name, str) or not tz_name.strip():
            raise DataProviderError("Twelve Data omitted the exchange timezone for historical bars")
        try:
            zone = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise DataProviderError("Twelve Data returned an unknown exchange timezone") from exc

        rows = []
        seen = set()
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise DataProviderError(f"Twelve Data OHLC row {index} is not an object")
            raw_dt = item.get("datetime")
            if not isinstance(raw_dt, str):
                raise DataProviderError(f"Twelve Data OHLC row {index} omitted datetime")
            try:
                stamp = pd.Timestamp(raw_dt)
                if stamp.tzinfo is None:
                    stamp = stamp.tz_localize(zone)
                stamp = stamp.tz_convert("UTC")
            except Exception as exc:
                raise DataProviderError(f"Twelve Data OHLC row {index} has an invalid timestamp") from exc
            if stamp in seen:
                raise DataProviderError(f"Twelve Data OHLC history contains duplicate timestamp at row {index}")
            seen.add(stamp)

            open_ = _finite_number(item.get("open"))
            high = _finite_number(item.get("high"))
            low = _finite_number(item.get("low"))
            close = _finite_number(item.get("close"))
            volume = _finite_number(item.get("volume"))
            if any(value is None or value <= 0 for value in (open_, high, low, close)):
                raise DataProviderError(f"Twelve Data OHLC row {index} contains missing or invalid prices")
            if high < max(open_, low, close) or low > min(open_, high, close):
                raise DataProviderError(f"Twelve Data OHLC row {index} has inconsistent high/low")
            if volume is not None and volume < 0:
                raise DataProviderError(f"Twelve Data OHLC row {index} has negative volume")
            rows.append((stamp, open_, high, low, close, volume if volume is not None else float("nan")))

        frame = pd.DataFrame(
            rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        if frame.empty:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"],
                index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
            )
        return frame.set_index("timestamp").sort_index().astype(float)


class GlobalMarketBoard:
    """Cached asynchronous global quote board. Single-source observations are research-only."""

    def __init__(
        self,
        http: PublicHttpClient,
        *,
        instruments: tuple[GlobalInstrument, ...] = GLOBAL_MARKET_UNIVERSE,
        provider: Optional[TwelveDataProvider] = None,
        cache_ttl_seconds: int = 900,
    ):
        self.instruments = instruments
        self.provider = provider or TwelveDataProvider(http)
        self.cache_ttl_seconds = max(60, int(cache_ttl_seconds))
        self._lock = threading.RLock()
        self._cache: Optional[dict[str, Any]] = None
        self._expires_at = 0.0
        self._refreshing = False
        self._last_error: Optional[str] = None

    def _missing_key_payload(self) -> dict[str, Any]:
        items = [
            {
                **instrument.to_public_dict(),
                "status": "API_KEY_MISSING",
                "freshness": "UNKNOWN",
                "validation_status": "NOT_CHECKED",
                "trade_eligible": False,
                "orders_submitted": False,
                "reason": "TWELVEDATA_API_KEY is not configured in the server environment.",
            }
            for instrument in self.instruments
        ]
        return {
            "status": "CONFIGURATION_REQUIRED",
            "provider": "Twelve Data",
            "configured": False,
            "validation_status": "SINGLE_SOURCE_ONLY",
            "trade_eligible": False,
            "orders_submitted": False,
            "updated_at_utc": None,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "refreshing": False,
            "items": items,
            "counts": {"total": len(items), "observed": 0, "unknown": len(items)},
            "scope_note": "A market-data API key is separate from OPENROUTER_API_KEY.",
        }

    def _refresh_sync(self) -> None:
        results: dict[str, dict[str, Any]] = {}
        try:
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {
                    executor.submit(self.provider.quote, instrument): instrument
                    for instrument in self.instruments
                }
                for future in as_completed(futures):
                    instrument = futures[future]
                    try:
                        results[instrument.instrument_id] = future.result()
                    except Exception as exc:
                        message = f"{type(exc).__name__}: {str(exc)[:220]}"
                        upper = message.upper()
                        status = "ENTITLEMENT_REQUIRED" if "403" in upper or "UPGRADE" in upper or "FORBIDDEN" in upper else "UNKNOWN"
                        results[instrument.instrument_id] = {
                            **instrument.to_public_dict(),
                            "status": status,
                            "freshness": "UNKNOWN",
                            "validation_status": "NOT_CHECKED",
                            "trade_eligible": False,
                            "orders_submitted": False,
                            "reason": message,
                            "provider": "Twelve Data",
                        }

            items = [results[item.instrument_id] for item in self.instruments]
            observed = sum(item.get("status") == "OBSERVED_SINGLE_SOURCE" for item in items)
            unknown = len(items) - observed
            if observed == 0:
                status = "UNKNOWN"
            elif unknown:
                status = "PARTIAL_SINGLE_SOURCE"
            else:
                status = "OBSERVED_SINGLE_SOURCE_NOT_VALIDATED"

            payload = {
                "status": status,
                "provider": "Twelve Data",
                "configured": True,
                "validation_status": "SINGLE_SOURCE_ONLY",
                "trade_eligible": False,
                "orders_submitted": False,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                "cache_ttl_seconds": self.cache_ttl_seconds,
                "refreshing": False,
                "items": items,
                "counts": {"total": len(items), "observed": observed, "unknown": unknown},
                "scope_note": (
                    "Curated watchlist only. One provider is not cross-validation; "
                    "no global-market paper or live order is permitted by this board."
                ),
            }
            with self._lock:
                self._cache = copy.deepcopy(payload)
                self._expires_at = time.monotonic() + self.cache_ttl_seconds
                self._last_error = None
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:220]}"
        finally:
            with self._lock:
                self._refreshing = False

    def _start_refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        worker = threading.Thread(
            target=self._refresh_sync,
            daemon=True,
            name="global-market-board-refresh",
        )
        try:
            worker.start()
        except Exception:
            with self._lock:
                self._refreshing = False
            raise

    def get_board(self, *, force_refresh: bool = False) -> dict[str, Any]:
        if not self.provider.configured:
            return self._missing_key_payload()
        with self._lock:
            cache = copy.deepcopy(self._cache)
            expired = self._expires_at <= time.monotonic()
            refreshing = self._refreshing
        if force_refresh or cache is None or expired:
            if not refreshing:
                self._start_refresh()
            with self._lock:
                cache = copy.deepcopy(self._cache)
                refreshing = self._refreshing
                last_error = self._last_error
            if cache is None:
                return {
                    "status": "LOADING",
                    "provider": "Twelve Data",
                    "configured": True,
                    "validation_status": "SINGLE_SOURCE_ONLY",
                    "trade_eligible": False,
                    "orders_submitted": False,
                    "updated_at_utc": None,
                    "cache_ttl_seconds": self.cache_ttl_seconds,
                    "refreshing": refreshing,
                    "items": [],
                    "counts": {"total": len(self.instruments), "observed": 0, "unknown": len(self.instruments)},
                    "scope_note": "Initial market quotes are being collected under the provider's rate limits.",
                }
        if cache is None:
            return self._missing_key_payload()
        cache["refreshing"] = refreshing
        if last_error:
            cache["refresh_error"] = last_error
        return cache
