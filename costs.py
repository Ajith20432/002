"""Execution-cost model used by paper mode and backtests."""
from __future__ import annotations

import time
from dataclasses import dataclass

from config import CFG


@dataclass(frozen=True)
class ExecutionCost:
    fee_rate: float
    slippage_bps: float
    latency_ms: int

    @property
    def fee_pct(self) -> float:
        return self.fee_rate * 100

    def fill_price(self, reference: float, side: str) -> float:
        # Conservative one-sided impact: BUY fills higher, SELL lower.
        impact_bps = self.slippage_bps + (CFG.latency_bps if self.latency_ms else 0.0)
        direction = 1.0 if side.lower() == "buy" else -1.0
        return reference * (1.0 + direction * impact_bps / 10000.0)

    def round_trip_fee(self, entry: float, exit: float, size: float) -> float:
        return (abs(entry * size) + abs(exit * size)) * self.fee_rate

    def sleep_model(self) -> None:
        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000.0)

COST_MODEL = ExecutionCost(
    fee_rate=CFG.fee_rate,
    slippage_bps=CFG.slippage_bps,
    latency_ms=CFG.execution_latency_ms,
)
