"""TradingView webhook signal: validated, rate-limited, and it EXPIRES. It is only ever one more vote."""
from __future__ import annotations

import hmac
import json
import time

from config import CFG

_last_accept = {"ts": 0.0}
MAX_BODY = 4096


class WebhookError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def handle_payload(raw, now=None):
    if not CFG.tv_webhook_secret:
        raise WebhookError("disabled", 404)
    if len(raw) > MAX_BODY:
        raise WebhookError("payload too large", 413)
    try:
        d = json.loads(raw.decode("utf-8"))
    except Exception:
        raise WebhookError("invalid json", 400) from None
    if not isinstance(d, dict):
        raise WebhookError("invalid body", 400)
    if not hmac.compare_digest(str(d.get("secret", "")).encode(), CFG.tv_webhook_secret.encode()):
        raise WebhookError("forbidden", 403)
    action = str(d.get("action", "")).lower()
    if action not in ("buy", "sell", "neutral"):
        raise WebhookError("invalid action", 400)
    try:
        confidence = float(d.get("confidence", 0))
    except (TypeError, ValueError):
        raise WebhookError("invalid confidence", 400) from None
    if not 0 <= confidence <= 100:
        raise WebhookError("invalid confidence", 400)
    now = time.time() if now is None else now
    if now - _last_accept["ts"] < 1:
        raise WebhookError("rate limited", 429)
    note = (action + " " + str(d.get("note", "")))[:500].strip()
    score = confidence if action == "buy" else -confidence if action == "sell" else 0.0
    with open(CFG.tv_signal_path, "w") as f:
        json.dump({"ts": now, "score": score, "note": note}, f)
    _last_accept["ts"] = now
    return score, note


def current_score(now=None):
    """(score, note) for the latest alert, or (None, reason) when there is none / it has expired."""
    now = time.time() if now is None else now
    try:
        with open(CFG.tv_signal_path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None, "no signal"
    if now - float(d.get("ts", 0)) > CFG.tv_signal_ttl_minutes * 60:
        return None, "signal expired"
    return float(d.get("score", 0)), str(d.get("note", ""))
