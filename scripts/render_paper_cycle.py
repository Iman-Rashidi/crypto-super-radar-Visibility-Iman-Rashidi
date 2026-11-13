"""Fail-closed entry point for scheduled Render paper-trading cycles.

Render Cron jobs have an ephemeral filesystem. Refuse to run a scheduled paper
cycle unless the shared Turso database is configured, so each invocation cannot
silently create a fresh local paper account.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crypto_super_radar.database import storage_backend
from crypto_super_radar.integrated_engine import WhaleAwareConfirmationEngine
from crypto_super_radar.paper_trading import PAPER_ASSETS, PaperTradingBot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one durable Crypto Super Radar paper-trading cycle."
    )
    parser.add_argument(
        "--asset",
        default="cex:BTCUSDT",
        choices=sorted(f"cex:{symbol}" for symbol in PAPER_ASSETS),
        help="Supported public CEX Spot market. Paper-only; no orders are submitted.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        backend = storage_backend()
    except Exception as exc:
        parser.error(
            "Durable storage is misconfigured. Set both TURSO_DATABASE_URL and "
            f"TURSO_AUTH_TOKEN on the Render Cron service. Details: {type(exc).__name__}"
        )

    if backend != "TURSO_CLOUD_SQLITE":
        parser.error(
            "Scheduled paper trading is blocked: Render Cron has no persistent "
            "local disk. Configure TURSO_DATABASE_URL and TURSO_AUTH_TOKEN first."
        )

    bot = None
    try:
        engine = WhaleAwareConfirmationEngine()
        bot = PaperTradingBot(engine)
        result = bot.run_once(args.asset)
        print(
            json.dumps(
                {
                    "service": "crypto-super-radar-paper-cycle",
                    "mode": "PAPER_ONLY",
                    "storage": backend,
                    "orders_submitted": False,
                    "transactions_signed": False,
                    "result": result,
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
        return 0
    finally:
        if bot is not None:
            bot.ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
