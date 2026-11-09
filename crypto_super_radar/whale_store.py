"""Local SQLite history for repeated whale-flow observations (no hosted DB)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


class WhaleStore:
    """Persist scan snapshots locally; Render free filesystems may reset on restart."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("CSR_DB_PATH", "data/crypto_super_radar.sqlite3")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS whale_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    exchange_net REAL,
                    wallet_net REAL,
                    cex_net REAL,
                    whale_bias TEXT,
                    coverage TEXT,
                    evidence_json TEXT NOT NULL
                )
            """)
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_whale_asset_time ON whale_observations(asset, observed_at)"
            )

    def record(self, asset: str, evidence: dict[str, Any], whale_bias: str = "UNKNOWN") -> int:
        exchange_net = evidence.get("net_exchange_flow_usd")
        wallet_net = evidence.get("net_whale_transfer_usd")
        cex_net = evidence.get("net_whale_trade_flow_usd")
        coverage = evidence.get("coverage")
        # Store only normalized summary evidence, not provider payloads or raw wallet data.
        summary = {
            "status": evidence.get("status", "UNKNOWN"),
            "reason": evidence.get("reason"),
            "interpretation": evidence.get("interpretation"),
            "event_count": evidence.get("event_count"),
            "whale_event_count": evidence.get("whale_event_count"),
            "large_trade_count": evidence.get("large_trade_count"),
            "coverage": coverage,
        }
        with self._lock, self._db:
            cursor = self._db.execute(
                """INSERT INTO whale_observations
                (asset, observed_at, status, exchange_net, wallet_net, cex_net,
                 whale_bias, coverage, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (asset, int(time.time()), str(evidence.get("status", "UNKNOWN")),
                 exchange_net, wallet_net, cex_net, whale_bias, coverage,
                 json.dumps(summary, ensure_ascii=False, default=str)),
            )
            return int(cursor.lastrowid)

    def history(self, asset: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM whale_observations WHERE asset=? ORDER BY observed_at DESC, id DESC LIMIT ?",
                (asset, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def summary(self, asset: str, limit: int = 10) -> dict[str, Any]:
        rows = self.history(asset, limit)
        known = [row for row in rows if row["status"] == "KNOWN"]
        biases = [row["whale_bias"] for row in known if row["whale_bias"] in {
            "ACCUMULATION_PRESSURE", "DISTRIBUTION_PRESSURE"
        }]
        recent_bias = "UNKNOWN"
        if biases:
            recent_bias = max(set(biases), key=biases.count)
        return {
            "observation_count": len(rows),
            "known_observation_count": len(known),
            "recent_bias": recent_bias,
            "latest_observation": rows[-1] if rows else None,
            "history": rows,
            "storage": "LOCAL_SQLITE",
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
