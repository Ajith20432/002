"""
strategy.py — multi-timeframe, multi-factor signal engine.

Several independent experts are scored separately, then combined using
weights supplied by brain.py's auto-learning brain — which adjusts those
weights over time based on which experts have actually been right:
  - three technical experts: 15m / 1h / 4h candles, each scored on
    trend / momentum / rsi / mean-reversion / volume / chart patterns
  - one optional "ai" expert: the consensus of whichever free/paid LLM
    APIs are configured in .env (see multi_ai.py)
  - one optional "news" expert: keyword-sentiment lean across recent
    headlines mentioning the traded coin (see news_feed.py)

This keeps the "multiple specialist agents + coordinator" idea from the
original design, but as transparent, auditable code — the AI panel is
just one more voice the brain learns to weigh, never a special override.

AI-council mode (STRATEGY_MODE=ai_council)
------------------------------------------
The second half of this file turns strategy.py into an *Intelligent Market Dossier Packager*: instead of
hard-coded RSI/MACD triggers deciding trades, it computes multi-timeframe context (15m + 1h EMA 20/50/200,
RSI, MACD, ATR, ADX, support/resistance zones), classifies the MARKET REGIME, and packages everything as a
compact structured dossier for multi_ai.get_ai_consensus() / get_council_decision(). The Gemini judge's
authorized decision becomes the signal. Nothing here can place a trade: every approved decision still has
to pass risk.py, which can veto anything and which fixes the final stop/size. The legacy technical
`generate_signal` stays: backtest/learn/optimize use it (an LLM's opinion cannot be replayed as it stood at a
past candle), and it is attached to the dossier as an advisory baseline.

Every function here takes an optional `brain` parameter, defaulting to the
production BRAIN singleton — pass an isolated AdaptiveBrain(state_path=...)
in tests so they never read or write the real brain_state.json.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from config import CFG

from brain import BRAIN, AdaptiveBrain
from indicators import enrich
from patterns import detect_patterns


@dataclass
class Signal:
    action: str        # "BUY" | "SELL" | "HOLD"
    confidence: float  # 0-100
    reasons: list[str]
    expert_scores: dict[str, float] = field(default_factory=dict)        # e.g. {"15m":.., "ai":..}
    factor_scores_by_tf: dict[str, dict[str, float]] = field(default_factory=dict)  # technical timeframes only
    pattern_notes_by_tf: dict[str, list[str]] = field(default_factory=dict)  # detected chart patterns, for logging


def score_timeframe(df: pd.DataFrame, brain: AdaptiveBrain = BRAIN) -> tuple[float, dict[str, float], list[str]]:
    """Return (total_score, factor_contributions, pattern_notes) for one timeframe's latest candle."""
    d = enrich(df)
    last = d.iloc[-1]
    fw = brain.factor_weights
    c: dict[str, float] = {}

    # Trend (EMA stack)
    if last["ema9"] > last["ema21"] > last["ema50"]:
        c["trend"] = fw["trend"]
    elif last["ema9"] < last["ema21"] < last["ema50"]:
        c["trend"] = -fw["trend"]
    else:
        c["trend"] = 0

    # Momentum (MACD histogram)
    c["momentum"] = fw["momentum"] if last["macd_hist"] > 0 else -fw["momentum"]

    # RSI — avoid chasing extremes, reward room to run
    if last["rsi14"] < 30:
        c["rsi"] = fw["rsi"]      # oversold, possible bounce
    elif last["rsi14"] > 70:
        c["rsi"] = -fw["rsi"]     # overbought, possible pullback
    else:
        c["rsi"] = 0

    # Mean reversion vs Bollinger bands
    if last["close"] < last["bb_lower"]:
        c["meanrev"] = fw["meanrev"]
    elif last["close"] > last["bb_upper"]:
        c["meanrev"] = -fw["meanrev"]
    else:
        c["meanrev"] = 0

    # Volume confirmation — amplifies whichever direction is already leading
    lean = 1 if sum(c.values()) >= 0 else -1
    c["volume"] = fw["volume"] * lean if last["vol_spike"] > 1.5 else 0

    # Chart patterns — candlestick + swing-based (see patterns.py). Raw score is -100..100;
    # scale it into this factor's own learned weight, same as every other factor here.
    pattern_raw, pattern_notes = detect_patterns(df)
    c["pattern"] = fw["pattern"] * (pattern_raw / 100)

    total = max(-100.0, min(100.0, sum(c.values())))
    return total, c, pattern_notes


def combine(
    scores: dict[str, float],
    factor_scores_by_tf: dict[str, dict[str, float]],
    brain: AdaptiveBrain = BRAIN,
) -> Signal:
    """
    scores: {expert_name: score}. Weights come from the auto-learning brain —
    a 15m blip (or a shaky AI opinion) shouldn't override a 4h downtrend
    unless it has actually proven more reliable lately.
    """
    weights = brain.expert_weights
    total_weight = sum(weights.get(name, 0.3) for name in scores)
    weighted = sum(scores[name] * weights.get(name, 0.3) for name in scores) / total_weight

    reasons = [f"{name}:{scores[name]:+.0f}" for name in scores]

    if weighted >= 25:
        action = "BUY"
    elif weighted <= -25:
        action = "SELL"
    else:
        action = "HOLD"

    return Signal(action, min(100.0, abs(weighted)), reasons, scores, factor_scores_by_tf)


def generate_signal(
    mtf_data: dict[str, pd.DataFrame],
    ai_score: float | None = None,
    news_score: float | None = None,
    brain: AdaptiveBrain = BRAIN,
    fng_score: float | None = None,
    ml_score: float | None = None,
    tv_score: float | None = None,
) -> Signal:
    """
    mtf_data: {timeframe: OHLCV DataFrame}
    ai_score: optional -100..100 consensus from multi_ai.get_ai_consensus().
    news_score: optional -100..100 keyword-sentiment lean from news_feed.news_sentiment().
              Both default to None to run purely on technical experts — this is what
              backtest/learn always do, since there's no way to ask an LLM or replay a
              news feed as it stood at a past candle.
    fng_score: optional -100..100 contrarian lean from fear_greed.py (same rule: live loop only).
    ml_score: optional -100..100 from ml_model.py (numpy logistic-regression expert; 0 when it has no proven edge).
    tv_score: optional -100..100 from tv_signal.py (a TradingView alert, expires after TV_SIGNAL_TTL_MINUTES).
    """
    scores: dict[str, float] = {}
    factor_scores_by_tf: dict[str, dict[str, float]] = {}
    pattern_notes_by_tf: dict[str, list[str]] = {}
    for tf, df in mtf_data.items():
        if len(df) <= 60:
            continue
        s, factors, pattern_notes = score_timeframe(df, brain=brain)
        scores[tf] = s
        factor_scores_by_tf[tf] = factors
        if pattern_notes:
            pattern_notes_by_tf[tf] = pattern_notes

    if ai_score is not None:
        scores["ai"] = max(-100.0, min(100.0, ai_score))
    if news_score is not None:
        scores["news"] = max(-100.0, min(100.0, news_score))

    if fng_score is not None:
        scores["fng"] = max(-100.0, min(100.0, fng_score))

    if ml_score is not None:
        scores["ml"] = max(-100.0, min(100.0, ml_score))
    if tv_score is not None:
        scores["tv"] = max(-100.0, min(100.0, tv_score))

    if not scores:
        return Signal("HOLD", 0, ["insufficient data"])
    signal = combine(scores, factor_scores_by_tf, brain=brain)
    signal.pattern_notes_by_tf = pattern_notes_by_tf
    return signal


# =====================================================================================================
# Market dossier packager (AI-council mode)
# =====================================================================================================
TRENDING_BULLISH = "TRENDING_BULLISH"
TRENDING_BEARISH = "TRENDING_BEARISH"
RANGING_SIDEWAYS = "RANGING_SIDEWAYS"
HIGH_VOLATILITY_BREAKOUT = "HIGH_VOLATILITY_BREAKOUT"
REGIMES = (TRENDING_BULLISH, TRENDING_BEARISH, RANGING_SIDEWAYS, HIGH_VOLATILITY_BREAKOUT)

DOSSIER_TIMEFRAMES = ("15m", "1h")
_MAX_HEADLINE_CHARS = 160


def _f(value: Any, ndigits: int | None = None) -> float | None:
    """JSON-safe float: NaN/inf/None -> None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, ndigits) if ndigits is not None else v


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX (trend STRENGTH, direction-agnostic). Pure pandas; a flat market yields 0, never NaN/inf."""
    high, low, close = df["high"], df["low"], df["close"]
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    alpha = 1.0 / period
    atr_s = tr.ewm(alpha=alpha, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0)


def closed_candles(df: pd.DataFrame, timeframe: str, now: float | None = None) -> pd.DataFrame:
    """Drop the still-forming last candle (its OPEN time + one period is in the future). Trading decisions on a
    forming candle see wicks that later disappear - the backtester only ever sees closed candles, so live
    must too."""
    from guards import timeframe_seconds
    if df is None or len(df) == 0:
        return df
    now = time.time() if now is None else now
    period = timeframe_seconds(timeframe)
    last_open = float(df["ts"].iloc[-1].timestamp())
    return df.iloc[:-1] if last_open + period > now else df


def support_resistance(df: pd.DataFrame, atr: float, lookback: int = 200, window: int = 5,
                       max_levels: int = 3) -> tuple[list[dict], list[dict]]:
    """Swing-pivot zones clustered within ~0.5 ATR (or 0.2% of price). Returns (supports_below, resistances_above),
    each sorted nearest-first, as {"level", "touches"}."""
    d = df.tail(lookback).reset_index(drop=True)
    if len(d) < 2 * window + 3:
        return [], []
    price = float(d["close"].iloc[-1])
    tol = max(0.5 * atr if atr and math.isfinite(atr) else 0.0, price * 0.002)
    highs, lows = d["high"].to_numpy(float), d["low"].to_numpy(float)
    pivots: list[float] = []
    for i in range(window, len(d) - window):
        seg_h, seg_l = highs[i - window:i + window + 1], lows[i - window:i + window + 1]
        if highs[i] >= seg_h.max():
            pivots.append(float(highs[i]))
        if lows[i] <= seg_l.min():
            pivots.append(float(lows[i]))
    zones: list[list[float]] = []   # [mean_level, touches]
    for p in sorted(pivots):
        if zones and abs(p - zones[-1][0]) <= tol:
            n = zones[-1][1]
            zones[-1][0] = (zones[-1][0] * n + p) / (n + 1)
            zones[-1][1] = n + 1
        else:
            zones.append([p, 1])
    sup = sorted((z for z in zones if z[0] < price), key=lambda z: price - z[0])[:max_levels]
    res = sorted((z for z in zones if z[0] > price), key=lambda z: z[0] - price)[:max_levels]
    return ([{"level": round(z[0], 4), "touches": int(z[1])} for z in sup],
            [{"level": round(z[0], 4), "touches": int(z[1])} for z in res])


def tf_snapshot(df: pd.DataFrame, timeframe: str) -> dict:
    """Everything the council needs to know about ONE timeframe, from its closed candles. Missing history yields
    None fields rather than exceptions (EMA200 needs 200+ candles)."""
    d = df.copy()
    close = d["close"]
    price = float(close.iloc[-1])
    e20, e50 = _ema(close, 20), _ema(close, 50)
    e200 = _ema(close, 200) if len(d) >= 200 else None
    from indicators import atr as _atr, macd as _macd, rsi as _rsi
    atr14 = _atr(d, 14)
    rsi14 = _rsi(close, 14)
    _, _, hist = _macd(close)
    adx14 = adx(d, 14)
    atr_now = float(atr14.iloc[-1])
    vol = d["volume"]
    vol_avg = float(vol.iloc[-21:-1].mean()) if len(vol) > 21 else float("nan")
    slope = float((e20.iloc[-1] - e20.iloc[-6]) / atr_now) if len(d) > 6 and atr_now > 0 else 0.0   # in ATRs per 5 bars
    if e200 is not None and float(e20.iloc[-1]) > float(e50.iloc[-1]) > float(e200.iloc[-1]):
        stack = "bullish"
    elif e200 is not None and float(e20.iloc[-1]) < float(e50.iloc[-1]) < float(e200.iloc[-1]):
        stack = "bearish"
    elif e200 is None and float(e20.iloc[-1]) > float(e50.iloc[-1]):
        stack = "bullish_partial"
    elif e200 is None and float(e20.iloc[-1]) < float(e50.iloc[-1]):
        stack = "bearish_partial"
    else:
        stack = "mixed"
    sup, res = support_resistance(d, atr_now)
    return {
        "timeframe": timeframe, "close": _f(price, 4), "last_candle_ts": str(d["ts"].iloc[-1]),
        "ema20": _f(e20.iloc[-1], 4), "ema50": _f(e50.iloc[-1], 4), "ema200": _f(e200.iloc[-1], 4) if e200 is not None else None,
        "ema_stack": stack, "ema20_slope_atr": _f(slope, 3),
        "price_vs_ema200_pct": _f((price / float(e200.iloc[-1]) - 1) * 100, 2) if e200 is not None else None,
        "rsi14": _f(rsi14.iloc[-1], 1), "macd_hist": _f(hist.iloc[-1], 5), "macd_hist_prev": _f(hist.iloc[-2], 5) if len(hist) > 1 else None,
        "atr14": _f(atr_now, 5), "atr_pct": _f(atr_now / price * 100, 3) if price > 0 else None,
        "adx14": _f(adx14.iloc[-1], 1),
        "volume_ratio": _f(float(vol.iloc[-1]) / vol_avg, 2) if vol_avg and vol_avg > 0 else None,
        "supports": sup, "resistances": res,
    }


def detect_regime(snap_fast: dict, snap_slow: dict, df_fast: pd.DataFrame | None = None) -> tuple[str, list[str]]:
    """Classify the market regime from the 15m (fast) and 1h (slow) snapshots. Order matters: a volatility
    breakout overrides trend labels because stops/targets behave differently in it.

    HIGH_VOLATILITY_BREAKOUT: ATR% on the fast frame >= 1.8x its own 50-candle median AND (the last candle's range is
                              >= 2.5 ATR OR volume >= BREAKOUT_VOLUME_MULT x average)
    TRENDING_*              : slow-frame ADX >= 25 with an aligned EMA stack (or both frames aligned with ADX >= 20)
    RANGING_SIDEWAYS        : everything else (low ADX / mixed EMAs / price chopping through the 1h EMA20)
    """
    reasons: list[str] = []
    adx_slow = snap_slow.get("adx14") or 0.0
    stack_s, stack_f = snap_slow.get("ema_stack", "mixed"), snap_fast.get("ema_stack", "mixed")
    if df_fast is not None and len(df_fast) >= 60 and snap_fast.get("atr14"):
        from indicators import atr as _atr
        atr_series = _atr(df_fast, 14)
        pct = (atr_series / df_fast["close"]).tail(50)
        median = float(pct.median()) if len(pct) else 0.0
        cur = float(pct.iloc[-1]) if len(pct) else 0.0
        last_range = float(df_fast["high"].iloc[-1] - df_fast["low"].iloc[-1])
        range_atr = last_range / float(atr_series.iloc[-1]) if float(atr_series.iloc[-1]) > 0 else 0.0
        vr = snap_fast.get("volume_ratio") or 0.0
        if median > 0 and cur >= 1.8 * median and (range_atr >= 2.5 or vr >= CFG.breakout_volume_mult):
            reasons.append(f"ATR% {cur / median:.1f}x its 50-candle median; last range {range_atr:.1f} ATR; volume {vr:.1f}x")
            return HIGH_VOLATILITY_BREAKOUT, reasons
    bull = stack_s.startswith("bullish") and (snap_slow.get("ema20_slope_atr") or 0) > 0
    bear = stack_s.startswith("bearish") and (snap_slow.get("ema20_slope_atr") or 0) < 0
    aligned_bull = bull and stack_f.startswith("bullish")
    aligned_bear = bear and stack_f.startswith("bearish")
    if adx_slow >= 25 and bull:
        reasons.append(f"1h ADX {adx_slow:.0f}, EMA20>50(>200) and rising")
        return TRENDING_BULLISH, reasons
    if adx_slow >= 25 and bear:
        reasons.append(f"1h ADX {adx_slow:.0f}, EMA20<50(<200) and falling")
        return TRENDING_BEARISH, reasons
    if adx_slow >= 20 and aligned_bull:
        reasons.append(f"1h ADX {adx_slow:.0f}, both frames bullish-aligned")
        return TRENDING_BULLISH, reasons
    if adx_slow >= 20 and aligned_bear:
        reasons.append(f"1h ADX {adx_slow:.0f}, both frames bearish-aligned")
        return TRENDING_BEARISH, reasons
    reasons.append(f"1h ADX {adx_slow:.0f} with {stack_s} EMA stack: no dominant trend")
    return RANGING_SIDEWAYS, reasons


_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize_headline(text: Any) -> str:
    """Headlines are UNTRUSTED third-party text that ends up inside an LLM prompt. Strip control characters, collapse
    whitespace, neutralize anything that looks like our JSON/role framing, and cap the length."""
    t = _CTRL.sub(" ", str(text))
    t = re.sub(r"[`{}\[\]<>]", "", t)
    t = re.sub(r"(?i)\b(ignore|disregard)\b.{0,40}\b(instruction|prompt|rule)s?\b", "[removed]", t)
    return re.sub(r"\s+", " ", t).strip()[:_MAX_HEADLINE_CHARS]


@dataclass
class MarketDossier:
    symbol: str
    price: float
    regime: str
    regime_reasons: list[str]
    timeframes: dict[str, dict]
    levels: dict[str, Any]
    trigger: dict[str, Any]
    baseline: dict[str, Any] | None = None       # legacy technical signal, ADVISORY only
    btc_context: str | None = None
    headlines: list[str] = field(default_factory=list)
    generated: float = field(default_factory=time.time)

    # duck-typing shim so legacy helpers that expect a Signal (action/confidence/reasons) keep working
    @property
    def action(self) -> str:
        return (self.baseline or {}).get("action", "HOLD")

    @property
    def confidence(self) -> float:
        return float((self.baseline or {}).get("confidence", 0.0))

    @property
    def reasons(self) -> list[str]:
        return list((self.baseline or {}).get("reasons", []))

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "price": self.price, "regime": self.regime, "regime_reasons": self.regime_reasons,
            "timeframes": self.timeframes, "levels": self.levels, "trigger": self.trigger,
            "technical_baseline_advisory": self.baseline, "btc_context": self.btc_context, "headlines": self.headlines,
        }

    def to_prompt(self) -> str:
        """Compact JSON the council reads. Headlines are fenced as data."""
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)


def build_dossier(symbol: str, mtf: dict[str, pd.DataFrame], *, trigger: str = "candle_close", trigger_detail: str = "",
                  headlines: list[str] | None = None, btc_note: str | None = None, baseline: "Signal | None" = None,
                  live_price: float | None = None, now: float | None = None) -> MarketDossier | None:
    """Package multi-timeframe context. Returns None when there is not enough closed history (fail closed).

    mtf must contain the DOSSIER_TIMEFRAMES (15m and 1h) - the same dict the technical loop already fetches.
    Indicators are computed on CLOSED candles only (SIGNAL_ON_CLOSED_CANDLES); `live_price` (intra-candle ticker)
    is reported separately so a breakout trigger can describe where price is NOW without contaminating the indicators.
    """
    snaps: dict[str, dict] = {}
    frames: dict[str, pd.DataFrame] = {}
    for tf in DOSSIER_TIMEFRAMES:
        df = mtf.get(tf)
        if df is None:
            return None
        d = closed_candles(df, tf, now) if CFG.signal_on_closed_candles else df
        if d is None or len(d) < 60:
            return None
        frames[tf] = d
        snaps[tf] = tf_snapshot(d, tf)
    fast, slow = DOSSIER_TIMEFRAMES
    regime, why = detect_regime(snaps[fast], snaps[slow], frames[fast])
    price = float(live_price) if live_price and math.isfinite(live_price) and live_price > 0 else float(frames[fast]["close"].iloc[-1])
    d24 = frames[fast].tail(96)   # 24h of 15m candles
    levels = {"high_24h": _f(d24["high"].max(), 4), "low_24h": _f(d24["low"].min(), 4),
              "supports_1h": snaps[slow]["supports"], "resistances_1h": snaps[slow]["resistances"],
              "supports_15m": snaps[fast]["supports"], "resistances_15m": snaps[fast]["resistances"]}
    base = None
    if baseline is not None:
        base = {"action": baseline.action, "confidence": round(float(baseline.confidence), 1), "reasons": list(baseline.reasons)[:8]}
    return MarketDossier(symbol=symbol, price=price, regime=regime, regime_reasons=why, timeframes=snaps, levels=levels,
                         trigger={"type": trigger, "detail": trigger_detail[:200]}, baseline=base, btc_context=btc_note,
                         headlines=[h for h in (sanitize_headline(x) for x in (headlines or [])[:5]) if h])


def signal_from_decision(action: str, confidence: float, note: str, dossier: MarketDossier | None = None) -> Signal:
    """Wrap the judge's authorized decision in the Signal type the rest of the bot (DB log, brain, risk) understands.
    The judge is recorded as the 'ai' expert so the brain's learning bookkeeping keeps working."""
    sign = 1.0 if action == "BUY" else -1.0 if action == "SELL" else 0.0
    reasons = [f"judge:{action}@{confidence:.0f}"]
    if dossier is not None:
        reasons.append(f"regime:{dossier.regime}")
    if note:
        reasons.append(note[:120])
    return Signal(action, float(confidence), reasons, {"ai": sign * float(confidence)}, {})


# -----------------------------------------------------------------------------------------------------
# Hybrid triggering: intra-candle breakout detection (polled every FAST_POLL_SECONDS)
# -----------------------------------------------------------------------------------------------------
@dataclass
class BreakoutEvent:
    kind: str            # 'volume_spike' | 'new_24h_high' | 'new_24h_low'
    detail: str


class BreakoutDetector:
    """Detects an abnormal move INSIDE the current 15m candle so the council does not have to wait for the close.

    Triggers when, for the forming candle,
      * volume >= BREAKOUT_VOLUME_MULT x the average of the previous 20 CLOSED candles (a partial candle already
        beating 2.5x the full-candle average is a real spike, so the test is conservative), or
      * price trades beyond the 24h high/low of the previous 96 closed candles by BREAKOUT_LEVEL_BUFFER_BPS.
    A per-symbol cooldown (FAST_TRACK_COOLDOWN_SECONDS) keeps one breakout from burning the AI quota every 10s.
    """

    def __init__(self) -> None:
        self._last_fire: dict[str, float] = {}

    def check(self, symbol: str, candles_15m: pd.DataFrame, live_price: float | None, *, now: float | None = None) -> BreakoutEvent | None:
        now = time.time() if now is None else now
        if now - self._last_fire.get(symbol, 0.0) < CFG.fast_track_cooldown_seconds:
            return None
        if candles_15m is None or len(candles_15m) < 22:
            return None
        forming = candles_15m.iloc[-1]
        closed = candles_15m.iloc[:-1]
        period = 15 * 60
        if float(forming["ts"].timestamp()) + period <= now:       # last row is actually closed: nothing is forming
            return None
        avg_vol = float(closed["volume"].tail(20).mean())
        event: BreakoutEvent | None = None
        if avg_vol > 0 and float(forming["volume"]) >= CFG.breakout_volume_mult * avg_vol:
            event = BreakoutEvent("volume_spike", f"forming-candle volume {float(forming['volume']) / avg_vol:.1f}x 20-candle average")
        price = _f(live_price) or _f(forming["close"])
        if event is None and price:
            hist = closed.tail(96)
            hi, lo = float(hist["high"].max()), float(hist["low"].min())
            buf = CFG.breakout_level_buffer_bps / 10_000.0
            if price >= hi * (1 + buf):
                event = BreakoutEvent("new_24h_high", f"price {price:.4f} above 24h high {hi:.4f}")
            elif price <= lo * (1 - buf):
                event = BreakoutEvent("new_24h_low", f"price {price:.4f} below 24h low {lo:.4f}")
        if event is not None:
            self._last_fire[symbol] = now
        return event
