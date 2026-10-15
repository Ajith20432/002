import math

import numpy as np
import pandas as pd
import pytest

import guards


def test_timeframe_seconds():
    assert guards.timeframe_seconds("15m") == 900
    assert guards.timeframe_seconds("4h") == 14400
    assert guards.timeframe_seconds("1d") == 86400


@pytest.mark.parametrize("bad", ["", "m", "15", "xh", "1y"])
def test_timeframe_seconds_rejects_garbage(bad):
    with pytest.raises(ValueError):
        guards.timeframe_seconds(bad)


def _df_with_last_open(ts):
    return pd.DataFrame({"ts": [pd.Timestamp(ts, unit="s", tz="UTC")]})


def test_fresh_candle_is_not_stale():
    now = 1_000_000.0
    stale, age = guards.is_stale(_df_with_last_open(now - 600), "15m", 2.5, now=now)
    assert not stale and age == 600


def test_old_candle_is_stale():
    now = 1_000_000.0
    stale, _ = guards.is_stale(_df_with_last_open(now - 3 * 900), "15m", 2.5, now=now)
    assert stale


def test_market_quality_accepts_tight_liquid_market():
    ok, _ = guards.market_quality({"bid": 100.0, "ask": 100.05, "quoteVolume": 5e6}, 15, 1e6)
    assert ok


def test_market_quality_rejects_wide_spread_thin_volume_and_missing_data():
    assert not guards.market_quality({"bid": 100.0, "ask": 101.0, "quoteVolume": 5e6}, 15, 1e6)[0]
    assert not guards.market_quality({"bid": 100.0, "ask": 100.01, "quoteVolume": 10}, 15, 1e6)[0]
    assert not guards.market_quality({"bid": None, "ask": 100.0, "quoteVolume": 5e6}, 15, 1e6)[0]
    assert not guards.market_quality({"bid": 101.0, "ask": 100.0, "quoteVolume": 5e6}, 15, 1e6)[0]  # crossed book
    assert not guards.market_quality({"bid": 100.0, "ask": 100.01}, 15, 1e6)[0]  # volume unknown -> reject


def test_degradation_needs_a_full_window_before_judging():
    degraded, pf = guards.strategy_degraded([-1] * 5, window=20, min_profit_factor=0.7)
    assert not degraded and math.isnan(pf)


def test_degradation_flags_a_losing_window_and_clears_a_winning_one():
    assert guards.strategy_degraded([-1, -1, 1] * 7, 20, 0.7)[0]
    assert not guards.strategy_degraded([2, -1] * 10, 20, 0.7)[0]


def test_degradation_only_looks_at_the_recent_window():
    old_losses = [-5] * 30
    recent_wins = [3, -1] * 10
    assert not guards.strategy_degraded(old_losses + recent_wins, 20, 0.7)[0]


def test_correlation_of_identical_series_is_one_and_independent_is_low():
    rng = np.random.default_rng(1)
    a = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 120)))
    b = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 120)))
    assert guards.rolling_correlation(a, a, 50) == pytest.approx(1.0)
    assert abs(guards.rolling_correlation(a, b, 50)) < 0.5


def test_correlation_needs_enough_data_and_variance():
    assert guards.rolling_correlation(pd.Series([1.0, 2, 3]), pd.Series([1.0, 2, 3]), 50) is None
    flat = pd.Series([100.0] * 60)
    assert guards.rolling_correlation(flat, flat, 50) is None


def test_correlation_regimes():
    assert guards.correlation_regime(None) == "unknown"
    assert guards.correlation_regime(0.9) == "high"
    assert guards.correlation_regime(0.5) == "moderate"
    assert guards.correlation_regime(0.1) == "broken"
