"""Non-trading exchange smoke test.

Loads public market metadata for Binance/Bybit/OKX/HTX and reports CCXT
capabilities. It never submits an order. Private credentials are only checked
for presence when --private-check is requested.
"""
from __future__ import annotations

import argparse
import json
import os

import ccxt

from config import CFG

SUPPORTED = ("binance", "bybit", "okx", "htx")


def smoke(private_check: bool = False) -> dict:
    out = {}
    for eid in SUPPORTED:
        try:
            ex = getattr(ccxt, eid)({"enableRateLimit": True, "options": {"adjustForTimeDifference": True}})
            ex.load_markets()
            symbol = CFG.symbol if CFG.symbol in ex.markets else next(iter(ex.markets), CFG.symbol)
            features = {}
            if hasattr(ex, "featureValue"):
                for f in ("stopLossPrice", "takeProfitPrice"):
                    try:
                        features[f] = bool(ex.featureValue(symbol, "createOrder", f))
                    except Exception:
                        features[f] = False
            out[eid] = {"ok": True, "symbol_checked": symbol,
                        "createOrder": bool(ex.has.get("createOrder")),
                        "fetchOHLCV": bool(ex.has.get("fetchOHLCV")),
                        "native_protection": features}
            if private_check:
                prefix = eid.upper()
                out[eid]["credentials_present"] = bool(os.getenv(f"{prefix}_API_KEY") and os.getenv(f"{prefix}_API_SECRET"))
        except Exception as exc:
            out[eid] = {"ok": False, "error": str(exc)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--private-check", action="store_true", help="only checks env credential presence; never places orders")
    args = ap.parse_args(argv)
    report = smoke(args.private_check)
    print(json.dumps(report, indent=2))
    return 0 if any(v.get("ok") for v in report.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
