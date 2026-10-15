import numpy as np
import pandas as pd

from indicators import atr, bollinger, ema, enrich, macd, rsi, volume_spike


def make_df(prices, volumes=None):
    n = len(prices)
    prices = pd.Series(prices, dtype=float)
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "open": prices, "high": prices + 0.5, "low": prices - 0.5, "close": prices,
        "volume": volumes if volumes is not None else pd.Series([10.0] * n),
    })


def test_ema_on_constant_series_equals_the_constant():
    s = pd.Series([50.0] * 30)
    result = ema(s, 9)
    assert np.allclose(result, 50.0)


def test_rsi_is_high_on_a_pure_uptrend():
    s = pd.Series(np.arange(1, 60, dtype=float))  # strictly increasing, no losses at all
    r = rsi(s, 14)
    assert r.iloc[-1] > 95


def test_rsi_is_low_on_a_pure_downtrend():
    s = pd.Series(np.arange(60, 1, -1, dtype=float))  # strictly decreasing, no gains at all
    r = rsi(s, 14)
    assert r.iloc[-1] < 5


def test_rsi_is_near_50_when_gains_and_losses_are_balanced():
    s = pd.Series([50.0 + (1 if i % 2 == 0 else -1) for i in range(60)])
    r = rsi(s, 14)
    assert 40 < r.iloc[-1] < 60


def test_atr_is_zero_for_a_perfectly_flat_market():
    df = make_df([100.0] * 30)
    df["high"] = 100.0
    df["low"] = 100.0
    a = atr(df, 14)
    assert np.allclose(a.iloc[5:], 0.0)


def test_atr_is_positive_when_there_is_a_real_range():
    df = make_df(np.linspace(100, 130, 30))
    a = atr(df, 14)
    assert a.iloc[-1] > 0


def test_bollinger_bands_are_ordered_upper_mid_lower():
    s = pd.Series(100 + np.cumsum(np.random.RandomState(0).randn(60)))
    upper, mid, lower = bollinger(s, period=20)
    tail = slice(25, None)  # skip the warm-up window where rolling stats are NaN
    assert (upper[tail] >= mid[tail]).all()
    assert (mid[tail] >= lower[tail]).all()


def test_macd_is_positive_once_an_uptrend_is_established():
    s = pd.Series(np.linspace(100, 200, 80))
    macd_line, signal_line, hist = macd(s)
    assert macd_line.iloc[-1] > 0


def test_volume_spike_is_about_one_for_constant_volume():
    v = pd.Series([10.0] * 30)
    spike = volume_spike(v, period=20)
    assert np.allclose(spike.iloc[25:], 1.0, atol=1e-6)


def test_volume_spike_flags_a_real_spike():
    v = pd.Series([10.0] * 29 + [100.0])
    spike = volume_spike(v, period=20)
    assert spike.iloc[-1] > 5


def test_enrich_adds_all_expected_columns_without_raising():
    df = make_df(100 + np.cumsum(np.random.RandomState(1).randn(80)))
    out = enrich(df)
    for col in ["ema9", "ema21", "ema50", "rsi14", "macd", "macd_signal",
                "macd_hist", "atr14", "bb_upper", "bb_mid", "bb_lower", "vol_spike"]:
        assert col in out.columns
