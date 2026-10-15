import pytest

import bot
import config
import db
from config import CFG


@pytest.fixture(autouse=True)
def pinned(monkeypatch, tmp_path):
    monkeypatch.setattr(config.CFG, "db_path", str(tmp_path / "b.db"))
    for k, v in dict(starting_capital=1000.0, risk_per_trade_pct=1.0, max_drawdown_pct=15.0, max_daily_loss_pct=5.0,
                     max_exposure_pct=50.0, allow_shorts=False, degradation_min_profit_factor=0.7).items():
        monkeypatch.setattr(CFG, k, v)


def _pos(action="BUY", entry=100.0, stop=95.0, take=110.0):
    return {"symbol": "ETH/USDT", "action": action, "entry": entry, "size": 1.0, "stop": stop, "take": take,
            "mode": "paper", "protection": {}, "expert_scores": {"1h": 50.0},
            "factor_scores_by_tf": {"1h": {"trend": 30.0}}, "initial_stop": stop, "opened_ts": 123.0}


@pytest.mark.parametrize("action,price,expected", [
    ("BUY", 94.0, "stop"), ("BUY", 111.0, "take"), ("BUY", 100.0, None),
    ("SELL", 106.0, "stop"), ("SELL", 89.0, "take"), ("SELL", 100.0, None),
])
def test_check_exit(action, price, expected):
    p = _pos(action, stop=95.0 if action == "BUY" else 105.0, take=110.0 if action == "BUY" else 90.0)
    assert bot.check_exit(p, price) == expected


def test_restore_state_on_an_empty_database_starts_fresh():
    conn = db.connect()
    equity, risk, positions = bot.restore_state(conn, ("ETH/USDT",))
    assert equity == 1000.0 and risk.peak_equity == 1000.0 and positions == {"ETH/USDT": None}
    assert risk.open_exposure == 0 and not risk.kill_switch_tripped


def test_restore_state_recovers_equity_peak_exposure_and_position_scores():
    """Regression: restarts used to reset equity/peak to STARTING_CAPITAL and lose the expert scores."""
    conn = db.connect()
    for e in (1000, 1100, 1050):
        db.log_equity(conn, e)
    db.save_open_position(conn, _pos())
    equity, risk, positions = bot.restore_state(conn, ("ETH/USDT",))
    assert equity == 1050 and risk.peak_equity == 1100
    assert risk.open_exposure == CFG.risk_per_trade_pct
    p = positions["ETH/USDT"]
    assert p["expert_scores"] == {"1h": 50.0} and p["factor_scores_by_tf"] == {"1h": {"trend": 30.0}}
    assert p["initial_stop"] == 95.0


def test_restart_does_not_reset_a_tripped_drawdown_limit():
    conn = db.connect()
    for e in (1000, 1200, 900):   # 25% below peak, limit is 15%
        db.log_equity(conn, e)
    _, risk, _ = bot.restore_state(conn, ("ETH/USDT",))
    assert risk.kill_switch_tripped


def test_a_restored_position_can_be_settled_and_learned_from_without_keyerror(isolated_brain):
    conn = db.connect()
    db.save_open_position(conn, _pos())
    _, _, positions = bot.restore_state(conn, ("ETH/USDT",))
    p = positions["ETH/USDT"]
    pnl = bot.settle_trade(p, 110.0)
    isolated_brain.update_from_trade(p["expert_scores"], p["factor_scores_by_tf"], p["action"], pnl)
    assert isolated_brain.trades_learned_from == 1


def test_settle_trade_long_short_and_fees():
    assert bot.settle_trade(_pos("BUY"), 110.0) == pytest.approx(10.0 - bot.COST_MODEL.round_trip_fee(100.0, 110.0, 1.0))
    assert bot.settle_trade(_pos("SELL", stop=105.0, take=90.0), 90.0) > 0
    assert bot.settle_trade(_pos("SELL", stop=105.0, take=90.0), 110.0) < 0


def test_entry_blockers_all_clear():
    assert bot.entry_blockers(signal_action="BUY", paused=False, live=True, stale=None,
                              quality=(True, "ok"), degraded=(False, 1.5)) == []


def test_entry_blockers_reports_each_reason():
    r = bot.entry_blockers(signal_action="SELL", paused=True, live=True, stale="1h old",
                           quality=(False, "spread"), degraded=(True, 0.4))
    text = " | ".join(r)
    for needle in ("paused", "stale", "short", "market quality", "degraded"):
        assert needle in text


def test_shorts_are_blocked_live_but_allowed_in_paper_and_when_enabled(monkeypatch):
    kw = dict(signal_action="SELL", paused=False, stale=None, quality=(True, ""), degraded=None)
    assert bot.entry_blockers(live=True, **kw)
    assert not bot.entry_blockers(live=False, **kw)
    monkeypatch.setattr(CFG, "allow_shorts", True)
    assert not bot.entry_blockers(live=True, **kw)


def test_doctor_counts_config_problems(monkeypatch):
    monkeypatch.setattr(CFG, "risk_per_trade_pct", 50.0)
    assert bot.doctor() >= 1
