"""Free public news RSS and crypto Fear & Greed data adapters."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any, Optional, Sequence

import requests

from .rate_limiter import SharedRateLimiter

FEAR_GREED_URL = "https://api.alternative.me/fng/"


def fetch_fear_greed_index(
    *,
    session: Optional[requests.Session] = None,
    timeout: float = 12.0,
    cache: Optional[dict] = None,
    cache_ttl_seconds: int = 3600,
    now: Optional[float] = None,
    limiter: Optional[SharedRateLimiter] = None,
) -> dict[str, Any]:
    """Fetch latest two daily observations; cache to avoid repeated requests."""
    cache = cache if cache is not None else {}
    now = time.time() if now is None else now
    cached = cache.get("alternative_me_fng")
    if cached and now - cached[0] <= cache_ttl_seconds:
        return cached[1]
    client = session or requests.Session()
    (limiter or SharedRateLimiter()).wait(FEAR_GREED_URL)
    try:
        response = client.get(FEAR_GREED_URL, params={"limit": 2}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        values = payload.get("data")
        if not isinstance(values, list) or not values:
            raise ValueError("Fear & Greed response contains no observations")
        current = values[0]
        value = float(current["value"])
        timestamp = int(current["timestamp"])
        if not 0 <= value <= 100 or timestamp <= 0:
            raise ValueError("Fear & Greed response contains invalid value or timestamp")
        previous = None
        if len(values) > 1:
            try:
                previous = float(values[1]["value"])
            except (KeyError, TypeError, ValueError):
                previous = None
        result = {
            "status": "KNOWN", "source": "Alternative.me",
            "value": value, "classification": current.get("value_classification"),
            "timestamp": timestamp, "previous_value": previous,
            "attribution": "Data from alternative.me",
            "scope": "Broad Bitcoin/large-crypto sentiment index; not asset-specific.",
        }
        if len(cache) >= 32:
            cache.pop(next(iter(cache)))
        cache["alternative_me_fng"] = (now, result)
        return result
    except Exception as exc:
        return {"status": "UNKNOWN", "source": "Alternative.me",
                "reason": f"Fear & Greed fetch failed: {type(exc).__name__}"}


def fetch_rss_headlines(
    feed_urls: Sequence[str],
    *,
    session: Optional[requests.Session] = None,
    timeout: float = 12.0,
    max_feeds: int = 5,
    max_items_per_feed: int = 30,
    limiter: Optional[SharedRateLimiter] = None,
) -> dict[str, Any]:
    """Fetch bounded RSS/Atom headlines without a paid news API key."""
    client = session or requests.Session()
    headlines: list[dict[str, str]] = []
    failures: list[str] = []
    empty_feeds: list[str] = []
    successful_feeds = 0
    unique = list(dict.fromkeys(feed_urls))[:max_feeds]
    for url in unique:
        try:
            (limiter or SharedRateLimiter()).wait(url)
            response = client.get(url, timeout=timeout, headers={"User-Agent": "CRYPTO-SUPER-RADAR/0.2"})
            response.raise_for_status()
            root = ET.fromstring(response.content)
            items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
            feed_headline_count = 0
            for item in items[:max_items_per_feed]:
                fields = {}
                for child in list(item):
                    key = child.tag.rsplit("}", 1)[-1].lower()
                    if key in {"title", "description", "summary", "published", "updated"}:
                        fields[key] = " ".join("".join(child.itertext()).split())
                title = fields.get("title", "")
                if title:
                    feed_headline_count += 1
                    headlines.append({
                        "title": title,
                        "summary": fields.get("description", fields.get("summary", "")),
                        "published": fields.get("published", fields.get("updated", "")),
                        "source_url": url,
                    })
            successful_feeds += 1
            if feed_headline_count == 0:
                empty_feeds.append(url)
        except Exception as exc:
            failures.append(f"{url}: {type(exc).__name__}")
    deduped = []
    seen = set()
    for item in headlines:
        key = item["title"].casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    coverage = (
        "KNOWN" if deduped and not failures and not empty_feeds
        else "PARTIAL" if deduped
        else "UNKNOWN"
    )
    return {
        "status": coverage,
        "headlines": deduped,
        "feed_count": len(unique),
        "successful_feed_count": successful_feeds,
        "empty_feeds": empty_feeds,
        "failed_feeds": failures,
        "coverage": coverage,
    }
