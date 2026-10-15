import json
import random

import pytest
from conftest import make_candles

import optimize
from backtester import override_cfg  # noqa: F401
from config import CFG
from strategy import generate_signal


def _signal_fn(df):
    cache = {}

    def fn(i):
        if i not in cache:
            cache[i] = generate_signal({"1h": df.iloc[: i + 1]})
        return cache[i]
    return fn


def test_sample_params_are_in_range_and_never_below_1r():
    rng = random.Random(1)
    for _ in range(200):
        p = optimize.sample_params(rng)
        for k, (lo, hi) in optimize.SEARCH_SPACE.items():
            assert lo <= p[k] <= hi or k == "atr_take_mult"
        assert p["atr_take_mult"] >= p["atr_stop_mult"]


def test_risk_limits_are_never_in_the_search_space():
    forbidden = {"risk_per_trade_pct", "max_exposure_pct", "max_drawdown_pct", "max_daily_loss_pct",
                 "max_position_notional_pct", "starting_capital"}
    assert forbidden.isdisjoint(optimize.SEARCH_SPACE)


def test_too_little_history_raises():
    df = make_candles(150)
    with pytest.raises(ValueError):
        optimize.optimize(df, _signal_fn(df), n_trials=3)


def test_optimize_is_deterministic_restores_cfg_and_reports_oos(tmp_path):
    df = make_candles(700, seed=11, vol=0.012)
    before = {k: getattr(CFG, k) for k in optimize.SEARCH_SPACE}
    fn = _signal_fn(df)
    r1 = optimize.optimize(df, fn, n_trials=8, seed=42)
    r2 = optimize.optimize(df, fn, n_trials=8, seed=42)
    assert r1 == r2
    assert {k: getattr(CFG, k) for k in optimize.SEARCH_SPACE} == before
    assert r1["verdict"] in {"SUGGEST", "OVERFIT_OR_NO_EDGE", "NO_CANDIDATE"}
    assert r1["baseline"]["test"] is not None
    assert r1["train_candles"] > r1["test_candles"] > 0
    if r1["suggested_env"]:
        assert set(r1["suggested_env"]) == {k.upper() for k in optimize.SEARCH_SPACE}
    out = tmp_path / "r.json"
    optimize.save_report(r1, str(out))
    assert json.loads(out.read_text())["verdict"] == r1["verdict"]


def test_score_disqualifies_few_trades():
    m = {"trades": 3, "return_pct": 50, "max_drawdown_pct": 1, "profit_factor": 9}
    assert optimize._score(m) == float("-inf")
    m["trades"] = 20
    assert optimize._score(m) > 0
