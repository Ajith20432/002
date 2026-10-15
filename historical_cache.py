"""Local OHLCV cache for reproducible historical replay/backtests.

Cache is deliberately file-based and dependency-light so it works in Termux. It never
changes live trading behaviour; callers explicitly opt in via ``use_cache=True``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd

DEFAULT_DIR = Path(os.getenv("ZORA_HISTORY_CACHE", "history_cache"))


def _safe(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_").replace(" ", "_")


def cache_path(symbol: str, timeframe: str, cache_dir: str | os.PathLike | None = None) -> Path:
    root = Path(cache_dir) if cache_dir else DEFAULT_DIR
    return root / f"{_safe(symbol)}__{timeframe}.csv"


def save(df: pd.DataFrame, symbol: str, timeframe: str, cache_dir=None) -> Path:
    if df is None or df.empty:
        raise ValueError("cannot cache empty OHLCV data")
    path = cache_path(symbol, timeframe, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df[["ts", "open", "high", "low", "close", "volume"]].copy()
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out.to_csv(path, index=False)
    meta = path.with_suffix(".json")
    meta.write_text(json.dumps({"symbol": symbol, "timeframe": timeframe,
                                "rows": len(out), "saved_at": time.time()}, indent=2))
    return path


def load(symbol: str, timeframe: str, cache_dir=None, min_rows: int = 1) -> pd.DataFrame | None:
    path = cache_path(symbol, timeframe, cache_dir)
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if len(df) < min_rows:
        return None
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def load_or_fetch(fetcher, symbol: str, timeframe: str, total: int, *, refresh: bool = False, cache_dir=None):
    if not refresh:
        cached = load(symbol, timeframe, cache_dir, min_rows=min(total, 60))
        if cached is not None and len(cached) >= total:
            return cached.tail(total).reset_index(drop=True)
    df = fetcher(symbol, timeframe, total)
    save(df, symbol, timeframe, cache_dir)
    return df.tail(total).reset_index(drop=True)
