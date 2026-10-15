"""
patterns.py — classic candlestick and swing-based chart pattern detection.

Two families of patterns, both computed from the same OHLCV data already
fetched for the technical experts — no extra API calls:

1. Candlestick patterns (1-3 candles): Doji, Hammer, Hanging Man, Shooting
   Star, Bullish/Bearish Engulfing, Morning Star, Evening Star. Read
   directly off OHLC relationships on the most recent candle(s).
2. Swing-based chart patterns: Double Top / Double Bottom, Head & Shoulders
   (regular and inverse), and a support/resistance breakout — all built on
   a simple local-extrema swing-point detector. Triangles, wedges, and
   flags are deliberately out of scope: reliably detecting them needs
   trendline fitting, which is a lot more code for patterns that are also
   the most subjective to begin with; this is a documented scope line, not
   a silent gap.

This becomes the "pattern" factor inside strategy.py's per-timeframe
scoring (see score_timeframe()), weighted and learned by brain.py exactly
like trend/momentum/rsi/meanrev/volume — a detected pattern is one more
vote, never a trade signal by itself, and risk.py is completely unaware
this module exists.

Chart patterns are famously subjective — two chartists can look at the same
candles and disagree on whether it's a head and shoulders. What's here uses
concrete, documented tolerances (the constants below) rather than
"eyeballing" it, so the same data always produces the same read — but that
also means it will miss fuzzier real-world versions of these patterns that
a human eye would still call valid, and will occasionally flag a shape that
a human wouldn't. Treat pattern names in the logs as "the detector's
opinion", not ground truth.
"""
from __future__ import annotations

import pandas as pd

# ---- tolerances (documented starting points, not tuned on live data) ----
SWING_WINDOW = 3            # candles required on each side for a local high/low to count as a swing point
LEVEL_TOLERANCE_PCT = 1.5   # how close two swing highs/lows must be (%) to call them "the same level"
NECKLINE_TOLERANCE_PCT = 3.0  # wider tolerance for head & shoulders' two shoulders
DOJI_BODY_RATIO = 0.1       # candle body must be under this fraction of its total range to count as a doji
WICK_BODY_RATIO = 2.0       # a hammer/shooting-star wick must be at least this many times the body
BREAKOUT_VOLUME_MULT = 1.3  # breakout volume must exceed this multiple of the recent average to be "confirmed"


def _body(row: pd.Series) -> float:
    return abs(row["close"] - row["open"])


def _range(row: pd.Series) -> float:
    return max(row["high"] - row["low"], 1e-9)


def _is_bullish(row: pd.Series) -> bool:
    return row["close"] > row["open"]


def _close_enough(a: float, b: float, tolerance_pct: float = LEVEL_TOLERANCE_PCT) -> bool:
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base * 100 <= tolerance_pct


# ---------------- candlestick patterns ----------------
def _detect_candlestick(df: pd.DataFrame) -> tuple[float, list[str]]:
    if len(df) < 3:
        return 0.0, []
    c2, c1, c0 = df.iloc[-3], df.iloc[-2], df.iloc[-1]  # c0 = most recent candle
    score = 0.0
    notes: list[str] = []

    # Doji — indecision. Score-neutral on its own; it's a flag, not a direction.
    if _body(c0) / _range(c0) < DOJI_BODY_RATIO:
        notes.append("doji (indecision)")

    lower_wick = min(c0["open"], c0["close"]) - c0["low"]
    upper_wick = c0["high"] - max(c0["open"], c0["close"])

    # Hammer / hanging man — long lower wick, small body near the top of the range.
    # Same shape, opposite read depending on whether it follows a fall or a rise.
    if lower_wick > WICK_BODY_RATIO * _body(c0) and upper_wick < _body(c0):
        if not _is_bullish(c1):
            score += 20
            notes.append("hammer (possible bullish reversal)")
        else:
            score -= 15
            notes.append("hanging man (possible bearish reversal)")

    # Shooting star — long upper wick, small body near the bottom, after a rise.
    if upper_wick > WICK_BODY_RATIO * _body(c0) and lower_wick < _body(c0) and _is_bullish(c1):
        score -= 20
        notes.append("shooting star (possible bearish reversal)")

    # Engulfing — c0's real body fully engulfs c1's real body, in the opposite direction.
    if _is_bullish(c0) and not _is_bullish(c1) and c0["close"] > c1["open"] and c0["open"] < c1["close"]:
        score += 25
        notes.append("bullish engulfing")
    elif not _is_bullish(c0) and _is_bullish(c1) and c0["open"] > c1["close"] and c0["close"] < c1["open"]:
        score -= 25
        notes.append("bearish engulfing")

    # Morning / evening star — big move, small indecisive middle candle, big reversal move.
    if (not _is_bullish(c2) and _body(c2) / _range(c2) > 0.5
            and _body(c1) / _range(c1) < DOJI_BODY_RATIO * 2
            and _is_bullish(c0) and c0["close"] > (c2["open"] + c2["close"]) / 2):
        score += 25
        notes.append("morning star (bullish reversal)")
    elif (_is_bullish(c2) and _body(c2) / _range(c2) > 0.5
            and _body(c1) / _range(c1) < DOJI_BODY_RATIO * 2
            and not _is_bullish(c0) and c0["close"] < (c2["open"] + c2["close"]) / 2):
        score -= 25
        notes.append("evening star (bearish reversal)")

    return score, notes


# ---------------- swing-point detection ----------------
def find_swings(df: pd.DataFrame, window: int = SWING_WINDOW) -> tuple[list[int], list[int]]:
    """
    Local-extrema swing highs/lows: index i is a swing high if its high is
    the unique max within [i-window, i+window], and similarly for lows.
    Returns (swing_high_indices, swing_low_indices), oldest to newest.
    """
    highs, lows = df["high"], df["low"]
    n = len(df)
    swing_highs, swing_lows = [], []
    for i in range(window, n - window):
        h_window = highs.iloc[i - window: i + window + 1]
        if highs.iloc[i] == h_window.max() and (h_window == h_window.max()).sum() == 1:
            swing_highs.append(i)
        l_window = lows.iloc[i - window: i + window + 1]
        if lows.iloc[i] == l_window.min() and (l_window == l_window.min()).sum() == 1:
            swing_lows.append(i)
    return swing_highs, swing_lows


# ---------------- double top / double bottom ----------------
def _detect_double_top_bottom(
    df: pd.DataFrame, swing_highs: list[int], swing_lows: list[int],
) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    last_price = df["close"].iloc[-1]

    if len(swing_highs) >= 2:
        i1, i2 = swing_highs[-2], swing_highs[-1]
        h1, h2 = df["high"].iloc[i1], df["high"].iloc[i2]
        trough = df["low"].iloc[i1:i2 + 1].min() if i2 > i1 else None
        if trough is not None and _close_enough(h1, h2) and trough < min(h1, h2) * 0.98:
            if last_price < trough:
                score -= 30
                notes.append("double top (confirmed breakdown)")
            else:
                score -= 10
                notes.append("possible double top (unconfirmed)")

    if len(swing_lows) >= 2:
        i1, i2 = swing_lows[-2], swing_lows[-1]
        l1, l2 = df["low"].iloc[i1], df["low"].iloc[i2]
        peak = df["high"].iloc[i1:i2 + 1].max() if i2 > i1 else None
        if peak is not None and _close_enough(l1, l2) and peak > max(l1, l2) * 1.02:
            if last_price > peak:
                score += 30
                notes.append("double bottom (confirmed breakout)")
            else:
                score += 10
                notes.append("possible double bottom (unconfirmed)")

    return score, notes


# ---------------- head & shoulders (regular and inverse) ----------------
def _detect_head_and_shoulders(
    df: pd.DataFrame, swing_highs: list[int], swing_lows: list[int],
) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    last_price = df["close"].iloc[-1]

    if len(swing_highs) >= 3 and len(swing_lows) >= 2:
        ls, head, rs = swing_highs[-3], swing_highs[-2], swing_highs[-1]
        ls_h, head_h, rs_h = df["high"].iloc[ls], df["high"].iloc[head], df["high"].iloc[rs]
        neckline_points = [i for i in swing_lows if ls < i < rs]
        if (head_h > ls_h and head_h > rs_h
                and _close_enough(ls_h, rs_h, NECKLINE_TOLERANCE_PCT)
                and len(neckline_points) >= 2):
            neckline = df["low"].iloc[neckline_points].mean()
            if last_price < neckline:
                score -= 35
                notes.append("head and shoulders (confirmed neckline break)")
            else:
                score -= 10
                notes.append("possible head and shoulders (neckline not yet broken)")

    if len(swing_lows) >= 3 and len(swing_highs) >= 2:
        ls, head, rs = swing_lows[-3], swing_lows[-2], swing_lows[-1]
        ls_l, head_l, rs_l = df["low"].iloc[ls], df["low"].iloc[head], df["low"].iloc[rs]
        neckline_points = [i for i in swing_highs if ls < i < rs]
        if (head_l < ls_l and head_l < rs_l
                and _close_enough(ls_l, rs_l, NECKLINE_TOLERANCE_PCT)
                and len(neckline_points) >= 2):
            neckline = df["high"].iloc[neckline_points].mean()
            if last_price > neckline:
                score += 35
                notes.append("inverse head and shoulders (confirmed neckline break)")
            else:
                score += 10
                notes.append("possible inverse head and shoulders (neckline not yet broken)")

    return score, notes


# ---------------- support / resistance breakout ----------------
def _detect_support_resistance_breakout(
    df: pd.DataFrame, swing_highs: list[int], swing_lows: list[int],
) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    last_price = df["close"].iloc[-1]
    last_volume = df["volume"].iloc[-1]
    lookback = df["volume"].iloc[-20:] if len(df) >= 20 else df["volume"]
    avg_volume = lookback.mean()
    volume_confirmed = bool(avg_volume > 0 and last_volume > BREAKOUT_VOLUME_MULT * avg_volume)

    if swing_highs:
        resistance = df["high"].iloc[swing_highs[-3:]].max()
        if last_price > resistance:
            score += 20 if volume_confirmed else 10
            notes.append("resistance breakout" + (" (volume confirmed)" if volume_confirmed else ""))

    if swing_lows:
        support = df["low"].iloc[swing_lows[-3:]].min()
        if last_price < support:
            score -= 20 if volume_confirmed else 10
            notes.append("support breakdown" + (" (volume confirmed)" if volume_confirmed else ""))

    return score, notes


def detect_patterns(df: pd.DataFrame) -> tuple[float, list[str]]:
    """
    Runs every pattern detector against the given OHLCV DataFrame.

    Returns (score, notes):
      score — -100..100, the combined read across every pattern detected
              this call (candlestick + swing-based).
      notes — human-readable names of what was actually detected, for
              logging — e.g. "bullish engulfing", "double top (confirmed
              breakdown)". Empty list when nothing was detected.
    """
    min_len = 2 * SWING_WINDOW + 5
    if len(df) < min_len:
        return 0.0, []

    total_score = 0.0
    all_notes: list[str] = []

    candle_score, candle_notes = _detect_candlestick(df)
    total_score += candle_score
    all_notes += candle_notes

    swing_highs, swing_lows = find_swings(df)

    dt_score, dt_notes = _detect_double_top_bottom(df, swing_highs, swing_lows)
    total_score += dt_score
    all_notes += dt_notes

    hs_score, hs_notes = _detect_head_and_shoulders(df, swing_highs, swing_lows)
    total_score += hs_score
    all_notes += hs_notes

    sr_score, sr_notes = _detect_support_resistance_breakout(df, swing_highs, swing_lows)
    total_score += sr_score
    all_notes += sr_notes

    total_score = max(-100.0, min(100.0, total_score))
    return total_score, all_notes
