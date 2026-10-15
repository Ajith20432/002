"""
metrics.py — performance statistics for backtests, the optimizer, the dashboard and /metrics.

Pure functions, no I/O. Sharpe/Sortino are per-observation (not annualised): they are meant
for comparing runs of the same strategy against each other, not for marketing.
"""
from __future__ import annotations

import math
import random
from collections.abc import Sequence


def profit_factor(pnls: Sequence[float]) -> float:
    """Gross profit / gross loss. inf when there are wins and no losses; 0.0 when there are no wins."""
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return math.inf if wins > 0 else 0.0
    return wins / losses


def max_drawdown_pct(equity: Sequence[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak * 100)
    return worst


def _returns(curve: Sequence[float]) -> list[float]:
    return [(b - a) / a for a, b in zip(curve, curve[1:], strict=False) if a > 0]


def sharpe_ratio(curve: Sequence[float]) -> float:
    r = _returns(curve)
    if len(r) < 2:
        return 0.0
    mean = sum(r) / len(r)
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1))
    return mean / sd if sd > 1e-12 else 0.0


def sortino_ratio(curve: Sequence[float]) -> float:
    r = _returns(curve)
    if len(r) < 2:
        return 0.0
    mean = sum(r) / len(r)
    downside = math.sqrt(sum(min(x, 0.0) ** 2 for x in r) / len(r))
    return mean / downside if downside > 1e-12 else 0.0


def max_consecutive(pnls: Sequence[float], negative: bool = True) -> int:
    """Longest consecutive loss/win streak. Zero PnL breaks both streaks."""
    best = run = 0
    for p in pnls:
        hit = p < 0 if negative else p > 0
        run = run + 1 if hit else 0
        best = max(best, run)
    return best


def recovery_factor(total_pnl: float, max_dd_pct: float, starting_equity: float) -> float:
    """Net profit divided by drawdown expressed in currency terms."""
    if starting_equity <= 0 or max_dd_pct <= 1e-12:
        return math.inf if total_pnl > 0 else 0.0
    dd_cash = starting_equity * max_dd_pct / 100.0
    return total_pnl / dd_cash


def calmar_ratio(total_pnl: float, max_dd_pct: float, starting_equity: float) -> float:
    """Simple-period return divided by max drawdown; intentionally not annualised."""
    if starting_equity <= 0 or max_dd_pct <= 1e-12:
        return 0.0
    return (total_pnl / starting_equity * 100.0) / max_dd_pct


def monte_carlo_drawdown(pnls: Sequence[float], starting_equity: float = 0.0,
                        runs: int = 1000, seed: int = 17) -> dict:
    """Shuffle trade order to estimate drawdown risk; not a prediction of future returns."""
    trades = list(float(x) for x in pnls)
    if not trades or starting_equity <= 0:
        return {"runs": 0, "p50_max_drawdown_pct": 0.0, "p95_max_drawdown_pct": 0.0,
                "p99_max_drawdown_pct": 0.0}
    rng = random.Random(seed)
    dds = []
    for _ in range(max(100, int(runs))):
        shuffled = trades[:]
        rng.shuffle(shuffled)
        equity = peak = starting_equity
        worst = 0.0
        for pnl in shuffled:
            equity += pnl
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / max(peak, 1e-12) * 100.0)
        dds.append(worst)
    dds.sort()
    q = lambda pct: dds[min(len(dds) - 1, int((len(dds) - 1) * pct))]
    return {"runs": len(dds), "p50_max_drawdown_pct": q(0.50),
            "p95_max_drawdown_pct": q(0.95), "p99_max_drawdown_pct": q(0.99)}


def compute_metrics(pnls: Sequence[float], equity_curve: Sequence[float] | None = None,
                    starting_equity: float = 0.0) -> dict:
    """Headline numbers. If no equity curve is given one is rebuilt from the trade PnLs."""
    pnls = list(pnls)
    curve = list(equity_curve) if equity_curve else []
    if not curve and starting_equity > 0:
        eq = starting_equity
        curve = [eq]
        for p in pnls:
            eq += p
            curve.append(eq)
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    total = sum(pnls)
    base = starting_equity if starting_equity > 0 else (curve[0] if curve else 0.0)
    return {
        "trades": n,
        "win_rate_pct": len(wins) / n * 100 if n else 0.0,
        "total_pnl": total,
        "return_pct": total / base * 100 if base > 0 else 0.0,
        "profit_factor": profit_factor(pnls),
        "expectancy": total / n if n else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": avg_win / abs(avg_loss) if avg_loss else 0.0,
        "max_drawdown_pct": max_drawdown_pct(curve) if curve else 0.0,
        "sharpe": sharpe_ratio(curve),
        "sortino": sortino_ratio(curve),
        "max_consecutive_losses": max_consecutive(pnls, True),
        "max_consecutive_wins": max_consecutive(pnls, False),
        "recovery_factor": recovery_factor(total, max_drawdown_pct(curve) if curve else 0.0, base),
        "calmar": calmar_ratio(total, max_drawdown_pct(curve) if curve else 0.0, base),
        "monte_carlo": monte_carlo_drawdown(pnls, base),
    }


def format_metrics(m: dict) -> str:
    pf = m.get("profit_factor")
    pf_txt = "inf" if pf is None or pf == math.inf else f"{pf:.2f}"
    return (f"trades={m['trades']} win%={m['win_rate_pct']:.1f} PF={pf_txt} "
            f"expectancy={m['expectancy']:+.3f} pnl={m['total_pnl']:+.2f} ret={m['return_pct']:+.2f}% "
            f"maxDD={m['max_drawdown_pct']:.2f}% sharpe={m['sharpe']:.2f} sortino={m['sortino']:.2f}")
