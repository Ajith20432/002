import json

import pytest

from brain import DEFAULT_EXPERT_WEIGHTS, DEFAULT_FACTOR_WEIGHTS, AdaptiveBrain


def test_starts_from_the_documented_defaults(isolated_brain):
    assert isolated_brain.expert_weights == DEFAULT_EXPERT_WEIGHTS
    assert isolated_brain.factor_weights == DEFAULT_FACTOR_WEIGHTS
    assert isolated_brain.trades_learned_from == 0


def test_a_winning_trade_increases_relative_trust_in_the_expert_that_agreed_most_strongly(isolated_brain):
    # "ai" agrees strongly (score 80) with the BUY that won; "news" barely leans the same way (score 5)
    before = dict(isolated_brain.expert_weights)
    isolated_brain.update_from_trade({"ai": 80, "news": 5}, {}, "BUY", pnl=10.0)
    after = isolated_brain.expert_weights

    # both technically "agreed" with the winning direction, but ai's conviction was much higher,
    # so after renormalizing to a fixed total, ai's *relative* share should grow more than news's.
    ai_growth = after["ai"] / before["ai"]
    news_growth = after["news"] / before["news"]
    assert ai_growth > news_growth


def test_a_losing_trade_reduces_trust_in_the_expert_that_agreed_with_the_losing_direction(isolated_brain):
    before = isolated_brain.expert_weights["ai"]
    isolated_brain.update_from_trade({"ai": 80, "15m": 0, "1h": 0, "4h": 0}, {}, "BUY", pnl=-10.0)
    after = isolated_brain.expert_weights["ai"]
    assert after < before


def test_an_expert_that_correctly_disagreed_with_a_losing_trade_gains_relative_trust(isolated_brain):
    # "news" leaned SELL (-60) while the bot took a BUY that then lost — news called it right.
    before = dict(isolated_brain.expert_weights)
    isolated_brain.update_from_trade({"ai": 60, "news": -60}, {}, "BUY", pnl=-10.0)
    after = isolated_brain.expert_weights

    ai_ratio = after["ai"] / before["ai"]        # agreed with the losing trade -> should shrink
    news_ratio = after["news"] / before["news"]  # correctly dissented -> should grow relatively
    assert news_ratio > ai_ratio


def test_breakeven_trade_does_not_change_anything(isolated_brain):
    before = dict(isolated_brain.expert_weights)
    isolated_brain.update_from_trade({"ai": 80}, {}, "BUY", pnl=0.0)
    assert isolated_brain.expert_weights == before
    assert isolated_brain.trades_learned_from == 0


def test_missing_expert_scores_does_not_crash_or_learn(isolated_brain):
    before = dict(isolated_brain.expert_weights)
    isolated_brain.update_from_trade({}, {}, "BUY", pnl=10.0)
    assert isolated_brain.expert_weights == before
    assert isolated_brain.trades_learned_from == 0


def test_no_single_expert_can_be_starved_to_zero_even_after_many_losses(isolated_brain):
    for _ in range(200):
        isolated_brain.update_from_trade({"ai": 100, "15m": 0, "1h": 0, "4h": 0, "news": 0},
                                          {}, "BUY", pnl=-10.0)
    total = sum(isolated_brain.expert_weights.values())
    assert isolated_brain.expert_weights["ai"] > 0.01 * total


def test_no_single_expert_can_dominate_even_after_many_wins(isolated_brain):
    for _ in range(200):
        isolated_brain.update_from_trade({"ai": 100, "15m": 0, "1h": 0, "4h": 0, "news": 0},
                                          {}, "BUY", pnl=10.0)
    total = sum(isolated_brain.expert_weights.values())
    assert isolated_brain.expert_weights["ai"] < 0.75 * total


def test_total_weight_is_conserved_across_updates(isolated_brain):
    total_before = sum(isolated_brain.expert_weights.values())
    isolated_brain.update_from_trade({"ai": 40, "15m": -20}, {}, "SELL", pnl=5.0)
    total_after = sum(isolated_brain.expert_weights.values())
    assert total_before == pytest.approx(total_after)


def test_trades_learned_from_increments_once_per_real_update(isolated_brain):
    isolated_brain.update_from_trade({"ai": 40}, {}, "BUY", pnl=5.0)
    isolated_brain.update_from_trade({"ai": 40}, {}, "BUY", pnl=-3.0)
    isolated_brain.update_from_trade({"ai": 40}, {}, "BUY", pnl=0.0)  # breakeven — doesn't count
    assert isolated_brain.trades_learned_from == 2


def test_state_persists_to_disk_and_reloads(tmp_path):
    state_file = tmp_path / "brain_state.json"

    b1 = AdaptiveBrain(state_path=state_file)
    b1.update_from_trade({"ai": 50}, {}, "BUY", pnl=10.0)
    assert state_file.exists()

    saved = json.loads(state_file.read_text())
    assert saved["trades_learned_from"] == 1

    b2 = AdaptiveBrain(state_path=state_file)  # simulates restarting the bot
    assert b2.trades_learned_from == 1
    assert b2.expert_weights == b1.expert_weights


def test_generate_signal_never_writes_state_only_update_from_trade_does(tmp_path):
    import numpy as np
    import pandas as pd

    from strategy import generate_signal

    state_file = tmp_path / "brain_state.json"
    b = AdaptiveBrain(state_path=state_file)

    prices = pd.Series(100 + np.cumsum(np.random.RandomState(0).randn(80)))
    df = pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=80, freq="1h"),
        "open": prices, "high": prices + 1, "low": prices - 1, "close": prices,
        "volume": pd.Series([10.0] * 80),
    })
    generate_signal({"1h": df}, brain=b)
    assert not state_file.exists()  # reading/scoring must never persist anything


def test_a_corrupted_state_file_falls_back_to_defaults_instead_of_crashing(tmp_path):
    state_file = tmp_path / "brain_state.json"
    state_file.write_text("{not valid json")

    b = AdaptiveBrain(state_path=state_file)
    assert b.expert_weights == DEFAULT_EXPERT_WEIGHTS
