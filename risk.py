"""risk.py — the independent risk engine. It can veto any signal; nothing upstream (technicals, AI council,
Gemini judge) can override it.

Exposure units: every open position counts as one *risk budget* (the % of equity it risks, scaled by any
size multiplier), and MAX_EXPOSURE_PCT caps the sum across all symbols. Hidden leverage is prevented
separately by MAX_POSITION_NOTIONAL_PCT.

What this module guarantees
  * Inputs are validated for NaN/inf (``nan < x`` is always False, so an unchecked NaN equity would silently
    disable the kill switch). Non-finite equity TRIPS the kill switch (fail safe).
  * A zero ATR_STOP_MULT cannot raise ZeroDivisionError; a minimum stop distance (MIN_STOP_BPS) stops a flat
    market from producing a stop tighter than the spread followed by an enormous (notional-capped) position.
  * Sizing risks 1-2% of CURRENT equity (auto-compounding), scaled by the judge's confidence, and can include
    round-trip fees + slippage so a stop-out really loses ~the risk budget.
  * Daily circuit breaker: a 4% daily drawdown locks NEW entries for 24h (open positions stay managed). The
    kill switch (MAX_DAILY_LOSS_PCT / MAX_DRAWDOWN_PCT) is the harder backstop above it.
  * Tiered exits (opt-in, TIERED_EXITS_ENABLED): TP1 closes 50% at 1.5R and moves the stop to breakeven,
    TP2 closes 25% at 3.0R, the last 25% is an ATR-trailed runner. Pure functions below; bot.py executes them.
  * All state mutation is guarded by a lock (Telegram /kill and the trading loop share this object), and the
    baselines that matter (daily start, peak, lock expiry) can be persisted/restored across restarts.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from config import CFG

logger = logging.getLogger("zora.risk")


@dataclass
class Decision:
    approved: bool
    reason: str
    position_size: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    exposure_pct: float = 0.0   # budget this trade consumes; store it on the position and use position_exposure()
    risk_amount: float = 0.0    # quote-currency loss if the stop fills (incl. fees/slippage when enabled)
    notional: float = 0.0       # position_size * entry
    risk_pct: float = 0.0       # % of equity this trade risks (after confidence scaling, before size multiplier)
    tiers: dict | None = None   # TP1/TP2 plan when tiered exits are enabled


def _finite_float(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def position_exposure(position: dict | None) -> float:
    """Risk budget a tracked position consumes. Positions saved before exposure was persisted fall back to
    the standard per-trade budget."""
    if not position:
        return 0.0
    v = _finite_float(position.get("exposure_pct"))
    if v is None or v < 0:
        return CFG.risk_per_trade_pct
    return v


def risk_pct_for(confidence: float | None) -> float:
    """Per-trade risk budget. Without a judge confidence it is RISK_PER_TRADE_PCT. With one, it scales linearly
    from RISK_MIN_PCT (at the judge's minimum confidence) to RISK_MAX_PCT (at 100)."""
    conf = _finite_float(confidence)
    lo, hi = CFG.risk_min_pct, CFG.risk_max_pct
    if conf is None or not (0 < lo <= hi):
        return CFG.risk_per_trade_pct
    floor = float(getattr(CFG, "ai_judge_min_confidence", 75) or 75)
    t = min(1.0, max(0.0, (conf - floor) / max(1e-9, 100.0 - floor)))
    return lo + (hi - lo) * t


class RiskEngine:
    def __init__(self, equity: float):
        self._lock = threading.RLock()
        eq = _finite_float(equity)
        if eq is None:
            raise ValueError(f"starting equity must be a finite number, got {equity!r}")
        self.equity = eq
        self.peak_equity = eq
        self.daily_start = eq
        self.open_exposure = 0.0
        self.kill_switch_tripped = False
        self.kill_reason = ""
        self.entries_locked_until = 0.0       # circuit breaker (epoch seconds)
        self.lock_reason = ""

    # ------------------------------------------------------------------ kill switch / circuit breaker
    def trip(self, reason: str) -> None:
        """Trip the kill switch. The first reason is kept (it is the cause; later ones are consequences)."""
        with self._lock:
            if not self.kill_switch_tripped:
                logger.error("KILL SWITCH tripped: %s", reason)
                self.kill_reason = reason
            self.kill_switch_tripped = True

    def lock_entries(self, hours: float, reason: str) -> None:
        with self._lock:
            until = time.time() + max(0.0, hours) * 3600.0
            if until > self.entries_locked_until:
                self.entries_locked_until = until
                self.lock_reason = reason
                logger.error("CIRCUIT BREAKER: new entries locked for %.1fh: %s", hours, reason)

    def entries_locked(self) -> bool:
        """True while the circuit breaker is active. When it expires the daily baseline is re-based to the
        current equity, otherwise the same loss would immediately re-trigger it."""
        with self._lock:
            if self.entries_locked_until <= 0:
                return False
            if time.time() < self.entries_locked_until:
                return True
            self.entries_locked_until = 0.0
            self.lock_reason = ""
            self.daily_start = self.equity
            logger.warning("circuit breaker expired; daily baseline re-based to %.2f", self.equity)
            return False

    def lock_remaining_seconds(self) -> int:
        with self._lock:
            return max(0, int(self.entries_locked_until - time.time()))

    # ------------------------------------------------------------------ equity tracking
    def update_equity(self, equity: float) -> None:
        eq = _finite_float(equity)
        if eq is None:
            self.trip(f"non-finite equity {equity!r}")
            return
        with self._lock:
            self.equity = eq
            self.peak_equity = max(self.peak_equity, eq)
            if eq <= 0:
                self.trip("equity is zero or negative")
                return
            drawdown = (self.peak_equity - eq) / self.peak_equity * 100 if self.peak_equity > 0 else 0.0
            daily_loss = (self.daily_start - eq) / self.daily_start * 100 if self.daily_start > 0 else 0.0
            if drawdown >= CFG.max_drawdown_pct:
                self.trip(f"drawdown {drawdown:.2f}% >= {CFG.max_drawdown_pct}% (peak {self.peak_equity:.2f}, now {eq:.2f})")
            elif daily_loss >= CFG.max_daily_loss_pct:
                self.trip(f"daily loss {daily_loss:.2f}% >= {CFG.max_daily_loss_pct}% (day start {self.daily_start:.2f}, now {eq:.2f})")
            if CFG.circuit_breaker_pct > 0 and daily_loss >= CFG.circuit_breaker_pct and self.entries_locked_until <= time.time():
                self.lock_entries(CFG.circuit_breaker_hours,
                                  f"daily drawdown {daily_loss:.2f}% >= {CFG.circuit_breaker_pct}% (day start {self.daily_start:.2f}, now {eq:.2f})")

    def reset_daily(self) -> None:
        with self._lock:
            self.daily_start = self.equity

    def snapshot(self) -> dict[str, float]:
        """Durable baselines (persist with db.set_state_json) so a restart cannot reset the daily-loss window."""
        with self._lock:
            return {"daily_start": self.daily_start, "peak_equity": self.peak_equity,
                    "entries_locked_until": self.entries_locked_until}

    def restore(self, daily_start: float | None = None, peak_equity: float | None = None,
                entries_locked_until: float | None = None) -> None:
        """Re-apply persisted baselines. Garbage values are ignored rather than trusted."""
        with self._lock:
            ds, pk, lk = _finite_float(daily_start), _finite_float(peak_equity), _finite_float(entries_locked_until)
            if ds is not None and ds > 0:
                self.daily_start = ds
            if pk is not None and pk > 0:
                self.peak_equity = max(self.peak_equity, pk)
            if lk is not None and lk > time.time():
                self.entries_locked_until = lk
                self.lock_reason = "restored circuit breaker lock"

    # ------------------------------------------------------------------ exposure bookkeeping
    def add_exposure(self, pct: float) -> None:
        with self._lock:
            self.open_exposure = max(0.0, self.open_exposure + float(pct))

    def remove_exposure(self, pct: float) -> None:
        with self._lock:
            self.open_exposure = max(0.0, self.open_exposure - float(pct))

    def set_exposure(self, pct: float) -> None:
        with self._lock:
            self.open_exposure = max(0.0, float(pct))

    # ------------------------------------------------------------------ the veto
    @staticmethod
    def _friction_rate() -> float:
        """One-sided fee + slippage as a fraction of price, mirroring costs.ExecutionCost."""
        impact_bps = CFG.slippage_bps + (CFG.latency_bps if CFG.execution_latency_ms else 0.0)
        return CFG.fee_rate + impact_bps / 10_000.0

    def evaluate(self, action: str, entry: float, atr: float, size_multiplier: float = 1.0, *,
                 confidence: float | None = None, ai_stop: float | None = None) -> Decision:
        """Approve/deny and size a trade.

        confidence: the judge's 0-100 confidence; scales the risk budget between RISK_MIN_PCT and RISK_MAX_PCT.
        ai_stop:    the judge's proposed stop. Adopted ONLY if it is on the correct side of entry and within
                    [MIN_STOP_BPS, 1.5 x the ATR stop distance]; otherwise the ATR stop stands. The AI can
                    tighten a stop, it can never widen risk beyond the engine's own bound.
        """
        with self._lock:
            return self._evaluate(action, entry, atr, size_multiplier, confidence, ai_stop)

    def _evaluate(self, action: str, entry: float, atr: float, size_multiplier: float,
                  confidence: float | None, ai_stop: float | None) -> Decision:
        if action not in ("BUY", "SELL"):
            return Decision(False, "no actionable signal")
        if self.kill_switch_tripped:
            return Decision(False, "kill switch is tripped")
        if self.entries_locked():
            return Decision(False, f"circuit breaker: entries locked for another {self.lock_remaining_seconds() / 3600:.1f}h "
                                   f"({self.lock_reason})")
        entry_f, atr_f = _finite_float(entry), _finite_float(atr)
        if entry_f is None or atr_f is None or entry_f <= 0 or atr_f <= 0:
            return Decision(False, "invalid price or ATR")
        entry, atr = entry_f, atr_f
        if self.equity <= 0:
            return Decision(False, "no equity")
        mult_f = _finite_float(size_multiplier)
        if mult_f is None:
            return Decision(False, "invalid size multiplier")
        mult = min(1.0, max(0.0, mult_f))

        raw_stop = atr * CFG.atr_stop_mult
        floor = entry * max(CFG.min_stop_bps, 0.0) / 10_000.0
        stop_dist = max(raw_stop, floor)
        ai_note = ""
        proposed = _finite_float(ai_stop)
        if proposed is not None and proposed > 0:
            dist = (entry - proposed) if action == "BUY" else (proposed - entry)
            if floor <= dist <= 1.5 * stop_dist:
                stop_dist, ai_note = dist, " (judge stop adopted)"
        if not math.isfinite(stop_dist) or stop_dist <= 0:
            return Decision(False, "stop distance is zero")           # e.g. ATR_STOP_MULT=0 and MIN_STOP_BPS=0
        scale = stop_dist / raw_stop if raw_stop > 0 else 1.0         # keep reward:risk when the stop distance changes
        take_dist = atr * CFG.atr_take_mult * scale

        if action == "BUY":
            stop, take = entry - stop_dist, entry + take_dist
            if stop <= 0:
                return Decision(False, "stop distance larger than price")
        else:
            stop, take = entry + stop_dist, entry - take_dist
            if take <= 0:
                return Decision(False, "take-profit distance larger than price")

        loss_per_unit = stop_dist
        if CFG.risk_include_costs:
            loss_per_unit += self._friction_rate() * (entry + stop)
        if not math.isfinite(loss_per_unit) or loss_per_unit <= 0:
            return Decision(False, "cannot compute risk per unit")

        risk_pct = risk_pct_for(confidence)
        size = (self.equity * risk_pct / 100) / loss_per_unit          # equity is CURRENT equity -> auto-compounding
        size = min(size, self.equity * CFG.max_position_notional_pct / 100 / entry)   # no hidden leverage
        size *= mult
        if not math.isfinite(size) or size <= 0:
            return Decision(False, "position size is zero")

        added = risk_pct * mult
        if self.open_exposure + added > CFG.max_exposure_pct + 1e-9:
            return Decision(False, "max exposure limit reached")

        tiers = plan_tiers(action, entry, stop, size) if CFG.tiered_exits_enabled else None
        if tiers:
            take = tiers["tp1"]            # displayed take = the next target
        return Decision(True, "approved" + ai_note, size, stop, take, added, risk_amount=size * loss_per_unit,
                        notional=size * entry, risk_pct=risk_pct, tiers=tiers)


# --------------------------------------------------------------------------- tiered exits (pure functions)
def plan_tiers(action: str, entry: float, stop: float, size: float) -> dict | None:
    """TP1/TP2 price levels in R multiples of the initial stop distance. None if the plan is not representable
    (non-finite input, or a short whose TP2 would be <= 0)."""
    entry_f, stop_f, size_f = _finite_float(entry), _finite_float(stop), _finite_float(size)
    if entry_f is None or stop_f is None or size_f is None or size_f <= 0:
        return None
    r = abs(entry_f - stop_f)
    if r <= 0:
        return None
    d = 1.0 if action == "BUY" else -1.0
    tp1, tp2 = entry_f + d * CFG.tp1_r * r, entry_f + d * CFG.tp2_r * r
    if tp1 <= 0 or tp2 <= 0:
        return None
    return {"stage": 0, "r": r, "tp1": tp1, "tp2": tp2, "initial_size": size_f}


def next_tier_exit(position: dict, price: float) -> tuple[str, float, float] | None:
    """('tp1'|'tp2', size_to_close, level) if the price has reached the next unfilled target, else None.
    Sizes are fractions of the ORIGINAL position, capped at what is still open."""
    tiers = position.get("tiers") or {}
    p = _finite_float(price)
    if not tiers or p is None:
        return None
    long = position.get("action") == "BUY"
    stage = int(tiers.get("stage", 0))
    initial = _finite_float(tiers.get("initial_size")) or _finite_float(position.get("size")) or 0.0
    remaining = _finite_float(position.get("size")) or 0.0
    if stage == 0:
        level, frac, name = _finite_float(tiers.get("tp1")), CFG.tp1_fraction, "tp1"
    elif stage == 1:
        level, frac, name = _finite_float(tiers.get("tp2")), CFG.tp2_fraction, "tp2"
    else:
        return None
    if level is None or remaining <= 0:
        return None
    if (long and p < level) or (not long and p > level):
        return None
    return name, min(remaining, initial * frac), level


def apply_tier_fill(position: dict, tier: str, closed_size: float) -> dict:
    """Update the position AFTER a scale-out leg filled: shrink the size, advance the stage, and
    - TP1: move the stop to breakeven (entry +/- round-trip fees, so 'breakeven' is truly ~0 net), never loosening it;
    - TP2: clear the fixed take - the remainder is a runner trailed by ATR (see runner_trail).
    Returns the same dict (mutated) for convenience."""
    tiers = position.setdefault("tiers", {})
    position["size"] = max(0.0, float(position["size"]) - float(closed_size))
    entry = float(position["entry"])
    long = position["action"] == "BUY"
    if tier == "tp1":
        tiers["stage"] = 1
        buffer = entry * 2.0 * CFG.fee_rate
        be = entry + buffer if long else entry - buffer
        cur = _finite_float(position.get("stop"))
        if cur is None or (be > cur if long else be < cur):
            position["stop"] = be
        position["take"] = tiers.get("tp2")
    elif tier == "tp2":
        tiers["stage"] = 2
        position["take"] = None
    return position


def runner_trail(position: dict, price: float, atr: float) -> float | None:
    """ATR trailing stop for the runner (stage 2). Returns a new, tighter stop or None. Never loosens a stop.
    Independent of TRAILING_STOP_ENABLED: the runner exists precisely to be trailed."""
    if int((position.get("tiers") or {}).get("stage", 0)) < 2:
        return None
    p, a, cur = _finite_float(price), _finite_float(atr), _finite_float(position.get("stop"))
    if p is None or a is None or a <= 0 or cur is None:
        return None
    if position.get("action") == "BUY":
        cand = p - a * CFG.trail_atr_mult
        return cand if cand > cur else None
    cand = p + a * CFG.trail_atr_mult
    return cand if cand < cur else None


def manage_stop(p: dict, price: float, atr: float) -> float | None:
    """Breakeven then ATR-trail. Returns a new (tighter) stop or None. Never loosens a stop."""
    if not CFG.trailing_stop_enabled:
        return None
    try:
        entry = float(p["entry"])
        action = p["action"]
        current = float(p.get("stop") or p.get("initial_stop"))
        initial = float(p.get("initial_stop") or current)
        price, atr = float(price), float(atr)
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (entry, current, initial, price, atr)) or atr <= 0:
        return None
    r = abs(entry - initial)
    if r <= 0:
        return None
    reached = CFG.breakeven_at_r * r
    if action == "BUY":
        if price - entry < reached:
            return None
        candidate = max(entry, price - atr * CFG.trail_atr_mult)
        return candidate if candidate > current else None
    if entry - price < reached:
        return None
    candidate = min(entry, price + atr * CFG.trail_atr_mult)
    return candidate if candidate < current else None
