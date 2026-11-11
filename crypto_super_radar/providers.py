
"""Free public market-data adapters for GeckoTerminal, DexScreener, Binance and Bybit."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pandas as pd
import requests

from .config import AppConfig
from .models import AssetSpec, MarketSnapshot
from .rate_limiter import SharedRateLimiter


class DataProviderError(RuntimeError):
    pass


NETWORKS = {"solana": "solana", "base": "base", "eth": "eth"}
DEX_CHAINS = {"solana": "solana", "base": "base", "eth": "ethereum"}


class PublicHttpClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.limiter = SharedRateLimiter(config.rate_limits.host_intervals)
        self.headers = {
            "User-Agent": config.user_agent,
            "Accept": "application/json",
        }

    @staticmethod
    def _retry_after_seconds(value: Optional[str], attempt: int) -> float:
        """Parse delta-seconds and HTTP-date Retry-After values, capped at 30 seconds."""
        fallback = min(2 ** attempt, 20)
        if not value:
            return fallback
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return fallback
        if not math.isfinite(delay):
            return fallback
        return min(max(delay, 0.0), 30.0)

    def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.config.request_timeout_seconds)
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}) or {})
        # GeckoTerminal's shared public quota is frequently IP-sensitive.
        # A 429 is handed to the discovery-level cooldown/cache and fallback
        # rather than retrying the same throttled host several times in one scan.
        host = urlparse(url).netloc.lower()
        attempts = 1 if host == "api.geckoterminal.com" else max(1, int(self.config.max_http_retries))
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            self.limiter.wait(url)
            try:
                response = requests.request(method, url, headers=headers, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                break

            status = int(response.status_code)
            if status >= 400:
                detail = (getattr(response, "text", "") or "")[:200]
                error = DataProviderError(
                    f"HTTP {status} from {urlparse(url).netloc}"
                    + (f": {detail}" if detail else "")
                )
                last_error = error
                retryable = status in {408, 425, 429} or status >= 500
                if retryable and attempt + 1 < attempts:
                    delay = (
                        self._retry_after_seconds(response.headers.get("Retry-After"), attempt)
                        if status == 429 else min(2 ** attempt, 8)
                    )
                    time.sleep(delay)
                    continue
                raise error

            try:
                return response.json()
            except ValueError as exc:
                raise DataProviderError(
                    f"Non-JSON response from {urlparse(url).netloc}"
                ) from exc

        raise DataProviderError(
            f"Request failed for {urlparse(url).netloc}: {last_error}"
        ) from last_error

    def get(self, url: str, params: Optional[dict] = None) -> Any:
        return self.request_json("GET", url, params=params)

    def post(self, url: str, payload: dict) -> Any:
        return self.request_json("POST", url, json=payload, headers={"Content-Type": "application/json"})


class GeckoTerminalProvider:
    BASE = "https://api.geckoterminal.com/api/v2"

    def __init__(self, http: PublicHttpClient):
        self.http = http

    @staticmethod
    def _num(value: Any) -> Optional[float]:
        return _safe_float(value)

    def token_pool(self, chain: str, address: str) -> Dict[str, Any]:
        network = NETWORKS[chain]
        url = f"{self.BASE}/networks/{network}/tokens/{address}/pools"
        payload = self.http.get(url, params={"page": 1, "include": "base_token,quote_token"})
        if not isinstance(payload, dict):
            raise DataProviderError("GeckoTerminal returned an unreadable pool response")
        pools = payload.get("data") or []
        if not isinstance(pools, list):
            raise DataProviderError("GeckoTerminal pool list has an invalid schema")
        included = {item.get("id"): item.get("attributes", {}) for item in payload.get("included", []) if isinstance(item, dict)}
        candidates = []
        for pool in pools:
            attrs = pool.get("attributes") or {}
            base_id = ((pool.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id")
            quote_id = ((pool.get("relationships") or {}).get("quote_token") or {}).get("data", {}).get("id")
            base_attrs = included.get(base_id, {})
            quote_attrs = included.get(quote_id, {})
            base_address = (base_attrs.get("address") or "").lower()
            quote_address = (quote_attrs.get("address") or "").lower()
            if base_address == address.lower():
                price = _positive_float(attrs.get("base_token_price_usd"))
            elif quote_address == address.lower():
                price = _positive_float(attrs.get("quote_token_price_usd"))
            else:
                # The token pools endpoint generally returns the token as base;
                # if no relationship match is available, do not guess a price.
                price = None
            liquidity = self._num(attrs.get("reserve_in_usd"))
            volume = self._num((attrs.get("volume_usd") or {}).get("h24"))
            if price is not None and liquidity is not None and volume is not None:
                candidates.append({
                    "price": price,
                    "liquidity": liquidity,
                    "volume_24h": volume,
                    "pool_address": attrs.get("address"),
                    "created_at": attrs.get("pool_created_at"),
                    "raw": attrs,
                })
        if not candidates:
            raise DataProviderError("GeckoTerminal returned no pool with a verifiable token-side USD price")
        return max(candidates, key=lambda item: item["liquidity"])

    def candles(self, chain: str, pool_address: str, token_address: Optional[str] = None, timeframe: str = "day", aggregate: int = 1, limit: int = 365) -> pd.DataFrame:
        network = NETWORKS[chain]
        url = f"{self.BASE}/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}"
        token_selector = token_address if token_address else "base"
        payload = self.http.get(url, params={"aggregate": aggregate, "limit": limit, "currency": "usd", "token": token_selector})
        rows = (((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        return _ohlcv_frame(rows)


class DexScreenerProvider:
    BASE = "https://api.dexscreener.com"

    def __init__(self, http: PublicHttpClient):
        self.http = http

    def token_pair(self, chain: str, address: str) -> Dict[str, Any]:
        url = f"{self.BASE}/token-pairs/v1/{DEX_CHAINS[chain]}/{address}"
        payload = self.http.get(url)
        pairs = payload if isinstance(payload, list) else payload.get("pairs", [])
        if not isinstance(pairs, list):
            raise DataProviderError("DexScreener returned an unreadable pair list")
        usable = []
        for pair in pairs or []:
            if not isinstance(pair, dict):
                raise DataProviderError("DexScreener pair entry has an invalid schema")
            base = pair.get("baseToken") or {}
            if not isinstance(base, dict):
                raise DataProviderError("DexScreener base-token entry has an invalid schema")
            liquidity = _safe_float((pair.get("liquidity") or {}).get("usd"))
            price = _positive_float(pair.get("priceUsd"))
            volume = _safe_float((pair.get("volume") or {}).get("h24"))
            if (base.get("address") or "").lower() != address.lower():
                continue
            if price is None or liquidity is None or volume is None:
                continue
            usable.append({
                "price": price,
                "liquidity": liquidity,
                "volume_24h": volume,
                "pair_address": pair.get("pairAddress"),
                "created_at_ms": pair.get("pairCreatedAt"),
                "raw": pair,
            })
        if not usable:
            raise DataProviderError("DexScreener returned no usable pair where the requested asset is the base token")
        return max(usable, key=lambda item: item["liquidity"])


class BinanceProvider:
    # Binance documents this host specifically for public market-data-only APIs.
    BASE = "https://data-api.binance.vision"

    def __init__(self, http: PublicHttpClient):
        self.http = http

    def ticker(self, symbol: str) -> Dict[str, float]:
        price = self.http.get(f"{self.BASE}/api/v3/ticker/price", {"symbol": symbol})
        stats = self.http.get(f"{self.BASE}/api/v3/ticker/24hr", {"symbol": symbol})
        p = _positive_float(price.get("price"))
        vol = _safe_float(stats.get("quoteVolume"))
        if p is None or vol is None:
            raise DataProviderError(f"Binance returned incomplete ticker for {symbol}")
        return {"price": p, "volume_24h": vol, "raw": stats}

    def candles(self, symbol: str, interval: str = "1d", limit: int = 365) -> pd.DataFrame:
        payload = self.http.get(f"{self.BASE}/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)})
        return _ohlcv_frame(payload)


class BybitProvider:
    BASE = "https://api.bybit.com"
    GATE = "https://api.gateio.ws/api/v4"

    def __init__(self, http: PublicHttpClient):
        self.http = http

    @staticmethod
    def _gate_currency_pair(symbol: str) -> str:
        """Map only an explicit USDT-quoted symbol to Gate's exact spot pair format."""
        normalized = str(symbol).upper()
        if not normalized.endswith("USDT") or len(normalized) <= 4:
            raise DataProviderError(
                f"Gate.io spot fallback supports only explicit USDT-quoted symbols: {symbol}"
            )
        return f"{normalized[:-4]}_USDT"

    def ticker(self, symbol: str) -> Dict[str, Any]:
        try:
            return self._bybit_ticker(symbol)
        except DataProviderError as exc:
            message = str(exc)
            fallback_eligible = (
                "HTTP 403" in message
                or "HTTP 451" in message
                or "Bybit API retCode=" in message
                or "Bybit has no spot ticker" in message
            )
            if not fallback_eligible:
                raise

            currency_pair = self._gate_currency_pair(symbol)
            try:
                payload = self.http.get(
                    f"{self.GATE}/spot/tickers",
                    {"currency_pair": currency_pair},
                )
                if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
                    raise DataProviderError(
                        f"Gate.io spot fallback returned no unique ticker for {currency_pair}"
                    )
                item = payload[0]
                returned_pair = item.get("currency_pair")
                if returned_pair != currency_pair:
                    raise DataProviderError(
                        f"Gate.io spot fallback pair mismatch: expected {currency_pair}, "
                        f"received {returned_pair!r}"
                    )
                price = _positive_float(item.get("last"))
                volume = _safe_float(item.get("quote_volume"))
                if price is None or volume is None:
                    raise DataProviderError(
                        f"Gate.io spot fallback returned incomplete ticker for {currency_pair}"
                    )
                return {
                    "price": price,
                    "volume_24h": volume,
                    "raw": item,
                    "provider": "Gate.io Spot",
                    "fallback_reason": message[:240],
                }
            except DataProviderError as fallback_error:
                raise DataProviderError(
                    f"Bybit spot unavailable ({message}); Gate.io fallback failed: {fallback_error}"
                ) from fallback_error

    def _bybit_ticker(self, symbol: str) -> Dict[str, Any]:
        payload = self.http.get(
            f"{self.BASE}/v5/market/tickers",
            {"category": "spot", "symbol": symbol},
        )
        if not isinstance(payload, dict):
            raise DataProviderError(
                f"Bybit spot ticker returned an invalid response schema for {symbol}"
            )
        ret_code = payload.get("retCode", "MISSING")
        if str(ret_code) != "0":
            ret_msg = payload.get("retMsg", "MISSING")
            raise DataProviderError(
                f"Bybit API retCode={ret_code!r} retMsg={ret_msg!r} for spot ticker {symbol}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise DataProviderError(
                f"Bybit spot ticker response omitted result object for {symbol}"
            )
        items = result.get("list")
        if not isinstance(items, list):
            raise DataProviderError(
                f"Bybit spot ticker response omitted list for {symbol}"
            )
        if not items:
            raise DataProviderError(
                f"Bybit has no spot ticker for {symbol} (retCode=0, retMsg={payload.get('retMsg')!r})"
            )
        if len(items) != 1 or not isinstance(items[0], dict):
            raise DataProviderError(
                f"Bybit returned a non-unique spot ticker response for {symbol}"
            )
        item = items[0]
        returned_symbol = item.get("symbol")
        if returned_symbol and returned_symbol != symbol:
            raise DataProviderError(
                f"Bybit spot ticker symbol mismatch: expected {symbol}, received {returned_symbol}"
            )
        price = _positive_float(item.get("lastPrice"))
        volume = _safe_float(item.get("turnover24h"))
        if price is None or volume is None:
            raise DataProviderError(f"Bybit returned incomplete ticker for {symbol}")
        return {"price": price, "volume_24h": volume, "raw": item}

    def candles(self, symbol: str, interval: str = "D", limit: int = 365) -> pd.DataFrame:
        payload = self.http.get(f"{self.BASE}/v5/market/kline", {"category": "spot", "symbol": symbol, "interval": interval, "limit": min(limit, 1000)})
        rows = ((payload.get("result") or {}).get("list") or [])
        # Bybit order: startTime, open, high, low, close, volume, turnover.
        return _ohlcv_frame(rows)


class SolanaRpcProvider:
    URL = "https://api.mainnet-beta.solana.com"

    def __init__(self, http: PublicHttpClient):
        self.http = http

    def _rpc(self, method: str, params: list) -> Any:
        payload = self.http.post(self.URL, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(payload, dict):
            raise DataProviderError(f"Solana RPC {method} returned an unreadable response")
        if payload.get("error"):
            raise DataProviderError(f"Solana RPC {method}: {payload['error']}")
        result = payload.get("result")
        if not isinstance(result, dict) or "value" not in result:
            raise DataProviderError(f"Solana RPC {method} omitted the required result value")
        return result["value"]

    def token_security(self, mint: str) -> Dict[str, Any]:
        account = self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if not account:
            raise DataProviderError("Solana mint account not found")
        info = (((account.get("data") or {}).get("parsed") or {}).get("info") or {})
        if not info:
            raise DataProviderError("Solana RPC did not return a parsed mint account")
        mint_authority = info.get("mintAuthority")
        freeze_authority = info.get("freezeAuthority")
        supply_payload = self.http.post(self.URL, {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]})
        if not isinstance(supply_payload, dict) or supply_payload.get("error"):
            raise DataProviderError("Solana RPC getTokenSupply returned an API error or unreadable response")
        supply_value = ((supply_payload.get("result") or {}).get("value") or {})
        if not isinstance(supply_value, dict):
            raise DataProviderError("Solana RPC getTokenSupply returned an invalid value schema")
        total_supply = _positive_float(supply_value.get("uiAmountString"))
        if total_supply is None:
            raise DataProviderError("Solana token supply is missing, non-finite or non-positive")
        largest = self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]) or []
        if not isinstance(largest, list):
            raise DataProviderError("Solana largest-token-account response has an invalid schema")
        top_amounts = []
        for row in largest:
            if not isinstance(row, dict):
                raise DataProviderError("Solana largest-token-account row has an invalid schema")
            amount = _safe_float(row.get("uiAmountString"))
            if amount is None:
                raise DataProviderError("Solana largest-token-account amount is missing or invalid")
            top_amounts.append(amount)
        top10_pct = None
        if total_supply and total_supply > 0 and top_amounts:
            top10_pct = min(100.0, sum(top_amounts[:10]) / total_supply * 100.0)
        return {
            "mint_authority_active": mint_authority is not None,
            "freeze_authority_active": freeze_authority is not None,
            "top10_token_account_concentration_pct": top10_pct,
            "mint_authority": mint_authority,
            "freeze_authority": freeze_authority,
            "supply": total_supply,
            "scope_note": "Top-10 token-account concentration is not wallet-owner concentration; LP and exchange accounts may distort it.",
        }


def _finite_float(value: Any) -> Optional[float]:
    """Parse finite numeric values, including signed funding; reject boolean schema values."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    """Parse a finite non-negative quantity such as volume or token supply."""
    result = _finite_float(value)
    return result if result is not None and result >= 0 else None


def _positive_float(value: Any) -> Optional[float]:
    """Parse a finite, strictly positive price or open-interest value."""
    result = _finite_float(value)
    return result if result is not None and result > 0 else None


def _ohlcv_frame(rows: Any) -> pd.DataFrame:
    """Normalize OHLCV rows; reject malformed, duplicate or future observations."""
    empty = pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
    )
    if rows is None:
        return empty
    if not isinstance(rows, (list, tuple)):
        raise DataProviderError("OHLCV response must be a list of candle rows")
    if not rows:
        return empty

    parsed = []
    invalid_timestamps = []
    seen_timestamps = set()
    now = pd.Timestamp.now(tz="UTC")
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise DataProviderError(f"Malformed OHLCV row at position {index}")
        raw_ts = _finite_float(row[0])
        if raw_ts is None or raw_ts <= 0:
            invalid_timestamps.append(index)
            continue
        if not raw_ts.is_integer():
            raise DataProviderError(f"Fractional OHLCV timestamp at position {index}")
        ts = int(raw_ts)
        if ts > 10**12:
            ts //= 1000
        try:
            stamp = pd.to_datetime(ts, unit="s", utc=True, errors="raise")
        except (TypeError, ValueError, OverflowError) as exc:
            raise DataProviderError(f"Unreadable OHLCV timestamp at position {index}") from exc
        if stamp > now + pd.Timedelta(minutes=5):
            raise DataProviderError(f"Future OHLCV timestamp at position {index}")
        if stamp in seen_timestamps:
            raise DataProviderError(f"Duplicate OHLCV timestamp at position {index}")
        seen_timestamps.add(stamp)

        open_, high, low, close = [_positive_float(value) for value in row[1:5]]
        volume = _safe_float(row[5])
        if any(value is None for value in (open_, high, low, close)) or volume is None:
            raise DataProviderError(f"Missing or non-finite OHLCV value at position {index}")
        if high < max(open_, low, close) or low > min(open_, high, close):
            raise DataProviderError(f"Inconsistent OHLC range at position {index}")
        parsed.append((stamp, open_, high, low, close, volume))

    if invalid_timestamps and parsed:
        raise DataProviderError(
            f"OHLCV response mixes valid candles with invalid timestamps at positions {invalid_timestamps}"
        )
    if not parsed:
        # An entirely unusable response remains an empty frame, which downstream
        # freshness/coverage gates classify as UNKNOWN rather than fabricating data.
        return empty

    frame = pd.DataFrame(parsed, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return frame.set_index("timestamp").sort_index().astype(float)


def relative_difference_pct(a: float, b: float) -> float:
    denominator = (abs(a) + abs(b)) / 2.0
    if denominator == 0:
        return 0.0
    return abs(a - b) / denominator * 100.0


class MarketDataService:
    """Fetch and cross-validate two independent public venues/data providers."""

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        http = PublicHttpClient(self.config)
        self.http = http
        self.gecko = GeckoTerminalProvider(http)
        self.dexscreener = DexScreenerProvider(http)
        self.binance = BinanceProvider(http)
        self.bybit = BybitProvider(http)
        self.solana_rpc = SolanaRpcProvider(http)
        self.perpetual = PerpetualMarketProvider(http)

    def snapshot(self, asset: AssetSpec) -> MarketSnapshot:
        from concurrent.futures import ThreadPoolExecutor
        threshold = self.config.thresholds.provider_discrepancy_pct
        with ThreadPoolExecutor(max_workers=2) as executor:
            if asset.market_type == "dex":
                future_a = executor.submit(self.gecko.token_pool, asset.chain, asset.address)
                future_b = executor.submit(self.dexscreener.token_pair, asset.chain, asset.address)
                a, b = future_a.result(), future_b.result()
                name_a, name_b = "GeckoTerminal", "DexScreener"
            elif asset.market_type == "perp":
                future_a = executor.submit(self.perpetual.binance_ticker, asset.symbol)
                future_b = executor.submit(self.perpetual.bybit_ticker, asset.symbol)
                a, b = future_a.result(), future_b.result()
                funding_diff = abs(a["funding_rate"] - b["funding_rate"])
                if funding_diff > 0.0002:
                    raise DataProviderError(f"Funding-rate discrepancy exceeds 2 bps: {funding_diff:.6f}")
                if a["open_interest"] <= 0 or b["open_interest"] <= 0:
                    raise DataProviderError("Open interest is zero or unavailable")
                name_a, name_b = a.get("provider", "Binance USD-M"), b.get("provider", "Bybit Linear")
            else:
                future_a = executor.submit(self.binance.ticker, asset.symbol)
                future_b = executor.submit(self.bybit.ticker, asset.symbol)
                a, b = future_a.result(), future_b.result()
                name_a, name_b = "Binance Spot", b.get("provider", "Bybit Spot")
        price_diff = relative_difference_pct(a["price"], b["price"])
        # DEX providers describe the same token/pool universe, so both price
        # and volume are compared. CEX venues have different traded volumes by
        # definition; compare prices there and screen each venue's turnover
        # independently instead of falsely requiring equal exchange volumes.
        volume_diff = relative_difference_pct(a["volume_24h"], b["volume_24h"]) if asset.market_type == "dex" else None
        if price_diff > threshold or (volume_diff is not None and volume_diff > threshold):
            volume_detail = f", volume discrepancy={volume_diff:.2f}%" if volume_diff is not None else ""
            raise DataProviderError(
                f"Cross-validation rejected asset: price discrepancy={price_diff:.2f}%"
                f"{volume_detail} (limit={threshold:.2f}%)"
            )
        liquidity_values = [v for v in (a.get("liquidity"), b.get("liquidity")) if v is not None]
        liquidity = min(liquidity_values) if liquidity_values else None
        return MarketSnapshot(
            asset=asset,
            price_usd=(a["price"] + b["price"]) / 2.0,
            volume_24h_usd=min(a["volume_24h"], b["volume_24h"]),
            liquidity_usd=liquidity,
            price_discrepancy_pct=price_diff,
            volume_discrepancy_pct=volume_diff,
            provider_a=name_a,
            provider_b=name_b,
            pool_address=a.get("pool_address"),
            pool_created_at=a.get("created_at"),
            pair_address=b.get("pair_address"),
            raw={"provider_a": a, "provider_b": b,
                 "funding_rate_8h": ((a.get("funding_rate", 0.0) + b.get("funding_rate", 0.0)) / 2.0) if asset.market_type == "perp" else None,
                 "open_interest_a": a.get("open_interest"), "open_interest_b": b.get("open_interest")},
        )

    def candles(self, asset: AssetSpec, snapshot: MarketSnapshot) -> pd.DataFrame:
        if asset.market_type == "dex":
            if not snapshot.pool_address:
                raise DataProviderError("No validated pool address for OHLCV request")
            return self.gecko.candles(asset.chain, snapshot.pool_address, token_address=asset.address, timeframe="day", aggregate=1, limit=365)
        if asset.market_type == "perp":
            try:
                return self.perpetual.binance_candles(asset.symbol, limit=365)
            except DataProviderError as exc:
                if not any(code in str(exc) for code in ("HTTP 403", "HTTP 451")):
                    raise
                instrument = f"{asset.symbol[:-4]}-{asset.symbol[-4:]}-SWAP"
                payload = self.perpetual.http.get(
                    f"{self.perpetual.OKX}/market/history-candles",
                    {"instId": instrument, "bar": "1Dutc", "limit": 365},
                )
                if not isinstance(payload, dict) or payload.get("code") != "0":
                    raise DataProviderError("OKX perpetual candle fallback returned an API error") from exc
                rows = payload.get("data")
                if not isinstance(rows, list):
                    raise DataProviderError("OKX perpetual candle fallback returned an invalid schema") from exc
                return _ohlcv_frame(rows)
        return self.binance.candles(asset.symbol, interval="1d", limit=365)

    def benchmark_candles(self) -> pd.DataFrame:
        return self.binance.candles("BTCUSDT", interval="1d", limit=365)

    def funding_history(self, asset: AssetSpec) -> pd.Series:
        if asset.market_type != "perp":
            return pd.Series(dtype=float)
        return self.perpetual.funding_history(asset.symbol)

    def security(self, asset: AssetSpec) -> Dict[str, Any]:
        if asset.market_type in {"cex", "perp"}:
            # Only major native assets have an explicit allowlist in this prototype.
            if asset.symbol in {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT", "GRAMUSDT"}:
                return {"status": "PASS", "reason": "Native major asset; token-contract authority checks are not applicable.", "scope": "Exchange/custody and market risks remain; listing is not a guarantee of safety."}
            return {"status": "UNKNOWN", "reason": "CEX instrument is not in the conservative native-asset allowlist."}
        if asset.chain == "solana":
            return {"status": "KNOWN", **self.solana_rpc.token_security(asset.address)}
        # Generic EVM bytecode/owner inspection is not sufficient to certify
        # mint, blacklist, proxy, honeypot and admin-role risks. Fail closed.
        return {
            "status": "UNKNOWN",
            "reason": "EVM token security is not certified by a generic free public-RPC check; no trade is allowed.",
        }

class PerpetualMarketProvider:
    """Public exchange market data with free Gate.io/OKX regional fallbacks."""

    BINANCE = "https://fapi.binance.com"
    BYBIT = "https://api.bybit.com"
    GATE = "https://api.gateio.ws/api/v4"
    OKX = "https://www.okx.com/api/v5"

    def __init__(self, http: PublicHttpClient):
        self.http = http

    def binance_ticker(self, symbol: str) -> Dict[str, Any]:
        try:
            return self._binance_ticker(symbol)
        except DataProviderError as exc:
            if not any(code in str(exc) for code in ("HTTP 403", "HTTP 451")):
                raise
            contract = f"{symbol[:-4]}_USDT"
            tickers = self.http.get(f"{self.GATE}/futures/usdt/tickers", {"contract": contract})
            meta = self.http.get(f"{self.GATE}/futures/usdt/contracts/{contract}")
            if not isinstance(tickers, list) or len(tickers) != 1 or not isinstance(meta, dict):
                raise DataProviderError(f"Gate.io fallback returned invalid ticker for {symbol}") from exc
            row = tickers[0]
            values = {
                "price": _positive_float(row.get("mark_price")),
                "volume_24h": _safe_float(row.get("volume_24h_settle") or row.get("volume_24h_quote")),
                "funding_rate": _finite_float(row.get("funding_rate")),
                "open_interest": _positive_float(row.get("total_size")),
            }
            interval = _positive_float(meta.get("funding_interval"))
            if interval is None or any(v is None for v in values.values()):
                raise DataProviderError(f"Gate.io fallback returned incomplete data for {symbol}") from exc
            return {**values, "funding_interval_hours": interval / 3600.0,
                    "raw": row, "provider": "Gate.io USDT Perpetual"}

    def _binance_ticker(self, symbol: str) -> Dict[str, Any]:
        price = self.http.get(f"{self.BINANCE}/fapi/v1/ticker/price", {"symbol": symbol})
        stats = self.http.get(f"{self.BINANCE}/fapi/v1/ticker/24hr", {"symbol": symbol})
        premium = self.http.get(f"{self.BINANCE}/fapi/v1/premiumIndex", {"symbol": symbol})
        oi = self.http.get(f"{self.BINANCE}/fapi/v1/openInterest", {"symbol": symbol})
        values = {
            "price": _positive_float(price.get("price")),
            "volume_24h": _safe_float(stats.get("quoteVolume")),
            "funding_rate": _finite_float(premium.get("lastFundingRate")),
            "open_interest": _positive_float(oi.get("openInterest")),
        }
        if any(values[k] is None for k in values):
            raise DataProviderError(f"Binance Futures returned incomplete ticker for {symbol}")
        return {**values, "raw": {"ticker": stats, "premium": premium, "open_interest": oi}}

    def bybit_ticker(self, symbol: str) -> Dict[str, Any]:
        try:
            return self._bybit_ticker(symbol)
        except DataProviderError as exc:
            if not any(code in str(exc) for code in ("HTTP 403", "HTTP 451")):
                raise
            instrument = f"{symbol[:-4]}-{symbol[-4:]}-SWAP"
            ticker = self.http.get(f"{self.OKX}/market/ticker", {"instId": instrument})
            funding = self.http.get(f"{self.OKX}/public/funding-rate", {"instId": instrument})
            interest = self.http.get(f"{self.OKX}/public/open-interest", {"instType": "SWAP", "instId": instrument})
            payloads = (ticker, funding, interest)
            if any(not isinstance(p, dict) or p.get("code") != "0" for p in payloads):
                raise DataProviderError(f"OKX perpetual fallback returned an API error for {symbol}") from exc
            rows = [p.get("data") for p in payloads]
            if any(not isinstance(row, list) or len(row) != 1 for row in rows):
                raise DataProviderError(f"OKX perpetual fallback has incomplete data for {symbol}") from exc
            t, f, o = (row[0] for row in rows)
            values = {
                "price": _positive_float(t.get("last")),
                "volume_24h": _safe_float(t.get("volCcy24h")),
                "funding_rate": _finite_float(f.get("fundingRate")),
                "open_interest": _positive_float(o.get("oi")),
            }
            if any(v is None for v in values.values()):
                raise DataProviderError(f"OKX perpetual fallback returned invalid values for {symbol}") from exc
            return {**values, "raw": {"ticker": t, "funding": f, "open_interest": o},
                    "provider": "OKX USDT Swap"}

    def _bybit_ticker(self, symbol: str) -> Dict[str, Any]:
        payload = self.http.get(f"{self.BYBIT}/v5/market/tickers", {"category": "linear", "symbol": symbol})
        if not isinstance(payload, dict) or payload.get("retCode") not in (0, "0"):
            raise DataProviderError(f"Bybit Linear ticker returned an API error for {symbol}")
        items = ((payload.get("result") or {}).get("list") or [])
        if not isinstance(items, list) or not items:
            raise DataProviderError(f"Bybit Linear has no market for {symbol}")
        item = items[0]
        values = {
            "price": _positive_float(item.get("lastPrice")),
            "volume_24h": _safe_float(item.get("turnover24h")),
            "funding_rate": _finite_float(item.get("fundingRate")),
            "open_interest": _positive_float(item.get("openInterest")),
        }
        if any(values[k] is None for k in values):
            raise DataProviderError(f"Bybit Linear returned incomplete ticker for {symbol}")
        return {**values, "raw": item}

    def binance_candles(self, symbol: str, limit: int = 365) -> pd.DataFrame:
        rows = self.http.get(f"{self.BINANCE}/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": min(limit, 1000)})
        return _ohlcv_frame(rows)

    def funding_interval_hours(self, symbol: str) -> float:
        try:
            return self._legacy_funding_interval_hours(symbol)
        except DataProviderError as exc:
            if not any(code in str(exc) for code in ("HTTP 403", "HTTP 451")):
                raise
            contract = f"{symbol[:-4]}_USDT"
            instrument = f"{symbol[:-4]}-{symbol[-4:]}-SWAP"
            meta = self.http.get(f"{self.GATE}/futures/usdt/contracts/{contract}")
            gate_seconds = _positive_float(meta.get("funding_interval")) if isinstance(meta, dict) else None
            okx = self.http.get(f"{self.OKX}/public/funding-rate-history", {"instId": instrument, "limit": 3})
            rows = okx.get("data") if isinstance(okx, dict) and okx.get("code") == "0" else None
            stamps = sorted(
                int(float(row["fundingTime"])) for row in rows
                if isinstance(row, dict) and _positive_float(row.get("fundingTime")) is not None
            ) if isinstance(rows, list) else []
            if gate_seconds is None or len(stamps) < 2:
                raise DataProviderError("Gate.io/OKX funding interval metadata is incomplete") from exc
            okx_hours = (stamps[-1] - stamps[-2]) / 3_600_000
            gate_hours = gate_seconds / 3600.0
            if okx_hours <= 0 or abs(gate_hours - okx_hours) > (1.0 / 6.0):
                raise DataProviderError(f"Gate.io/OKX funding interval mismatch: {gate_hours:g}h vs {okx_hours:g}h") from exc
            return gate_hours

    def _legacy_funding_interval_hours(self, symbol: str) -> float:
        """Require Binance USD-M and Bybit Linear funding settlement intervals to agree."""
        binance_payload = self.http.get(f"{self.BINANCE}/fapi/v1/fundingInfo")
        if not isinstance(binance_payload, list):
            raise DataProviderError("Binance funding-info response has an invalid schema")
        binance_matches = [
            row for row in binance_payload
            if isinstance(row, dict) and row.get("symbol") == symbol
        ]
        if len(binance_matches) > 1:
            raise DataProviderError("Binance returned duplicate funding-interval metadata")
        if binance_matches:
            binance_hours = _positive_float(binance_matches[0].get("fundingIntervalHours"))
            if binance_hours is None:
                raise DataProviderError("Binance adjusted funding interval is missing or invalid")
        else:
            # Binance documents 8 hours as the default; fundingInfo returns
            # symbols whose funding caps/floors/interval have been adjusted.
            binance_hours = 8.0

        bybit_payload = self.http.get(
            f"{self.BYBIT}/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol},
        )
        if not isinstance(bybit_payload, dict) or bybit_payload.get("retCode") not in (0, "0"):
            raise DataProviderError("Bybit instrument metadata returned an API error or unreadable response")
        items = ((bybit_payload.get("result") or {}).get("list") or [])
        matches = [
            item for item in items
            if isinstance(item, dict) and item.get("symbol") == symbol
        ] if isinstance(items, list) else []
        if len(matches) != 1:
            raise DataProviderError("Bybit must return exactly one matching linear instrument")
        bybit_minutes = _positive_float(matches[0].get("fundingInterval"))
        if bybit_minutes is None or not bybit_minutes.is_integer():
            raise DataProviderError("Bybit funding interval is missing or invalid")
        bybit_hours = bybit_minutes / 60.0

        if abs(binance_hours - bybit_hours) > 1e-9:
            raise DataProviderError(
                f"Funding settlement interval mismatch: Binance={binance_hours:g}h, Bybit={bybit_hours:g}h"
            )
        return binance_hours

    def funding_history(self, symbol: str, limit: int = 1000) -> pd.Series:
        try:
            return self._legacy_funding_history(symbol, limit)
        except DataProviderError as exc:
            if not any(code in str(exc) for code in ("HTTP 403", "HTTP 451")):
                raise
            interval = self.funding_interval_hours(symbol)
            contract, instrument = f"{symbol[:-4]}_USDT", f"{symbol[:-4]}-{symbol[-4:]}-SWAP"
            gate_rows = self.http.get(f"{self.GATE}/futures/usdt/funding_rate",
                                      {"contract": contract, "limit": min(limit, 1000)})
            okx_payload = self.http.get(f"{self.OKX}/public/funding-rate-history",
                                        {"instId": instrument, "limit": min(limit, 100)})
            okx_rows = okx_payload.get("data") if isinstance(okx_payload, dict) and okx_payload.get("code") == "0" else None
            if not isinstance(gate_rows, list) or not isinstance(okx_rows, list):
                raise DataProviderError("Gate.io/OKX historical funding returned an invalid schema") from exc
            def series(rows, ts_key, rate_key, scale):
                points = []
                for row in rows:
                    if not isinstance(row, dict):
                        raise DataProviderError("Historical funding row has an invalid schema")
                    stamp = _positive_float(row.get(ts_key))
                    rate = _finite_float(row.get(rate_key))
                    if stamp is None or rate is None:
                        raise DataProviderError("Historical funding timestamp or rate is invalid")
                    points.append((pd.to_datetime(int(stamp), unit=scale, utc=True), rate))
                result = pd.Series(dict(points), dtype=float).sort_index()
                if result.index.has_duplicates:
                    raise DataProviderError("Historical funding contains duplicate timestamps")
                return result
            gate = series(gate_rows, "t", "r", "s")
            okx = series(okx_rows, "fundingTime", "fundingRate", "ms")
            if gate.empty or okx.empty:
                raise DataProviderError("Gate.io/OKX historical funding is empty") from exc
            left = gate.rename("gate").reset_index().rename(columns={"index": "timestamp"})
            right = okx.rename("okx").reset_index().rename(columns={"index": "timestamp"})
            matched = pd.merge_asof(left.sort_values("timestamp"), right.sort_values("timestamp"),
                                    on="timestamp", direction="nearest", tolerance=pd.Timedelta(minutes=10))
            matched = matched.dropna(subset=["okx"])
            if matched.empty or (matched["gate"].sub(matched["okx"]).abs() > 0.0002).any():
                raise DataProviderError("Gate.io/OKX historical funding is unavailable or divergent") from exc
            result = pd.Series((matched["gate"].to_numpy() + matched["okx"].to_numpy()) / 2.0,
                               index=pd.DatetimeIndex(matched["timestamp"])).sort_index()
            result.attrs["funding_interval_hours"] = interval
            return result

    def _legacy_funding_history(self, symbol: str, limit: int = 1000) -> pd.Series:
        """Fetch cross-checked funding history with a validated common settlement interval."""
        funding_interval_hours = self.funding_interval_hours(symbol)
        binance_rows = self.http.get(f"{self.BINANCE}/fapi/v1/fundingRate", {"symbol": symbol, "limit": min(limit, 1000)})
        bybit_payload = self.http.get(f"{self.BYBIT}/v5/market/funding/history", {"category": "linear", "symbol": symbol, "limit": 200})
        if not isinstance(bybit_payload, dict) or bybit_payload.get("retCode") not in (0, "0"):
            raise DataProviderError("Bybit historical funding returned an API error or unreadable response")
        bybit_rows = ((bybit_payload.get("result") or {}).get("list") or [])

        def series_from(rows: Any, ts_key: str, rate_key: str) -> pd.Series:
            if not isinstance(rows, list):
                raise DataProviderError("Historical funding response has an invalid schema")
            parsed = []
            now = pd.Timestamp.now(tz="UTC")
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise DataProviderError(f"Malformed funding row at position {index}")
                try:
                    stamp = int(row[ts_key])
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise DataProviderError(f"Invalid funding timestamp at position {index}") from exc
                rate = _finite_float(row.get(rate_key))
                if stamp <= 0 or rate is None:
                    raise DataProviderError(f"Missing or non-finite funding observation at position {index}")
                timestamp = pd.to_datetime(stamp, unit="ms", utc=True, errors="coerce")
                if pd.isna(timestamp) or timestamp > now + pd.Timedelta(minutes=5):
                    raise DataProviderError(f"Unreadable or future funding timestamp at position {index}")
                parsed.append((timestamp, rate))
            if not parsed:
                return pd.Series(dtype=float)
            result = pd.Series(
                [rate for _, rate in parsed],
                index=pd.DatetimeIndex([ts for ts, _ in parsed]),
            ).sort_index()
            if result.index.has_duplicates:
                raise DataProviderError("Historical funding contains duplicate timestamps")
            return result

        binance = series_from(binance_rows, "fundingTime", "fundingRate")
        bybit = series_from(bybit_rows, "fundingRateTimestamp", "fundingRate")
        if binance.empty or bybit.empty:
            raise DataProviderError("Historical funding unavailable from both venues")

        latest_a_ts, latest_b_ts = binance.index[-1], bybit.index[-1]
        if abs(latest_a_ts - latest_b_ts) > pd.Timedelta(minutes=10):
            raise DataProviderError("Latest funding timestamps differ by more than 10 minutes")
        if pd.Timestamp.now(tz="UTC") - min(latest_a_ts, latest_b_ts) > pd.Timedelta(hours=24):
            raise DataProviderError("Latest historical funding observations are stale")
        if abs(float(binance.iloc[-1]) - float(bybit.iloc[-1])) > 0.0002:
            raise DataProviderError("Latest funding rates diverge by more than 2 bps between venues")

        def validate_overlap(merged: pd.DataFrame) -> pd.DataFrame:
            if len(merged) < 3:
                raise DataProviderError("Insufficient overlapping historical funding observations")
            if (merged["binance"].sub(merged["bybit"]).abs() > 0.0002).any():
                raise DataProviderError("Historical funding observations diverge by more than 2 bps")
            return merged

        common = binance.index.intersection(bybit.index)
        if len(common) >= 3:
            matched = pd.DataFrame({"binance": binance.loc[common], "bybit": bybit.loc[common]})
            matched = validate_overlap(matched)
            return ((matched["binance"] + matched["bybit"]) / 2.0).sort_index()

        left = binance.rename("binance").reset_index().rename(columns={"index": "timestamp"}).sort_values("timestamp")
        right = bybit.rename("bybit").reset_index().rename(columns={"index": "timestamp"}).sort_values("timestamp")
        merged = pd.merge_asof(left, right, on="timestamp", direction="nearest", tolerance=pd.Timedelta("10min"))
        merged = validate_overlap(merged.dropna())
        merged["funding"] = (merged["binance"] + merged["bybit"]) / 2.0
        result = pd.Series(merged["funding"].to_numpy(), index=pd.DatetimeIndex(merged["timestamp"])).sort_index()
        result.attrs["funding_interval_hours"] = funding_interval_hours
        return result
