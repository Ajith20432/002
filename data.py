"""
data.py — market data access via ccxt
"""
from __future__ import annotations

import time

import ccxt
import pandas as pd

from config import CFG
from exchange_manager import MANAGER


def get_exchange(exchange_id: str | None = None, authenticated: bool = False) -> ccxt.Exchange:
    """Return a configured ccxt exchange. Supports Binance, Bybit, OKX and HTX."""
    return MANAGER.get(exchange_id or CFG.exchange_id, authenticated=authenticated)


def _clean_ohlcv(raw: list, symbol: str, timeframe: str, min_rows: int) -> pd.DataFrame:
    """Validate and normalise raw ccxt candles. Raises ValueError on anything untradeable."""
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    if df.empty:
        raise ValueError(f"Exchange returned no candles for {symbol} {timeframe}")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    numeric = ["open", "high", "low", "close", "volume"]
    df[numeric] = df[numeric].apply(pd.to_numeric, errors="coerce")
    if df[numeric].isna().any().any():
        raise ValueError(f"Exchange returned invalid OHLCV values for {symbol} {timeframe}")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError(f"Exchange returned non-positive prices for {symbol} {timeframe}")
    if not df["ts"].is_monotonic_increasing:
        df = df.sort_values("ts").reset_index(drop=True)
    if df["ts"].duplicated().any():
        df = df.drop_duplicates("ts", keep="last").reset_index(drop=True)
    if len(df) < min_rows:
        raise ValueError(f"Insufficient OHLCV history for {symbol} {timeframe}: {len(df)} candles")
    return df


def fetch_ohlcv_df(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 300, retries: int = 3
) -> pd.DataFrame:
    """Fetch OHLCV candles as a pandas DataFrame, with basic retry."""
    last_err = None
    for attempt in range(retries):
        try:
            raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            return _clean_ohlcv(raw, symbol, timeframe, min(limit, 60))
        except Exception as e:  # network hiccups, rate limits, etc.
            last_err = e
            if attempt < retries - 1:  # no point sleeping after the final attempt
                time.sleep(2)
    raise RuntimeError(f"Failed to fetch OHLCV for {symbol} {timeframe}: {last_err}")


def fetch_ohlcv_history(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, total: int, page: int = 500, retries: int = 3,
) -> pd.DataFrame:
    """
    Fetch `total` candles by paging forward with `since`. A single fetch_ohlcv call is capped by the
    exchange (often 500-1000 candles), so `paper-validate`'s 2000-candle request was silently
    truncated and "validated" far less history than it claimed.
    """
    from guards import timeframe_seconds
    period_ms = timeframe_seconds(timeframe) * 1000
    now_ms = int(time.time() * 1000)
    since = now_ms - total * period_ms
    rows: list = []
    for _ in range(max(2, total // max(1, page // 2) + 4)):  # hard cap on pages: never loop forever
        batch = None
        for attempt in range(retries):
            try:
                batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=page)
                break
            except Exception as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"Failed to fetch OHLCV history for {symbol} {timeframe}: {e}") from e
                time.sleep(2)
        if not batch:
            break
        rows.extend(batch)
        next_since = int(batch[-1][0]) + period_ms
        if next_since <= since or next_since >= now_ms:
            break
        since = next_since
    return _clean_ohlcv(rows, symbol, timeframe, min(total, 60)).tail(total).reset_index(drop=True)


def fetch_multi_timeframe(
    exchange: ccxt.Exchange, symbol: str, timeframes: tuple[str, ...]
) -> dict[str, pd.DataFrame]:
    """Return {timeframe: DataFrame} for every requested timeframe."""
    return {tf: fetch_ohlcv_df(exchange, symbol, tf) for tf in timeframes}
