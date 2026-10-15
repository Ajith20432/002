"""
conftest.py — shared pytest fixtures.

The central one is `isolated_brain`, which every test that touches
strategy/brain logic should use instead of the module-level `brain.BRAIN`
singleton — it's backed by a throwaway JSON file under pytest's tmp_path,
so tests never read or write the real brain_state.json, and tests can't
pollute each other's learned weights by running in the same process.
"""
from __future__ import annotations

import os
import sys

import pytest

# Flat project layout (no package/__init__.py) — make the project root
# importable regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import AdaptiveBrain  # noqa: E402


@pytest.fixture
def isolated_brain(tmp_path) -> AdaptiveBrain:
    return AdaptiveBrain(state_path=tmp_path / "brain_state.json")


def make_candles(n: int = 400, seed: int = 3, start: float = 100.0, drift: float = 0.0004, vol: float = 0.01):
    """Deterministic synthetic OHLCV (ENRICHED) with a trending-then-choppy random walk, 1h spacing, UTC ts."""
    import numpy as np
    import pandas as pd

    from indicators import enrich

    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start * np.cumprod(1 + rets)
    open_ = np.concatenate([[start], close[:-1]])
    spread = np.abs(rng.normal(0, vol / 2, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    ts = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low, "close": close,
                       "volume": rng.uniform(100, 1000, n)})
    return enrich(df)
