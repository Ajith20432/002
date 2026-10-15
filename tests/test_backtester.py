from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import make_candles

import backtester
from config import CFG
from costs import ExecutionCost


def _sig(action):
    return SimpleNamespace(action=action, confidence=80, expert_scores={"1h": 80}, factor_scores_by_tf={"1h": {"trend": 30}})


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    for k, v in dict(risk_per_trade_pct=1.0, max_exposure_pct=50.0, max_daily_loss_pct=50.0, max_drawdown_pct=90.0,
                     atr_stop_mult=2.0, atr_take_mult=3.0, max_position_notional_pct=100.0, trailing_stop_enabled=False,
                     slippage_bps=0.0, latency_bps=0.0, fee_rate=0.0, starting_capital=1000.0).items():
        monkeypatch.setattr(CFG, k, v)
    monkeypatch.setattr(backtester, "COST_MODEL", ExecutionCost(fee_rate=0.0, slippage_bps=0.0, latency_ms=0))


def _flat(n=80, price=100.0):
    df = make_candles(n, vol=0.0001, drift=0.0)
    for c in ("open", "high", "low", "close"):
        df[c] = price
    df["atr14"] = 2.0
    return df


def test_intrabar_low_triggers_the_stop_even_when_the_close_recovers():
    df = _flat()
    entry_i = 60
    df.loc[entry_i + 1, "low"] = 90.0   # wick far below the 96 stop, close stays 100
    res = backtester.simulate(df, lambda i: _sig("BUY") if i == entry_i else _sig("HOLD"), end_idx=75)
    assert [t["reason"] for t in res.trades] == ["stop"]
    assert res.trades[0]["pnl"] < 0


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    df = _flat()
    df.loc[61, ["open", "high", "low", "close"]] = [80.0, 81.0, 79.0, 80.0]
    res = backtester.simulate(df, lambda i: _sig("BUY") if i == 60 else _sig("HOLD"), end_idx=70)
    assert res.trades[0]["exit"] == pytest.approx(80.0)


def test_take_profit_hits_on_the_high():
    df = _flat()
    df.loc[62, "high"] = 120.0
    res = backtester.simulate(df, lambda i: _sig("BUY") if i == 60 else _sig("HOLD"), end_idx=70)
    assert res.trades[0]["reason"] == "take" and res.trades[0]["pnl"] > 0


def test_stop_wins_when_one_candle_touches_both_levels():
    df = _flat()
    df.loc[61, "high"] = 120.0
    df.loc[61, "low"] = 90.0
    res = backtester.simulate(df, lambda i: _sig("BUY") if i == 60 else _sig("HOLD"), end_idx=70)
    assert res.trades[0]["reason"] == "stop"


def test_short_positions_mirror_the_logic():
    df = _flat()
    df.loc[62, "low"] = 80.0
    res = backtester.simulate(df, lambda i: _sig("SELL") if i == 60 else _sig("HOLD"), end_idx=70)
    assert res.trades[0]["reason"] == "take" and res.trades[0]["pnl"] > 0


def test_open_position_is_closed_at_the_end_not_dropped():
    df = _flat()
    res = backtester.simulate(df, lambda i: _sig("BUY") if i == 60 else _sig("HOLD"), end_idx=70)
    assert len(res.trades) == 1 and res.trades[0]["reason"] == "end"


def test_never_enters_on_the_last_candle():
    df = _flat()
    res = backtester.simulate(df, lambda i: _sig("BUY"), start_idx=60, end_idx=60)
    assert res.trades == []


def test_daily_loss_baseline_resets_each_day(monkeypatch):
    """Regression: the baseline never reset, so ordinary cumulative losses tripped the 'daily' kill switch."""
    monkeypatch.setattr(CFG, "max_daily_loss_pct", 2.0)
    df = _flat(n=400)
    entries = set(range(60, 380, 24))          # one entry per day...
    for e in entries:
        df.loc[e + 1, "low"] = 90.0            # ...stopped out on the next candle (~1% loss each)
    res = backtester.simulate(df, lambda i: _sig("BUY") if i in entries else _sig("HOLD"))
    assert len(res.trades) >= 10
    assert not res.kill_switch_tripped         # ~1%/day never breaches 2% in any single day


def test_same_day_losses_do_trip_the_daily_kill_switch(monkeypatch):
    monkeypatch.setattr(CFG, "max_daily_loss_pct", 2.0)
    df = _flat(n=200)
    entries = {60, 62, 64, 66}                 # four ~1% losses inside one day
    for e in entries:
        df.loc[e + 1, "low"] = 90.0
    res = backtester.simulate(df, lambda i: _sig("BUY") if i in entries else _sig("HOLD"))
    assert res.kill_switch_tripped


def test_on_close_hook_receives_position_and_pnl():
    df = _flat()
    df.loc[62, "high"] = 120.0
    seen = []
    backtester.simulate(df, lambda i: _sig("BUY") if i == 60 else _sig("HOLD"), end_idx=70,
                        on_close=lambda pos, pnl: seen.append((pos["expert_scores"], pnl)))
    assert seen and seen[0][0] == {"1h": 80} and seen[0][1] > 0


def test_override_cfg_restores_even_after_an_error():
    before = CFG.atr_stop_mult
    with pytest.raises(RuntimeError):
        with backtester.override_cfg(atr_stop_mult=9.9):
            assert CFG.atr_stop_mult == 9.9
            raise RuntimeError("boom")
    assert CFG.atr_stop_mult == before


def test_equity_curve_length_and_final_equity_consistent():
    df = _flat()
    df.loc[62, "high"] = 120.0
    res = backtester.simulate(df, lambda i: _sig("BUY") if i == 60 else _sig("HOLD"), end_idx=70)
    assert len(res.equity_curve) == 70 - 60 + 1
    assert res.final_equity == pytest.approx(1000 + sum(res.pnls))
    assert isinstance(res.equity_curve[0], float)
    assert isinstance(df["ts"].iloc[0], pd.Timestamp)
