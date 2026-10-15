"""
indicators.py — technical indicators computed with pandas (no TA-Lib dependency,
so it installs cleanly inside Termux).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI, with the zero-loss edge case handled explicitly.

    A naive avg_gain/avg_loss division sends a pure uptrend (avg_loss ==
    exactly 0) to a divide-by-zero -> NaN -> filled with a neutral 50, which
    is backwards: zero losses at all means maximally overbought, i.e. 100,
    not neutral. Only a truly flat market (avg_gain AND avg_loss both zero)
    or the undefined first row should read as neutral.
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))

    result = result.where(avg_loss != 0, other=100.0)                          # no losses at all -> 100
    result = result.where(~((avg_gain == 0) & (avg_loss == 0)), other=50.0)    # no movement at all -> 50
    return result.fillna(50.0)                                                 # undefined first row -> 50


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(series: pd.Series, period: int = 20, mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = mid + mult * std
    lower = mid - mult * std
    return upper, mid, lower


def volume_spike(volume: pd.Series, period: int = 20) -> pd.Series:
    avg = volume.rolling(period).mean()
    return volume / avg.replace(0, np.nan)


def trend_direction(series: pd.Series, fast: int = 9, slow: int = 21) -> str:
    """Simple EMA-cross trend read for one timeframe."""
    f, s = ema(series, fast), ema(series, slow)
    if f.iloc[-1] > s.iloc[-1] and f.iloc[-2] <= s.iloc[-2]:
        return "bullish_cross"
    if f.iloc[-1] < s.iloc[-1] and f.iloc[-2] >= s.iloc[-2]:
        return "bearish_cross"
    return "bullish" if f.iloc[-1] > s.iloc[-1] else "bearish"


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicators as columns to a candle DataFrame."""
    out = df.copy()
    out["ema9"] = ema(out["close"], 9)
    out["ema21"] = ema(out["close"], 21)
    out["ema50"] = ema(out["close"], 50)
    out["rsi14"] = rsi(out["close"], 14)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(out["close"])
    out["atr14"] = atr(out, 14)
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = bollinger(out["close"])
    out["vol_spike"] = volume_spike(out["volume"])
    return out
