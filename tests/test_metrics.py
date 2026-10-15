import math

from metrics import compute_metrics, max_drawdown_pct, profit_factor, sharpe_ratio, sortino_ratio


def test_profit_factor_basic_and_edges():
    assert profit_factor([10, -5]) == 2.0
    assert math.isinf(profit_factor([10, 5]))
    assert profit_factor([]) == 0.0
    assert profit_factor([-3]) == 0.0


def test_max_drawdown_is_peak_to_trough():
    assert max_drawdown_pct([100, 120, 90, 110]) == 25.0
    assert max_drawdown_pct([100, 101, 102]) == 0.0
    assert max_drawdown_pct([]) == 0.0


def test_sharpe_and_sortino_degrade_to_zero_on_no_data_or_flat_curve():
    assert sharpe_ratio([100]) == 0.0
    assert sharpe_ratio([100, 100, 100, 100]) == 0.0
    assert sortino_ratio([100, 101, 102, 103]) == 0.0  # no downside at all


def test_sharpe_is_positive_for_a_noisy_uptrend():
    curve = [100, 101, 100.5, 102, 101.5, 103, 104]
    assert sharpe_ratio(curve) > 0
    assert sortino_ratio(curve) > 0


def test_compute_metrics_headline_numbers():
    m = compute_metrics([10, -5, 10, -5], starting_equity=1000)
    assert m["trades"] == 4
    assert m["win_rate_pct"] == 50.0
    assert m["total_pnl"] == 10
    assert m["return_pct"] == 1.0
    assert m["profit_factor"] == 2.0
    assert m["expectancy"] == 2.5
    assert m["payoff_ratio"] == 2.0


def test_compute_metrics_on_nothing_does_not_raise():
    m = compute_metrics([], starting_equity=0)
    assert m["trades"] == 0 and m["win_rate_pct"] == 0 and m["max_drawdown_pct"] == 0
