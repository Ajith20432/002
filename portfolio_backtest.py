"""Portfolio-level historical replay built on ZORA's single-symbol simulation engine."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from backtester import simulate
from bot import _fetch_history, make_signal_fn
from config import CFG
from indicators import enrich
from metrics import compute_metrics


@dataclass
class SymbolResult:
    symbol: str
    candles: int
    allocation: float
    final_equity: float
    return_pct: float
    trades: int
    profit_factor: float
    max_drawdown_pct: float
    kill_switch: bool


def run(symbols: list[str], candles: int = 1500) -> dict:
    symbols = list(dict.fromkeys(s for s in symbols if s))
    if not symbols:
        raise ValueError("at least one symbol is required")
    total_capital = float(CFG.starting_capital)
    allocation = total_capital / len(symbols)
    results: list[SymbolResult] = []
    aggregate_pnls: list[float] = []
    aggregate_curve: list[float] = []
    for symbol in symbols:
        raw = _fetch_history(symbol, candles)
        df = enrich(raw)
        res = simulate(df, make_signal_fn(raw), starting_equity=allocation)
        m = compute_metrics(res.pnls, res.equity_curve, allocation)
        results.append(SymbolResult(symbol, len(df), allocation, res.final_equity, m["return_pct"],
                                    m["trades"], m["profit_factor"], m["max_drawdown_pct"],
                                    res.kill_switch_tripped))
        aggregate_pnls.extend(res.pnls)
        # Equal-length history is the normal case; when it differs, pad with the last equity.
        curve = list(res.equity_curve)
        if not aggregate_curve:
            aggregate_curve = curve
        else:
            n = max(len(aggregate_curve), len(curve))
            aggregate_curve += [0.0] * (n - len(aggregate_curve))
            curve += [curve[-1] if curve else allocation] * (n - len(curve))
            aggregate_curve = [a + b for a, b in zip(aggregate_curve, curve)]
    metrics = compute_metrics(aggregate_pnls, aggregate_curve, total_capital)
    return {
        "symbols": symbols,
        "candles": candles,
        "starting_equity": total_capital,
        "final_equity": total_capital + metrics["total_pnl"],
        "metrics": metrics,
        "equity_curve": aggregate_curve,
        "symbols_report": [asdict(x) for x in results],
        "passed": bool(results) and not any(x.kill_switch for x in results),
        "disclaimer": "Historical replay only; it does not predict future profitability and uses the configured exchange-cost model.",
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ZORA multi-symbol portfolio backtest")
    ap.add_argument("symbols", nargs="*", default=None)
    ap.add_argument("--candles", type=int, default=1500)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    symbols = args.symbols or list(CFG.symbols)
    report = run(symbols, max(120, args.candles))
    if args.json:
        print(json.dumps(report, indent=2, default=str).replace("Infinity", "null"))
    else:
        m = report["metrics"]
        print(f"PORTFOLIO {','.join(symbols)} | candles={args.candles} | equity {report['final_equity']:.2f} | return {m['return_pct']:+.2f}%")
        print(f"trades={m['trades']} win%={m['win_rate_pct']:.1f} PF={m['profit_factor'] if m['profit_factor'] != float('inf') else 'inf'} "
              f"maxDD={m['max_drawdown_pct']:.2f}% Sharpe={m['sharpe']:.2f} Sortino={m['sortino']:.2f}")
        for x in report["symbols_report"]:
            print(f"  {x['symbol']}: return={x['return_pct']:+.2f}% trades={x['trades']} PF={x['profit_factor']:.2f} DD={x['max_drawdown_pct']:.2f}%")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
