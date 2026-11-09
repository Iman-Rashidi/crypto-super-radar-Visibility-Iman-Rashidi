"""High-level public news, social-RSS and Fear & Greed sentiment pipeline."""
from __future__ import annotations

from typing import Optional, Sequence

import requests

from .ai_gateway import enrich_public_headlines
from .rate_limiter import SharedRateLimiter
from .sentiment import analyze_market_sentiment, fear_greed_to_sentiment
from .sentiment_feeds import fetch_fear_greed_index, fetch_rss_headlines


def fetch_market_sentiment(
    news_feed_urls: Sequence[str],
    *,
    social_feed_urls: Sequence[str] = (),
    social_text: Optional[Sequence[str]] = None,
    session: Optional[requests.Session] = None,
    cache: Optional[dict] = None,
    limiter: Optional[SharedRateLimiter] = None,
) -> dict:
    """Combine free news RSS, optional social RSS/text and Fear & Greed.

    The social source is mandatory for a KNOWN full-coverage result. If no
    social feed/text is supplied, the overall status is PARTIAL and the
    fail-closed decision engine must not treat the sentiment gate as PASS.

    Optional AI enrichment summarizes public news headlines only. It does
    not contribute to the numeric sentiment score or any mandatory gate.
    """
    rate_limiter = limiter or SharedRateLimiter()
    news = fetch_rss_headlines(news_feed_urls, session=session, limiter=rate_limiter)
    social = fetch_rss_headlines(social_feed_urls, session=session, limiter=rate_limiter) if social_feed_urls else {
        "status": "UNKNOWN", "headlines": [], "feed_count": 0,
        "failed_feeds": [], "coverage": "UNKNOWN",
    }
    fear_greed = fetch_fear_greed_index(session=session, cache=cache, limiter=rate_limiter)
    news_items = [
        " ".join(part for part in (item.get("title", ""), item.get("summary", "")) if part)
        for item in news.get("headlines", [])
    ]
    social_text_items = [
        " ".join(str(item).split())
        for item in (social_text or [])
        if isinstance(item, str) and item.strip()
    ]
    social_items = social_text_items + [
        " ".join(part for part in (item.get("title", ""), item.get("summary", "")) if part)
        for item in social.get("headlines", [])
    ]
    numeric = {}
    if fear_greed.get("status") == "KNOWN":
        numeric["fear_greed"] = fear_greed_to_sentiment(fear_greed["value"])
    scored = analyze_market_sentiment(
        {"news": news_items, "social": social_items},
        numeric_scores=numeric,
    )
    news_complete = news.get("status") == "KNOWN" and news.get("coverage") == "KNOWN"
    social_feed_complete = social.get("status") == "KNOWN" and social.get("coverage") == "KNOWN"
    social_text_complete = bool(social_text_items)
    core_coverage = [
        news_complete,
        social_feed_complete or social_text_complete,
        fear_greed.get("status") == "KNOWN",
    ]
    if all(core_coverage):
        status = "KNOWN"
    elif any(core_coverage):
        status = "PARTIAL"
    else:
        status = "UNKNOWN"
    scored.update({
        "status": status,
        "news": news,
        "social": social,
        "fear_greed": fear_greed,
        "social_coverage": "KNOWN" if core_coverage[1] else ("PARTIAL" if social.get("status") == "PARTIAL" else "UNKNOWN"),
        "social_text_count": len(social_text_items),
        "news_coverage": news.get("coverage", "UNKNOWN"),
        "coverage_note": "News, social and Fear & Greed are separate evidence sources. Missing social coverage is PARTIAL, not silently treated as neutral.",
        "ai_enrichment": enrich_public_headlines(news.get("headlines", []), session=session, cache=cache),
    })
    return scored
