"""Production validation harness for ZORA.

Runs deterministic long-window paper stress tests, execution-cost sensitivity,
and native-protection/multi-exchange contract checks without placing real orders.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import asdict, dataclass
from types import SimpleNamespace

import pandas as pd

import backtester
from config import CFG
from costs import ExecutionCost
from metrics import compute_metrics


@dataclass
class ValidationCase:
    name: str
    candles: int
    trades: int
    final_equity: float
    return_pct: float
    max_drawdown_pct: float
    profit_factor: float
    kill_switch: bool
    passed: bool
    note: str = ""


def synthetic_ohlcv(candles: int = 30 * 24 * 4, seed: int = 7) -> pd.DataFrame:
    """Deterministic 15m stress data: trend regimes + volatility shocks."""
    rng = random.Random(seed)
    rows, price = [], 2000.0
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    for i in range(candles):
        regime = math.sin(i / 180.0) * 0.0008
        shock = (rng.random() - 0.5) * 0.003
        if i % 480 in range(460, 480):
            shock += (rng.random() - 0.5) * 0.012
        op = price
        close = max(100.0, op * (1.0 + regime + shock))
        hi = max(op, close) * (1.0 + rng.random() * 0.0015)
        lo = min(op, close) * (1.0 - rng.random() * 0.0015)
        rows.append((ts, op, hi, lo, close, max(1.0, 1000 + rng.random() * 500)))
        price, ts = close, ts + pd.Timedelta(minutes=15)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    # A conservative ATR proxy sufficient for the pure paper engine.
    tr = pd.concat([(df.high - df.low), (df.high - df.close.shift()).abs(), (df.low - df.close.shift()).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean().bfill()
    return df


def signal_factory(df: pd.DataFrame):
    def signal(i: int):
        if i < 60:
            return SimpleNamespace(action="HOLD", confidence=0, expert_scores={}, factor_scores_by_tf={})
        fast = df.close.iloc[max(0, i - 8):i + 1].mean()
        slow = df.close.iloc[max(0, i - 32):i + 1].mean()
        if fast > slow * 1.001:
            action = "BUY"
        elif fast < slow * 0.999 and CFG.allow_shorts:
            action = "SELL"
        else:
            action = "HOLD"
        return SimpleNamespace(action=action, confidence=65 if action != "HOLD" else 0,
                               expert_scores={"paper": 65}, factor_scores_by_tf={"15m": {"trend": 65}})
    return signal


def max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else CFG.starting_capital
    worst = 0.0
    for x in equity:
        peak = max(peak, x)
        worst = max(worst, (peak - x) / max(peak, 1e-9) * 100.0)
    return worst


def profit_factor(pnls: list[float]) -> float:
    gains = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def run_case(name: str, candles: int, cost: ExecutionCost, seed: int = 7) -> ValidationCase:
    df = synthetic_ohlcv(candles, seed)
    with backtester.override_cfg(starting_capital=1000.0, risk_per_trade_pct=0.5,
                                 max_exposure_pct=30.0, max_daily_loss_pct=3.0,
                                 max_drawdown_pct=15.0, trailing_stop_enabled=False):
        old = backtester.COST_MODEL
        backtester.COST_MODEL = cost
        try:
            res = backtester.simulate(df, signal_factory(df))
        finally:
            backtester.COST_MODEL = old
    dd = max_drawdown(res.equity_curve)
    pf = profit_factor(res.pnls)
    passed = bool(res.trades) and not res.kill_switch_tripped and dd <= 15.0 and math.isfinite(res.final_equity)
    return ValidationCase(name, candles, len(res.trades), res.final_equity,
                          (res.final_equity / 1000.0 - 1.0) * 100.0, dd, pf,
                          res.kill_switch_tripped, passed,
                          "synthetic stress window; not a profitability guarantee")


def native_protection_contract() -> dict:
    """Mock exchange test: both native protection orders must be armed or the gate refuses."""
    calls = []
    ex = SimpleNamespace(id="mock", options={}, market=lambda s: {"spot": False},
                         featureValue=lambda *a: True,
                         create_order=lambda *a: calls.append(a) or {"id": str(len(calls))})
    # Keep the offline validation/replay path importable without ccxt installed.
    # The live executor is needed only for this explicit native-protection contract check.
    from execution import LiveExecutor
    out = LiveExecutor().place_protection(ex, "ETH/USDT", "BUY", 1.0, 1900.0, 2200.0)
    return {"passed": bool(out and len(calls) == 2), "orders": len(calls)}


def walk_forward_check(days: int = 30, folds: int = 3) -> dict:
    """Run the same deterministic strategy on sequential train/test-style windows.

    The signal itself is deliberately parameter-free here; the purpose is to detect
    regime collapse across unseen chronological segments rather than optimize them.
    """
    total = max(120, days * 24 * 4)
    fold_size = max(40, total // max(1, folds))
    results = []
    for fold in range(max(1, folds)):
        start = fold * fold_size
        end = min(total, start + fold_size)
        if end - start < 40:
            continue
        df = synthetic_ohlcv(total, seed=7 + fold)
        segment = df.iloc[start:end].reset_index(drop=True)
        if len(segment) < 80:
            continue
        # Warm up inside each chronological test segment to avoid carrying state.
        with backtester.override_cfg(starting_capital=1000.0, risk_per_trade_pct=0.5,
                                     max_exposure_pct=30.0, max_daily_loss_pct=3.0,
                                     max_drawdown_pct=15.0, trailing_stop_enabled=False,
                                     allow_shorts=True):
            res = backtester.simulate(segment, signal_factory(segment))
        m = compute_metrics(res.pnls, res.equity_curve, 1000.0)
        results.append({"fold": fold + 1, "trades": m["trades"], "return_pct": m["return_pct"],
                        "profit_factor": m["profit_factor"], "max_drawdown_pct": m["max_drawdown_pct"],
                        "passed": bool(m["trades"] >= 5 and m["profit_factor"] >= 0.9 and m["max_drawdown_pct"] <= 15.0)})
    return {"folds": results, "passed": bool(results) and all(x["passed"] for x in results)}


def run_all(days: int = 30) -> dict:
    candles = days * 24 * 4
    cases = [
        run_case("baseline", candles, ExecutionCost(CFG.fee_rate, CFG.slippage_bps, CFG.execution_latency_ms)),
        run_case("high_cost", candles, ExecutionCost(max(CFG.fee_rate, 0.002), max(CFG.slippage_bps, 15.0), 750)),
        run_case("stress_2x_cost", candles, ExecutionCost(CFG.fee_rate * 2, CFG.slippage_bps * 2, CFG.execution_latency_ms * 2)),
    ]
    protection = native_protection_contract()
    walk_forward = walk_forward_check(days, folds=3)
    by_name = {c.name: c for c in cases}
    warnings = []
    if by_name["high_cost"].return_pct <= 0:
        warnings.append("return is not positive under the high-cost case: the edge may exist only at unrealistically low costs")
    if by_name["stress_2x_cost"].profit_factor < 1.0:
        warnings.append("profit factor < 1 when fees/slippage/latency are doubled")
    if min(c.trades for c in cases) < 30:
        warnings.append("fewer than 30 trades in a case: statistics are not meaningful, use a longer --days window")
    return {"days": days, "cases": [asdict(c) for c in cases],
            "native_protection_contract": protection,
            "walk_forward": walk_forward,
            "warnings": warnings,
            "overall_pass": all(c.passed for c in cases) and protection["passed"] and walk_forward["passed"],
            "disclaimer": "Synthetic validation only. Run historical paper validation and exchange sandbox/public smoke tests before live trading."}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ZORA production validation harness")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv)
    report = run_all(max(1, args.days))
    # profit factor can be infinity (no losing trade); JSON has no Infinity, so print it as null
    print(json.dumps(report, indent=2, default=str).replace("Infinity", "null"))
    return 0 if report["overall_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
