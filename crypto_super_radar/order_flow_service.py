"""CLI supervisor for persistent public Binance/Bybit Spot order-flow streams.

Run explicitly as a research-data worker:
    crypto-super-radar-orderflow --symbols BTCUSDT ETHUSDT SOLUSDT
or:
    python -m crypto_super_radar.order_flow_service --providers binance bybit

It places no orders. WebSocket health and CVD remain UNKNOWN/PARTIAL until
VenueOrderFlow warm-up and coverage gates are satisfied.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from typing import Any

from .database import connect_database, storage_backend
from .order_flow import VenueOrderFlow
from .order_flow_runtime import OrderFlowStreamWorker
from .order_flow_store import OrderFlowEventStore

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DEFAULT_PROVIDERS = ("binance", "bybit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-super-radar-orderflow",
        description="Persistent public Spot order-flow observations (no trading)",
    )
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--providers", nargs="+", choices=DEFAULT_PROVIDERS, default=list(DEFAULT_PROVIDERS))
    parser.add_argument("--window-seconds", type=int, default=300)
    parser.add_argument("--minimum-coverage-pct", type=float, default=80.0)
    parser.add_argument("--stale-after-seconds", type=int, default=15)
    parser.add_argument("--base-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=0,
                        help="0 means reconnect until SIGINT/SIGTERM")
    return parser


async def _run(args: argparse.Namespace) -> int:
    symbols = tuple(dict.fromkeys(str(s).strip().upper() for s in args.symbols))
    if not symbols or any(not symbol.isalnum() or not 4 <= len(symbol) <= 20 for symbol in symbols):
        raise ValueError("symbols must be normalized Spot symbols containing 4-20 alphanumeric characters")
    if args.window_seconds < 1 or args.stale_after_seconds < 1:
        raise ValueError("window and stale thresholds must be positive")
    if not 0 < args.minimum_coverage_pct <= 100:
        raise ValueError("minimum coverage must be greater than 0 and at most 100")
    if args.max_attempts < 0:
        raise ValueError("max attempts cannot be negative")

    connection = connect_database()
    store = OrderFlowEventStore(connection)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    workers: list[OrderFlowStreamWorker] = []
    trackers: list[VenueOrderFlow] = []
    for provider in args.providers:
        for symbol in symbols:
            venue_name = f"{provider}:{symbol}"
            tracker = VenueOrderFlow(
                venue_name,
                window_ms=args.window_seconds * 1000,
                min_coverage_pct=args.minimum_coverage_pct,
                stale_after_ms=args.stale_after_seconds * 1000,
            )
            trackers.append(tracker)
            workers.append(OrderFlowStreamWorker(
                provider=provider,
                symbol=symbol,
                tracker=tracker,
                event_store=store,
                base_backoff_seconds=args.base_backoff_seconds,
                max_backoff_seconds=args.max_backoff_seconds,
                max_attempts=args.max_attempts or None,
            ))

    print(json.dumps({
        "event": "orderflow_worker_start",
        "providers": list(args.providers),
        "symbols": list(symbols),
        "worker_count": len(workers),
        "storage": storage_backend(),
        "trade_authorization": "NO_TRADE",
    }, ensure_ascii=False), flush=True)

    async def stop_if_all_done(tasks: list[asyncio.Task]) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        if all(task.done() for task in tasks):
            stop_event.set()

    tasks = [asyncio.create_task(worker.run(stop_event)) for worker in workers]
    watcher = asyncio.create_task(stop_if_all_done(tasks))
    try:
        await stop_event.wait()
    finally:
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        report = []
        for tracker in trackers:
            snapshot = tracker.snapshot(int(time.time() * 1000))
            report.append({
                "venue": snapshot["venue"],
                "status": snapshot["status"],
                "coverage_pct": snapshot["coverage_pct"],
                "event_count": snapshot["event_count"],
                "cvd_base": snapshot["cvd_base"],
                "last_receive_age_ms": snapshot["last_receive_age_ms"],
            })
        print(json.dumps({
            "event": "orderflow_worker_stop",
            "feed_report": report,
            "trade_authorization": "NO_TRADE",
        }, ensure_ascii=False), flush=True)
        connection.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"orderflow worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
