"""
brain.py — the "auto-learning brain".

What it actually does: it adjusts HOW MUCH the strategy trusts each expert
(15m/1h/4h technical timeframes, plus the optional "ai" and "news" experts)
and each technical factor (trend/momentum/rsi/mean-reversion/volume/chart
patterns), based
on whether trades that leaned on them actually turned out to be winners or
losers. Over time, experts/factors that have been reliable get more say in
future signals; ones that have been misleading get less.

Design choices, and why:
- This is a simple, auditable "multiplicative weights" (Hedge-style) online
  learning rule, not a deep neural net — it runs fine on a phone with no
  heavy ML dependencies, and the learned weights are always human-readable
  JSON you can open and inspect.
- Weights are clamped (never zero, never dominant) so the brain can adapt
  but can't collapse onto a single factor after a lucky/unlucky streak.
- IMPORTANT: this only changes which SIGNAL gets generated. It has no
  access to position sizing, stop distance, or the kill switches — those
  live in risk.py and the brain cannot touch them. Learning can make the
  bot's opinions better calibrated; it can never make it take bigger risks.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

DEFAULT_STATE_PATH = os.getenv("BRAIN_STATE_PATH", "brain_state.json")

DEFAULT_EXPERT_WEIGHTS: dict[str, float] = {
    "15m": 0.15, "1h": 0.25, "4h": 0.30, "ai": 0.15, "news": 0.15, "fng": 0.10,
    "ml": 0.10, "tv": 0.10,
}
DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    "trend": 30, "momentum": 15, "rsi": 20, "meanrev": 10, "volume": 10, "pattern": 20,
}

LEARNING_RATE = 0.15
MIN_WEIGHT_FRACTION = 0.05   # no expert can be starved to (near) zero
MAX_WEIGHT_FRACTION = 0.70   # no single expert can dominate completely


class AdaptiveBrain:
    """
    state_path: where learned weights persist as JSON. Defaults to
    BRAIN_STATE_PATH (or "brain_state.json"). Tests should pass an explicit
    tmp_path-based state_path so they never read or write the real
    production brain_state.json.
    """

    def __init__(self, state_path: str | Path | None = None) -> None:
        self.state_path = str(state_path) if state_path is not None else DEFAULT_STATE_PATH
        self.expert_weights: dict[str, float] = dict(DEFAULT_EXPERT_WEIGHTS)
        self.factor_weights: dict[str, float] = dict(DEFAULT_FACTOR_WEIGHTS)
        self.trades_learned_from: int = 0
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    data = json.load(f)
                # Merge onto the defaults rather than replacing them: a state file written by an older
                # version lacks newer experts/factors ("news", "pattern", "fng"...), and strategy.py
                # indexes factor_weights[...] directly, so a missing key would crash every cycle.
                self.expert_weights = self._merge(self.expert_weights, data.get("expert_weights"))
                self.factor_weights = self._merge(self.factor_weights, data.get("factor_weights"))
                self.trades_learned_from = int(data.get("trades_learned_from", 0))
            except Exception:
                pass  # brain state is a cache, not a source of truth — fall back to defaults

    @staticmethod
    def _merge(defaults: dict[str, float], loaded: object) -> dict[str, float]:
        merged = dict(defaults)
        if isinstance(loaded, dict):
            for key, val in loaded.items():
                if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) and val > 0:
                    merged[str(key)] = float(val)
        return merged

    def save(self) -> None:
        """Atomic write (temp file + rename) so a crash or phone battery-death mid-save can't corrupt the state."""
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w") as f:
            json.dump({
                "expert_weights": self.expert_weights,
                "factor_weights": self.factor_weights,
                "trades_learned_from": self.trades_learned_from,
            }, f, indent=2)
        os.replace(tmp, self.state_path)

    # ---------- core update rule ----------
    @staticmethod
    def _multiplicative_update(
        weights: dict[str, float],
        contributions: dict[str, float],
        sig_dir: int,
        pnl_sign: int,
        lr: float,
    ) -> None:
        total_original = sum(weights.values())
        if total_original <= 0:
            return

        for key, s in contributions.items():
            if key not in weights or s == 0:
                continue
            agree = 1 if (s > 0) == (sig_dir > 0) else -1
            reward = agree * pnl_sign * (abs(s) / 100)
            weights[key] *= math.exp(lr * reward)

        lo = MIN_WEIGHT_FRACTION * total_original
        hi = MAX_WEIGHT_FRACTION * total_original
        for key in weights:
            weights[key] = min(max(weights[key], lo), hi)

        # renormalize back to the original total so downstream scale stays comparable
        new_total = sum(weights.values())
        if new_total > 0:
            scale = total_original / new_total
            for key in weights:
                weights[key] *= scale

    def update_from_trade(
        self,
        expert_scores: dict[str, float],
        factor_scores_by_tf: dict[str, dict[str, float]],
        action: str,
        pnl: float,
    ) -> None:
        """
        expert_scores:        {expert_name: total_score}  e.g. {"15m":.., "1h":.., "4h":.., "ai":..}
                               "ai"/"news" are only present when that panel actually answered.
        factor_scores_by_tf:  {timeframe: {factor_name: contribution}}  (technical timeframes only)
        action: "BUY" or "SELL" — direction actually taken
        pnl: realized pnl of the closed trade (paper or live)
        """
        sig_dir = 1 if action == "BUY" else -1
        pnl_sign = 1 if pnl > 0 else (-1 if pnl < 0 else 0)
        if pnl_sign == 0 or not expert_scores:
            return  # breakeven or no data — nothing to learn from

        self._multiplicative_update(self.expert_weights, expert_scores, sig_dir, pnl_sign, LEARNING_RATE)

        combined_factors: dict[str, float] = {}
        counts: dict[str, int] = {}
        for factors in factor_scores_by_tf.values():
            for f, v in factors.items():
                combined_factors[f] = combined_factors.get(f, 0) + v
                counts[f] = counts.get(f, 0) + 1
        for f in combined_factors:
            combined_factors[f] /= counts[f]

        self._multiplicative_update(self.factor_weights, combined_factors, sig_dir, pnl_sign, LEARNING_RATE)

        self.trades_learned_from += 1
        self.save()

    def snapshot(self) -> dict:
        return {
            "expert_weights": {k: round(v, 4) for k, v in self.expert_weights.items()},
            "factor_weights": {k: round(v, 2) for k, v in self.factor_weights.items()},
            "trades_learned_from": self.trades_learned_from,
        }


BRAIN = AdaptiveBrain()  # the production singleton, used by strategy.py's default parameter
