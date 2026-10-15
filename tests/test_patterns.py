import numpy as np
import pandas as pd

from patterns import detect_patterns, find_swings

PAD = [(100, 100.2, 99.8, 100)] * 9  # neutral flat candles to clear the min-length warm-up


def _candles(rows, volume=100.0):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["volume"] = volume
    df["ts"] = pd.date_range("2026-01-01", periods=len(df), freq="1h")
    return df


def _path(*segments):
    """Concatenate linear ramps, dropping each segment's duplicate boundary point
    so a shared peak/trough value doesn't create a false plateau."""
    prices = list(np.linspace(*segments[0]))
    for start, end, n in segments[1:]:
        prices += list(np.linspace(start, end, n))[1:]
    return prices


def _swing_df(prices, volumes=None):
    prices = pd.Series(prices, dtype=float)
    n = len(prices)
    volumes = volumes if volumes is not None else [100.0] * n
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "open": prices, "close": prices,
        "high": prices + 0.1, "low": prices - 0.1,
        "volume": pd.Series(volumes, dtype=float),
    })


# ---------------- basic shape ----------------
def test_detect_patterns_returns_neutral_on_too_short_a_dataframe():
    short_df = _candles([(100, 100.2, 99.8, 100)] * 5)
    score, notes = detect_patterns(short_df)
    assert score == 0.0
    assert notes == []


def test_score_is_always_within_bounds():
    rows = PAD + [(100.5, 100.6, 99.4, 99.5), (99.4, 101.6, 99.3, 101.5)]
    score, _ = detect_patterns(_candles(rows))
    assert -100 <= score <= 100


# ---------------- candlestick patterns ----------------
def test_bullish_engulfing_scores_positive():
    rows = PAD + [(100.5, 100.6, 99.4, 99.5), (99.4, 101.6, 99.3, 101.5)]
    score, notes = detect_patterns(_candles(rows))
    assert score > 0
    assert "bullish engulfing" in notes


def test_bearish_engulfing_scores_negative():
    rows = PAD + [(99.5, 100.6, 99.4, 100.5), (100.6, 100.7, 98.4, 98.5)]
    score, notes = detect_patterns(_candles(rows))
    assert score < 0
    assert "bearish engulfing" in notes


def test_hammer_after_a_decline_scores_positive():
    rows = PAD + [(101, 101.1, 99, 100), (100, 100.3, 97, 100.2)]
    score, notes = detect_patterns(_candles(rows))
    assert score > 0
    assert any("hammer" in n for n in notes)


def test_hanging_man_after_a_rise_scores_negative():
    rows = PAD + [(99, 100.1, 98.9, 100), (100, 100.3, 97, 100.2)]
    score, notes = detect_patterns(_candles(rows))
    assert score < 0
    assert any("hanging man" in n for n in notes)


def test_shooting_star_after_a_rise_scores_negative():
    rows = PAD + [(99, 100.1, 98.9, 100), (100, 103, 99.9, 100.2)]
    score, notes = detect_patterns(_candles(rows))
    assert score < 0
    assert any("shooting star" in n for n in notes)


def test_morning_star_scores_positive():
    rows = PAD[:-1] + [
        (105, 105.2, 99.8, 100),      # big bearish candle
        (99.9, 100.3, 99.6, 100.0),   # tiny indecisive middle candle
        (100.1, 104, 100.0, 103.5),   # big bullish candle, closes above c2's midpoint
    ]
    score, notes = detect_patterns(_candles(rows))
    assert score > 0
    assert any("morning star" in n for n in notes)


def test_evening_star_scores_negative():
    rows = PAD[:-1] + [
        (99.8, 105.2, 99.6, 105),     # big bullish candle
        (105.0, 105.3, 104.7, 104.9),  # tiny indecisive middle candle
        (104.8, 104.9, 101.0, 101.5),  # big bearish candle, closes below c2's midpoint
    ]
    score, notes = detect_patterns(_candles(rows))
    assert score < 0
    assert any("evening star" in n for n in notes)


def test_a_lone_doji_is_noted_but_score_neutral():
    rows = PAD + [(100, 100.5, 99.5, 100), (100, 100.05, 99.95, 100.0)]
    score, notes = detect_patterns(_candles(rows))
    assert score == 0.0
    assert "doji (indecision)" in notes


# ---------------- swing-point detection ----------------
def test_find_swings_identifies_a_single_clean_peak_and_trough():
    prices = _path((100, 100, 5), (100, 130, 8), (130, 90, 8), (90, 110, 8))
    df = _swing_df(prices)
    swing_highs, swing_lows = find_swings(df)
    assert len(swing_highs) == 1
    assert len(swing_lows) == 1
    assert df["high"].iloc[swing_highs[0]] == df["high"].max()
    assert df["low"].iloc[swing_lows[0]] == df["low"].min()


def test_find_swings_ignores_a_flat_plateau():
    prices = [100.0] * 30
    df = _swing_df(prices)
    swing_highs, swing_lows = find_swings(df)
    assert swing_highs == []
    assert swing_lows == []


# ---------------- double top / double bottom ----------------
def test_double_top_confirmed_breakdown_scores_negative():
    prices = _path((100, 100, 5), (100, 130, 10), (130, 110, 10), (110, 130, 10), (130, 105, 10))
    score, notes = detect_patterns(_swing_df(prices))
    assert score < 0
    assert "double top (confirmed breakdown)" in notes


def test_double_bottom_confirmed_breakout_scores_positive():
    prices = _path((100, 100, 5), (100, 70, 10), (70, 90, 10), (90, 70, 10), (70, 95, 10))
    score, notes = detect_patterns(_swing_df(prices))
    assert score > 0
    assert "double bottom (confirmed breakout)" in notes


def test_two_similar_peaks_with_no_breakdown_yet_is_only_a_possible_double_top():
    # Same shape as the confirmed case, but the pullback after the second peak
    # stays well above the trough instead of breaking through it. The second
    # peak needs a few candles of *lookback room* after it too — find_swings
    # requires window candles on both sides to confirm a swing point at all,
    # so a peak sitting on the very last candle would never be recognized.
    prices = _path((100, 100, 5), (100, 130, 10), (130, 110, 10), (110, 130, 6), (130, 125, 5))
    score, notes = detect_patterns(_swing_df(prices))
    assert "possible double top (unconfirmed)" in notes
    assert "double top (confirmed breakdown)" not in notes


# ---------------- head & shoulders ----------------
def test_head_and_shoulders_confirmed_break_scores_negative():
    prices = _path(
        (100, 100, 5), (100, 120, 8), (120, 105, 8), (105, 140, 8),
        (140, 103, 8), (103, 122, 8), (122, 95, 10),
    )
    score, notes = detect_patterns(_swing_df(prices))
    assert score < 0
    assert "head and shoulders (confirmed neckline break)" in notes


def test_inverse_head_and_shoulders_confirmed_break_scores_positive():
    prices = _path(
        (100, 100, 5), (100, 80, 8), (80, 95, 8), (95, 60, 8),
        (60, 97, 8), (97, 78, 8), (78, 105, 10),
    )
    score, notes = detect_patterns(_swing_df(prices))
    assert score > 0
    assert "inverse head and shoulders (confirmed neckline break)" in notes


def test_uneven_shoulders_do_not_trigger_head_and_shoulders():
    # right shoulder far taller than left shoulder -> shoulders aren't "the same level"
    prices = _path(
        (100, 100, 5), (100, 110, 8), (110, 100, 8), (100, 140, 8),
        (140, 100, 8), (100, 135, 8), (135, 95, 10),
    )
    _, notes = detect_patterns(_swing_df(prices))
    assert not any("head and shoulders" in n for n in notes)


# ---------------- support / resistance breakout ----------------
def test_resistance_breakout_with_volume_spike_scores_more_than_without():
    prices = _path((100, 100, 5), (100, 120, 8), (120, 110, 8), (110, 125, 10))
    n = len(prices)

    score_normal, notes_normal = detect_patterns(_swing_df(prices, volumes=[100.0] * n))
    score_spike, notes_spike = detect_patterns(
        _swing_df(prices, volumes=[100.0] * (n - 1) + [500.0])
    )

    assert "resistance breakout" in notes_normal
    assert "resistance breakout (volume confirmed)" in notes_spike
    assert score_spike > score_normal
