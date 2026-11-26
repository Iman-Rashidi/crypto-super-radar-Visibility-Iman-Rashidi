"""Read-only discovery of BloFin demo-trading challenges.

This module never authenticates, registers for campaigns, places orders, or handles
exchange credentials. It deliberately classifies uncertain campaigns as UNKNOWN.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from typing import Callable
from urllib.parse import urljoin, urlparse

ACTIVITY_URL = "https://blofin.com/activity/en"
_ALLOWED_HOSTS = {"blofin.com", "www.blofin.com", "v3.blofin.com"}
_DEMO_TERMS = (
    "demo trading", "demo competition", "demo account", "virtual account",
    "simulated trading", "demo futures",
)
_LIVE_TERMS = (
    "live futures trade", "real futures trade", "real funds", "live trading",
    "deposit", "net deposit", "taker volume", "first deposit",
)
_REWARD_TERMS = ("reward", "prize", "funding", "voucher", "usdt", "competition", "challenge")


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._anchor_depth = 0
        self._anchor_href = ""
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._anchor_depth += 1
            if self._anchor_depth == 1:
                self._anchor_href = dict(attrs).get("href") or ""
                self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_depth:
            self._anchor_depth -= 1
            if self._anchor_depth == 0:
                label = " ".join(" ".join(self._anchor_text).split())
                if self._anchor_href:
                    self.links.append((label, self._anchor_href))
                self._anchor_href = ""
                self._anchor_text = []

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if cleaned:
            self.text_parts.append(cleaned)
            if self._anchor_depth:
                self._anchor_text.append(cleaned)

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)


@dataclass(frozen=True)
class DemoChallenge:
    title: str
    url: str
    source_url: str
    demo_evidence: bool
    live_requirement_evidence: bool
    reward_evidence: bool
    demo_only_status: str
    campaign_status: str
    eligibility_note: str


def _safe_blofin_url(base: str, href: str) -> str | None:
    absolute = urljoin(base, href)
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        return None
    return absolute


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = re.sub(r"\s+", " ", text.lower())
    return any(term in lowered for term in terms)


def _campaign_status(text: str, now: datetime) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ("ended", "campaign has ended", "registration closed")):
        return "ENDED"
    if any(term in lowered for term in ("ongoing", "registration open", "register now", "join now")):
        return "POSSIBLY_ACTIVE"
    # Do not infer activity from incomplete or ambiguous date fragments.
    return "UNKNOWN"


def classify_campaign(
    title: str,
    url: str,
    source_url: str,
    detail_text: str,
    now: datetime | None = None,
) -> DemoChallenge:
    now = now or datetime.now(timezone.utc)
    evidence = f"{title} {detail_text}"
    has_demo = _contains_any(evidence, _DEMO_TERMS)
    has_live = _contains_any(evidence, _LIVE_TERMS)
    has_reward = _contains_any(evidence, _REWARD_TERMS)

    if has_demo and has_live:
        demo_only = "NO_LIVE_REQUIREMENTS"
        note = "Demo task exists, but the campaign also mentions deposit/live-trading requirements."
    elif has_demo:
        demo_only = "POTENTIALLY_DEMO_ONLY"
        note = "Demo evidence found; full terms and regional eligibility still require verification."
    else:
        demo_only = "NOT_DEMO_CONFIRMED"
        note = "No explicit Demo-trading requirement was found in the retrieved text."

    return DemoChallenge(
        title=title.strip() or "Untitled BloFin campaign",
        url=url,
        source_url=source_url,
        demo_evidence=has_demo,
        live_requirement_evidence=has_live,
        reward_evidence=has_reward,
        demo_only_status=demo_only,
        campaign_status=_campaign_status(detail_text, now),
        eligibility_note=note,
    )


def scan_blofin_demo_challenges(
    fetch_text: Callable[[str], str],
    activity_url: str = ACTIVITY_URL,
    now: datetime | None = None,
    max_pages: int = 30,
) -> dict:
    """Scan official BloFin activity links and return an auditable JSON-safe report."""
    source = _safe_blofin_url(ACTIVITY_URL, activity_url)
    if source is None:
        raise ValueError("Only official HTTPS BloFin activity URLs are allowed.")

    activity_html = fetch_text(source)
    activity = _PageText()
    activity.feed(activity_html)
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, href in activity.links:
        target = _safe_blofin_url(source, href)
        searchable = f"{label} {href}".lower()
        if not target or target in seen:
            continue
        if not any(term in searchable for term in ("demo", "competition", "challenge")):
            continue
        seen.add(target)
        candidates.append((label or "BloFin Demo/competition campaign", target))
        if len(candidates) >= max_pages:
            break

    results: list[DemoChallenge] = []
    errors: list[dict] = []
    for title, target in candidates:
        try:
            page_html = fetch_text(target)
            page = _PageText()
            page.feed(page_html)
            results.append(classify_campaign(title, target, source, page.text, now))
        except Exception as exc:  # noqa: BLE001 - preserve partial scan with typed errors
            errors.append({"url": target, "error_type": type(exc).__name__})

    results.sort(key=lambda item: (
        item.campaign_status != "POSSIBLY_ACTIVE",
        item.demo_only_status != "POTENTIALLY_DEMO_ONLY",
        item.title.lower(),
    ))
    return {
        "schema_version": "1.0",
        "source": source,
        "scanned_at_utc": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "mode": "READ_ONLY_PUBLIC_DATA",
        "orders_submitted": False,
        "registration_submitted": False,
        "results": [asdict(item) for item in results],
        "errors": errors,
        "limitations": [
            "A campaign is not confirmed active from a date fragment alone.",
            "POTENTIALLY_DEMO_ONLY is not an eligibility guarantee; full terms and region rules must be checked.",
            "No login, account data, API key, registration, deposit, or trade is used.",
            "BloFin may render or change campaign pages dynamically; missing text remains UNKNOWN.",
        ],
    }
