"""Read-only paper-trading performance gate from ZORA's SQLite journal."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

import db
from config import CFG
from metrics import compute_metrics


def build_report(days: int = 30) -> dict:
    conn = db.connect()
    try:
        since = time.time() - max(1, days) * 86400
        rows = conn.execute("SELECT pnl FROM trades WHERE ts >= ? AND result != 'open' AND pnl IS NOT NULL ORDER BY ts", (since,)).fetchall()
        equity_rows = conn.execute("SELECT ts,equity FROM equity_curve WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        pnls = [float(r[0]) for r in rows]
        curve = [float(r[1]) for r in equity_rows]
        m = compute_metrics(pnls, curve or None, CFG.starting_capital)
        min_trades = max(30, days)
        warnings = []
        if len(pnls) < min_trades:
            warnings.append(f"only {len(pnls)} closed paper trades; need at least {min_trades} for the gate")
        if m["profit_factor"] < 1.0:
            warnings.append("profit factor is below 1.0")
        if m["max_drawdown_pct"] > CFG.max_drawdown_pct:
            warnings.append("maximum drawdown exceeded configured limit")
        if m["monte_carlo"]["p95_max_drawdown_pct"] > CFG.max_drawdown_pct:
            warnings.append("Monte Carlo P95 drawdown exceeds configured limit")
        if m["expectancy"] <= 0:
            warnings.append("expectancy is not positive")
        passed = len(pnls) >= min_trades and m["profit_factor"] >= 1.0 and m["max_drawdown_pct"] <= CFG.max_drawdown_pct and m["expectancy"] > 0
        return {"generated": datetime.now(timezone.utc).isoformat(), "window_days": days, "trades": len(pnls),
                "metrics": m, "warnings": warnings, "passed": passed,
                "next_gate": "testnet/sandbox only" if passed else "continue paper trading"}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ZORA paper-trading performance gate")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv)
    report = build_report(args.days)
    print(json.dumps(report, indent=2, default=str).replace("Infinity", "null"))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
