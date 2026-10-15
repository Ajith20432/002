"""Backtest/learn helpers: resampling and the no-look-ahead guarantee of make_signal_fn."""
import pandas as pd
import pytest
from conftest import make_candles

import bot
from config import CFG


def _raw(n=600):
    df = make_candles(n, vol=0.004)
    return df[["ts", "open", "high", "low", "close", "volume"]]


def test_resample_aggregates_ohlcv_correctly():
    raw = _raw(48)[["ts", "open", "high", "low", "close", "volume"]]
    h = bot.resample_ohlcv(raw, "4h")
    first = raw.iloc[:4]
    ts0 = h.iloc[0]["ts"]
    chunk = raw[(raw["ts"] >= ts0) & (raw["ts"] < ts0 + pd.Timedelta(hours=4))]
    assert h.iloc[0]["high"] == chunk["high"].max() and h.iloc[0]["low"] == chunk["low"].min()
    assert h.iloc[0]["volume"] == pytest.approx(chunk["volume"].sum())
    assert len(first) == 4


def test_signal_fn_ignores_the_future(monkeypatch):
    monkeypatch.setattr(CFG, "timeframes", ("1h", "4h"))
    from brain import AdaptiveBrain
    raw = _raw(700)
    brain = AdaptiveBrain(state_path="/tmp/zora_test_brain_replay.json")
    a = bot.make_signal_fn(raw, brain=brain)
    i = 500
    sig_a = a(i)
    mutated = raw.copy()
    mutated.loc[i + 1:, ["open", "high", "low", "close"]] *= 3.0     # wildly different future
    b = bot.make_signal_fn(mutated, brain=brain)
    sig_b = b(i)
    assert sig_a.action == sig_b.action and sig_a.expert_scores == sig_b.expert_scores


def test_cli_parser_accepts_documented_commands():
    p = bot.build_parser()
    assert p.parse_args(["backtest", "ETH/USDT", "800"]).candles == 800
    assert p.parse_args(["run", "--once"]).once is True
    for cmd in ("doctor", "status", "brain", "metrics", "dashboard", "ai-health", "ai-test", "live", "learn", "dca", "grid", "optimize"):
        assert p.parse_args([cmd]).command == cmd
    args, rest = p.parse_known_args(["validate", "--days", "5"])
    assert args.command == "validate" and rest == ["--days", "5"]


def test_validate_forwards_its_flags(monkeypatch, capsys):
    import validation
    seen = {}
    monkeypatch.setattr(validation, "main", lambda argv=None: seen.setdefault("argv", argv) and 0)
    assert bot.main(["validate", "--days", "7"]) == 0
    assert seen["argv"] == ["--days", "7"]
