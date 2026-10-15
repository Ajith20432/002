"""Regression tests for bugs found in v0.6.0, plus tests for the new safety behaviour."""
import json

import ccxt
import pandas as pd
import pytest

import brain
import config
import data
import db
import execution
import news_feed
import telegram_control
from brain import AdaptiveBrain
from config import CFG
from risk import RiskEngine, manage_stop
from strategy import generate_signal

# ---------------- brain ----------------

def test_old_state_file_without_new_keys_is_merged_onto_defaults(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"expert_weights": {"15m": 0.2, "1h": 0.3, "4h": 0.5},
                                "factor_weights": {"trend": 30, "momentum": 15}, "trades_learned_from": 4}))
    b = AdaptiveBrain(state_path=path)
    assert set(brain.DEFAULT_FACTOR_WEIGHTS) <= set(b.factor_weights)   # "pattern" etc. were missing -> KeyError in strategy
    assert "news" in b.expert_weights and "fng" in b.expert_weights
    assert b.expert_weights["4h"] == 0.5 and b.trades_learned_from == 4


def test_garbage_weights_in_state_file_are_ignored(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"expert_weights": {"1h": "x", "4h": -1, "15m": float("nan"), "ai": True}}))
    b = AdaptiveBrain(state_path=path)
    assert b.expert_weights == brain.DEFAULT_EXPERT_WEIGHTS


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "s.json"
    b = AdaptiveBrain(state_path=path)
    b.save()
    assert path.exists() and not (tmp_path / "s.json.tmp").exists()
    assert json.loads(path.read_text())["expert_weights"]["1h"] == brain.DEFAULT_EXPERT_WEIGHTS["1h"]


def test_signal_with_an_old_brain_file_no_longer_crashes(tmp_path):
    from conftest import make_candles
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"factor_weights": {"trend": 30}}))
    sig = generate_signal({"1h": make_candles(200)}, brain=AdaptiveBrain(state_path=path))
    assert sig.action in ("BUY", "SELL", "HOLD")


def test_fng_expert_is_included_when_given_and_clamped():
    from conftest import make_candles
    sig = generate_signal({"1h": make_candles(200)}, fng_score=500)
    assert sig.expert_scores["fng"] == 100.0
    assert "fng" not in generate_signal({"1h": make_candles(200)}).expert_scores

# ---------------- news ----------------

@pytest.mark.parametrize("headline,coin,expected", [
    ("We work together on new methods", "ETH", False),       # 'eth' inside other words
    ("Solution providers adapt to change", "SOL", False),
    ("Teams adapt quickly to change", "ADA", False),
    ("Ether climbs as ETH ETFs see inflow", "ETH", True),
    ("$ETH holders rejoice", "ETH", True),
    ("Binance Coin rallies", "BNB", True),
])
def test_symbol_matching_uses_whole_words(headline, coin, expected):
    assert news_feed._matches_symbol(headline, coin) is expected


def test_sentiment_keywords_use_whole_words(monkeypatch):
    monkeypatch.setitem(news_feed._cache, "ts", 10**15)
    monkeypatch.setitem(news_feed._cache, "headlines", ["Bitcoin bank defines new urban strategy again"])
    score, matched = news_feed.news_sentiment("BTC/USDT")
    assert matched and score == 0   # 'ban' in bank/urban, 'fine' in defines, 'gain' in again must NOT score


def test_sentiment_still_handles_plurals_and_phrases(monkeypatch):
    monkeypatch.setitem(news_feed._cache, "ts", 10**15)
    monkeypatch.setitem(news_feed._cache, "headlines", ["Bitcoin hits record high as exchanges report outflows"])
    score, _ = news_feed.news_sentiment("BTC/USDT")
    assert score == 0  # +1 (record high) and -1 (outflows)
    monkeypatch.setitem(news_feed._cache, "headlines", ["Ethereum exploits and hacks spread"])
    assert news_feed.news_sentiment("ETH/USDT")[0] < 0

# ---------------- risk ----------------

@pytest.fixture
def risk_cfg(monkeypatch):
    for k, v in dict(risk_per_trade_pct=1.0, max_exposure_pct=100.0, max_position_notional_pct=30.0, atr_stop_mult=2.0,
                     atr_take_mult=3.0, max_drawdown_pct=50.0, max_daily_loss_pct=50.0, trailing_stop_enabled=True,
                     breakeven_at_r=1.0, trail_atr_mult=2.0).items():
        monkeypatch.setattr(CFG, k, v)


def test_tiny_atr_cannot_create_a_position_bigger_than_the_notional_cap(risk_cfg):
    d = RiskEngine(1000).evaluate("BUY", 100.0, 0.01)   # 1% risk over a 0.02 stop would be 500 units = 50x the account
    assert d.approved and d.position_size * 100.0 <= 300.0 + 1e-6


def test_size_multiplier_shrinks_but_never_vetoes_or_grows(risk_cfg):
    base = RiskEngine(1000).evaluate("BUY", 100.0, 2.0)
    half = RiskEngine(1000).evaluate("BUY", 100.0, 2.0, size_multiplier=0.5)
    assert half.approved and half.position_size == pytest.approx(base.position_size / 2, rel=1e-3)
    assert RiskEngine(1000).evaluate("BUY", 100.0, 2.0, size_multiplier=5).position_size == base.position_size


def test_nan_atr_and_bad_price_are_rejected(risk_cfg):
    assert not RiskEngine(1000).evaluate("BUY", 100.0, float("nan")).approved
    assert not RiskEngine(1000).evaluate("BUY", 0.0, 2.0).approved


def test_zero_equity_trips_the_kill_switch_instead_of_dividing_by_zero(risk_cfg):
    r = RiskEngine(1000)
    r.update_equity(0)
    assert r.kill_switch_tripped


def _p(action="BUY", entry=100.0, stop=96.0):
    return {"action": action, "entry": entry, "stop": stop, "initial_stop": stop}


def test_stop_does_not_move_before_one_r(risk_cfg):
    assert manage_stop(_p(), 103.0, 2.0) is None


def test_stop_goes_to_breakeven_then_trails_and_never_retreats(risk_cfg):
    p = _p()
    s1 = manage_stop(p, 104.0, 1.0)             # +1R, trail 2*1=2 below -> 102
    assert s1 == 102.0
    p["stop"] = s1
    assert manage_stop(p, 103.0, 1.0) is None   # price pulled back: trailing candidate 101 < 102
    assert manage_stop(p, 108.0, 1.0) == 106.0


def test_breakeven_floor_for_longs_and_ceiling_for_shorts(risk_cfg):
    long_stop = manage_stop(_p(), 104.0, 3.0)   # price-6 = 98 < entry -> floor at entry
    assert long_stop == 100.0
    short = manage_stop({"action": "SELL", "entry": 100.0, "stop": 104.0, "initial_stop": 104.0}, 96.0, 3.0)
    assert short == 100.0


def test_trailing_can_be_disabled(risk_cfg, monkeypatch):
    monkeypatch.setattr(CFG, "trailing_stop_enabled", False)
    assert manage_stop(_p(), 120.0, 1.0) is None

# ---------------- db ----------------

def test_old_database_without_meta_column_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE open_positions (symbol TEXT PRIMARY KEY, ts REAL, action TEXT, entry REAL, size REAL,
                 stop_loss REAL, take_profit REAL, mode TEXT, protection_json TEXT)""")
    c.execute("INSERT INTO open_positions VALUES ('ETH/USDT',0,'BUY',100,1,95,110,'paper','{}')")
    c.commit()
    c.close()
    monkeypatch.setattr(config.CFG, "db_path", str(path))
    conn = db.connect()
    pos = db.load_open_positions(conn)[0]
    assert pos["symbol"] == "ETH/USDT" and pos["expert_scores"] == {} and pos["initial_stop"] == 95


def test_recent_closed_pnls_order_and_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config.CFG, "db_path", str(tmp_path / "x.db"))
    conn = db.connect()
    for pnl in (1.0, -2.0, 3.0):
        db.log_trade(conn, "ETH/USDT", "BUY", 1, 1, 1, 1, "paper", result="tp", pnl=pnl)
    db.log_trade(conn, "ETH/USDT", "BUY", 1, 1, 1, 1, "paper", result="open")
    assert db.recent_closed_pnls(conn, 2) == [-2.0, 3.0]

# ---------------- execution ----------------

class _Ex:
    def __init__(self, spot=True, fail_second=False):
        self.calls, self.cancelled, self.spot, self.fail_second = [], [], spot, fail_second
        self.options = {}

    def market(self, s):
        return {"limits": {"amount": {"min": 0.0001}}, "spot": self.spot}

    def amount_to_precision(self, s, a):
        return a

    def featureValue(self, *a):
        return True

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append((side, price, params))
        if self.fail_second and len(self.calls) == 2:
            raise ccxt.ExchangeError("rejected")
        return {"id": str(len(self.calls)), "average": 100.0}

    def cancel_order(self, oid, symbol):
        self.cancelled.append(oid)


def test_market_buy_passes_the_reference_price_for_exchanges_that_need_it():
    ex = _Ex()
    execution.LiveExecutor().open(ex, "ETH/USDT", "BUY", 1.0, 2500.0)
    assert ex.calls[0][1] == 2500.0   # HTX computes cost = amount * price, ccxt refuses without it


def test_market_sell_does_not_get_a_price():
    ex = _Ex()
    execution.LiveExecutor().close(ex, "ETH/USDT", "BUY", 1.0, 2500.0)
    assert ex.calls[0][1] is None


def test_buy_price_can_be_switched_off_via_exchange_option():
    ex = _Ex()
    ex.options["createMarketBuyOrderRequiresPrice"] = False
    execution.LiveExecutor().open(ex, "ETH/USDT", "BUY", 1.0, 2500.0)
    assert ex.calls[0][1] is None


def test_reduce_only_is_not_sent_on_spot_but_is_on_derivatives():
    spot, swap = _Ex(spot=True), _Ex(spot=False)
    execution.LiveExecutor().place_protection(spot, "ETH/USDT", "BUY", 1, 90, 120)
    execution.LiveExecutor().place_protection(swap, "ETH/USDT", "BUY", 1, 90, 120)
    assert "reduceOnly" not in spot.calls[0][2]
    assert swap.calls[0][2]["reduceOnly"] is True


def test_half_a_bracket_is_cancelled_when_the_take_profit_leg_fails():
    ex = _Ex(fail_second=True)
    alerts = []
    out = execution.LiveExecutor(alerts.append).place_protection(ex, "ETH/USDT", "BUY", 1, 90, 120)
    assert out is None
    assert ex.cancelled == ["1"]    # the orphaned stop must not survive


def test_protection_triggered_detects_a_native_fill_and_ignores_open_orders():
    class E:
        def fetch_order(self, oid, symbol):
            return {"status": "closed", "average": 95.0} if oid == "sl1" else {"status": "open"}
    ex = execution.LiveExecutor()
    assert ex.protection_triggered(E(), "ETH/USDT", {"stop": {"id": "sl1"}, "take": {"id": "tp1"}}) == ("stop", 95.0)
    assert ex.protection_triggered(E(), "ETH/USDT", {"stop": {"id": "x"}, "take": {"id": "tp1"}}) is None
    assert ex.protection_triggered(E(), "ETH/USDT", {"software_only": True}) is None
    assert ex.protection_triggered(E(), "ETH/USDT", {}) is None

# ---------------- telegram ----------------

class _R:
    def __init__(self, d):
        self.d = d

    def raise_for_status(self):
        pass

    def json(self):
        return self.d


def test_long_messages_are_truncated_to_telegrams_limit(monkeypatch):
    monkeypatch.setattr(CFG, "telegram_bot_token", "T")
    monkeypatch.setattr(CFG, "telegram_chat_id", "1")
    sent = {}
    monkeypatch.setattr(telegram_control.requests, "post", lambda url, timeout=None, json=None: sent.update(json) or _R({}))
    telegram_control.send("x" * 9000)
    assert len(sent["text"]) <= 4096


def test_drain_discards_commands_queued_while_offline(monkeypatch):
    monkeypatch.setattr(CFG, "telegram_bot_token", "T")
    monkeypatch.setattr(CFG, "telegram_chat_id", "1")
    batches = [{"result": [{"update_id": 7, "message": {"chat": {"id": 1}, "text": "/kill"}}]}, {"result": []}]
    monkeypatch.setattr(telegram_control.requests, "get", lambda url, params=None, timeout=None: _R(batches.pop(0)))
    p = telegram_control.CommandPoller()
    assert p.drain() == 1 and p.offset == 8
    monkeypatch.setattr(telegram_control.requests, "get", lambda url, params=None, timeout=None: _R({"result": []}))
    assert p.poll() == []

# ---------------- data ----------------

def _raw(n, start_ms=1_700_000_000_000, step=3_600_000):
    return [[start_ms + i * step, 100 + i, 101 + i, 99 + i, 100.5 + i, 10] for i in range(n)]


def test_clean_rejects_nonpositive_prices_and_short_history():
    bad = _raw(70)
    bad[5][4] = 0
    with pytest.raises(ValueError):
        data._clean_ohlcv(bad, "ETH/USDT", "1h", 60)
    with pytest.raises(ValueError):
        data._clean_ohlcv(_raw(10), "ETH/USDT", "1h", 60)


def test_clean_sorts_and_dedupes():
    raw = _raw(70)
    raw = raw[::-1] + [raw[3]]
    df = data._clean_ohlcv(raw, "ETH/USDT", "1h", 60)
    assert df["ts"].is_monotonic_increasing and len(df) == 70


def test_history_pages_forward_until_it_has_enough(monkeypatch):
    period = 3_600_000
    now_ms = 1_800_000_000_000
    monkeypatch.setattr(data.time, "time", lambda: now_ms / 1000)
    universe = [[now_ms - (1500 - i) * period, 1 + i, 2 + i, 1, 1.5 + i, 5] for i in range(1500)]

    class Ex:
        calls = 0

        def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
            Ex.calls += 1
            return [c for c in universe if c[0] >= since][:500]    # exchange caps every page at 500
    df = data.fetch_ohlcv_history(Ex(), "ETH/USDT", "1h", 1200)
    assert len(df) == 1200 and Ex.calls >= 3
    assert df["ts"].is_monotonic_increasing


def test_history_error_is_wrapped(monkeypatch):
    monkeypatch.setattr(data.time, "sleep", lambda s: None)

    class Ex:
        def fetch_ohlcv(self, *a, **k):
            raise ccxt.NetworkError("down")
    with pytest.raises(RuntimeError):
        data.fetch_ohlcv_history(Ex(), "ETH/USDT", "1h", 100)


def test_fetch_df_does_not_sleep_after_the_last_failed_attempt(monkeypatch):
    sleeps = []
    monkeypatch.setattr(data.time, "sleep", sleeps.append)

    class Ex:
        def fetch_ohlcv(self, *a, **k):
            raise ccxt.NetworkError("down")
    with pytest.raises(RuntimeError):
        data.fetch_ohlcv_df(Ex(), "ETH/USDT", "1h")
    assert len(sleeps) == 2    # 3 attempts -> 2 backoffs, not 3
    assert isinstance(pd.Timestamp("2026-01-01"), pd.Timestamp)

# ---------------- config ----------------

def test_validate_flags_dangerous_settings(monkeypatch):
    c = config.Config(risk_per_trade_pct=25, max_exposure_pct=10, atr_stop_mult=3, atr_take_mult=1, poll_seconds=1,
                      starting_capital=0, dashboard_port=8787, dashboard_host="0.0.0.0")
    text = " ".join(c.validate())
    for needle in ("RISK_PER_TRADE_PCT", "ATR_TAKE_MULT", "POLL_SECONDS", "STARTING_CAPITAL", "DASHBOARD_HOST"):
        assert needle in text


def test_validate_passes_for_sane_defaults():
    assert config.Config(live_trading=False).validate() == []


def test_live_flag_without_unlock_is_called_out():
    assert any("LIVE_TRADING" in p for p in config.Config(live_trading=True).validate())
