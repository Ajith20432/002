"""dca.py — capped DCA cycle simulation (historical only). Fixed order sizes, hard stop, never martingale."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from config import CFG
from costs import COST_MODEL


@dataclass
class DCAResult:
    cycles: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    final_equity: float = 0.0
    max_capital_used_pct: float = 0.0


def simulate_dca(df: pd.DataFrame) -> DCAResult:
    equity = CFG.starting_capital
    curve, cycles = [equity], []
    max_used = 0.0
    i = 0
    while i < len(df) - 1:
        entry = float(df.iloc[i].close)
        spent = equity * CFG.dca_base_order_pct / 100
        qty, avg, buys, peak_spend = spent / entry, entry, 1, spent
        reason, exit_price, j = "end_of_data", float(df.iloc[-1].close), i + 1
        while j < len(df):
            p = float(df.iloc[j].close)
            if p >= avg * (1 + CFG.dca_take_profit_pct / 100):
                reason, exit_price = "take", p
                break
            if p <= avg * (1 - CFG.dca_stop_loss_pct / 100):
                reason, exit_price = "stop", p
                break
            if buys <= CFG.dca_max_safety_orders and p <= avg * (1 - CFG.dca_dip_pct / 100):
                spend = equity * CFG.dca_base_order_pct / 100      # fixed size: never scaled up on losses
                new_qty = spend / p
                avg = (avg * qty + p * new_qty) / (qty + new_qty)
                qty += new_qty
                peak_spend += spend
                buys += 1
            j += 1
        pnl = (exit_price - avg) * qty - COST_MODEL.round_trip_fee(avg, exit_price, qty)
        equity += pnl
        curve.append(equity)
        max_used = max(max_used, peak_spend / CFG.starting_capital * 100)
        cycles.append({"reason": reason, "pnl": pnl, "buys": buys, "avg_entry": avg})
        if reason == "end_of_data":
            break
        i = max(j + 1, i + 1)
    return DCAResult(cycles, curve, equity, max_used)
