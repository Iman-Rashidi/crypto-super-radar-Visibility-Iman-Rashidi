"""Read-only public market-data adapter pinned to BloFin Demo Spot."""
from __future__ import annotations

from typing import Any

import requests


DEMO_SPOT_REST_URL = "https://demo-trading-openapi.blofin.com"
_ALLOWED_BARS = frozenset({
    "1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H",
    "8H", "12H", "1D", "3D", "1W", "1M",
})


class BloFinDemoSpotError(RuntimeError):
    """Base error for public BloFin Demo Spot market-data reads."""


class BloFinDemoSpotResponseError(BloFinDemoSpotError):
    """BloFin returned an invalid or unsuccessful public market-data response."""


class BloFinDemoSpotReadOnly:
    """GET-only Demo Spot client. No credentials and no order methods exist."""

    def __init__(self, *, session: Any = None, timeout: float = 10.0):
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.base_url = DEMO_SPOT_REST_URL

    def get_candles(
        self,
        inst_id: str = "BTC-USDT",
        *,
        bar: str = "1H",
        limit: int = 120,
        after: int | str | None = None,
        before: int | str | None = None,
    ) -> list:
        if not isinstance(inst_id, str) or not inst_id.strip():
            raise ValueError("inst_id is required")
        if bar not in _ALLOWED_BARS:
            raise ValueError("unsupported BloFin Spot candle interval")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1440:
            raise ValueError("limit must be between 1 and 1440")
        if after is not None and before is not None:
            raise ValueError("after and before cannot be used simultaneously")
        params = {
            "instType": "SPOT",
            "instId": inst_id.strip().upper(),
            "bar": bar,
            "limit": str(limit),
        }
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        try:
            response = self.session.get(
                f"{self.base_url}/api/v1/spot/market/candles",
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise BloFinDemoSpotError("BloFin Demo Spot market-data request failed") from exc
        except (TypeError, ValueError) as exc:
            raise BloFinDemoSpotResponseError("BloFin Demo Spot returned invalid JSON") from exc

        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            code = payload.get("code") if isinstance(payload, dict) else "INVALID_ENVELOPE"
            raise BloFinDemoSpotResponseError(
                f"BloFin Demo Spot rejected the candles request (code={str(code)[:24]})"
            )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise BloFinDemoSpotResponseError("BloFin Demo Spot candles data is not a list")
        return rows
