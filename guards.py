"""
guards.py — pre-trade sanity gates. Every guard is fail-safe: when the data needed to judge a
condition is missing or unreadable, it says NO to opening a NEW position. Guards never block
closing or managing a position that is already open.
"""
from __future__ import annotations

import math
import time

import pandas as pd

from config import CFG
from metrics import profit_factor

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_seconds(tf: str) -> int:
    """'15m' -> 900. Raises ValueError for anything that is not <positive int><m|h|d|w>."""
    if not isinstance(tf, str) or len(tf) < 2 or tf[-1] not in _UNIT_SECONDS or not tf[:-1].isdigit():
        raise ValueError(f"unrecognised timeframe {tf!r}")
    n = int(tf[:-1])
    if n <= 0:
        raise ValueError(f"unrecognised timeframe {tf!r}")
    return n * _UNIT_SECONDS[tf[-1]]


def is_stale(df: pd.DataFrame, timeframe: str, max_candles: float, now: float | None = None) -> tuple[bool, float]:
    """
    (stale, age_seconds). The newest candle's OPEN time may be at most `max_candles` periods old
    (2.5 by default = the forming candle plus grace for exchange/API lag). An empty frame is stale.
    """
    if df is None or len(df) == 0:
        return True, math.inf
    now = time.time() if now is None else now
    age = now - float(df["ts"].iloc[-1].timestamp())
    return age > max_candles * timeframe_seconds(timeframe), age


def market_quality(ticker: dict | None, max_spread_bps: float, min_quote_volume: float) -> tuple[bool, str]:
    """Spread + 24h quote-volume gate. Missing/crossed quotes or unknown volume => refuse."""
    if not ticker:
        return False, "no ticker data"
    bid, ask = ticker.get("bid"), ticker.get("ask")
    if not bid or not ask or bid <= 0 or ask <= 0 or bid > ask:
        return False, "no usable bid/ask"
    spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
    if spread_bps > max_spread_bps:
        return False, f"spread {spread_bps:.1f}bps > {max_spread_bps:.1f}bps"
    qv = ticker.get("quoteVolume")
    if qv is None:
        return False, "24h volume unknown"
    if qv < min_quote_volume:
        return False, f"24h volume {qv:,.0f} < {min_quote_volume:,.0f}"
    return True, f"spread {spread_bps:.1f}bps, 24h vol {qv:,.0f}"


def strategy_degraded(pnls: list[float], window: int, min_profit_factor: float) -> tuple[bool, float]:
    """
    (degraded, rolling_pf) over the last `window` closed trades. With fewer trades than the
    window there is not enough evidence to judge: (False, nan).
    """
    if window <= 0 or len(pnls) < window:
        return False, math.nan
    pf = profit_factor(pnls[-window:])
    return pf < min_profit_factor, pf


def rolling_correlation(a: pd.Series, b: pd.Series, window: int) -> float | None:
    """Pearson correlation of the last `window` percentage returns; None if not computable."""
    n = min(len(a), len(b))
    if n < window + 1 or window < 3:
        return None
    ra = a.reset_index(drop=True).iloc[-n:].pct_change().iloc[-window:]
    rb = b.reset_index(drop=True).iloc[-n:].pct_change().iloc[-window:]
    if ra.std() == 0 or rb.std() == 0 or ra.isna().all() or rb.isna().all():
        return None                                # a flat series has no defined correlation
    corr = ra.corr(rb)
    return None if corr is None or math.isnan(corr) else float(corr)


def correlation_regime(corr: float | None, break_threshold: float | None = None) -> str:
    """'unknown' | 'broken' | 'moderate' | 'high' — 'broken' means BTC no longer explains ETH."""
    if corr is None:
        return "unknown"
    threshold = CFG.correlation_break_threshold if break_threshold is None else break_threshold
    if corr < threshold:
        return "broken"
    return "high" if corr >= 0.7 else "moderate"
