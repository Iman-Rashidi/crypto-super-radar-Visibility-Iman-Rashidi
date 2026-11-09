"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from typing import List

from .engine import ConfirmationEngine
from .models import AssetSpec
from .reporting import print_report, results_dataframe
from .selftest import run_automated_tests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-super-radar",
        description="CRYPTO SUPER RADAR — Persian-first, zero-cost public-data quant scanner",
    )
    parser.add_argument("--asset", action="append", default=[], metavar="MARKET:IDENTIFIER",
                        help="Repeatable: solana:MINT, base:0xTOKEN, eth:0xTOKEN, cex:BTCUSDT, perp:BTCUSDT")
    parser.add_argument("--headline", action="append", default=[],
                        help="Headline text for local sentiment; repeat for multiple headlines")
    parser.add_argument("--output", default="", help="Optional CSV output path")
    parser.add_argument("--open-portfolio-risk-pct", type=float, default=0.0,
                        help="User-supplied current portfolio risk as percentage of equity")
    parser.add_argument("--test", action="store_true", help="Run offline automated tests")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.test:
        return 0 if run_automated_tests() else 1
    if not args.asset:
        build_parser().print_help()
        print("\nنمونه Spot: python -m crypto_super_radar --asset cex:BTCUSDT --headline 'Bitcoin adoption increases'")
        print("نمونه Perpetual: python -m crypto_super_radar --asset perp:BTCUSDT --headline 'Bitcoin institutional adoption increases'")
        return 2
    try:
        assets = [AssetSpec.parse(item) for item in args.asset]
    except ValueError as exc:
        print(f"خطای ورودی: {exc}", file=sys.stderr)
        return 2
    engine = ConfirmationEngine()
    results = [engine.analyze(asset, args.headline, args.open_portfolio_risk_pct) for asset in assets]
    frame = results_dataframe(results)
    print_report(frame)
    if args.output:
        frame.drop(columns=["gates"], errors="ignore").to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"\nگزارش CSV ذخیره شد: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
