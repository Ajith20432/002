"""Multi-exchange factory and capability registry for Binance, Bybit, OKX and HTX."""
from __future__ import annotations

import os

import ccxt

from config import CFG

SUPPORTED = ("binance", "bybit", "okx", "htx")


class ExchangeManager:
    def __init__(self):
        self._instances: dict = {}

    def _credentials(self, exchange_id: str):
        prefix = exchange_id.upper()
        is_default = exchange_id == CFG.exchange_id
        key = os.getenv(f"{prefix}_API_KEY") or (CFG.api_key if is_default else "")
        secret = os.getenv(f"{prefix}_API_SECRET") or (CFG.api_secret if is_default else "")
        password = os.getenv(f"{prefix}_API_PASSWORD") or (CFG.api_password if is_default else "")
        return key, secret, password

    def get(self, exchange_id=None, authenticated=False):
        eid = (exchange_id or CFG.exchange_id).lower()
        if eid not in SUPPORTED:
            raise ValueError(f"Unsupported exchange: {eid}. Supported: {', '.join(SUPPORTED)}")
        cache_key = (eid, bool(authenticated))
        if cache_key in self._instances:
            return self._instances[cache_key]
        params = {"enableRateLimit": True, "options": {"adjustForTimeDifference": True}}
        if authenticated:       # credentials are attached ONLY when explicitly requested (live executor)
            key, secret, password = self._credentials(eid)
            if key and secret:
                params.update(apiKey=key, secret=secret)
            if password:
                params["password"] = password
        ex = getattr(ccxt, eid)(params)
        ex.load_markets()
        self._instances[cache_key] = ex
        return ex

    def capabilities(self, exchange_id=None):
        ex = self.get(exchange_id, authenticated=False)
        symbol = CFG.symbol if CFG.symbol in ex.markets else next(iter(ex.markets), CFG.symbol)

        def feature(name: str) -> bool:
            try:
                return bool(hasattr(ex, "featureValue") and ex.featureValue(symbol, "createOrder", name))
            except Exception:
                return False

        return {"id": ex.id, "name": ex.name, "symbol_checked": symbol,
                "fetchOHLCV": bool(ex.has.get("fetchOHLCV")), "createOrder": bool(ex.has.get("createOrder")),
                "fetchPositions": bool(ex.has.get("fetchPositions")),
                "stopLossPrice": feature("stopLossPrice"), "takeProfitPrice": feature("takeProfitPrice")}

    def all_capabilities(self):
        return {eid: self.capabilities(eid) for eid in SUPPORTED}


MANAGER = ExchangeManager()
