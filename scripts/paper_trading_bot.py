"""Run the free paper trading bot locally; no exchange orders are submitted."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crypto_super_radar.integrated_engine import WhaleAwareConfirmationEngine
from crypto_super_radar.paper_trading import PAPER_ASSETS, PaperTradingBot


def main() -> int:
    parser = argparse.ArgumentParser(description="CRYPTO SUPER RADAR paper trading bot")
    parser.add_argument(
        "--asset", default="cex:BTCUSDT",
        choices=sorted(f"cex:{symbol}" for symbol in PAPER_ASSETS),
        help="Supported public spot market (paper mode only).",
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=900,
        help="Seconds between read-only market scans; minimum 60.",
    )
    parser.add_argument("--once", action="store_true", help="Run one paper cycle and exit.")
    args = parser.parse_args()
    if args.interval_seconds < 60:
        parser.error("--interval-seconds must be at least 60 to respect public API limits.")

    engine = WhaleAwareConfirmationEngine()
    bot = PaperTradingBot(engine)
    try:
        while True:
            result = bot.run_once(args.asset)
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        print("Paper bot stopped. No exchange orders were submitted.", flush=True)
        return 0
    finally:
        bot.ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
