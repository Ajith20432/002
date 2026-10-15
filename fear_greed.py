"""fear_greed.py — contrarian Fear & Greed expert (free alternative.me API, no key).

Extreme fear -> positive (buy) lean, extreme greed -> negative lean, with a neutral dead-zone
(40-60) so an ordinary reading casts no vote.
"""
from __future__ import annotations

import time

import requests

URL = "https://api.alternative.me/fng/?limit=1"
CACHE_SECONDS = 900   # the index updates daily; 15 minutes is plenty
_cache: dict = {"ts": 0.0, "value": None}


def score_from_index(index: float) -> float:
    """0..100 index -> contrarian -100..100 score: 10 -> +80, 90 -> -80, 0 -> +100, 40..60 -> 0."""
    index = max(0.0, min(100.0, float(index)))
    if 40 <= index <= 60:
        return 0.0
    return (50.0 - index) * 2.0


def fetch(timeout: float = 5) -> tuple[int, str] | None:
    """(index, classification) or None on any network / shape / range error."""
    try:
        r = requests.get(URL, timeout=timeout)
        r.raise_for_status()
        d = r.json()["data"][0]
        value = int(float(d["value"]))
        if not 0 <= value <= 100:
            return None
        return value, str(d.get("value_classification", ""))
    except Exception:
        return None


def get_index(force: bool = False) -> tuple[int, str] | None:
    """Cached fetch. A failed refresh keeps serving the last good value."""
    now = time.time()
    if not force and _cache["value"] is not None and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["value"]
    fresh = fetch()
    if fresh is not None:
        _cache.update(ts=now, value=fresh)
    return _cache["value"]


def fear_greed_score() -> tuple[float | None, str]:
    """(score, note). score is None when the index has never been available."""
    got = get_index()
    if got is None:
        return None, "fear & greed unavailable"
    index, label = got
    return score_from_index(index), f"fear&greed {index} ({label})"
