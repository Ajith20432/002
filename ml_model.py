"""Small dependency-light logistic-regression expert, used only as an advisory vote.

Walk-forward and leak-free: the features for predicting candle t's direction use returns up to
t-1 only, the model is fit on the first 80% of rows, and it only speaks (score != 0) when it beats
ML_MIN_HOLDOUT_ACCURACY on the final 20% it never saw.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from config import CFG

LOOKBACK = 8   # returns needed before the first usable row


@dataclass
class MLResult:
    score: float
    prob_up: float | None
    holdout_accuracy: float | None


def fit_logistic(x, y, steps: int = 2500, lr: float = 0.08):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    x = np.column_stack([np.ones(len(x)), x])
    w = np.zeros(x.shape[1])
    for _ in range(steps):
        p = 1 / (1 + np.exp(-np.clip(x @ w, -30, 30)))
        w -= lr * (x.T @ (p - y) / len(y))
    return w


def predict_proba(w, x):
    x = np.asarray(x, float)
    x = np.column_stack([np.ones(len(x)), x])
    return 1 / (1 + np.exp(-np.clip(x @ w, -30, 30)))


def _row(ret: np.ndarray, i: int) -> list[float]:
    """Features describing the market just BEFORE return i (never ret[i] itself)."""
    return [ret[i - 1], float(np.mean(ret[i - 5:i])), float(np.mean(ret[i - 8:i]))]


def _dataset(df):
    """(x, y, x_next): x[k] predicts y[k]; x_next predicts the not-yet-seen next candle."""
    close = df["close"].astype(float).to_numpy()
    ret = np.diff(close) / close[:-1]
    if len(ret) <= LOOKBACK:
        return np.empty((0, 3)), np.empty(0), None
    idx = range(LOOKBACK, len(ret))
    x = np.asarray([_row(ret, i) for i in idx])
    y = np.asarray([1.0 if ret[i] > 0 else 0.0 for i in idx])
    return x, y, np.asarray([_row(ret, len(ret))])


def ml_expert(df, min_accuracy: float | None = None) -> MLResult:
    if not CFG.ml_enabled or len(df) < 140:
        return MLResult(0.0, None, None)
    x, y, x_next = _dataset(df)
    n = len(x)
    split = int(n * 0.8)
    if split < 60 or n - split < 20 or x_next is None:
        return MLResult(0.0, None, None)
    mu = x[:split].mean(0)
    sd = x[:split].std(0)
    sd[sd < 1e-9] = 1.0
    xn = (x - mu) / sd
    w = fit_logistic(xn[:split], y[:split])
    acc = float(((predict_proba(w, xn[split:]) > 0.5) == (y[split:] > 0.5)).mean())
    threshold = CFG.ml_min_holdout_accuracy if min_accuracy is None else min_accuracy
    if acc < threshold:
        return MLResult(0.0, None, acc)
    p = float(predict_proba(w, (x_next - mu) / sd)[0])
    return MLResult((p - 0.5) * 200, p, acc)
