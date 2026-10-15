"""bot.py — ZORA command entry point.

Paper mode is always the default. `live` needs LIVE_TRADING + I_UNDERSTAND_THE_RISK + API keys AND a
typed confirmation. Every entry, paper or live, passes through the same risk engine.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal as _signal
import time
from datetime import datetime, timezone

import db
from brain import BRAIN
from config import CFG
from costs import COST_MODEL
from risk import RiskEngine, apply_tier_fill, manage_stop, next_tier_exit, position_exposure, runner_trail

logger = logging.getLogger("zora.bot")


# --------------------------------------------------------------------------- pure helpers (unit-tested)
def check_exit(position: dict, price: float) -> str | None:
    """'stop' | 'take' | None for the current price (software-monitored levels)."""
    stop, take = position.get("stop"), position.get("take")
    if position["action"] == "BUY":
        if stop is not None and price <= stop:
            return "stop"
        if take is not None and price >= take:
            return "take"
    else:
        if stop is not None and price >= stop:
            return "stop"
        if take is not None and price <= take:
            return "take"
    return None


def settle_trade(p: dict, exit_price: float) -> float:
    """Net PnL of a closed position: price move minus round-trip fees."""
    direction = 1 if p["action"] == "BUY" else -1
    gross = (exit_price - p["entry"]) * p["size"] * direction
    return gross - COST_MODEL.round_trip_fee(p["entry"], exit_price, p["size"])


def sync_exposure(risk: RiskEngine, positions: dict) -> None:
    """Exposure is recomputed from what is actually open (each position's persisted risk budget), so it can never
    drift after restarts/failures."""
    risk.set_exposure(sum(position_exposure(p) for p in positions.values() if p))


def restore_state(conn, symbols):
    """Rebuild equity, peak, exposure and open positions from the database after a restart."""
    equity = db.summary(conn)["latest_equity"]
    peak = max(db.peak_equity(conn) or equity, equity)
    risk = RiskEngine(equity)
    risk.peak_equity = peak
    positions: dict[str, dict | None] = {s: None for s in symbols}
    for p in db.load_open_positions(conn):
        if p["symbol"] not in positions:
            logger.warning("open position for %s is not in SYMBOLS; it will still be managed", p["symbol"])
        positions[p["symbol"]] = p
    sync_exposure(risk, positions)
    saved = db.get_state_json(conn, "risk", {}) or {}
    if isinstance(saved, dict) and saved.get("day") == str(datetime.now(timezone.utc).date()):
        # same UTC day: a restart must not reset the daily-loss window or release an active circuit breaker
        risk.restore(saved.get("daily_start"), saved.get("peak_equity"), saved.get("entries_locked_until"))
    risk.update_equity(equity)
    return equity, risk, positions


def entry_blockers(*, signal_action: str, paused: bool, live: bool, stale: str | None,
                   quality: tuple[bool, str] | None, degraded: tuple[bool, float] | None,
                   ai_block: tuple[bool, str] | None = None) -> list[str]:
    """Reasons a NEW entry must not happen. Empty list = clear. Guards never block exits."""
    reasons: list[str] = []
    if paused:
        reasons.append("entries paused")
    if stale:
        reasons.append(f"stale data ({stale})")
    if signal_action == "SELL" and live and not CFG.allow_shorts:
        reasons.append("short entries disabled live (ALLOW_SHORTS=false)")
    if quality is not None and not quality[0]:
        reasons.append(f"market quality: {quality[1]}")
    if degraded is not None and degraded[0]:
        reasons.append(f"strategy degraded (rolling PF {degraded[1]:.2f})")
    if ai_block is not None and not ai_block[0] and CFG.ai_entry_block_on_unavailable:
        reasons.append(f"AI safety: {ai_block[1]}")
    return reasons


def doctor() -> int:
    """Offline config / database / brain check. Returns the number of problems found."""
    problems = list(CFG.validate())
    try:
        conn = db.connect()
        check = db.integrity_check(conn)
        if check != "ok":
            problems.append(f"database integrity check failed: {check}")
        stale = db.pending_intents(conn)
        if stale:
            problems.append(f"{len(stale)} unresolved order intent(s) in the database: a previous run may have died mid-order; "
                            "verify the exchange, then start the bot and send /unblock")
        db.close(conn)
    except Exception as e:  # noqa: BLE001 - report, don't crash the checker
        problems.append(f"database {CFG.db_path} cannot be opened: {e}")
    from config import env_file_warnings
    problems.extend(env_file_warnings())
    try:
        BRAIN.snapshot()
    except Exception as e:  # noqa: BLE001
        problems.append(f"brain state unreadable: {e}")
    for p in problems:
        print(" -", p)
    return len(problems)


def ai_available() -> bool:
    return any((CFG.groq_api_key, CFG.openrouter_api_key, CFG.gemini_api_key, CFG.anthropic_api_key, CFG.openai_api_key))


# --------------------------------------------------------------------------- historical replay helpers
def resample_ohlcv(df, tf: str):
    """Aggregate base candles into a higher timeframe (epoch-aligned, UTC)."""
    from guards import timeframe_seconds
    out = (df.set_index("ts").resample(f"{timeframe_seconds(tf)}s", origin="epoch")
           .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
           .dropna().reset_index())
    return out


def make_signal_fn(raw, brain=BRAIN, tail: int = 220):
    """signal_fn(i) for the backtester: technical experts only, no look-ahead (higher timeframes use
    only candles that had already CLOSED at candle i)."""
    import pandas as pd

    from guards import timeframe_seconds
    from strategy import generate_signal
    base_tf = min(CFG.timeframes, key=timeframe_seconds)
    base_secs = timeframe_seconds(base_tf)
    higher = {}
    for tf in CFG.timeframes:
        if timeframe_seconds(tf) > base_secs:
            h = resample_ohlcv(raw, tf)
            closes = pd.DatetimeIndex(h["ts"] + pd.Timedelta(seconds=timeframe_seconds(tf)))
            higher[tf] = (h, closes)

    def signal_fn(i: int):
        ts_end = raw["ts"].iloc[i] + pd.Timedelta(seconds=base_secs)
        mtf = {base_tf: raw.iloc[max(0, i + 1 - tail):i + 1]}
        for tf, (h, closes) in higher.items():
            k = int(closes.searchsorted(ts_end, side="right"))
            mtf[tf] = h.iloc[max(0, k - tail):k]
        return generate_signal(mtf, brain=brain)
    return signal_fn


def _fetch_history(symbol: str, candles: int, tf: str | None = None):
    import data
    from guards import timeframe_seconds
    tf = tf or min(CFG.timeframes, key=timeframe_seconds)
    return data.fetch_ohlcv_history(data.get_exchange(), symbol, tf, candles)


# --------------------------------------------------------------------------- the live/paper trading loop
def _fmt(value, nd: int = 4) -> str:
    return "—" if value is None else f"{float(value):.{nd}f}"


class Trader:
    def __init__(self, live: bool = False, exchange=None, executor=None):
        import data
        import telegram_control
        from execution import LiveExecutor, PaperExecutor
        from strategy import BreakoutDetector
        self.live = live
        self.tg = telegram_control
        self.conn = db.connect()
        self.equity, self.risk, self.positions = restore_state(self.conn, CFG.symbols)
        self.exchange = exchange if exchange is not None else data.get_exchange(authenticated=live)
        self.executor = executor or (LiveExecutor(self.alert) if live else PaperExecutor())
        self.paused = False
        self.poller = self.tg.CommandPoller()
        self.blocked: dict[str, str] = {}
        self.last_signal: dict[str, str] = {}
        self.last_ai: dict[str, tuple[float, float | None, list[str]]] = {}
        self.regime: dict[str, str] = {}
        self.btc_note = "n/a"
        self.last_day = datetime.now(timezone.utc).date()
        self.last_heartbeat = time.time()
        self.kill_alerted = False
        self.lock_alerted = False
        self.failures = 0
        self.stop_requested = False
        self.detector = BreakoutDetector()
        self._atr: dict[str, float] = {}               # last known ATR per symbol, so exits work between candle fetches
        self._last_boundary: int | None = None
        self._pending_writes: list[tuple] = []         # DB writes that failed; retried every cycle
        self._db_paused = False
        self._fatal_alerted = False

    # ----- plumbing
    @property
    def mode(self) -> str:
        return "live" if self.live else "paper"

    def alert(self, msg: str) -> None:
        logger.info(msg)
        try:
            self.tg.send(msg)
        except Exception as e:  # noqa: BLE001 - alerts must never take the loop down
            logger.warning("alert delivery failed: %s", e)

    def _db_write(self, fn, *args, **kwargs) -> bool:
        """Run a DB write; on failure remember it and retry next cycle instead of losing a trade record."""
        try:
            fn(self.conn, *args, **kwargs)
            return True
        except Exception as e:  # noqa: BLE001
            logger.critical("database write %s failed: %s", getattr(fn, "__name__", fn), e)
            self._pending_writes.append((fn, args, kwargs))
            if not self._db_paused:
                self._db_paused = True
                self.paused = True
                self.alert(f"[{self.mode}] DATABASE WRITE FAILED ({e}); new entries paused, will retry. Free disk space?")
            return False

    def _flush_pending_writes(self) -> None:
        if not self._pending_writes:
            return
        remaining: list[tuple] = []
        for fn, args, kwargs in self._pending_writes:
            try:
                fn(self.conn, *args, **kwargs)
            except Exception as e:  # noqa: BLE001
                logger.error("retry of %s still failing: %s", getattr(fn, "__name__", fn), e)
                remaining.append((fn, args, kwargs))
        self._pending_writes = remaining
        if not remaining and self._db_paused:
            self._db_paused = False
            self.paused = False
            self.alert(f"[{self.mode}] database writes recovered; entries resumed")

    def _unrealized(self, prices: dict[str, float]) -> float:
        total = 0.0
        for s, p in self.positions.items():
            if p and s in prices:
                total += (prices[s] - p["entry"]) * p["size"] * (1 if p["action"] == "BUY" else -1)
        return total

    def _ticker(self, symbol: str) -> dict | None:
        try:
            t = self.exchange.fetch_ticker(symbol)
            return t if isinstance(t, dict) else None
        except Exception as e:  # noqa: BLE001 - AttributeError for paper doubles, network errors live
            logger.debug("ticker unavailable for %s: %s", symbol, e)
            return None

    @staticmethod
    def _ticker_price(ticker: dict | None) -> float | None:
        if not ticker:
            return None
        for v in (ticker.get("last"), (float(ticker["bid"]) + float(ticker["ask"])) / 2 if ticker.get("bid") and ticker.get("ask") else None):
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0 and f == f and f != float("inf"):
                return f
        return None

    def _live_price(self, symbol: str, fallback: float | None) -> float | None:
        """Exits and live entries use the ticker (seconds old) instead of the candle close, but only where that
        matters (live, or the hybrid fast loop). Plain paper mode keeps the candle close, as before."""
        if self.live or CFG.hybrid_trigger_enabled:
            p = self._ticker_price(self._ticker(symbol))
            if p:
                return p
        return fallback

    # ----- startup
    def startup(self) -> None:
        self.tg.setup_commands()
        dropped = self.poller.drain()
        if dropped:
            logger.info("discarded %d Telegram command(s) queued while offline", dropped)
        for intent in db.pending_intents(self.conn):
            if self.live:
                self.blocked[intent["symbol"]] = f"unresolved {intent['kind']} order intent {intent['client_id']} (crash during an order?)"
                self.alert(f"[live] {intent['symbol']} entries BLOCKED: {self.blocked[intent['symbol']]}. Check the exchange, then /unblock.")
            else:
                db.resolve_intent(self.conn, intent["client_id"], "failed", "paper restart")
        if self.live and CFG.reconcile_on_start:
            for symbol in self.positions:
                r = self.executor.reconcile(self.exchange, symbol, self.positions[symbol])
                if not r.get("safe"):
                    self.blocked[symbol] = r.get("reason", "reconcile failed")
                    self.alert(f"[live] {symbol} entries BLOCKED: {self.blocked[symbol]}")
                    continue
                pos = r.get("position") or {}
                held = float(pos.get("contracts") or 0)
                tracked = self.positions[symbol]
                if tracked is None and held > 0 and not pos.get("spot_balance"):
                    self.blocked[symbol] = "exchange shows a position the bot is not tracking"
                    self.alert(f"[live] {symbol} entries BLOCKED: {self.blocked[symbol]}")
                if tracked is not None and held <= 0:
                    self.alert(f"[live] {symbol}: tracked position not found on exchange; verify manually")
        self.alert(f"ZORA {self.mode} started | {CFG.exchange_id} | {','.join(CFG.symbols)} | equity {self.equity:.2f} | "
                   f"strategy {CFG.strategy_mode}{' + hybrid trigger' if CFG.hybrid_trigger_enabled else ''}")

    # ----- one routine cycle (candle close)
    def cycle(self) -> None:
        import data
        self._flush_pending_writes()
        self._daily_rollover()
        btc = None
        if CFG.btc_context_symbol and CFG.btc_context_symbol not in self.positions:
            try:
                from guards import timeframe_seconds
                tf = min(CFG.timeframes, key=timeframe_seconds)
                btc = data.fetch_ohlcv_df(self.exchange, CFG.btc_context_symbol, tf)
                chg = (btc["close"].iloc[-1] / btc["close"].iloc[-24] - 1) * 100 if len(btc) > 24 else 0.0
                self.btc_note = f"{CFG.btc_context_symbol} {btc['close'].iloc[-1]:.2f} ({chg:+.2f}% / 24 candles)"
            except Exception as e:  # noqa: BLE001 - context only
                logger.warning("BTC context unavailable: %s", e)
        prices: dict[str, float] = {}
        for symbol in list(self.positions):
            try:
                price = self._process_symbol(symbol, btc)
                if price is not None:
                    prices[symbol] = price
                self.failures = 0
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the loop
                self.failures += 1
                logger.exception("cycle failed for %s", symbol)
                if self.failures % 5 == 0:
                    self.alert(f"[{self.mode}] {self.failures} consecutive cycle failures; check logs/network")
        self._finish_cycle(prices)

    def _finish_cycle(self, prices: dict[str, float]) -> None:
        self.risk.update_equity(self.equity + self._unrealized(prices))
        self._db_write(db.log_equity, self.equity)       # realized equity; open PnL is only tracked in memory
        self._db_write(db.set_state_json, "risk", {**self.risk.snapshot(), "day": str(self.last_day)})
        if self.risk.kill_switch_tripped and not self.kill_alerted:
            self.kill_alerted = True
            self.alert(f"[{self.mode}] KILL SWITCH tripped: {self.risk.kill_reason or 'manual'} (equity {self.risk.equity:.2f}, "
                       f"peak {self.risk.peak_equity:.2f}). No new entries until restart; open positions stay managed by their stops.")
        if self.risk.entries_locked() and not self.lock_alerted:
            self.lock_alerted = True
            self.alert(f"[{self.mode}] CIRCUIT BREAKER: new entries locked for {self.risk.lock_remaining_seconds() / 3600:.1f}h "
                       f"({self.risk.lock_reason}). Open positions stay managed.")
        elif not self.risk.entries_locked():
            self.lock_alerted = False
        if getattr(self.executor, "fatal", False) and not self._fatal_alerted:
            self._fatal_alerted = True
            self.paused = True
            self.alert(f"[{self.mode}] exchange authentication/permission failure: entries paused. Fix the API keys and restart.")
        self._publish()
        hb = CFG.telegram_heartbeat_minutes
        if hb and time.time() - self.last_heartbeat >= hb * 60:
            self.last_heartbeat = time.time()
            self.tg.send(self._cmd_status())

    def _daily_rollover(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.last_day:
            self.last_day = today
            self.risk.reset_daily()

    def _process_symbol(self, symbol: str, btc) -> float | None:
        import data
        import guards
        from indicators import enrich
        try:
            mtf = data.fetch_multi_timeframe(self.exchange, symbol, CFG.timeframes)
        except Exception:
            self._manage_from_ticker(symbol)      # exits must not depend on a candle fetch succeeding
            raise
        base_tf = min(CFG.timeframes, key=guards.timeframe_seconds)
        base = enrich(mtf[base_tf])
        stale, age = guards.is_stale(mtf[base_tf], base_tf, CFG.stale_data_max_candles)
        candle_price, atr = float(base["close"].iloc[-1]), float(base["atr14"].iloc[-1])
        if atr == atr and atr > 0:
            self._atr[symbol] = atr
        stale_txt = f"{age / 60:.0f} min old" if stale else None
        if stale:
            logger.warning("%s data is stale (%s); skipping new entries this cycle", symbol, stale_txt)
            return self._manage_from_ticker(symbol)     # stale candles never freeze the management of an open position
        price = self._live_price(symbol, candle_price) or candle_price
        pos = self.positions.get(symbol)
        if pos:
            self._manage_position(symbol, pos, price, atr)
        if self.positions.get(symbol) is None:
            self._consider_entry(symbol, mtf, base, price, atr, stale_txt, btc)
        return price

    def _manage_from_ticker(self, symbol: str) -> float | None:
        pos = self.positions.get(symbol)
        price = self._ticker_price(self._ticker(symbol))
        atr = self._atr.get(symbol)
        if pos and price and atr:
            self._manage_position(symbol, pos, price, atr)
        return price

    # ----- exits
    @staticmethod
    def _native(pos: dict) -> bool:
        prot = pos.get("protection") or {}
        return any(isinstance(v, dict) and v.get("id") for v in prot.values())

    def _manage_position(self, symbol: str, pos: dict, price: float, atr: float) -> None:
        if self.live:
            status = getattr(self.executor, "protection_status", None)
            if status is not None:
                state, kind, fill = status(self.exchange, symbol, pos.get("protection"))
                if state == "lost" and self._native(pos):
                    self.alert(f"[live] {symbol}: native {kind} order was cancelled by the exchange; position is now software-protected only")
                    pos["protection"] = {"software_only": True}
                    self._db_write(db.save_open_position, pos)
                hit = (kind, fill) if state == "triggered" else None
            else:
                hit = self.executor.protection_triggered(self.exchange, symbol, pos.get("protection"))
            if hit:
                kind, fill = hit
                prot = pos.get("protection") or {}
                sibling = "take" if kind == "stop" else "stop"      # the other leg must not survive and re-open a position
                self.executor.cancel_protection(self.exchange, symbol, {sibling: prot.get(sibling)})
                level = pos.get("stop") if kind == "stop" else pos.get("take")
                self._finalize(symbol, pos, float(fill) if fill else float(level or price), f"{kind}-native")
                return
        tiered = bool(pos.get("tiers"))
        reason = check_exit(dict(pos, take=None) if tiered else pos, price)
        if reason:
            self._close(symbol, pos, price, reason)
            return
        if tiered:
            hit_tier = next_tier_exit(pos, price)
            if hit_tier:
                self._scale_out(symbol, pos, *hit_tier, price)
                return
            new_stop = runner_trail(pos, price, atr)
            if new_stop is not None and abs(new_stop - float(pos["stop"])) >= 0.25 * atr:   # avoid re-placing the order every tick
                self._move_stop(symbol, pos, new_stop)
            return
        if not self._native(pos):
            new_stop = manage_stop(pos, price, atr)
            if new_stop is not None:
                pos["stop"] = new_stop
                self._db_write(db.save_open_position, pos)
                logger.info("%s stop moved to %.4f", symbol, new_stop)

    def _move_stop(self, symbol: str, pos: dict, new_stop: float) -> bool:
        old = pos["stop"]
        if self.live and self._native(pos):
            if not self.executor.cancel_protection(self.exchange, symbol, pos.get("protection")):
                logger.warning("%s: could not cancel the old stop; keeping it", symbol)
                return False
            prot = self.executor.place_protection(self.exchange, symbol, pos["action"], pos["size"], new_stop, None)
            if prot is None:
                self.alert(f"[live] CRITICAL {symbol}: could not re-arm the native stop at {new_stop:.4f}; software stop only. Check the exchange.")
                prot = {"software_only": True}
            pos["protection"] = prot
        pos["stop"] = new_stop
        self._db_write(db.save_open_position, pos)
        logger.info("%s stop moved %.4f -> %.4f", symbol, old, new_stop)
        return True

    def _scale_out(self, symbol: str, pos: dict, tier: str, size: float, level: float, price: float) -> None:
        """Close a TP1/TP2 slice. TP1 moves the stop to breakeven (risk-free), TP2 releases the ATR-trailed runner."""
        from copy import deepcopy
        side = "sell" if pos["action"] == "BUY" else "buy"
        trial = apply_tier_fill(deepcopy(pos), tier, size)
        if self.live:
            out = self.executor.close_protected(self.exchange, symbol, pos["action"], size, price, pos.get("protection"),
                                                pos.get("stop"), None, position_size=pos["size"])
            if out.kind == "native":
                self._finalize(symbol, pos, float(out.price or pos["stop"]), f"{out.leg}-native")
                return
            if out.kind in ("failed", "unknown"):
                if out.protection is not None:
                    pos["protection"] = out.protection
                    self._db_write(db.save_open_position, pos)
                self.alert(f"[live] {symbol} {tier.upper()} scale-out not completed ({out.kind}); will retry")
                return
            fill, filled = float(out.price), float(out.filled)
            if out.kind == "partial":
                if out.protection is not None:
                    pos["protection"] = out.protection
                self._book_leg(symbol, pos, "partial", filled, fill, advance=False)
                return
            remaining = max(0.0, pos["size"] - filled)
            pos["protection"] = (self.executor.rearm(self.exchange, symbol, pos["action"], remaining, trial["stop"], None, True, None)
                                 if remaining > 0 else {})
        else:
            fill, filled = COST_MODEL.fill_price(level, side), float(size)
        self._book_leg(symbol, pos, tier, filled, fill, advance=True)

    def _book_leg(self, symbol: str, pos: dict, result: str, filled: float, fill: float, *, advance: bool) -> None:
        leg_pnl = settle_trade({"action": pos["action"], "entry": pos["entry"], "size": filled}, fill)
        if advance:
            apply_tier_fill(pos, result, filled)
            if result == "tp1":
                pos["exposure_pct"] = 0.0           # stop at breakeven: this trade no longer risks capital
        else:
            pos["size"] = max(0.0, pos["size"] - filled)
        pos.setdefault("tiers", {})["realized"] = float(pos.get("tiers", {}).get("realized", 0.0)) + leg_pnl
        self.equity += leg_pnl
        self.risk.update_equity(self.equity)
        self._db_write(db.record_partial_close, pos, closed_size=filled, exit_price=fill, pnl=leg_pnl,
                       equity=self.equity, mode=self.mode, result=result)
        sync_exposure(self.risk, self.positions)
        self.alert(f"[{self.mode}] {result.upper()} {pos['action']} {symbol}: closed {filled:.6f} @ {fill:.4f} PnL {leg_pnl:+.2f} | "
                   f"remaining {pos['size']:.6f} stop {_fmt(pos.get('stop'))} | equity {self.equity:.2f}")

    def _close(self, symbol: str, pos: dict, price: float, reason: str) -> bool:
        side = "sell" if pos["action"] == "BUY" else "buy"
        if self.live:
            out = self.executor.close_protected(self.exchange, symbol, pos["action"], pos["size"], price, pos.get("protection"),
                                                pos.get("stop"), pos.get("take"), position_size=pos["size"])
            if out.kind == "native":
                self._finalize(symbol, pos, float(out.price or pos.get("stop") or price), f"{out.leg}-native")
                return True
            if out.kind == "closed":
                self._finalize(symbol, pos, float(out.price), reason)
                return True
            if out.protection is not None:
                pos["protection"] = out.protection
            if out.kind == "partial":
                self._book_leg(symbol, pos, "partial", float(out.filled), float(out.price), advance=False)
                self.alert(f"[live] {symbol} {reason} exit only PARTLY filled; remainder stays protected and will be retried")
                return False
            self._db_write(db.save_open_position, pos)
            self.alert(f"[live] {symbol} {reason} exit {'state UNKNOWN' if out.kind == 'unknown' else 'FAILED'}; will retry next cycle. "
                       "Check the exchange manually.")
            return False
        if reason == "stop":      # a gap through the stop fills at the (worse) market price
            ref = min(price, pos["stop"]) if pos["action"] == "BUY" else max(price, pos["stop"])
        else:
            ref = pos["take"] if reason == "take" and pos.get("take") else price
        fill = COST_MODEL.fill_price(ref, side)
        self._finalize(symbol, pos, float(fill), reason)
        return True

    def _finalize(self, symbol: str, pos: dict, exit_price: float, reason: str) -> None:
        pnl = settle_trade(pos, exit_price)
        realized_before = float((pos.get("tiers") or {}).get("realized", 0.0))
        equity_after = self.equity + pnl
        self._db_write(db.record_close, pos, exit_price=exit_price, pnl=pnl, result=reason, equity=equity_after, mode=self.mode)
        self.positions[symbol] = None
        self.equity = equity_after
        self.risk.update_equity(self.equity)
        sync_exposure(self.risk, self.positions)
        BRAIN.update_from_trade(pos.get("expert_scores") or {}, pos.get("factor_scores_by_tf") or {}, pos["action"], pnl + realized_before)
        self.alert(f"[{self.mode}] CLOSED {pos['action']} {symbol} @ {exit_price:.4f} ({reason}) PnL {pnl:+.2f}"
                   f"{f' (+{realized_before:.2f} banked earlier)' if realized_before else ''} | equity {self.equity:.2f}")

    # ----- entries
    def _experts(self, symbol: str, mtf: dict, base_raw):
        import fear_greed
        import ml_model
        import news_feed
        import tv_signal
        extra: dict = {}
        notes: list[str] = []
        headlines: list[str] = []
        try:
            score, headlines = news_feed.news_sentiment(symbol)
            if headlines:
                extra["news_score"] = score
        except Exception as e:  # noqa: BLE001
            logger.warning("news expert failed: %s", e)
        if CFG.fear_greed_enabled:
            try:
                score, note = fear_greed.fear_greed_score()
                if score is not None:
                    extra["fng_score"] = score
                    notes.append(note)
            except Exception as e:  # noqa: BLE001
                logger.warning("fear&greed expert failed: %s", e)
        try:
            res = ml_model.ml_expert(base_raw)
            if res.prob_up is not None:     # only vote when the model has proven an edge out-of-sample
                extra["ml_score"] = res.score
        except Exception as e:  # noqa: BLE001
            logger.warning("ml expert failed: %s", e)
        score, note = tv_signal.current_score()
        if score is not None:
            extra["tv_score"] = score
            notes.append(f"tv: {note}")
        return extra, headlines, notes

    def _ai(self, symbol: str, pre_signal, headlines: list[str]):
        if not ai_available():
            return None, []
        last = self.last_ai.get(symbol)
        if last and time.time() - last[0] < CFG.ai_cycle_min_seconds:
            return last[1], last[2]
        import multi_ai
        try:
            score, notes = multi_ai.get_ai_consensus(symbol, pre_signal, headlines, self.exchange, self.btc_note)
        except Exception as e:  # noqa: BLE001 - the AI panel is advisory only
            logger.warning("AI panel failed: %s", e)
            score, notes = None, [f"ai error: {e}"]
        self.last_ai[symbol] = (time.time(), score, notes)
        return score, notes

    def _signal_inputs(self, mtf: dict) -> dict:
        """Technical experts see CLOSED candles only (parity with the backtester); a forming candle's wick is not a signal."""
        if not CFG.signal_on_closed_candles:
            return mtf
        from strategy import closed_candles
        out = {}
        for tf, df in mtf.items():
            try:
                out[tf] = closed_candles(df, tf)
            except Exception:  # noqa: BLE001
                out[tf] = df
        return out

    def _consider_entry(self, symbol, mtf, base, price, atr, stale_txt, btc, *, trigger: str = "candle_close",
                        trigger_detail: str = "", ticker: dict | None = None) -> None:
        import ai_guard
        import guards
        from strategy import build_dossier, generate_signal, signal_from_decision
        extra, headlines, notes = self._experts(symbol, mtf, mtf[min(mtf, key=guards.timeframe_seconds)])
        sig_mtf = self._signal_inputs(mtf)
        decision_ai = None
        if CFG.strategy_mode == "ai_council":
            baseline = generate_signal(sig_mtf, **extra)           # advisory technical baseline, folded into the dossier
            ai_block = ai_guard.before_cycle()
            if not ai_block[0]:
                self.last_signal[symbol] = f"HOLD [ai unavailable: {ai_block[1]}]"
                if not CFG.ai_fallback_to_technical:
                    db.log_decision(self.conn, symbol, baseline.__class__("HOLD", 0, ["ai unavailable"]), False, f"AI safety: {ai_block[1]}")
                    return
                signal, ai_notes = generate_signal(sig_mtf, **extra), []
            else:
                dossier = build_dossier(symbol, sig_mtf, trigger=trigger, trigger_detail=trigger_detail, headlines=headlines,
                                        btc_note=self.btc_note, baseline=baseline, live_price=price)
                if dossier is None:
                    logger.warning("%s: not enough closed candle history for a dossier", symbol)
                    return
                self.regime[symbol] = dossier.regime
                import multi_ai
                decision_ai = multi_ai.get_council_decision(symbol, dossier, self.exchange)
                ai_notes = decision_ai.notes
                if not decision_ai.authorized:
                    hold = baseline.__class__("HOLD", float(decision_ai.confidence), [f"judge:{decision_ai.reason}"])
                    self.last_signal[symbol] = f"HOLD [{dossier.regime}; {decision_ai.reason or 'no authorization'}]"
                    if decision_ai.decision != "HOLD" or not decision_ai.answered:     # log vetoed/failed attempts, not every quiet HOLD
                        db.log_decision(self.conn, symbol, hold, False, decision_ai.reason or "not authorized", " | ".join(ai_notes)[:500])
                    return
                signal = signal_from_decision(decision_ai.decision, decision_ai.confidence, decision_ai.note, dossier)
        else:
            pre = generate_signal(sig_mtf, **extra)
            ai_score, ai_notes = self._ai(symbol, pre, headlines)
            signal = generate_signal(sig_mtf, ai_score=ai_score, **extra)
        self.last_signal[symbol] = f"{signal.action} {signal.confidence:.0f}% [{', '.join(signal.reasons)}]"
        if signal.action == "HOLD":
            return
        note = " | ".join(notes + ai_notes[:2])[:500] or None

        quality = None
        if self.live:
            ticker = ticker or self._ticker(symbol)
            try:
                quality = guards.market_quality(ticker if ticker is not None else self.exchange.fetch_ticker(symbol),
                                                CFG.max_spread_bps, CFG.min_quote_volume)
            except Exception as e:  # noqa: BLE001 - fail safe: unknown quality means no entry
                quality = (False, f"ticker unavailable: {e}")
        degraded = guards.strategy_degraded(db.recent_closed_pnls(self.conn, CFG.degradation_window),
                                            CFG.degradation_window, CFG.degradation_min_profit_factor)
        ai_block_entry = ai_guard.before_cycle() if CFG.strategy_mode != "ai_council" else None
        blockers = entry_blockers(signal_action=signal.action, paused=self.paused, live=self.live,
                                  stale=stale_txt, quality=quality, degraded=degraded, ai_block=ai_block_entry)
        if symbol in self.blocked:
            blockers.append(f"reconcile: {self.blocked[symbol]}")
        if blockers:
            db.log_decision(self.conn, symbol, signal, False, "; ".join(blockers), note)
            return

        size_mult = 1.0
        if btc is not None and symbol != CFG.btc_context_symbol:
            corr = guards.rolling_correlation(base["close"], btc["close"], CFG.correlation_window)
            if guards.correlation_regime(corr) == "broken":
                size_mult = CFG.correlation_size_mult
        if decision_ai is not None:
            decision = self.risk.evaluate(signal.action, price, atr, size_mult, confidence=decision_ai.confidence,
                                          ai_stop=decision_ai.stop_loss)
        else:
            decision = self.risk.evaluate(signal.action, price, atr, size_mult)
        db.log_decision(self.conn, symbol, signal, decision.approved, decision.reason, note)
        if decision.approved:
            self._open(symbol, signal, decision, price)

    def _open(self, symbol: str, signal, decision, price: float) -> None:
        from execution import new_client_id
        action, size = signal.action, decision.position_size
        side = "buy" if action == "BUY" else "sell"
        intent: str | None = None
        if self.live:
            if CFG.native_protection_required and not self.executor._native_supported(self.exchange, symbol):
                self.alert(f"[live] {symbol} entry refused: native stop/take-profit not confirmed for this market")
                return
            r = self.executor.reconcile(self.exchange, symbol)
            if not r.get("safe") or r.get("open_orders"):
                self.alert(f"[live] {symbol} entry refused: exchange state not clean ({r.get('reason', 'open orders exist')})")
                return
            intent = new_client_id()
            db.record_intent(self.conn, intent, symbol, "open", side, size, price)      # write-ahead: survives a crash mid-order
            fill = self.executor.open(self.exchange, symbol, action, size, price, client_id=intent)
            if fill is None:
                if getattr(self.executor, "state_unknown", False):
                    db.resolve_intent(self.conn, intent, "unknown", getattr(self.executor, "last_error", None))
                    self.blocked[symbol] = "an entry order may be live on the exchange (state unknown); verify, then /unblock"
                    self.alert(f"[live] {symbol} entries BLOCKED: {self.blocked[symbol]}")
                else:
                    db.resolve_intent(self.conn, intent, "failed", getattr(self.executor, "last_error", None))
                return
            size = getattr(self.executor, "last_amount", None) or size        # track what the exchange actually accepted
        else:
            fill = COST_MODEL.fill_price(price, side)
        pos = {"symbol": symbol, "action": action, "entry": float(fill), "size": size, "stop": decision.stop_loss,
               "take": decision.take_profit, "initial_stop": decision.stop_loss, "mode": self.mode, "protection": {},
               "expert_scores": signal.expert_scores, "factor_scores_by_tf": signal.factor_scores_by_tf,
               "opened_ts": time.time(), "exposure_pct": decision.exposure_pct}
        if decision.tiers:
            from risk import plan_tiers
            pos["tiers"] = plan_tiers(action, float(fill), decision.stop_loss, size) or {}
            if pos["tiers"]:
                pos["take"] = pos["tiers"]["tp1"]
        # Persist BEFORE placing protection: from here on a crash leaves a tracked position, not an orphan.
        self.positions[symbol] = pos
        self._db_write(db.record_open, pos, intent)
        self.risk.add_exposure(decision.exposure_pct)
        sync_exposure(self.risk, self.positions)
        if self.live:
            native_take = None if pos.get("tiers") else decision.take_profit        # tiered exits are software-managed slices
            prot = self.executor.place_protection(self.exchange, symbol, action, size, decision.stop_loss, native_take)
            if prot is None:                       # never stay in a live trade without protection
                self.alert(f"[live] {symbol} protection failed; closing the new position immediately")
                if self.executor.close(self.exchange, symbol, action, size, price) is not None:
                    self._finalize(symbol, pos, float(getattr(self.executor, "last_fill", None).price if getattr(self.executor, "last_fill", None) else fill),
                                   "protection_failed")
                    return
                self.alert(f"[live] CRITICAL {symbol}: could not close an unprotected position. Close it manually NOW.")
                prot = {"software_only": True}
            pos["protection"] = prot
            self._db_write(db.save_open_position, pos)
        tier_txt = f" TP1 {pos['tiers']['tp1']:.4f} TP2 {pos['tiers']['tp2']:.4f}" if pos.get("tiers") else f" TP {decision.take_profit:.4f}"
        self.alert(f"[{self.mode}] OPENED {action} {symbol} {size:.6f} @ {fill:.4f} SL {decision.stop_loss:.4f}{tier_txt} "
                   f"(risk {decision.risk_pct:.2f}%)")

    # ----- hybrid trigger: 10-second polling between candle closes
    def _boundary(self) -> int:
        from guards import timeframe_seconds
        secs = timeframe_seconds(min(CFG.timeframes, key=timeframe_seconds))
        return int(time.time() // secs)

    def tick(self) -> None:
        """One fast-poll iteration. On a new candle close it runs the routine cycle; otherwise it (1) manages open
        positions off the live ticker and (2) looks for an intra-candle breakout on flat symbols."""
        import data
        from guards import timeframe_seconds
        try:
            self._daily_rollover()
            base_tf = min(CFG.timeframes, key=timeframe_seconds)
            secs = timeframe_seconds(base_tf)
            now = time.time()
            boundary = int(now // secs)
            if boundary != self._last_boundary and now - boundary * secs >= 3:    # 3s grace for the exchange to publish the closed candle
                self._last_boundary = boundary
                self.cycle()
                return
            for symbol in list(self.positions):
                pos = self.positions.get(symbol)
                if pos:
                    self._manage_from_ticker(symbol)
                    continue
                if self.paused or self.risk.kill_switch_tripped or self.risk.entries_locked() or symbol in self.blocked:
                    continue
                candles = data.fetch_ohlcv_df(self.exchange, symbol, base_tf, limit=25, retries=1)
                ticker = self._ticker(symbol)
                event = self.detector.check(symbol, candles, self._ticker_price(ticker))
                if event is not None:
                    self._fast_track(symbol, event, ticker)
        except Exception:  # noqa: BLE001 - a bad tick must never stop the loop
            logger.exception("fast-poll tick failed")

    def _fast_track(self, symbol: str, event, ticker: dict | None) -> None:
        import data
        import guards
        from indicators import enrich
        logger.info("%s breakout trigger: %s (%s)", symbol, event.kind, event.detail)
        mtf = data.fetch_multi_timeframe(self.exchange, symbol, CFG.timeframes)
        base_tf = min(CFG.timeframes, key=guards.timeframe_seconds)
        base = enrich(mtf[base_tf])
        stale, age = guards.is_stale(mtf[base_tf], base_tf, CFG.stale_data_max_candles)
        if stale:
            return
        atr = float(base["atr14"].iloc[-1])
        if atr == atr and atr > 0:
            self._atr[symbol] = atr
        price = self._ticker_price(ticker) or float(base["close"].iloc[-1])
        self.alert(f"[{self.mode}] FAST-TRACK {symbol}: {event.kind} — {event.detail}")
        self._consider_entry(symbol, mtf, base, price, atr, None, None, trigger="breakout", trigger_detail=event.detail, ticker=ticker)

    # ----- dashboard / telegram
    def _publish(self) -> None:
        try:
            import dashboard
            dashboard.publish(mode=self.mode, paused=self.paused, kill_switch=self.risk.kill_switch_tripped,
                              circuit_breaker_seconds=self.risk.lock_remaining_seconds(),
                              equity=round(self.equity, 2), exposure_pct=round(self.risk.open_exposure, 2),
                              signals=dict(self.last_signal), regime=dict(self.regime), btc=self.btc_note, updated=time.time())
        except Exception:  # noqa: BLE001 - the dashboard is optional
            pass

    def handle_command(self, text: str) -> str:
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            return "Commands: /status /signal /regime /council /btc /ai /brain /pnl /positions /metrics /guards /risk /pause /resume /unblock /kill"
        if cmd == "/status":
            return self._cmd_status()
        if cmd == "/signal":
            return "\n".join(f"{s}: {v}" for s, v in self.last_signal.items()) or "no signal computed yet"
        if cmd == "/regime":
            return "\n".join(f"{s}: {v}" for s, v in self.regime.items()) or "no regime computed yet (needs STRATEGY_MODE=ai_council)"
        if cmd == "/council":
            import multi_ai
            d = multi_ai.LAST_DECISIONS
            return "\n".join(f"{s}: {x.decision} {x.confidence}% authorized={x.authorized} — {x.reason or x.note}" for s, x in d.items()) or "no council decision yet"
        if cmd == "/btc":
            return self.btc_note
        if cmd == "/ai":
            if not ai_available():
                return "no AI provider keys configured"
            import multi_ai
            ok, msg = multi_ai.groq_healthcheck()
            return f"groq: {msg}" + ("" if ok or CFG.groq_api_key else "")
        if cmd == "/brain":
            return json.dumps(BRAIN.snapshot(), indent=1)
        if cmd == "/pnl":
            s = db.summary(self.conn)
            return f"closed trades {s['closed_trades']} | realized PnL {s['realized_pnl']:+.2f} | equity {self.equity:.2f}"
        if cmd == "/positions":
            lines = [f"{p['action']} {s} {p['size']:.6f} @ {p['entry']:.4f} SL {_fmt(p.get('stop'))} TP {_fmt(p.get('take'))}"
                     + (f" [tier stage {p['tiers'].get('stage', 0)}]" if p.get("tiers") else "")
                     for s, p in self.positions.items() if p]
            return "\n".join(lines) or "no open positions"
        if cmd == "/metrics":
            from metrics import compute_metrics, format_metrics
            return format_metrics(compute_metrics(db.recent_closed_pnls(self.conn, 500), None, CFG.starting_capital))
        if cmd == "/guards":
            return (f"paused={self.paused} kill={self.risk.kill_switch_tripped} exposure={self.risk.open_exposure:.2f}/"
                    f"{CFG.max_exposure_pct} blocked={self.blocked or 'none'} | {self.btc_note}")
        if cmd == "/risk":
            r = self.risk
            return (f"equity {r.equity:.2f} peak {r.peak_equity:.2f} day-start {r.daily_start:.2f} | circuit breaker "
                    f"{'LOCKED ' + format(r.lock_remaining_seconds() / 3600, '.1f') + 'h' if r.entries_locked() else 'ok'} | "
                    f"kill {r.kill_reason or 'no'}")
        if cmd == "/pause":
            self.paused = True
            return "new entries paused (open positions stay managed)"
        if cmd == "/resume":
            self.paused = False
            return "new entries resumed" + (" — but the kill switch is still tripped" if self.risk.kill_switch_tripped else "")
        if cmd == "/unblock":
            parts = text.split()
            targets = [parts[1]] if len(parts) > 1 else list(self.blocked)
            for sym in targets:
                self.blocked.pop(sym, None)
            for intent in db.pending_intents(self.conn):
                if not len(parts) > 1 or intent["symbol"] == parts[1]:
                    db.resolve_intent(self.conn, intent["client_id"], "failed", "cleared manually via /unblock")
            return f"unblocked: {', '.join(targets) or 'nothing was blocked'} (verify the exchange state first!)"
        if cmd == "/kill":
            self.risk.trip("manual /kill")
            self.paused = True
            return "KILL SWITCH tripped: no new entries until the bot is restarted. Open positions stay managed by their stops."
        return "unknown command; try /help"

    def _cmd_status(self) -> str:
        open_n = sum(1 for p in self.positions.values() if p)
        return (f"ZORA {self.mode} | equity {self.equity:.2f} (peak {self.risk.peak_equity:.2f}) | open {open_n} | "
                f"exposure {self.risk.open_exposure:.2f}% | paused={self.paused} kill={self.risk.kill_switch_tripped}"
                f"{' | breaker LOCKED' if self.risk.entries_locked() else ''}")

    def _handle_commands(self) -> None:
        for text in self.poller.poll():
            try:
                self.tg.send(self.handle_command(text))
            except Exception as e:  # noqa: BLE001
                logger.warning("command %r failed: %s", text, e)

    # ----- main loop
    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop_requested and time.monotonic() < end:
            time.sleep(min(2.0, max(0.0, end - time.monotonic())))
            self._handle_commands()

    def run(self, once: bool = False) -> None:
        self.startup()
        try:
            if CFG.hybrid_trigger_enabled and not once:
                self.cycle()                                  # evaluate immediately, then follow candle closes + breakouts
                self._last_boundary = self._boundary()
                while not self.stop_requested:
                    self.tick()
                    self._sleep(CFG.fast_poll_seconds)
            else:
                while not self.stop_requested:
                    self._handle_commands()
                    self.cycle()
                    if once:
                        break
                    self._sleep(CFG.poll_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Graceful stop: persist the risk baselines, flush failed writes, announce, and checkpoint the DB WAL.
        Open positions are NOT closed: native stops keep guarding them while the bot is down."""
        self._flush_pending_writes()
        try:
            db.set_state_json(self.conn, "risk", {**self.risk.snapshot(), "day": str(self.last_day)})
        except Exception as e:  # noqa: BLE001
            logger.warning("could not persist risk state at shutdown: %s", e)
        self.alert(f"ZORA {self.mode} stopped. Open positions: {[s for s, p in self.positions.items() if p] or 'none'}")


# --------------------------------------------------------------------------- CLI commands
def _symbol_arg(args) -> str:
    return args.symbol or CFG.symbol


def cmd_backtest(args, learn: bool = False) -> int:
    from backtester import simulate
    from metrics import compute_metrics, format_metrics
    symbol = _symbol_arg(args)
    from indicators import enrich
    raw = _fetch_history(symbol, args.candles)
    df = enrich(raw)
    sig = make_signal_fn(raw, brain=BRAIN)    # backtest reads the learned weights but never updates them
    hook = (lambda p, pnl: BRAIN.update_from_trade(p["expert_scores"], p["factor_scores_by_tf"], p["action"], pnl)) if learn else None
    res = simulate(df, sig, on_close=hook)
    m = compute_metrics(res.pnls, res.equity_curve, CFG.starting_capital)
    print(f"{'learn' if learn else 'backtest'} {symbol} {len(df)} candles | final equity {res.final_equity:.2f} "
          f"| kill switch {'TRIPPED' if res.kill_switch_tripped else 'ok'}")
    print(format_metrics(m))
    if learn:
        print("brain updated:", json.dumps(BRAIN.snapshot()))
    return 0


def cmd_optimize(args) -> int:
    from indicators import enrich
    from optimize import optimize, save_report
    symbol = _symbol_arg(args)
    raw = _fetch_history(symbol, args.candles)
    df = enrich(raw)
    base_fn, cache = make_signal_fn(raw), {}

    def sig(i):
        if i not in cache:
            cache[i] = base_fn(i)
        return cache[i]
    report = optimize(df, sig)
    save_report(report)
    print(json.dumps(report, indent=2))
    print("report saved to optimize_report.json (nothing was changed in .env)")
    return 0


def cmd_dca(args) -> int:
    from dca import simulate_dca
    symbol = _symbol_arg(args)
    df = _fetch_history(symbol, args.candles)
    r = simulate_dca(df)
    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    print(f"DCA {symbol}: cycles={len(r.cycles)} final equity {r.final_equity:.2f} "
          f"({(r.final_equity / CFG.starting_capital - 1) * 100:+.2f}%) max capital used {r.max_capital_used_pct:.1f}% | buy&hold {bh:+.2f}%")
    return 0


def cmd_grid(args) -> int:
    from grid import simulate_grid
    symbol = _symbol_arg(args)
    df = _fetch_history(symbol, args.candles)
    r = simulate_grid(df)
    print(f"GRID {symbol}: round trips {r.round_trips} realized {r.realized_pnl:+.2f} unrealized {r.unrealized_pnl:+.2f} "
          f"fees {r.fees:.2f} stopped={r.stopped} final equity {r.final_equity:.2f} | buy&hold {r.buy_and_hold_pct:+.2f}%")
    return 0


def cmd_metrics(_args) -> int:
    from metrics import compute_metrics, format_metrics
    conn = db.connect()
    pnls = db.recent_closed_pnls(conn, 5000)
    curve = [e for _, e in db.equity_series(conn, 5000)]
    print(format_metrics(compute_metrics(pnls, curve or None, CFG.starting_capital)))
    return 0


def cmd_status(_args) -> int:
    conn = db.connect()
    s = db.summary(conn)
    print(f"equity {s['latest_equity']:.2f} | closed trades {s['closed_trades']} | realized PnL {s['realized_pnl']:+.2f}")
    for p in db.load_open_positions(conn):
        print(f"  open {p['action']} {p['symbol']} {p['size']:.6f} @ {p['entry']:.4f} SL {p['stop']} TP {p['take']} ({p['mode']})")
    for t in db.recent_trades(conn, 5):
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(t['ts']))} {t['symbol']} {t['action']} {t['result']} pnl={t['pnl']}")
    return 0


def cmd_run(args, live: bool) -> int:
    problems = CFG.validate()
    if live:
        if not CFG.live_allowed():
            print("LIVE is locked. Needs LIVE_TRADING=true, I_UNDERSTAND_THE_RISK=true and EXCHANGE_API_KEY/SECRET.")
            return 2
        if problems:
            print("Refusing to go live with configuration problems:")
            for p in problems:
                print(" -", p)
            return 2
        print(f"LIVE on {CFG.exchange_id}: symbols={','.join(CFG.symbols)} risk/trade={CFG.risk_per_trade_pct}% "
              f"max exposure={CFG.max_exposure_pct}% daily loss={CFG.max_daily_loss_pct}% drawdown={CFG.max_drawdown_pct}%")
        answer = CFG.live_confirmation if CFG.live_confirmation else input("Type I ACCEPT THE RISK to continue: ")
        if answer.strip() != "I ACCEPT THE RISK":
            print("Aborted.")
            return 2
    else:
        for p in problems:
            print("warning:", p)
    server = None
    try:
        from dashboard import start_background
        server = start_background()
    except ImportError:
        logger.warning("dashboard unavailable; continuing without it")
    except OSError as e:
        logger.warning("dashboard could not start (%s); continuing without it", e)
    trader = Trader(live=live)
    _signal.signal(_signal.SIGTERM, lambda *_: setattr(trader, "stop_requested", True))
    try:
        trader.run(once=args.once)
    finally:
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        db.close(trader.conn)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bot.py", description="ZORA trading bot (paper by default)")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("backtest", "learn", "optimize", "dca", "grid", "portfolio-backtest", "paper-report", "replay-report"):
        p = sub.add_parser(name)
        p.add_argument("symbol", nargs="?", default=None)
        p.add_argument("candles", nargs="?", type=int, default=1500)
    for name in ("run", "live"):
        p = sub.add_parser(name)
        p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    for name in ("doctor", "status", "brain", "metrics", "dashboard", "ai-health", "ai-test"):
        sub.add_parser(name)
    for name in ("validate", "exchange-smoke", "exchange-sandbox"):
        sub.add_parser(name, add_help=False)      # extra flags are forwarded to the module
    return ap


def main(argv: list[str] | None = None) -> int:
    from logging_setup import configure_logging
    configure_logging()
    args, rest = build_parser().parse_known_args(argv)
    c = args.command
    if c not in ("validate", "exchange-smoke", "exchange-sandbox", "portfolio-backtest", "paper-report", "replay-report") and rest:
        build_parser().error(f"unrecognized arguments: {' '.join(rest)}")
    if c == "doctor":
        n = doctor()
        print("ZORA doctor:", "OK" if n == 0 else f"{n} issue(s) found")
        return 0 if n == 0 else 1
    if c == "dashboard":
        from dashboard import run_fastapi
        run_fastapi()
        return 0
    if c == "portfolio-backtest":
        from portfolio_backtest import main as m
        return m(rest)
    if c == "paper-report":
        from paper_report import main as m
        return m(rest)
    if c == "replay-report":
        from replay_report import main as m
        return m(rest)
    if c == "validate":
        from validation import main as m
        return m(rest)
    if c == "exchange-smoke":
        from exchange_smoke import main as m
        return m(rest)
    if c == "exchange-sandbox":
        from exchange_sandbox import main as m
        return m(rest)
    if c == "brain":
        print(json.dumps(BRAIN.snapshot(), indent=2))
        return 0
    if c == "ai-health":
        import multi_ai
        ok, msg = multi_ai.groq_healthcheck()
        print(msg)
        return 0 if ok else 1
    if c == "ai-test":
        import multi_ai
        ok, msg = multi_ai.groq_probe()
        print(msg)
        return 0 if ok else 1
    handlers = {"backtest": cmd_backtest, "learn": lambda a: cmd_backtest(a, learn=True), "optimize": cmd_optimize,
                "dca": cmd_dca, "grid": cmd_grid, "metrics": cmd_metrics, "status": cmd_status,
                "run": lambda a: cmd_run(a, live=False), "live": lambda a: cmd_run(a, live=True)}
    return handlers[c](args)


if __name__ == "__main__":
    raise SystemExit(main())
