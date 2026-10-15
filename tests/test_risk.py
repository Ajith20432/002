import pytest

from config import CFG
from risk import RiskEngine


@pytest.fixture(autouse=True)
def controlled_risk_config():
    """Pin every risk-relevant CFG field so tests don't depend on .env contents."""
    saved = {
        "risk_per_trade_pct": CFG.risk_per_trade_pct,
        "max_exposure_pct": CFG.max_exposure_pct,
        "max_daily_loss_pct": CFG.max_daily_loss_pct,
        "max_drawdown_pct": CFG.max_drawdown_pct,
        "atr_stop_mult": CFG.atr_stop_mult,
        "atr_take_mult": CFG.atr_take_mult,
    }
    CFG.risk_per_trade_pct = 1.0
    CFG.max_exposure_pct = 10.0
    CFG.max_daily_loss_pct = 5.0
    CFG.max_drawdown_pct = 20.0
    CFG.atr_stop_mult = 2.0
    CFG.atr_take_mult = 3.0
    yield
    for k, v in saved.items():
        setattr(CFG, k, v)


def test_rejects_a_non_actionable_signal():
    engine = RiskEngine(1000)
    decision = engine.evaluate("HOLD", 100, 2.0)
    assert not decision.approved
    assert "no actionable signal" in decision.reason


def test_rejects_invalid_atr():
    engine = RiskEngine(1000)
    decision = engine.evaluate("BUY", 100, 0)
    assert not decision.approved


def test_buy_places_stop_below_and_take_above_entry():
    engine = RiskEngine(1000)
    decision = engine.evaluate("BUY", 100, 2.0)
    assert decision.approved
    assert decision.stop_loss < 100 < decision.take_profit


def test_sell_places_stop_above_and_take_below_entry():
    engine = RiskEngine(1000)
    decision = engine.evaluate("SELL", 100, 2.0)
    assert decision.approved
    assert decision.take_profit < 100 < decision.stop_loss


def test_position_size_scales_with_risk_amount_and_inversely_with_stop_distance():
    engine = RiskEngine(1000)
    tight_stop = engine.evaluate("BUY", 100, 1.0)   # smaller ATR -> tighter stop
    wide_stop = engine.evaluate("BUY", 100, 4.0)    # larger ATR -> wider stop
    # same $ risk budget spread over a wider stop distance -> smaller position size
    assert tight_stop.position_size > wide_stop.position_size


def test_kill_switch_trips_on_drawdown_and_blocks_all_further_trades():
    engine = RiskEngine(1000)
    engine.update_equity(750)  # 25% drawdown, over the 20% limit set in the fixture
    assert engine.kill_switch_tripped
    decision = engine.evaluate("BUY", 100, 2.0)
    assert not decision.approved
    assert "kill switch" in decision.reason


def test_kill_switch_trips_on_daily_loss():
    engine = RiskEngine(1000)
    engine.update_equity(940)  # 6% down today, over the 5% daily-loss limit
    assert engine.kill_switch_tripped


def test_reset_daily_clears_the_daily_loss_baseline_but_not_drawdown():
    engine = RiskEngine(1000)
    engine.update_equity(970)          # 3% down — under both limits, no trip
    assert not engine.kill_switch_tripped
    engine.reset_daily()               # new day starts counting daily loss from 970
    engine.update_equity(930)          # ~4.1% down from the new daily baseline — still under 5%
    assert not engine.kill_switch_tripped


def test_exposure_gate_blocks_once_max_exposure_is_reached():
    engine = RiskEngine(1000)
    engine.add_exposure(9.5)  # just under the 10% cap set in the fixture
    decision = engine.evaluate("BUY", 100, 2.0)  # would add another 1% -> 10.5%, over the cap
    assert not decision.approved
    assert "exposure" in decision.reason


def test_remove_exposure_frees_up_room_for_a_new_trade():
    engine = RiskEngine(1000)
    engine.add_exposure(9.5)
    engine.remove_exposure(9.5)
    decision = engine.evaluate("BUY", 100, 2.0)
    assert decision.approved


def test_remove_exposure_never_goes_negative():
    engine = RiskEngine(1000)
    engine.remove_exposure(5.0)  # nothing was ever added
    assert engine.open_exposure == 0.0


def test_decision_reports_the_exposure_it_consumes_and_halves_with_the_multiplier():
    engine = RiskEngine(1000)
    full = engine.evaluate("BUY", 100, 2.0)
    half = engine.evaluate("BUY", 100, 2.0, size_multiplier=0.5)
    assert full.exposure_pct == 1.0 and half.exposure_pct == 0.5
    assert half.position_size == pytest.approx(full.position_size / 2)


def test_stop_never_loosens_and_breakeven_needs_one_r():
    from risk import manage_stop
    p = {"action": "BUY", "entry": 100.0, "stop": 96.0, "initial_stop": 96.0}
    assert manage_stop(p, 103.0, 2.0) is None            # < 1R in profit
    CFG.trailing_stop_enabled = True
    assert manage_stop(p, 108.0, 2.0) == 104.0           # trails 2 ATR behind
    p["stop"] = 106.0
    assert manage_stop(p, 108.0, 2.0) is None            # would loosen -> refused
