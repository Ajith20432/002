"""
optimize.py — walk-forward parameter search (`python bot.py optimize`).

Freqtrade-style hyperopt without the optuna dependency (which is heavy on a phone): seeded
random search over EXIT parameters only, scored on a TRAIN window, then the best candidates
are re-tested on a held-out TEST window the search never saw. Out-of-sample results are what
count; if they collapse the report says OVERFIT instead of recommending anything.

Safety rules, on purpose:
  * risk-per-trade, exposure, drawdown and daily-loss limits are NEVER searched — an optimizer
    that is rewarded for profit will happily raise them.
  * nothing is written to .env. The report only SUGGESTS values; a human applies them.
  * signals are computed once and reused (they don't depend on the exit parameters), so
    hundreds of trials cost seconds, not hours.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass

import pandas as pd

from backtester import WINDOW, SimResult, override_cfg, simulate
from metrics import compute_metrics

SEARCH_SPACE: dict[str, tuple[float, float]] = {
    "atr_stop_mult": (1.0, 3.5),
    "atr_take_mult": (1.5, 6.0),
    "breakeven_at_r": (0.8, 2.0),
    "trail_atr_mult": (1.5, 3.5),
}
MIN_TRAIN_TRADES = 8


@dataclass
class Trial:
    params: dict[str, float]
    train: dict[str, float]
    test: dict[str, float] | None = None
    score: float = -math.inf


def _score(m: dict[str, float]) -> float:
    """Return% minus a drawdown penalty; too few trades is disqualifying (a lucky handful proves nothing)."""
    if m["trades"] < MIN_TRAIN_TRADES:
        return -math.inf
    pf = min(m["profit_factor"], 5.0)  # cap: infinity must not dominate
    return m["return_pct"] - 0.75 * m["max_drawdown_pct"] + 2.0 * (pf - 1.0)


def _metrics(res: SimResult, start_equity: float) -> dict[str, float]:
    return compute_metrics(res.pnls, res.equity_curve, start_equity)


def sample_params(rng: random.Random) -> dict[str, float]:
    p = {k: round(rng.uniform(lo, hi), 2) for k, (lo, hi) in SEARCH_SPACE.items()}
    if p["atr_take_mult"] < p["atr_stop_mult"]:  # never reward / risk < 1
        p["atr_take_mult"] = round(p["atr_stop_mult"] + 0.5, 2)
    return p


def optimize(
    df: pd.DataFrame, signal_fn, n_trials: int = 60, train_frac: float = 0.7, seed: int = 7, top_k: int = 3,
    starting_equity: float | None = None,
) -> dict:
    """df must be enriched. Returns a JSON-serialisable report."""
    from config import CFG
    equity0 = CFG.starting_capital if starting_equity is None else starting_equity
    last = len(df) - 1
    split = WINDOW + int((last - WINDOW) * train_frac)
    if split - WINDOW < 100 or last - split < 50:
        raise ValueError(f"not enough candles ({len(df)}) for a train/test split — fetch more history")

    rng = random.Random(seed)
    baseline_params = {k: getattr(CFG, k) for k in SEARCH_SPACE}
    trials: list[Trial] = []
    for params in [baseline_params] + [sample_params(rng) for _ in range(n_trials)]:
        with override_cfg(**params):
            train = _metrics(simulate(df, signal_fn, WINDOW, split, equity0), equity0)
        trials.append(Trial(params, train, score=_score(train)))

    baseline, rest = trials[0], trials[1:]
    best = sorted((t for t in rest if t.score > -math.inf), key=lambda t: t.score, reverse=True)[:top_k]
    for t in [baseline] + best:
        with override_cfg(**t.params):
            t.test = _metrics(simulate(df, signal_fn, split, last, equity0), equity0)

    verdict, pick = "NO_CANDIDATE", None
    if best:
        winner = max(best, key=lambda t: t.test["return_pct"] - 0.75 * t.test["max_drawdown_pct"])
        beats_baseline = (winner.test["return_pct"] - 0.75 * winner.test["max_drawdown_pct"]
                          > baseline.test["return_pct"] - 0.75 * baseline.test["max_drawdown_pct"])
        healthy = winner.test["trades"] >= 5 and winner.test["profit_factor"] > 1.0 and winner.test["return_pct"] > 0
        if healthy and beats_baseline:
            verdict, pick = "SUGGEST", winner
        else:
            verdict = "OVERFIT_OR_NO_EDGE"

    def _json_safe(value):
        """Recursively convert metrics to strict JSON-safe values.

        Metrics may contain nested Monte Carlo summaries and non-finite values
        (for example an infinite profit factor when there are no losing trades).
        """
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(value, 4) if math.isfinite(value) else None
        return value

    def dump(t: Trial) -> dict:
        return {"params": _json_safe(t.params), "train": _json_safe(t.train),
                "test": _json_safe(t.test),
                "train_score": None if not math.isfinite(t.score) else round(t.score, 3)}

    return {
        "verdict": verdict,
        "train_candles": split - WINDOW, "test_candles": last - split, "trials": n_trials, "seed": seed,
        "baseline": dump(baseline),
        "top": [dump(t) for t in best],
        "suggested_env": ({k.upper(): v for k, v in pick.params.items()} if pick else None),
        "note": "Suggestion only. Nothing was changed. Re-run on other windows before trusting it; "
                "past results never guarantee future ones.",
    }


def save_report(report: dict, path: str = "optimize_report.json") -> None:
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
