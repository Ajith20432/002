"""Safe execution adapters with native protection and reconciliation helpers.

Hardening over the original executor (details in AUDIT_REPORT.md):

* **Retries are idempotent.** Before an order is re-sent after a timeout/network error we look it up by its
  client order id; if the first attempt actually reached the exchange we adopt it instead of doubling the
  position. Backoff is exponential with jitter (longer for rate limits) and bounded by EXEC_MAX_ATTEMPTS.
* **Fills are measured, not assumed.** ``FillResult`` carries the filled amount, average price, status and
  adverse slippage. Partial fills are reported (the bot sizes the position from what was filled), orders that
  come back "open" are polled, and base-currency fees on spot buys are deducted from the amount we believe we
  hold (otherwise the later sell is rejected for insufficient balance and the position can never be closed).
* **Slippage control.** A pre-trade check refuses entries whose price moved more than MAX_SLIPPAGE_BPS since
  the decision; EXEC_ORDER_STYLE=limit_ioc turns entries into price-capped immediate-or-cancel orders;
  exits always stay market orders (getting out beats price) but their slippage is measured and alerted.
* **Protected close.** A software exit used to be sent on top of the exchange-side stop/take orders. On a
  spot account that second sell can dump coins the bot never bought. ``close_protected`` checks whether the
  native leg already filled, cancels the legs, verifies, and only then sends the close - and re-arms the
  protection if the close fails, so a failed exit never leaves a live position naked.
* Exchange error text is passed through ``redact`` before it reaches logs/Telegram.
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

try:
    import ccxt
except ImportError:  # keep paper/replay/contract tests usable without the optional exchange client
    class _CCXTNetworkError(Exception):
        pass

    class _CCXTBaseError(Exception):
        pass

    class _CCXTFallback:
        NetworkError = _CCXTNetworkError
        BaseError = _CCXTBaseError

    ccxt = _CCXTFallback()  # type: ignore[assignment]

from config import CFG
from logging_setup import redact

logger = logging.getLogger("zora.execution")


class _NeverRaised(Exception):
    """Placeholder for ccxt classes missing from this ccxt build; must never match a real error."""


_AuthError: type[Exception] = getattr(ccxt, "AuthenticationError", _NeverRaised)
_RateLimit: type[Exception] = getattr(ccxt, "RateLimitExceeded", _NeverRaised)
_NotFound: type[Exception] = getattr(ccxt, "OrderNotFound", _NeverRaised)
_TERMINAL = {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}
_DEAD = {"canceled", "cancelled", "expired", "rejected"}


# --------------------------------------------------------------------------- small helpers
def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _err(e: BaseException) -> str:
    """Exchange/network error text, scrubbed of credentials and truncated (ccxt can embed whole HTML pages)."""
    return redact(str(e))[:300]


def new_client_id(prefix: str = "zora") -> str:
    """Alphanumeric only and <= 32 chars: OKX documents alphanumeric-only client ids (the old 'zora-<ms>' with a
    hyphen is rejected there); Binance allows 36 chars, HTX 64."""
    return f"{prefix}{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"[:32]


@dataclass
class FillResult:
    order_id: str | None
    client_id: str
    requested: float
    filled: float                      # NET amount we hold/closed (base-currency fees already deducted on spot buys)
    price: float                       # average fill price (falls back to the reference price)
    ref_price: float | None
    status: str                        # 'filled' | 'partial'
    slippage_bps: float | None         # adverse-positive: > 0 means we did worse than the reference price
    gross_filled: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def complete(self) -> bool:
        return self.gross_filled >= self.requested * (1 - 1e-4)


class OrderRejected(Exception):
    """The exchange definitively refused the order, or every network attempt failed with a verified-empty
    lookup (nothing is live either way)."""

    def __init__(self, message: str, network: bool = False) -> None:
        super().__init__(message)
        self.network = network


class OrderStateUnknown(Exception):
    """Network failure on every attempt and the order could not be looked up: it MAY be live on the exchange."""


class OrderAuthError(Exception):
    """Credentials/permissions problem. Retrying will not help; trading must stop."""


@dataclass
class CloseOutcome:
    kind: str                          # native | closed | partial | failed | unknown
    price: float | None = None
    filled: float = 0.0
    leg: str | None = None             # 'stop' | 'take' for kind == native
    protection: dict | None = None     # protection dict to store on the position when it changed (re-armed)


# --------------------------------------------------------------------------- paper
class PaperExecutor:
    live = False
    last_amount: float | None = None
    last_fill: FillResult | None = None
    last_error: str | None = None
    state_unknown = False
    fatal = False

    def open(self, exchange, symbol, action, amount, price, client_id: str | None = None):
        return price

    def close(self, exchange, symbol, action, amount, price):
        return price

    def place_protection(self, *args, **kwargs):
        return {"stop": "paper", "take": "paper"}

    def cancel_protection(self, *args, **kwargs):
        return True


# --------------------------------------------------------------------------- live
class LiveExecutor:
    live = True

    def __init__(self, telegram_alert: Callable[[str], None] = lambda msg: None) -> None:
        self.alert = telegram_alert
        self.last_amount: float | None = None     # amount actually held/closed by the most recent order (exchange precision, net of fees)
        self.last_fill: FillResult | None = None
        self.last_error: str | None = None
        self.state_unknown = False                # True: an order may be live that we could not prove either way
        self.fatal = False                        # True: authentication/permission failure - the bot must stop trading
        self._lock = threading.RLock()            # one order flow at a time

    # ------------------------------------------------------------------ sizing / order shape
    def _sized_amount(self, exchange, symbol: str, amount: float, price: float | None = None) -> float | None:
        amount_f = _num(amount)
        if amount_f is None or amount_f <= 0:
            return None
        try:
            market = exchange.market(symbol)
            sized = float(exchange.amount_to_precision(symbol, amount_f))
            limits = (market.get("limits", {}) or {})
            min_amount = (limits.get("amount", {}) or {}).get("min")
            if min_amount and sized < min_amount:
                return None
            min_cost = (limits.get("cost", {}) or {}).get("min")
            if min_cost and price and sized * price < min_cost:
                return None
            return sized if sized > 0 else None
        except Exception:  # noqa: BLE001 - best-effort: let the exchange itself reject a truly invalid size
            return amount_f

    @staticmethod
    def _market_buy_needs_price(exchange, side: str) -> bool:
        """HTX (and a few others) price a MARKET BUY by cost = amount * price, so ccxt refuses it without a price."""
        opts = getattr(exchange, "options", None) or {}
        return side == "buy" and bool(opts.get("createMarketBuyOrderRequiresPrice", True))

    def _order_spec(self, exchange, symbol: str, side: str, ref_price: float | None, style: str,
                    client_id: str) -> tuple[str, float | None, dict]:
        params: dict[str, Any] = {"clientOrderId": client_id}
        band = max(0.0, CFG.max_slippage_bps) / 10_000.0
        if style == "limit_ioc" and ref_price and band > 0:
            limit = ref_price * (1 + band) if side == "buy" else ref_price * (1 - band)
            try:
                limit = float(exchange.price_to_precision(symbol, limit))
            except Exception:  # noqa: BLE001 - unrounded price is still a valid cap
                pass
            params["timeInForce"] = "IOC"
            return "limit", limit, params
        price = ref_price if (ref_price and self._market_buy_needs_price(exchange, side)) else None
        return "market", price, params

    @staticmethod
    def _backoff(attempt: int, rate_limited: bool) -> float:
        base = max(0.0, CFG.exec_backoff_seconds) * (2 ** attempt) * (3 if rate_limited else 1)
        return min(base, 30.0) * random.uniform(0.8, 1.2)

    # ------------------------------------------------------------------ idempotent send
    @staticmethod
    def _find_order(exchange, symbol: str, client_id: str, since_ms: int) -> tuple[dict | None, bool]:
        """(order, lookup_worked). Look for an order whose creation response we never received."""
        worked = False
        has = getattr(exchange, "has", None) or {}
        for method in ("fetch_open_orders", "fetch_closed_orders", "fetch_orders"):
            fn = getattr(exchange, method, None)
            if fn is None or has.get(method) is False:
                continue
            try:
                orders = fn(symbol, since_ms, 50)
            except Exception as e:  # noqa: BLE001
                logger.debug("%s lookup failed: %s", method, _err(e))
                continue
            worked = True
            for o in orders or []:
                cid = o.get("clientOrderId") or (o.get("info") or {}).get("clientOrderId")
                if cid == client_id:
                    return o, True
        return None, worked

    def _send(self, exchange, symbol: str, order_type: str, side: str, amount: float, price: float | None,
              params: dict, client_id: str) -> tuple[dict, bool]:
        """create_order with bounded, idempotent retries. Returns (order, adopted_from_lookup)."""
        attempts = max(1, int(CFG.exec_max_attempts))
        started_ms = int(time.time() * 1000) - 5_000
        last: BaseException | None = None
        lookups_ok = True
        rate_limited = False
        for attempt in range(attempts):
            if attempt:
                time.sleep(self._backoff(attempt - 1, rate_limited))
                found, worked = self._find_order(exchange, symbol, client_id, started_ms)
                if found is not None:
                    logger.warning("order %s reached the exchange despite the network error; adopting it", client_id)
                    return found, True
                lookups_ok = lookups_ok and worked
            try:
                return exchange.create_order(symbol, order_type, side, amount, price, params), False
            except _AuthError as e:
                self.fatal = True
                raise OrderAuthError(_err(e)) from e
            except ccxt.NetworkError as e:
                last = e
                rate_limited = isinstance(e, _RateLimit)
                logger.warning("%s %s attempt %d/%d failed (network): %s", symbol, side, attempt + 1, attempts, _err(e))
            except ccxt.BaseError as e:
                raise OrderRejected(_err(e)) from e
        # attempts exhausted: one last look so we never report "failed" for an order that is actually live
        found, worked = self._find_order(exchange, symbol, client_id, started_ms)
        if found is not None:
            return found, True
        lookups_ok = lookups_ok and worked
        detail = _err(last) if last else "unknown"
        if lookups_ok:
            raise OrderRejected(f"not accepted after {attempts} attempts (network): {detail}", network=True)
        raise OrderStateUnknown(f"network failure after {attempts} attempts and the order could not be looked up: {detail}")

    # ------------------------------------------------------------------ fills
    def _settle(self, exchange, symbol: str, side: str, order: dict, requested: float,
                ref_price: float | None, client_id: str) -> FillResult | None:
        """Turn an exchange order dict into a FillResult (polling if it is still open). None => nothing filled."""
        order = dict(order or {})
        oid = order.get("id")
        status = str(order.get("status") or "").lower()
        polls = max(0, int(CFG.exec_fill_poll_attempts))
        while status and status not in _TERMINAL and oid and polls > 0 and hasattr(exchange, "fetch_order"):
            time.sleep(max(0.0, CFG.exec_fill_poll_seconds))
            try:
                order = exchange.fetch_order(oid, symbol) or order
            except Exception as e:  # noqa: BLE001 - keep what we already know
                logger.debug("fetch_order(%s) failed while polling: %s", oid, _err(e))
            status = str(order.get("status") or "").lower()
            polls -= 1
        if status and status not in _TERMINAL and oid:
            try:                       # never leave the remainder of an order working in the background
                exchange.cancel_order(oid, symbol)
                order = exchange.fetch_order(oid, symbol) or order
                status = str(order.get("status") or "").lower()
            except Exception as e:  # noqa: BLE001
                logger.warning("could not cancel the unfinished remainder of %s: %s", oid, _err(e))

        filled = _num(order.get("filled"))
        if filled is None:
            # legacy/minimal order dicts: an order with no fill data that is not explicitly dead is treated as filled
            filled = 0.0 if status in _DEAD else requested
        if status and status not in _TERMINAL and filled <= 0:
            self.state_unknown = True
            self.last_error = f"order {oid} is still {status} with no confirmed fill"
            self.alert(f"[live] {symbol} {side} order {oid} unresolved ({status}); check the exchange before trading it again")
            return None
        if filled <= 0:
            self.last_error = "order was not filled (price band / liquidity)"
            return None

        price = _num(order.get("average")) or _num(order.get("price"))
        if not price:
            cost = _num(order.get("cost"))
            price = cost / filled if cost else None
        price = price or ref_price or 0.0

        gross = filled
        if side == "buy":
            filled = max(0.0, filled - self._base_fee(exchange, symbol, order))

        slip: float | None = None
        if ref_price and ref_price > 0 and price > 0:
            slip = (price - ref_price) / ref_price * 1e4 if side == "buy" else (ref_price - price) / ref_price * 1e4
        fill = FillResult(order_id=str(oid) if oid is not None else None, client_id=client_id, requested=requested,
                          filled=filled, price=float(price), ref_price=ref_price,
                          status="filled" if gross >= requested * (1 - 1e-4) else "partial",
                          slippage_bps=slip, gross_filled=gross, raw=order)
        if fill.status == "partial":
            self.alert(f"[live] {symbol} {side} PARTIAL fill: {gross:g} of {requested:g}")
        if slip is not None and CFG.max_slippage_bps > 0 and slip > CFG.max_slippage_bps:
            self.alert(f"[live] {symbol} {side} slippage {slip:.1f}bps exceeded the {CFG.max_slippage_bps:.0f}bps cap")
        return fill

    @staticmethod
    def _base_fee(exchange, symbol: str, order: dict) -> float:
        """Fee charged in the BASE asset on a spot buy. Without deducting it we'd try to sell coins we don't have."""
        try:
            if not (exchange.market(symbol) or {}).get("spot"):
                return 0.0
        except Exception:  # noqa: BLE001
            return 0.0
        base = symbol.split("/")[0].split(":")[0]
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        total = 0.0
        for f in fees:
            if isinstance(f, dict) and f.get("currency") == base:
                total += _num(f.get("cost")) or 0.0
        return total

    def _place(self, exchange, symbol: str, side: str, amount: float, ref_price: float | None = None, *,
               kind: str = "open", client_id: str | None = None) -> FillResult | None:
        with self._lock:
            self.last_fill, self.last_error, self.state_unknown = None, None, False
            sized = self._sized_amount(exchange, symbol, amount, ref_price)
            self.last_amount = sized
            if not sized:
                self.last_error = "below the exchange minimum"
                self.alert(f"[live] {symbol} {side} skipped — below the exchange minimum")
                return None
            client_id = client_id or new_client_id()
            style = CFG.exec_order_style if kind == "open" else "market"   # exits must always get out
            order_type, order_price, params = self._order_spec(exchange, symbol, side, ref_price, style, client_id)
            try:
                order, adopted = self._send(exchange, symbol, order_type, side, sized, order_price, params, client_id)
            except OrderAuthError as e:
                self.last_error = f"authentication/permission error: {e}"
                self.alert(f"[live] {symbol} {side} AUTH ERROR: {e}. Trading is halted until the API keys are fixed.")
                return None
            except OrderRejected as e:
                self.last_error = str(e)
                suffix = "FAILED after retry (network)" if e.network else "rejected"
                self.alert(f"[live] {symbol} {side} {suffix}: {e}" + ("; reconcile manually" if e.network else ""))
                return None
            except OrderStateUnknown as e:
                self.state_unknown = True
                self.last_error = str(e)
                self.alert(f"[live] {symbol} {side} FAILED after retry (network) and its state is UNKNOWN: {e}; reconcile manually")
                return None
            fill = self._settle(exchange, symbol, side, order, sized, ref_price, client_id)
            if fill is None:
                if not self.state_unknown:
                    self.alert(f"[live] {symbol} {side} not filled ({self.last_error})")
                return None
            self.last_fill = fill
            self.last_amount = fill.filled
            if adopted:
                self.alert(f"[live] {symbol} {side}: recovered an order that had reached the exchange ({fill.order_id})")
            return fill

    def _price_still_valid(self, exchange, symbol: str, side: str, ref_price: float | None) -> bool:
        """Refuse an entry whose price has run away from the decision price (stale signal / fast market)."""
        cap = CFG.max_slippage_bps
        if cap <= 0 or not ref_price or ref_price <= 0 or not hasattr(exchange, "fetch_ticker"):
            return True
        try:
            t = exchange.fetch_ticker(symbol)
            live = _num(t.get("ask") if side == "buy" else t.get("bid")) or _num(t.get("last"))
        except Exception as e:  # noqa: BLE001 - cannot check -> do not block (the quality guard already ran)
            logger.debug("pre-trade ticker unavailable for %s: %s", symbol, _err(e))
            return True
        if not live or live <= 0:
            return True
        adverse = (live - ref_price) / ref_price * 1e4 if side == "buy" else (ref_price - live) / ref_price * 1e4
        if adverse > cap:
            self.last_error = f"price moved {adverse:.1f}bps against the entry since the decision (cap {cap:.0f}bps)"
            self.alert(f"[live] {symbol} {side} entry skipped — {self.last_error}")
            return False
        return True

    # ------------------------------------------------------------------ open / close
    def open(self, exchange, symbol: str, action: str, amount: float, price: float,
             client_id: str | None = None) -> float | None:
        side = "buy" if action == "BUY" else "sell"
        self.last_fill, self.last_error, self.state_unknown, self.last_amount = None, None, False, None
        if not self._price_still_valid(exchange, symbol, side, price):
            return None
        fill = self._place(exchange, symbol, side, amount, price, kind="open", client_id=client_id)
        if fill is None:
            return None
        self.alert(f"[live] OPENED {action} {symbol} amount={fill.filled:g} @ ~{fill.price}")
        return float(fill.price)

    def close(self, exchange, symbol: str, action: str, amount: float, price: float,
              client_id: str | None = None) -> float | None:
        side = "sell" if action == "BUY" else "buy"
        fill = self._place(exchange, symbol, side, amount, price, kind="close", client_id=client_id)
        if fill is None:
            return None
        self.alert(f"[live] CLOSED {action} {symbol} amount={fill.filled:g} @ ~{fill.price}")
        return float(fill.price)

    # ------------------------------------------------------------------ native protection
    def _native_supported(self, exchange, symbol: str) -> bool:
        try:
            if hasattr(exchange, "featureValue"):
                return bool(exchange.featureValue(symbol, "createOrder", "stopLossPrice") or
                            exchange.featureValue(symbol, "createOrder", "takeProfitPrice"))
        except Exception:  # noqa: BLE001
            pass
        # Never assume support: protection is safety-critical.
        return False

    def place_protection(self, exchange, symbol: str, action: str, amount: float, stop: float, take: float | None):
        """Place exchange-native conditional exits. Refuse to trade unprotected if required.

        The stop is the safety-critical leg and is always required. The take-profit leg is required unless
        NATIVE_TAKE_PROFIT_REQUIRED=false (or `take` is None, e.g. a runner), in which case a rejected take leg
        (typical on spot, where two sell orders for the same balance cannot coexist) degrades to a software
        take-profit with a native stop.
        """
        if not self._native_supported(exchange, symbol):
            msg = f"[live] {symbol} native stop/take-profit capability not confirmed by exchange adapter"
            self.alert(msg)
            if CFG.native_protection_required:
                return None
            return {"software_only": True}
        exit_side = "sell" if action == "BUY" else "buy"
        base: dict[str, Any]
        try:
            # reduceOnly is a derivatives flag; spot markets reject it. Unknown market -> keep it (safer on swaps).
            base = {} if exchange.market(symbol).get("spot") else {"reduceOnly": True}
        except Exception:  # noqa: BLE001
            base = {"reduceOnly": True}
        orders: dict[str, Any] = {}
        sl_id, tp_id = new_client_id("zoraSL"), new_client_id("zoraTP")
        with self._lock:
            try:
                orders["stop"], _ = self._send(exchange, symbol, "market", exit_side, amount, None,
                                               {"stopLossPrice": stop, **base, "clientOrderId": sl_id}, sl_id)
            except (OrderRejected, OrderAuthError, OrderStateUnknown) as e:
                self._protection_failed(symbol, "stop", e)
                return None
            if take is None:
                orders["take_software"] = True
            else:
                try:
                    orders["take"], _ = self._send(exchange, symbol, "market", exit_side, amount, None,
                                                   {"takeProfitPrice": take, **base, "clientOrderId": tp_id}, tp_id)
                except (OrderRejected, OrderAuthError, OrderStateUnknown) as e:
                    if CFG.native_take_profit_required:
                        self._protection_failed(symbol, "take-profit", e)
                        # Don't leave half a bracket behind: an orphaned stop would later fire on its own
                        # and open an unwanted position in the opposite direction.
                        self.cancel_protection(exchange, symbol, orders)
                        return None
                    logger.warning("%s native take-profit not armed (%s); native stop + software take-profit", symbol, e)
                    self.alert(f"[live] {symbol} take-profit could not be armed natively ({e}); stop is native, take-profit is software")
                    orders["take_software"] = True
        self.alert(f"[live] NATIVE PROTECTION armed {symbol}: SL={stop} TP={take}")
        return orders

    def _protection_failed(self, symbol: str, leg: str, e: BaseException) -> None:
        if isinstance(e, OrderStateUnknown):
            self.state_unknown = True
        self.last_error = f"{leg} protection: {e}"
        self.alert(f"[live] native protection FAILED for {symbol}: {leg}: {e}")

    def protection_status(self, exchange, symbol: str, protection: dict | None) -> tuple[str, str | None, float | None]:
        """('none'|'armed'|'triggered'|'lost'|'unknown', leg, fill_price).

        'triggered': a native leg filled (the exchange already closed the position).
        'lost':      a leg was cancelled/expired by the exchange - we are no longer protected natively.
        'unknown':   the exchange could not tell us; callers must NOT assume either way.
        """
        if not protection or protection.get("software_only"):
            return "none", None, None
        legs = [(k, (protection.get(k) or {}).get("id") if isinstance(protection.get(k), dict) else None)
                for k in ("stop", "take")]
        legs = [(k, oid) for k, oid in legs if oid]
        if not legs:
            return "none", None, None
        unknown, lost = False, None
        for key, oid in legs:
            try:
                order = exchange.fetch_order(oid, symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s: cannot read %s protection %s: %s", symbol, key, oid, _err(e))
                unknown = True
                continue
            st = str(order.get("status", "")).lower()
            if st in ("closed", "filled"):
                return "triggered", key, _num(order.get("average")) or _num(order.get("price"))
            if st in _DEAD:
                lost = key
        if unknown:
            return "unknown", None, None
        if lost:
            return "lost", lost, None
        return "armed", None, None

    def protection_triggered(self, exchange, symbol: str, protection: dict | None):
        """
        Did the exchange already close the position via its native stop/take order?
        Returns ("stop"|"take", average_fill_price_or_None) or None. Without this, the bot's own
        price check would fire a SECOND closing order on top of the one the exchange already filled.
        """
        state, leg, fill = self.protection_status(exchange, symbol, protection)
        return (leg, fill) if state == "triggered" and leg else None

    def _cancel_legs(self, exchange, symbol: str, protection: dict | None) -> tuple[list[str], list[str]]:
        """(failed, ambiguous). A leg the exchange no longer knows (OrderNotFound) is 'ambiguous': it may have been
        cancelled OR filled and archived - callers must re-verify before acting on it."""
        failed: list[str] = []
        ambiguous: list[str] = []
        if not protection or protection.get("software_only"):
            return failed, ambiguous
        for key in ("stop", "take"):
            order = protection.get(key) or {}
            oid = order.get("id") if isinstance(order, dict) else None
            if not oid:
                continue
            try:
                exchange.cancel_order(oid, symbol)
            except _NotFound:
                ambiguous.append(key)
            except ccxt.BaseError as e:
                failed.append(key)
                self.alert(f"[live] failed to cancel {key} protection {oid}: {_err(e)}")
        return failed, ambiguous

    def cancel_protection(self, exchange, symbol: str, protection: dict | None) -> bool:
        failed, _ambiguous = self._cancel_legs(exchange, symbol, protection)
        return not failed

    def _cancel_sibling(self, exchange, symbol: str, protection: dict | None, filled_leg: str | None) -> None:
        sibling = "take" if filled_leg == "stop" else "stop"
        self.cancel_protection(exchange, symbol, {sibling: (protection or {}).get(sibling)})

    def close_protected(self, exchange, symbol: str, action: str, amount: float, price: float,
                        protection: dict | None, stop: float | None, take: float | None,
                        position_size: float | None = None) -> CloseOutcome:
        """Close (all or part of) a position that may be guarded by native orders WITHOUT ever double-selling.

        1. native leg already filled?            -> report it, send nothing
        2. state unreadable?                     -> 'unknown': send nothing, retry next cycle
        3. cancel the legs and re-verify         -> a leg that filled during the cancel is reported as native
        4. send the market close
        5. close failed/partial                  -> re-arm protection for what is still open

        `amount` is what to close; `position_size` is the whole position when this is only a scale-out leg (TP1/TP2).
        If the close fails or fills partly, protection is re-armed for what is STILL OPEN (position_size minus the
        fill). After a fully successful scale-out the caller re-arms the remainder itself via `rearm`.
        """
        total = float(position_size) if position_size else float(amount)
        with self._lock:
            state, leg, fill = self.protection_status(exchange, symbol, protection)
            if state == "triggered":
                self._cancel_sibling(exchange, symbol, protection, leg)
                return CloseOutcome("native", fill, float(amount), leg)
            if state == "unknown":
                self.alert(f"[live] {symbol} exit postponed: cannot verify the native protection orders; will retry")
                return CloseOutcome("unknown")
            has_native = state in ("armed", "lost")
            if has_native:
                failed, ambiguous = self._cancel_legs(exchange, symbol, protection)
                if failed or ambiguous:
                    state2, leg2, fill2 = self.protection_status(exchange, symbol, protection)
                    if state2 == "triggered":
                        self._cancel_sibling(exchange, symbol, protection, leg2)
                        return CloseOutcome("native", fill2, float(amount), leg2)
                    if failed:                         # a live leg could not be cleared: do not risk a double sell
                        return CloseOutcome("failed")
                    if state2 in ("armed", "unknown"):  # 'not found' but we cannot prove it is gone
                        return CloseOutcome("unknown")
            fill_price = self.close(exchange, symbol, action, amount, price)
            result = self.last_fill
            if fill_price is None or result is None:
                if self.state_unknown:
                    return CloseOutcome("unknown")
                return CloseOutcome("failed", protection=self.rearm(exchange, symbol, action, total, stop, take,
                                                                    has_native, protection))
            if not result.complete:
                remaining = max(0.0, total - result.gross_filled)
                prot = (self.rearm(exchange, symbol, action, remaining, stop, take, has_native, protection)
                        if remaining > 0 else protection)
                return CloseOutcome("partial", result.price, result.filled, protection=prot)
            return CloseOutcome("closed", result.price, result.filled)

    def rearm(self, exchange, symbol: str, action: str, amount: float, stop: float | None, take: float | None,
              had_native: bool, old: dict | None) -> dict | None:
        """Place fresh native protection for `amount` (what is still open). Falls back to software-only with a
        CRITICAL alert if it cannot be armed."""
        if not had_native or stop is None or amount <= 0:
            return old
        prot = self.place_protection(exchange, symbol, action, amount, stop, take)
        if prot is None:
            self.alert(f"[live] CRITICAL {symbol}: native protection could NOT be re-armed for the open remainder. "
                       "Check the exchange NOW.")
            return {"software_only": True}
        return prot

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, exchange, symbol: str, expected_position=None) -> dict:
        """Return exchange truth. If it cannot be proven, return safe=False (callers must then refuse entries)."""
        try:
            open_orders = exchange.fetch_open_orders(symbol)
        except Exception as e:  # noqa: BLE001
            return {"safe": False, "reason": f"cannot fetch open orders: {_err(e)}", "open_orders": []}
        result: dict[str, Any] = {"safe": True, "open_orders": open_orders, "position": None}
        try:
            try:
                is_spot = bool(exchange.market(symbol).get("spot"))
            except Exception:  # noqa: BLE001
                is_spot = False
            has = getattr(exchange, "has", None) or {}
            if not is_spot and has.get("fetchPositions"):
                positions = exchange.fetch_positions([symbol])
                result["position"] = next(
                    (p for p in positions if abs(float(p.get("contracts") or p.get("contractSize") or 0)) > 0), None)
            elif has.get("fetchBalance"):
                base = symbol.split("/")[0]
                balance = exchange.fetch_balance()
                result["position"] = {"contracts": float((balance.get("total") or {}).get(base) or 0), "spot_balance": True}
        except Exception as e:  # noqa: BLE001
            return {"safe": False, "reason": f"cannot reconcile position: {_err(e)}", "open_orders": open_orders}
        return result
