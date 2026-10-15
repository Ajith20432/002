import json
from pathlib import Path

import pandas as pd

from historical_cache import load, save
from replay_report import build_html


def test_history_cache_roundtrip(tmp_path):
    df = pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC"),
        "open": [1,2,3], "high": [2,3,4], "low": [0.5,1.5,2.5], "close": [1.5,2.5,3.5], "volume": [10,20,30]
    })
    path = save(df, "BTC/USDT", "1h", tmp_path)
    got = load("BTC/USDT", "1h", tmp_path, min_rows=3)
    assert path.exists()
    assert len(got) == 3
    assert float(got.iloc[-1].close) == 3.5


def test_replay_report_contains_curve_and_symbols():
    report = {"symbols": ["BTC/USDT"], "candles": 100, "final_equity": 1010,
              "passed": True, "equity_curve": [1000, 1005, 1010],
              "metrics": {"return_pct": 1, "trades": 2, "win_rate_pct": 50, "profit_factor": 2,
                          "max_drawdown_pct": 0.2, "sharpe": 1, "sortino": 1},
              "symbols_report": [{"symbol": "BTC/USDT", "candles": 100, "return_pct": 1,
                                  "trades": 2, "profit_factor": 2, "max_drawdown_pct": .2, "kill_switch": False}]}
    html = build_html(report)
    assert "Portfolio equity curve" in html
    assert "BTC/USDT" in html
    assert "polyline" in html
