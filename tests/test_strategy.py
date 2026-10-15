import numpy as np
import pandas as pd

from strategy import generate_signal, score_timeframe


def make_uptrend_df(n=120):
    prices = pd.Series(np.linspace(100, 140, n))
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "open": prices, "high": prices + 1, "low": prices - 1, "close": prices,
        "volume": pd.Series([10.0] * n),
    })


def make_downtrend_df(n=120):
    prices = pd.Series(np.linspace(140, 100, n))
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "open": prices, "high": prices + 1, "low": prices - 1, "close": prices,
        "volume": pd.Series([10.0] * n),
    })


def make_flat_df(n=120):
    prices = pd.Series([100.0] * n)
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "open": prices, "high": prices + 0.1, "low": prices - 0.1, "close": prices,
        "volume": pd.Series([10.0] * n),
    })


def test_score_timeframe_is_bounded(isolated_brain):
    score, factors, _patterns = score_timeframe(make_uptrend_df(), brain=isolated_brain)
    assert -100 <= score <= 100
    assert set(factors.keys()) == {"trend", "momentum", "rsi", "meanrev", "volume", "pattern"}


def test_score_timeframe_is_positive_on_a_clean_uptrend(isolated_brain):
    score, _, _ = score_timeframe(make_uptrend_df(), brain=isolated_brain)
    assert score > 0


def test_score_timeframe_is_negative_on_a_clean_downtrend(isolated_brain):
    score, _, _ = score_timeframe(make_downtrend_df(), brain=isolated_brain)
    assert score < 0


def test_generate_signal_holds_with_insufficient_data(isolated_brain):
    tiny_df = make_flat_df(n=10)  # under the 60-candle warm-up window
    signal = generate_signal({"1h": tiny_df}, brain=isolated_brain)
    assert signal.action == "HOLD"
    assert signal.reasons == ["insufficient data"]


def test_generate_signal_is_buy_on_agreement_across_timeframes(isolated_brain):
    mtf = {"15m": make_uptrend_df(), "1h": make_uptrend_df(), "4h": make_uptrend_df()}
    signal = generate_signal(mtf, brain=isolated_brain)
    assert signal.action == "BUY"
    assert set(signal.expert_scores.keys()) == {"15m", "1h", "4h"}


def test_generate_signal_folds_in_ai_and_news_as_extra_experts(isolated_brain):
    mtf = {"1h": make_uptrend_df()}
    signal = generate_signal(mtf, ai_score=75.0, news_score=-20.0, brain=isolated_brain)
    assert signal.expert_scores["ai"] == 75.0
    assert signal.expert_scores["news"] == -20.0


def test_ai_and_news_scores_are_clamped_to_valid_range(isolated_brain):
    mtf = {"1h": make_uptrend_df()}
    signal = generate_signal(mtf, ai_score=500.0, news_score=-500.0, brain=isolated_brain)
    assert signal.expert_scores["ai"] == 100.0
    assert signal.expert_scores["news"] == -100.0


def test_a_weak_lone_timeframe_holds_rather_than_forcing_a_trade(isolated_brain):
    mtf = {"1h": make_flat_df()}
    signal = generate_signal(mtf, brain=isolated_brain)
    assert signal.action == "HOLD"


def test_generate_signal_uses_the_given_brains_weights_not_the_global_singleton(isolated_brain):
    # Skew the isolated brain heavily toward "news" and away from everything else,
    # then confirm a strong opposing news_score can flip the outcome.
    isolated_brain.expert_weights = {"15m": 0.01, "1h": 0.01, "4h": 0.01, "ai": 0.01, "news": 0.96}
    mtf = {"1h": make_uptrend_df()}  # technical experts alone would say BUY
    signal = generate_signal(mtf, news_score=-100.0, brain=isolated_brain)
    assert signal.action == "SELL"
