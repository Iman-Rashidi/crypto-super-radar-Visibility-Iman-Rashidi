"""Integrated CLI: market data + whale flows + full sentiment coverage."""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from typing import List, Optional

from .integrated_engine import WhaleAwareConfirmationEngine
from .models import AssetSpec
from .reporting import print_report, results_dataframe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-super-radar",
        description="CRYPTO SUPER RADAR — whale, market and sentiment intelligence",
    )
    parser.add_argument("--asset", action="append", default=[], metavar="MARKET:IDENTIFIER",
                        help="Repeatable: solana:MINT, base:0xTOKEN, eth:0xTOKEN, cex:BTCUSDT, perp:BTCUSDT")
    parser.add_argument("--headline", action="append", default=[],
                        help="Additional news headline for local sentiment scoring")
    parser.add_argument("--social-post", action="append", default=[],
                        help="Optional social post text; does not replace public social RSS coverage")
    parser.add_argument("--news-rss", action="append", default=None,
                        help="Public news RSS URL; repeat to add feeds")
    parser.add_argument("--social-rss", action="append", default=None,
                        help="Public social/community RSS URL; repeat to add feeds")
    parser.add_argument("--output", default="", help="Optional CSV output path")
    parser.add_argument("--open-portfolio-risk-pct", type=float, default=0.0,
                        help="User-supplied current portfolio risk as percentage of equity")
    parser.add_argument("--test", action="store_true", help="Run offline unit tests")
    return parser


def _run_tests() -> bool:
    tests_dir = Path(__file__).resolve().parents[1] / "tests"
    if tests_dir.exists():
        suite = unittest.defaultTestLoader.discover(str(tests_dir), pattern="test*.py")
        return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()
    from .selftest import run_automated_tests
    return run_automated_tests()


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.test:
        return 0 if _run_tests() else 1
    if not args.asset:
        build_parser().print_help()
        print("\nنمونه Spot: python -m crypto_super_radar --asset cex:BTCUSDT")
        print("نمونه DEX: python -m crypto_super_radar --asset base:0x...")
        return 2
    try:
        assets = [AssetSpec.parse(item) for item in args.asset]
    except ValueError as exc:
        print(f"خطای ورودی: {exc}", file=sys.stderr)
        return 2
    engine = WhaleAwareConfirmationEngine(
        news_feed_urls=args.news_rss,
        social_feed_urls=args.social_rss,
    )
    results = [
        engine.analyze(
            asset,
            headlines=args.headline,
            open_portfolio_risk_pct=args.open_portfolio_risk_pct,
            social_posts=args.social_post,
        )
        for asset in assets
    ]
    frame = results_dataframe(results)
    print_report(frame)
    if args.output:
        frame.drop(columns=["gates"], errors="ignore").to_csv(
            args.output, index=False, encoding="utf-8-sig"
        )
        print(f"\nگزارش CSV ذخیره شد: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
