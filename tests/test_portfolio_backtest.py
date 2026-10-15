import pandas as pd

import portfolio_backtest


def test_portfolio_run_aggregates_symbols(monkeypatch):
    def fake_fetch(symbol, candles):
        ts = pd.date_range("2026-01-01", periods=140, freq="15min", tz="UTC")
        close = pd.Series([100 + i * (0.03 if symbol == "BTC/USDT" else 0.02) for i in range(140)])
        return pd.DataFrame({"ts": ts, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0})
    monkeypatch.setattr(portfolio_backtest, "_fetch_history", fake_fetch)
    monkeypatch.setattr(portfolio_backtest.CFG, "starting_capital", 1000.0)
    monkeypatch.setattr(portfolio_backtest.CFG, "timeframes", ("15m",))
    report = portfolio_backtest.run(["BTC/USDT", "ETH/USDT"], candles=140)
    assert report["starting_equity"] == 1000.0
    assert len(report["symbols_report"]) == 2
    assert "metrics" in report
