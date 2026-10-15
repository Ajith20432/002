"""
backtester.py — one pure simulation engine shared by `backtest`, `learn`, `paper-validate`
and `optimize`, so every number in the project comes from the same code path.

Realism rules (the old inline loop broke most of them):
  * exits use each candle's HIGH/LOW, not just its close — a stop that was touched intrabar
    is a stop that was hit. If one candle touches both stop and take, the stop wins (pessimistic).
  * a gap through the stop fills at the candle OPEN (worse than the stop), never at a better price.
  * fees + slippage come from the same COST_MODEL the live paper loop uses.
  * the daily-loss baseline resets every UTC day (the old loop never reset it, so a normal run
    tripped the "daily" kill switch after 3% cumulative loss and silently stopped testing).
  * a position still open at the end is closed at the last close instead of being dropped.
  * trailing / breakeven stops use the same risk.manage_stop as paper trading.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

import pandas as pd

from config import CFG
from costs import COST_MODEL
from risk import RiskEngine, manage_stop

WINDOW = 60  # candles of warm-up before the first signal


@dataclass
class SimResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    final_equity: float = 0.0
    kill_switch_tripped: bool = False

    @property
    def pnls(self) -> list[float]:
        return [t["pnl"] for t in self.trades]


@contextmanager
def override_cfg(**overrides):
    """Temporarily change CFG fields (restored even on error) — how the optimizer varies parameters."""
    saved = {k: getattr(CFG, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(CFG, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(CFG, k, v)


def _utc_day(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def simulate(
    df: pd.DataFrame,
    signal_fn: Callable[[int], object],
    start_idx: int = WINDOW,
    end_idx: int | None = None,
    starting_equity: float | None = None,
    on_close: Callable[[dict, float], None] | None = None,
) -> SimResult:
    """
    df: ENRICHED candles (needs open/high/low/close/atr14/ts).
    signal_fn(i): the Signal computed from data up to and including candle i.
    on_close(position, pnl): called after every closed trade (the `learn` hook).
    """
    equity = CFG.starting_capital if starting_equity is None else starting_equity
    end_idx = len(df) - 1 if end_idx is None else min(end_idx, len(df) - 1)
    risk = RiskEngine(equity)
    res = SimResult(final_equity=equity)
    position: dict | None = None
    last_day = None

    def close_position(exit_ref: float, reason: str, i: int) -> None:
        nonlocal equity, position
        side = "sell" if position["action"] == "BUY" else "buy"
        exit_price = COST_MODEL.fill_price(float(exit_ref), side)
        direction = 1 if position["action"] == "BUY" else -1
        gross = direction * (exit_price - position["entry"]) * position["size"]
        pnl = gross - COST_MODEL.round_trip_fee(position["entry"], exit_price, position["size"])
        equity += pnl
        risk.update_equity(equity)
        risk.remove_exposure(CFG.risk_per_trade_pct)
        res.trades.append({
            "action": position["action"], "entry": position["entry"], "exit": exit_price,
            "size": position["size"], "pnl": pnl, "reason": reason,
            "bars": i - position["bar"], "entry_ts": position["ts"], "exit_ts": df["ts"].iloc[i],
        })
        if on_close:
            on_close(position, pnl)
        position = None

    for i in range(start_idx, end_idx + 1):
        row = df.iloc[i]
        day = _utc_day(row["ts"])
        if last_day is not None and day != last_day:
            risk.reset_daily()
        last_day = day

        if position:
            o, h, lo = float(row["open"]), float(row["high"]), float(row["low"])
            long = position["action"] == "BUY"
            stop, take = position["stop"], position["take"]
            hit_stop = lo <= stop if long else h >= stop
            hit_take = h >= take if long else lo <= take
            if hit_stop:
                gap = min(o, stop) if long else max(o, stop)   # gapped through the stop -> fill at the open
                close_position(gap, "stop", i)
            elif hit_take:
                close_position(take, "take", i)
            else:
                new_stop = manage_stop(position, float(row["close"]), float(row["atr14"]))
                if new_stop is not None:
                    position["stop"] = new_stop

        mtm = equity
        if position:
            d = 1 if position["action"] == "BUY" else -1
            mtm += d * (float(row["close"]) - position["entry"]) * position["size"]
        res.equity_curve.append(mtm)

        if risk.kill_switch_tripped:
            res.kill_switch_tripped = True
            break

        if i >= end_idx:
            break  # no entering on the last candle: it could never be exited inside this window
        if not position:
            signal = signal_fn(i)
            price = float(row["close"])
            decision = risk.evaluate(signal.action, price, float(row["atr14"]))
            if decision.approved:
                risk.add_exposure(CFG.risk_per_trade_pct)
                entry = COST_MODEL.fill_price(price, "buy" if signal.action == "BUY" else "sell")
                position = {
                    "action": signal.action, "entry": entry, "size": decision.position_size,
                    "stop": decision.stop_loss, "initial_stop": decision.stop_loss, "take": decision.take_profit,
                    "expert_scores": signal.expert_scores, "factor_scores_by_tf": signal.factor_scores_by_tf,
                    "bar": i, "ts": row["ts"],
                }

    if position:  # never silently drop an open trade
        close_position(float(df["close"].iloc[min(end_idx, len(df) - 1)]), "end", min(end_idx, len(df) - 1))
        if res.equity_curve:
            res.equity_curve[-1] = equity

    res.final_equity = equity
    res.kill_switch_tripped = res.kill_switch_tripped or risk.kill_switch_tripped
    return res
