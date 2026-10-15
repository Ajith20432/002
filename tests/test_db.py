import config
import db


def _fresh_conn(tmp_path, monkeypatch, name="test.db"):
    monkeypatch.setattr(config.CFG, "db_path", str(tmp_path / name))
    return db.connect()


def test_connect_creates_all_three_tables(tmp_path, monkeypatch):
    conn = _fresh_conn(tmp_path, monkeypatch)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trades", "equity_curve", "decision_log"} <= tables


def test_connect_is_idempotent_when_called_again_on_the_same_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config.CFG, "db_path", str(tmp_path / "test.db"))
    db.connect()
    db.connect()  # CREATE TABLE IF NOT EXISTS -> must not raise on the second call


def test_log_trade_and_log_equity_then_summary_roundtrip(tmp_path, monkeypatch):
    conn = _fresh_conn(tmp_path, monkeypatch)
    db.log_trade(conn, "BTC/USDT", "BUY", 100.0, 1.0, 98.0, 105.0, "paper", result="open")
    db.log_trade(conn, "BTC/USDT", "BUY", 105.0, 1.0, 98.0, 105.0, "paper", result="tp", pnl=5.0)
    db.log_equity(conn, 1005.0)

    s = db.summary(conn)
    assert s["closed_trades"] == 1        # the still-"open" row must not count as closed
    assert s["realized_pnl"] == 5.0
    assert s["latest_equity"] == 1005.0


def test_summary_on_an_empty_db_falls_back_to_starting_capital(tmp_path, monkeypatch):
    monkeypatch.setattr(config.CFG, "starting_capital", 500.0)
    conn = _fresh_conn(tmp_path, monkeypatch)
    s = db.summary(conn)
    assert s["closed_trades"] == 0
    assert s["realized_pnl"] == 0
    assert s["latest_equity"] == 500.0


def test_open_trades_are_excluded_from_realized_pnl(tmp_path, monkeypatch):
    conn = _fresh_conn(tmp_path, monkeypatch)
    db.log_trade(conn, "BTC/USDT", "BUY", 100.0, 1.0, 98.0, 105.0, "paper", result="open")
    db.log_trade(conn, "ETH/USDT", "SELL", 50.0, 2.0, 52.0, 46.0, "paper", result="open")
    s = db.summary(conn)
    assert s["closed_trades"] == 0
    assert s["realized_pnl"] == 0


class _FakeSignal:
    action = "BUY"
    confidence = 42.0


def test_log_decision_stores_the_signals_fields(tmp_path, monkeypatch):
    conn = _fresh_conn(tmp_path, monkeypatch)
    db.log_decision(conn, "ETH/USDT", _FakeSignal(), True, "ok", "fake ai note")
    row = conn.execute(
        "SELECT symbol, action, confidence, approved, reason, llm_note FROM decision_log"
    ).fetchone()
    assert tuple(row) == ("ETH/USDT", "BUY", 42.0, 1, "ok", "fake ai note")


def test_log_decision_stores_rejection_reason_when_not_approved(tmp_path, monkeypatch):
    conn = _fresh_conn(tmp_path, monkeypatch)
    db.log_decision(conn, "BTC/USDT", _FakeSignal(), False, "max exposure limit reached")
    row = conn.execute("SELECT approved, reason FROM decision_log").fetchone()
    assert tuple(row) == (0, "max exposure limit reached")


def test_summary_sums_pnl_across_multiple_closed_trades(tmp_path, monkeypatch):
    conn = _fresh_conn(tmp_path, monkeypatch)
    db.log_trade(conn, "BTC/USDT", "BUY", 100.0, 1.0, 98.0, 105.0, "paper", result="tp", pnl=5.0)
    db.log_trade(conn, "ETH/USDT", "SELL", 50.0, 1.0, 52.0, 46.0, "paper", result="sl", pnl=-2.0)
    s = db.summary(conn)
    assert s["closed_trades"] == 2
    assert s["realized_pnl"] == 3.0
