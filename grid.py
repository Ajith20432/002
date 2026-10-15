"""
grid.py — spot grid trading (historical paper simulation).

An arithmetic grid of GRID_LEVELS levels spans start_price * (1 +/- GRID_RANGE_PCT/100). Each slot i
buys at level i and sells at level i+1; every completed round trip banks one grid step minus fees.
It profits from sideways chop and loses to a sustained trend, so it has hard limits:

  * only GRID_CAPITAL_PCT of equity is ever allocated (split evenly across slots, fixed size);
  * if price closes GRID_STOP_PCT below the grid floor the grid liquidates its inventory and STOPS —
    a grid with no floor just accumulates a falling asset;
  * within one candle sells are processed before buys and a slot bought on a candle cannot also
    sell on that same candle (pessimistic fill ordering).
Maker-style limit fills at the level price, fees from FEE_RATE, no slippage. No live orders here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from config import CFG


@dataclass
class GridResult:
    round_trips: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees: float = 0.0
    stopped: bool = False
    equity_curve: list[float] = field(default_factory=list)
    final_equity: float = 0.0
    buy_and_hold_pct: float = 0.0
    levels: list[float] = field(default_factory=list)
    pnls: list[float] = field(default_factory=list)   # one entry per completed round trip


def build_levels(center: float, n_levels: int | None = None, range_pct: float | None = None) -> list[float]:
    n = CFG.grid_levels if n_levels is None else n_levels
    rng = CFG.grid_range_pct if range_pct is None else range_pct
    if n < 2 or rng <= 0 or center <= 0:
        raise ValueError("grid needs >= 2 levels, a positive range and a positive price")
    lo, hi = center * (1 - rng / 100), center * (1 + rng / 100)
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def simulate_grid(df: pd.DataFrame, start_equity: float | None = None) -> GridResult:
    equity0 = start_equity if start_equity is not None else CFG.starting_capital
    res = GridResult()
    if df is None or len(df) == 0:
        res.final_equity = equity0
        return res
    levels = build_levels(float(df["close"].iloc[0]))
    res.levels = levels
    slots = len(levels) - 1
    per_slot = equity0 * CFG.grid_capital_pct / 100 / slots
    floor = levels[0]
    stop_px = floor * (1 - CFG.grid_stop_pct / 100)
    cash = equity0
    qty = [0.0] * slots            # inventory held by slot i (bought at levels[i], to sell at levels[i+1])
    fee_rate = CFG.fee_rate
    liquidated = False

    for row in df.itertuples(index=False):
        high, low, close = float(row.high), float(row.low), float(row.close)
        if not liquidated:
            sold_or_bought_now = set()
            for i in range(slots):                                   # sells first
                if qty[i] > 0 and high >= levels[i + 1]:
                    proceeds = qty[i] * levels[i + 1]
                    fee_sell = proceeds * fee_rate
                    cost = qty[i] * levels[i]
                    fee_buy = cost * fee_rate
                    cash += proceeds - fee_sell
                    pnl = proceeds - cost - fee_sell - fee_buy
                    res.realized_pnl += pnl
                    res.fees += fee_sell + fee_buy
                    res.pnls.append(pnl)
                    res.round_trips += 1
                    qty[i] = 0.0
                    sold_or_bought_now.add(i)
            for i in range(slots):                                   # then buys
                if qty[i] == 0 and i not in sold_or_bought_now and low <= levels[i] and cash >= per_slot * (1 + fee_rate):
                    qty[i] = per_slot / levels[i]
                    cash -= per_slot * (1 + fee_rate)
            if close < stop_px:
                inv = sum(qty)
                if inv > 0:
                    proceeds = inv * close
                    fee_sell = proceeds * fee_rate
                    cost = sum(q * levels[i] for i, q in enumerate(qty))
                    pnl = proceeds - cost - fee_sell - cost * fee_rate
                    cash += proceeds - fee_sell
                    res.realized_pnl += pnl
                    res.fees += fee_sell + cost * fee_rate
                    res.pnls.append(pnl)
                    qty = [0.0] * slots
                liquidated = True
                res.stopped = True
        equity = cash + sum(qty) * close
        res.equity_curve.append(equity)

    last = float(df["close"].iloc[-1])
    held_cost = sum(q * levels[i] for i, q in enumerate(qty))
    res.unrealized_pnl = sum(qty) * last - held_cost
    res.final_equity = res.equity_curve[-1]
    res.buy_and_hold_pct = (last / float(df["close"].iloc[0]) - 1) * 100
    return res
