"""SQLite journal for immutable closed-trade analytics."""
from __future__ import annotations
import json, os, sqlite3
from pathlib import Path

_COLUMNS=("trade_id","symbol","side","entry_price","exit_price","entry_time","exit_time","exit_reason","r_multiple","mfe_r","mae_r","entry_context_json","exit_context_json")

def log_closed_trade(trade: dict, db_path: str | os.PathLike | None = None) -> None:
    if not isinstance(trade, dict): raise TypeError("trade must be a dict")
    missing=[k for k in _COLUMNS if k not in trade]
    if missing: raise ValueError("missing trade fields: "+", ".join(missing))
    path=str(db_path or os.getenv("TRADE_JOURNAL_DB") or (Path(__file__).with_name("closed_trades.sqlite3")))
    values=[]
    for key in _COLUMNS:
        value=trade[key]
        if key.endswith("_context_json") and not isinstance(value,str): value=json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)
        values.append(value)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS closed_trades (
            trade_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
            entry_price REAL NOT NULL, exit_price REAL NOT NULL, entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL, exit_reason TEXT NOT NULL, r_multiple REAL NOT NULL,
            mfe_r REAL NOT NULL, mae_r REAL NOT NULL, entry_context_json TEXT NOT NULL,
            exit_context_json TEXT NOT NULL)""")
        placeholders=",".join("?" for _ in _COLUMNS)
        conn.execute(f"INSERT OR REPLACE INTO closed_trades ({','.join(_COLUMNS)}) VALUES ({placeholders})",values)
