"""High-level public sentiment fetch and normalization pipeline."""
from __future__ import annotations

from typing import Optional, Sequence

import requests

from .sentiment import analyze_market_sentiment, fear_greed_to_sentiment
from .sentiment_feeds import fetch_fear_greed_index, fetch_rss_headlines


def fetch_market_sentiment(
    feed_urls: Sequence[str],
    *,
    social_text: Optional[Sequence[str]] = None,
    session: Optional[requests.Session] = None,
    cache: Optional[dict] = None,
) -> dict:
    """Combine free RSS news and Fear & Greed; social posts are explicit inputs."""
    rss = fetch_rss_headlines(feed_urls, session=session)
    fear_greed = fetch_fear_greed_index(session=session, cache=cache)
    news_text = [
        " ".join(part for part in (item.get("title", ""), item.get("summary", "")) if part)
        for item in rss.get("headlines", [])
    ]
    numeric = {}
    if fear_greed.get("status") == "KNOWN":
        numeric["fear_greed"] = fear_greed_to_sentiment(fear_greed["value"])
    sources = {
        "news": news_text,
        "social": list(social_text or []),
    }
    scored = analyze_market_sentiment(sources, numeric_scores=numeric)
    core_coverage = [rss.get("status") == "KNOWN", fear_greed.get("status") == "KNOWN"]
    if all(core_coverage):
        status = "KNOWN"
    elif any(core_coverage):
        status = "PARTIAL"
    else:
        status = "UNKNOWN"
    scored.update({
        "status": status,
        "rss": rss,
        "fear_greed": fear_greed,
        "social_coverage": "KNOWN" if social_text else "UNKNOWN",
        "coverage_note": "News RSS and Fear & Greed are public baseline sources; social coverage is not silently inferred when no posts are supplied.",
    })
    return scored
