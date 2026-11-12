"""Optional TradingAgents second-opinion bridge for the CRYPTO SUPER RADAR paper bot.

The adapter is deliberately fail-closed: when enabled, an unavailable model,
missing credentials, unsupported ticker, or unparseable rating blocks a new
paper entry. It never changes the radar's twelve mandatory gates and never
submits exchange orders.
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_ASSET_TICKERS = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "SOLUSDT": "SOL-USD",
}
_CACHE_TTL_SECONDS = 21_600
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def is_trading_agents_enabled() -> bool:
    """The extra LLM review is opt-in and disabled by default."""
    return os.getenv("CSR_TRADINGAGENTS_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def rating_allows_entry(rating: Any) -> bool:
    """Only an explicit Buy/Overweight opinion can pass the second-opinion veto."""
    return str(rating or "").strip().casefold() in {"buy", "overweight"}


def _asset_symbol(asset_input: Any) -> str | None:
    raw = getattr(asset_input, "raw", asset_input)
    raw = str(raw or "").strip().upper()
    if ":" in raw:
        market, symbol = raw.split(":", 1)
        if market.lower() != "cex":
            return None
        raw = symbol
    return _ASSET_TICKERS.get(raw)


def _safe_excerpt(value: Any, limit: int = 1_200) -> str | None:
    if isinstance(value, dict):
        value = value.get("content") or value.get("text") or str(value)
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())
    return clean[:limit] if clean else None


def _load_graph_class():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph


def _build_config(data_dir: Path, model: str) -> dict[str, Any]:
    from tradingagents.default_config import build_default_config

    config = copy.deepcopy(build_default_config())
    config.update({
        "llm_provider": "google",
        "quick_think_provider": "google",
        "deep_think_provider": "google",
        "quick_think_llm": model,
        "deep_think_llm": model,
        "output_language": "English",
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "max_tool_rounds": 2,
        "max_recur_limit": 100,
        "checkpoint_enabled": False,
        "temperature": 0.1,
        "max_tokens": 1_024,
        "google_thinking_level": "low",
        "results_dir": str(data_dir / "reports"),
        "data_cache_dir": str(data_dir / "cache"),
        "memory_log_path": str(data_dir / "memory" / "trading_memory.md"),
        # Keep the optional LLM review isolated from the radar's own ledger.
        "news_article_limit": 8,
        "global_news_article_limit": 4,
        "global_news_lookback_days": 3,
    })
    return config


def run_trading_agents_review(asset_input: Any) -> dict[str, Any]:
    """Run TradingAgents for a supported spot ticker and return a veto-only review."""
    if not is_trading_agents_enabled():
        return {"status": "SKIPPED", "reason": "TradingAgents review is disabled."}

    ticker = _asset_symbol(asset_input)
    if ticker is None:
        return {
            "status": "UNKNOWN",
            "reason": "TradingAgents supports only BTC, ETH and SOL spot reviews in this adapter.",
        }
    if sys.version_info < (3, 11):
        return {
            "status": "UNKNOWN",
            "reason": "TradingAgents v0.7.1 requires Python 3.11 or later.",
        }

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {
            "status": "UNKNOWN",
            "reason": "Google Gemini API key is missing; no second-opinion review was run.",
        }
    # The SDK and TradingAgents look for GOOGLE_API_KEY. Never override a key
    # already provided explicitly by the host.
    os.environ.setdefault("GOOGLE_API_KEY", api_key)

    model = os.getenv("CSR_TRADINGAGENTS_MODEL", "gemini-3.8-flash").strip()
    if not model:
        return {"status": "UNKNOWN", "reason": "TradingAgents model configuration is empty."}

    cache_key = ticker
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > now:
            return copy.deepcopy(cached[1])

    try:
        import tradingagents

        if getattr(tradingagents, "__version__", None) != "0.7.1":
            return {
                "status": "UNKNOWN",
                "reason": "The bridge is pinned to TradingAgents v0.7.1; installed version differs.",
            }

        from tradingagents.agents.rating import run_rating
        from tradingagents.dataflows.date_window import get_current_date

        data_dir = Path(
            os.getenv("CSR_TRADINGAGENTS_DATA_DIR")
            or (Path(tempfile.gettempdir()) / "crypto-super-radar-tradingagents")
        ).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        config = _build_config(data_dir, model)
        graph_class = _load_graph_class()
        graph = graph_class(
            selected_analysts=("market", "social", "news"),
            debug=False,
            config=config,
        )
        final_state, signal = graph.propagate(
            ticker,
            trade_date=get_current_date(),
            asset_type="crypto",
        )
        rating = str(signal or run_rating(final_state) or "REVIEW").strip()
        if rating.casefold() == "review":
            status = "UNKNOWN"
            reason = "TradingAgents could not produce a parseable five-tier rating."
        elif rating_allows_entry(rating):
            status = "PASS"
            reason = "TradingAgents returned an explicit bullish rating; the radar's existing gates remain mandatory."
        else:
            status = "VETO"
            reason = "TradingAgents did not return Buy/Overweight; new paper entry is blocked."

        review = {
            "status": status,
            "rating": rating,
            "reason": reason,
            "model": model,
            "ticker": ticker,
            "version": tradingagents.__version__,
            "reports": {
                key: _safe_excerpt(final_state.get(key))
                for key in (
                    "market_report",
                    "sentiment_report",
                    "news_report",
                    "investment_plan",
                    "final_trade_decision",
                )
                if _safe_excerpt(final_state.get(key))
            } if isinstance(final_state, dict) else {},
        }
    except Exception as exc:
        # Avoid returning provider exception text, which can sometimes contain
        # request URLs or other sensitive configuration.
        review = {
            "status": "UNKNOWN",
            "reason": "TradingAgents review failed; no new paper entry is permitted.",
            "error_type": type(exc).__name__,
            "model": model,
            "ticker": ticker,
        }

    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS, copy.deepcopy(review))
        if len(_CACHE) > 32:
            oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
            _CACHE.pop(oldest, None)
    return review
