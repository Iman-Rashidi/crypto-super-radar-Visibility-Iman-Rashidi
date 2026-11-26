"""Run the read-only BloFin Demo Challenge Hunter and print/save JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import requests

from crypto_super_radar.demo_challenge_hunter import (
    ACTIVITY_URL,
    scan_blofin_demo_challenges,
)

ALLOWED_HOSTS = {"blofin.com", "www.blofin.com", "v3.blofin.com"}


def fetch_text(url: str) -> str:
    current_url = url
    for _ in range(4):
        parsed = urlparse(current_url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("Only official HTTPS BloFin domains are allowed.")
        response = requests.get(
            current_url,
            headers={"User-Agent": "CRYPTO-SUPER-RADAR-DemoChallengeHunter/1.0"},
            timeout=(5, 20),
            allow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            if not location:
                raise ValueError("BloFin returned a redirect without a Location header.")
            from urllib.parse import urljoin
            current_url = urljoin(current_url, location)
            continue
        response.raise_for_status()
        return response.text
    raise ValueError("Too many redirects while fetching official BloFin pages.")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Optional JSON output path.")
    parser.add_argument("--max-pages", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 30:
        parser.error("--max-pages must be between 1 and 30")

    report = scan_blofin_demo_challenges(
        fetch_text,
        activity_url=ACTIVITY_URL,
        max_pages=args.max_pages,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not report["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
