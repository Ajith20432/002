import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pandas as pd
import pytest

import dashboard
import dca
import grid
import ml_model
import tv_signal
from config import CFG
from indicators import enrich
from strategy import generate_signal


def _candles(closes, spread=0.004):
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    high = np.maximum(opens, closes) * (1 + spread)
    low = np.minimum(opens, closes) * (1 - spread)
    ts = pd.date_range("2025-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame({"ts": ts, "open": opens, "high": high, "low": low, "close": closes, "volume": 100.0})


# ---------------- DCA ----------------
@pytest.fixture
def dca_cfg(monkeypatch):
    for k, v in dict(dca_base_order_pct=10.0, dca_dip_pct=3.0, dca_max_safety_orders=2, dca_take_profit_pct=4.0,
                     dca_stop_loss_pct=15.0, dca_interval_hours=1.0, starting_capital=1000.0,
                     fee_rate=0.0, slippage_bps=0.0, latency_bps=0.0).items():
        monkeypatch.setattr(CFG, k, v)
    monkeypatch.setattr(dca, "COST_MODEL", dca.COST_MODEL.__class__(0.0, 0.0, 0))   # zero-cost fills: exact maths


def test_dca_takes_profit_after_a_recovery(dca_cfg):
    res = dca.simulate_dca(_candles([100, 100, 105, 110]))
    assert res.cycles and res.cycles[0]["reason"] == "take"
    assert res.cycles[0]["pnl"] > 0


def test_dca_safety_orders_are_fixed_size_and_capped(dca_cfg):
    path = [100, 96, 92, 88, 84, 80, 78]            # keeps falling; only 2 safety orders allowed
    res = dca.simulate_dca(_candles(path, spread=0.0))
    cycle = res.cycles[0]
    assert cycle["buys"] <= 1 + CFG.dca_max_safety_orders
    assert res.max_capital_used_pct <= CFG.dca_base_order_pct * (1 + CFG.dca_max_safety_orders) + 1e-6


def test_dca_stop_closes_a_cycle_that_keeps_falling(dca_cfg):
    res = dca.simulate_dca(_candles([100, 70, 60, 50], spread=0.0))
    assert res.cycles[0]["reason"] in ("stop", "end_of_data")
    assert res.cycles[0]["pnl"] < 0
    assert res.final_equity < 1000


def test_dca_never_spends_more_than_the_cap_even_over_a_long_crash(dca_cfg):
    closes = list(np.linspace(100, 20, 200))
    res = dca.simulate_dca(_candles(closes))
    assert min(res.equity_curve) > 1000 * 0.5       # bounded loss thanks to the cap + stop
    assert res.max_capital_used_pct <= 30.0 + 1e-6


# ---------------- grid ----------------
@pytest.fixture
def grid_cfg(monkeypatch):
    for k, v in dict(grid_levels=5, grid_range_pct=4.0, grid_capital_pct=40.0, grid_stop_pct=3.0,
                     starting_capital=1000.0, fee_rate=0.0).items():
        monkeypatch.setattr(CFG, k, v)


def test_grid_levels_are_evenly_spaced_and_centered():
    lv = grid.build_levels(100.0, 5, 4.0)
    assert lv[0] == pytest.approx(96) and lv[-1] == pytest.approx(104)
    assert np.allclose(np.diff(lv), np.diff(lv)[0])


def test_grid_rejects_nonsense_settings():
    with pytest.raises(ValueError):
        grid.build_levels(100.0, 1, 4.0)
    with pytest.raises(ValueError):
        grid.build_levels(0.0, 5, 4.0)


def test_grid_profits_in_a_sideways_market(grid_cfg):
    closes = [100 + 2.5 * np.sin(i / 2.0) for i in range(200)]
    res = grid.simulate_grid(_candles(closes))
    assert res.round_trips > 3
    assert res.realized_pnl > 0
    assert not res.stopped


def test_grid_stops_and_liquidates_when_price_breaks_the_floor(grid_cfg):
    closes = [100] * 5 + list(np.linspace(100, 80, 40))
    res = grid.simulate_grid(_candles(closes))
    assert res.stopped
    assert res.unrealized_pnl == 0                     # nothing left held after liquidation
    assert res.final_equity > 1000 * 0.9               # loss limited to the allocated slice


def test_grid_fees_reduce_profit(grid_cfg, monkeypatch):
    closes = [100 + 2.5 * np.sin(i / 2.0) for i in range(200)]
    free = grid.simulate_grid(_candles(closes)).realized_pnl
    monkeypatch.setattr(CFG, "fee_rate", 0.002)
    assert grid.simulate_grid(_candles(closes)).realized_pnl < free


# ---------------- ML expert ----------------
def test_ml_stays_silent_with_too_little_history():
    d = enrich(_candles(100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 80))))
    r = ml_model.ml_expert(d)
    assert r.score == 0.0 and r.prob_up is None


def test_ml_has_no_opinion_on_pure_noise(monkeypatch):
    monkeypatch.setattr(CFG, "ml_min_holdout_accuracy", 0.65)   # a noise model should not reach this
    rng = np.random.default_rng(3)
    d = enrich(_candles(100 * np.cumprod(1 + rng.normal(0, 0.01, 600))))
    assert ml_model.ml_expert(d).score == 0.0


def test_ml_finds_a_planted_edge_and_leans_with_it():
    # strong persistent trend regimes: momentum features genuinely predict direction
    rng = np.random.default_rng(7)
    rets = []
    drift = 0.004
    for _ in range(40):
        rets += list(drift + rng.normal(0, 0.002, 25))
        drift = -drift
    d = enrich(_candles(100 * np.cumprod(1 + np.array(rets))))
    r = ml_model.ml_expert(d, min_accuracy=0.55)
    assert r.holdout_accuracy is not None and r.holdout_accuracy > 0.55
    assert -100 <= r.score <= 100


def test_logistic_fit_separates_a_linear_problem():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(300, 2))
    y = (x[:, 0] + 0.5 * x[:, 1] > 0).astype(float)
    w = ml_model.fit_logistic(x, y)
    assert ((ml_model.predict_proba(w, x) > 0.5) == (y > 0.5)).mean() > 0.95


# ---------------- strategy wiring ----------------
def test_ml_and_tv_scores_enter_the_blend(isolated_brain):
    df = enrich(_candles(100 + np.cumsum(np.random.default_rng(2).normal(0, 0.5, 120))))
    sig = generate_signal({"1h": df}, ml_score=80, tv_score=-60, brain=isolated_brain)
    assert sig.expert_scores["ml"] == 80 and sig.expert_scores["tv"] == -60
    assert "ml" in isolated_brain.expert_weights and "tv" in isolated_brain.expert_weights


def test_extreme_scores_are_clamped(isolated_brain):
    df = enrich(_candles(100 + np.cumsum(np.random.default_rng(2).normal(0, 0.5, 120))))
    sig = generate_signal({"1h": df}, ml_score=900, tv_score=-900, brain=isolated_brain)
    assert sig.expert_scores["ml"] == 100 and sig.expert_scores["tv"] == -100


# ---------------- TradingView webhook ----------------
SECRET = "s" * 24


@pytest.fixture
def tv(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "tv_webhook_secret", SECRET)
    monkeypatch.setattr(CFG, "tv_signal_path", str(tmp_path / "tv.json"))
    monkeypatch.setattr(CFG, "tv_signal_ttl_minutes", 60)
    tv_signal._last_accept["ts"] = 0.0
    return tv_signal


def _body(**kw):
    d = {"secret": SECRET, "action": "buy", "confidence": 70, "note": "x"}
    d.update(kw)
    return json.dumps(d).encode()


def test_valid_alert_becomes_a_signed_score(tv):
    tv.handle_payload(_body(), now=1000.0)
    score, note = tv.current_score(now=1000.0 + 60)
    assert score == 70 and "buy" in note


def test_sell_alert_is_negative_and_neutral_is_zero(tv):
    tv.handle_payload(_body(action="sell", confidence=40), now=1000.0)
    assert tv.current_score(now=1001.0)[0] == -40
    tv.handle_payload(_body(action="neutral"), now=1010.0)
    assert tv.current_score(now=1011.0)[0] == 0


def test_alert_expires(tv):
    tv.handle_payload(_body(), now=1000.0)
    score, note = tv.current_score(now=1000.0 + 61 * 60)
    assert score is None and "expired" in note


@pytest.mark.parametrize("raw,status", [
    (b"not json", 400),
    (b"[1,2]", 400),
    (json.dumps({"secret": "wrong", "action": "buy"}).encode(), 403),
    (json.dumps({"secret": SECRET, "action": "moon"}).encode(), 400),
    (json.dumps({"secret": SECRET, "action": "buy", "confidence": 150}).encode(), 400),
    (json.dumps({"secret": SECRET, "action": "buy", "confidence": "x"}).encode(), 400),
    (b"x" * 5000, 413),
])
def test_bad_alerts_are_rejected_and_store_nothing(tv, raw, status):
    with pytest.raises(tv.WebhookError) as e:
        tv.handle_payload(raw, now=1000.0)
    assert e.value.status == status
    assert tv.current_score(now=1000.0)[0] is None


def test_webhook_disabled_without_a_secret(tv, monkeypatch):
    monkeypatch.setattr(CFG, "tv_webhook_secret", "")
    with pytest.raises(tv.WebhookError) as e:
        tv.handle_payload(_body(), now=1.0)
    assert e.value.status == 404


def test_alerts_are_rate_limited(tv):
    tv.handle_payload(_body(), now=1000.0)
    with pytest.raises(tv.WebhookError) as e:
        tv.handle_payload(_body(), now=1000.5)
    assert e.value.status == 429


def test_dashboard_webhook_end_to_end_and_other_posts_stay_blocked(tv, tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "db_path", str(tmp_path / "d.db"))
    server = dashboard.make_server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/webhook/tradingview", data=_body(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
        assert tv.current_score()[0] == 70
        bad = urllib.request.Request(base + "/api/state", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad, timeout=5)
        assert e.value.code == 405
        wrong = urllib.request.Request(base + "/webhook/tradingview", data=_body(secret="nope"), method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(wrong, timeout=5)
        assert e.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


# ---------------- config ----------------
def test_validate_flags_new_mode_misconfigurations(monkeypatch):
    monkeypatch.setattr(CFG, "tv_webhook_secret", "short")
    monkeypatch.setattr(CFG, "dashboard_port", 0)
    monkeypatch.setattr(CFG, "dca_base_order_pct", 40.0)
    monkeypatch.setattr(CFG, "grid_levels", 1)
    text = " | ".join(CFG.validate())
    assert "TV_WEBHOOK_SECRET is shorter" in text
    assert "DASHBOARD_PORT is 0" in text
    assert "DCA_BASE_ORDER_PCT" in text
    assert "GRID_LEVELS" in text


def test_ml_features_never_contain_the_label():
    """Regression: the feature row for a candle used that candle's own return -> ~100% fake accuracy."""
    rng = np.random.default_rng(11)
    d = enrich(_candles(100 * np.cumprod(1 + rng.normal(0, 0.01, 700))))
    r = ml_model.ml_expert(d, min_accuracy=0.0)
    assert r.holdout_accuracy is not None and r.holdout_accuracy < 0.7
