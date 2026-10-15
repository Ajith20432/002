"""Optional CCXT sandbox smoke harness. Never creates an order.

Use dedicated exchange testnet/sandbox keys in a separate environment. The
exchange adapter is switched to sandbox mode before market loading when CCXT
supports it. HTX/testnet availability is reported rather than assumed.
"""
from __future__ import annotations

import argparse
import json
import os

import ccxt

EXCHANGES = ("binance", "bybit", "okx", "htx")


def run(check_private: bool = False) -> dict:
    report = {}
    for eid in EXCHANGES:
        try:
            ex = getattr(ccxt, eid)({"enableRateLimit": True})
            sandbox_supported = hasattr(ex, "set_sandbox_mode")
            sandbox_error = None
            if sandbox_supported:
                try:
                    ex.set_sandbox_mode(True)
                except Exception as exc:
                    sandbox_error = str(exc)
            ex.load_markets()
            item = {
                "ok": True,
                "sandbox_method_available": sandbox_supported,
                "sandbox_error": sandbox_error,
                "markets": len(ex.markets),
                "has_createOrder": bool(ex.has.get("createOrder")),
                "has_fetchBalance": bool(ex.has.get("fetchBalance")),
                "orders_submitted": 0,
            }
            if check_private:
                p = eid.upper()
                item["test_credentials_present"] = bool(os.getenv(f"{p}_API_KEY") and os.getenv(f"{p}_API_SECRET"))
            report[eid] = item
        except Exception as exc:
            report[eid] = {"ok": False, "orders_submitted": 0, "error": str(exc)}
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ZORA exchange sandbox smoke test (no orders)")
    ap.add_argument("--private-check", action="store_true")
    args = ap.parse_args(argv)
    report = run(args.private_check)
    print(json.dumps(report, indent=2))
    return 0 if any(x.get("ok") for x in report.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
