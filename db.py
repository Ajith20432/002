"""SQLite persistence for ZORA trading state and audit data.

Design (see AUDIT_REPORT.md for the reasoning):

* ONE serialized writer connection per process (:class:`ZoraConnection`, guarded by a re-entrant lock) plus
  short-lived read-only connections for the dashboard (:func:`connect_readonly`). SQLite allows a single
  writer at a time, so a pool of writer connections only adds "database is locked" errors; WAL mode lets
  readers run concurrently with the writer without blocking it.
* Every multi-step state change goes through :func:`transaction` (``BEGIN IMMEDIATE`` ... ``COMMIT``), so a
  crash can never leave "trade logged but position still open" (which would double-close on restart).
* ``synchronous=FULL``: a trade record that was acknowledged is on disk. The exchange is the source of
  truth for fills, so losing the last write after a power cut on a phone would desynchronise us from it.
* The module-level functions keep their historical ``(conn, ...)`` signatures; they accept a plain
  ``sqlite3.Connection`` too (e.g. the dashboard's read-only one).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import Any

from config import CFG

logger = logging.getLogger("zora.db")

SCHEMA_VERSION = 3

SCHEMA: dict[str, str] = {
    "trades": """CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
        price REAL NOT NULL, size REAL NOT NULL, stop_loss REAL, take_profit REAL, mode TEXT NOT NULL,
        result TEXT NOT NULL DEFAULT 'open', pnl REAL, exchange_id TEXT DEFAULT 'htx')""",
    "equity_curve": """CREATE TABLE IF NOT EXISTS equity_curve (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, equity REAL NOT NULL)""",
    "decision_log": """CREATE TABLE IF NOT EXISTS decision_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
        confidence REAL NOT NULL, approved INTEGER NOT NULL, reason TEXT, llm_note TEXT)""",
    "open_positions": """CREATE TABLE IF NOT EXISTS open_positions (
        symbol TEXT PRIMARY KEY, ts REAL NOT NULL, action TEXT NOT NULL, entry REAL NOT NULL, size REAL NOT NULL,
        stop_loss REAL, take_profit REAL, mode TEXT NOT NULL, protection_json TEXT DEFAULT '{}',
        expert_scores_json TEXT DEFAULT '{}', factor_scores_json TEXT DEFAULT '{}', initial_stop REAL,
        exposure_pct REAL, tiers_json TEXT DEFAULT '{}')""",
    # Small key/value store for state that must survive restarts (risk baselines, kill switch, ...).
    "bot_state": """CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL)""",
    # Write-ahead journal of order intents: written BEFORE an order is sent, resolved after the position is
    # persisted. A row still 'pending' at startup means "an order may be live on the exchange that the
    # database never learned about" -> the symbol is blocked until a human (or reconcile) resolves it.
    "order_intents": """CREATE TABLE IF NOT EXISTS order_intents (
        client_id TEXT PRIMARY KEY, ts REAL NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, side TEXT NOT NULL,
        amount REAL NOT NULL, ref_price REAL, status TEXT NOT NULL DEFAULT 'pending', detail TEXT, updated REAL)""",
}

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_trades_result_id ON trades(result, id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)",
    "CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(ts)",
    "CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_intents_status ON order_intents(status)",
)

_OPEN_POSITION_MIGRATIONS: dict[str, str] = {
    "expert_scores_json": "TEXT DEFAULT '{}'",
    "factor_scores_json": "TEXT DEFAULT '{}'",
    "initial_stop": "REAL",
    "exposure_pct": "REAL",
    "tiers_json": "TEXT DEFAULT '{}'",
}

_POSITION_JSON_COLUMNS = (("protection_json", "protection"), ("expert_scores_json", "expert_scores"),
                          ("factor_scores_json", "factor_scores_by_tf"), ("tiers_json", "tiers"))


# --------------------------------------------------------------------------- connection
class ZoraConnection(sqlite3.Connection):
    """sqlite3 connection that carries a re-entrant lock and its transaction nesting depth.

    ``check_same_thread=False`` is only safe because every public function in this module serialises access
    through ``lock``; never use the raw connection from another thread without holding it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()
        self.tx_depth = 0


def _guard(conn: sqlite3.Connection) -> AbstractContextManager[Any]:
    lock = getattr(conn, "lock", None)
    return lock if lock is not None else nullcontext()


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _begin_immediate(conn: sqlite3.Connection, attempts: int = 4) -> None:
    """BEGIN IMMEDIATE takes the write lock up front, so a transaction never dies half-way with SQLITE_BUSY."""
    delay = 0.05
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as e:
            if not _is_busy(e) or attempt == attempts - 1:
                raise
            logger.warning("database busy at BEGIN (attempt %d/%d); retrying", attempt + 1, attempts)
            time.sleep(delay)
            delay *= 2


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error as e:  # noqa: BLE001 - nothing more we can do; the original error is re-raised by the caller
        logger.error("rollback failed: %s", e)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic block: everything inside commits together or not at all. Re-entrant (inner blocks join the outer)."""
    lock = getattr(conn, "lock", None)
    if lock is None:                       # plain connection (read-only dashboard, tests): sqlite3's own semantics
        with conn:
            yield conn
        return
    with lock:
        if conn.tx_depth > 0:
            conn.tx_depth += 1
            try:
                yield conn
            finally:
                conn.tx_depth -= 1
            return
        _begin_immediate(conn)
        conn.tx_depth = 1
        try:
            yield conn
        except BaseException:
            conn.tx_depth = 0
            _rollback_quietly(conn)
            raise
        conn.tx_depth = 0
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            _rollback_quietly(conn)
            raise


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent: safe on a brand-new file and on databases written by any older ZORA version."""
    for sql in SCHEMA.values():
        conn.execute(sql)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(open_positions)")}
    for col, typ in _OPEN_POSITION_MIGRATIONS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE open_positions ADD COLUMN {col} {typ}")
    trade_cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    if "exchange_id" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN exchange_id TEXT DEFAULT 'htx'")
    for sql in _INDEXES:
        conn.execute(sql)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def connect(path: str | None = None) -> ZoraConnection:
    """Open (creating/migrating if needed) the writer connection."""
    target = Path(path or CFG.db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit; all multi-statement work uses explicit transaction() blocks.
    conn = sqlite3.connect(str(target), timeout=10, check_same_thread=False, isolation_level=None,
                           factory=ZoraConnection)
    try:
        conn.row_factory = sqlite3.Row
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode and str(mode[0]).lower() != "wal":
            logger.warning("SQLite WAL mode unavailable on this filesystem (got %s): the dashboard may block "
                           "the trader. Keep DB_PATH on internal storage, not shared/SD storage.", mode[0])
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        with transaction(conn):
            _apply_migrations(conn)
    except Exception:
        conn.close()
        raise
    return conn


def connect_readonly(path: str | None = None) -> sqlite3.Connection:
    """Short-lived read-only connection (dashboard). URI-encoded so paths with spaces/#/? still work."""
    uri = Path(path or CFG.db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def close(conn: sqlite3.Connection) -> None:
    """Graceful shutdown: fold the WAL back into the main file, then close. Never raises."""
    try:
        with _guard(conn):
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as e:  # noqa: BLE001
                logger.warning("WAL checkpoint failed during shutdown: %s", e)
            conn.close()
    except sqlite3.Error as e:  # noqa: BLE001
        logger.warning("closing database failed: %s", e)


def integrity_check(conn: sqlite3.Connection) -> str:
    """'ok' or a description of the corruption. Used by `bot.py doctor`."""
    with _guard(conn):
        row = conn.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row else "no result"


# --------------------------------------------------------------------------- validation helpers
def _finite(value: Any, name: str, *, positive: bool = False, allow_zero: bool = True) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} is not a number: {value!r}") from e
    if not math.isfinite(v):
        raise ValueError(f"{name} is not finite: {value!r}")
    if positive and (v < 0 or (v == 0 and not allow_zero)):
        raise ValueError(f"{name} must be positive: {value!r}")
    return v


def _opt_finite(value: Any, name: str) -> float | None:
    """Optional numeric column: None passes through; NaN/inf is logged and stored as NULL (never poison metrics)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        logger.error("%s is not a number (%r); storing NULL", name, value)
        return None
    if not math.isfinite(v):
        logger.error("%s is not finite (%r); storing NULL", name, value)
        return None
    return v


# --------------------------------------------------------------------------- writes
def log_trade(conn: sqlite3.Connection, symbol: str, action: str, price: float, size: float,
              stop: float | None, take: float | None, mode: str, result: str = "open",
              pnl: float | None = None, exchange_id: str | None = None) -> int:
    price_f = _finite(price, "price", positive=True, allow_zero=False)
    size_f = _finite(size, "size", positive=True)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO trades(ts,symbol,action,price,size,stop_loss,take_profit,mode,result,pnl,exchange_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), symbol, action, price_f, size_f, _opt_finite(stop, "stop"), _opt_finite(take, "take"),
             mode, result, _opt_finite(pnl, "pnl"), exchange_id or CFG.exchange_id))
        return int(cur.lastrowid or 0)


def log_equity(conn: sqlite3.Connection, equity: float) -> None:
    value = _finite(equity, "equity")
    with transaction(conn):
        conn.execute("INSERT INTO equity_curve(ts,equity) VALUES(?,?)", (time.time(), value))


def log_decision(conn: sqlite3.Connection, symbol: str, signal: Any, approved: bool, reason: str,
                 llm_note: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO decision_log(ts,symbol,action,confidence,approved,reason,llm_note) VALUES(?,?,?,?,?,?,?)",
            (time.time(), symbol, signal.action, float(signal.confidence), int(bool(approved)), reason, llm_note))


def save_open_position(conn: sqlite3.Connection, p: dict) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO open_positions(symbol,ts,action,entry,size,stop_loss,take_profit,mode,protection_json,
                   expert_scores_json,factor_scores_json,initial_stop,exposure_pct,tiers_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET ts=excluded.ts, action=excluded.action, entry=excluded.entry,
                   size=excluded.size, stop_loss=excluded.stop_loss, take_profit=excluded.take_profit, mode=excluded.mode,
                   protection_json=excluded.protection_json, expert_scores_json=excluded.expert_scores_json,
                   factor_scores_json=excluded.factor_scores_json, initial_stop=excluded.initial_stop,
                   exposure_pct=excluded.exposure_pct, tiers_json=excluded.tiers_json""",
            (p["symbol"], p.get("opened_ts", time.time()), p["action"], _finite(p["entry"], "entry", positive=True),
             _finite(p["size"], "size", positive=True), p.get("stop"), p.get("take"), p.get("mode", "paper"),
             json.dumps(p.get("protection", {}), default=str), json.dumps(p.get("expert_scores", {})),
             json.dumps(p.get("factor_scores_by_tf", {})), p.get("initial_stop", p.get("stop")),
             _opt_finite(p.get("exposure_pct"), "exposure_pct"), json.dumps(p.get("tiers") or {}, default=str)))


def delete_open_position(conn: sqlite3.Connection, symbol: str) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))


# --- atomic multi-step helpers: the only way the trader changes position state ---------------------------
def record_open(conn: sqlite3.Connection, position: dict, intent_id: str | None = None) -> None:
    """Persist a newly opened position, its trade row and (optionally) resolve its order intent - atomically."""
    with transaction(conn):
        save_open_position(conn, position)
        log_trade(conn, position["symbol"], position["action"], position["entry"], position["size"],
                  position.get("stop"), position.get("take"), position.get("mode", "paper"))
        if intent_id:
            resolve_intent(conn, intent_id, "done")


def record_close(conn: sqlite3.Connection, position: dict, *, exit_price: float, pnl: float, result: str,
                 equity: float, mode: str, intent_id: str | None = None) -> None:
    """Trade row + position delete + equity point (+ intent resolution) in ONE transaction."""
    with transaction(conn):
        log_trade(conn, position["symbol"], position["action"], exit_price, position["size"],
                  position.get("stop"), position.get("take"), mode, result=result, pnl=pnl)
        delete_open_position(conn, position["symbol"])
        log_equity(conn, equity)
        if intent_id:
            resolve_intent(conn, intent_id, "done")


def record_partial_close(conn: sqlite3.Connection, position: dict, *, closed_size: float, exit_price: float,
                         pnl: float, equity: float, mode: str, result: str = "partial",
                         intent_id: str | None = None) -> None:
    """A close order only partly filled (or a scale-out leg hit): book the filled part, keep the remainder open
    (position['size'] is the REMAINING size when this is called)."""
    with transaction(conn):
        log_trade(conn, position["symbol"], position["action"], exit_price, closed_size,
                  position.get("stop"), position.get("take"), mode, result=result, pnl=pnl)
        save_open_position(conn, position)
        log_equity(conn, equity)
        if intent_id:
            resolve_intent(conn, intent_id, "done")


# --- durable key/value state ------------------------------------------------------------------------------
def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    with _guard(conn):
        row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO bot_state(key,value,ts) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts", (key, value, time.time()))


def get_state_json(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    raw = get_state(conn, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        logger.error("state %r is corrupt JSON; ignoring it", key)
        return default


def set_state_json(conn: sqlite3.Connection, key: str, value: Any) -> None:
    set_state(conn, key, json.dumps(value))


# --- order-intent journal ---------------------------------------------------------------------------------
def record_intent(conn: sqlite3.Connection, client_id: str, symbol: str, kind: str, side: str, amount: float,
                  ref_price: float | None) -> None:
    """Call BEFORE sending an order. `kind` is 'open' or 'close'."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO order_intents(client_id,ts,symbol,kind,side,amount,ref_price,status,updated) "
            "VALUES(?,?,?,?,?,?,?,'pending',?) ON CONFLICT(client_id) DO UPDATE SET status='pending', updated=excluded.updated",
            (client_id, time.time(), symbol, kind, side, _finite(amount, "amount", positive=True),
             _opt_finite(ref_price, "ref_price"), time.time()))


def resolve_intent(conn: sqlite3.Connection, client_id: str, status: str, detail: str | None = None) -> None:
    """status: 'done' (position persisted) | 'failed' (order definitively not placed) | 'unknown' (needs a human)."""
    with transaction(conn):
        conn.execute("UPDATE order_intents SET status=?, detail=?, updated=? WHERE client_id=?",
                     (status, detail, time.time(), client_id))


def pending_intents(conn: sqlite3.Connection) -> list[dict]:
    with _guard(conn):
        rows = conn.execute("SELECT * FROM order_intents WHERE status IN ('pending','unknown') ORDER BY ts").fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- reads
def load_open_positions(conn: sqlite3.Connection) -> list[dict]:
    rows: list[dict] = []
    with _guard(conn):
        raw = conn.execute("SELECT * FROM open_positions ORDER BY symbol").fetchall()
    for r in raw:
        d = dict(r)
        d["stop"] = d.pop("stop_loss")
        d["take"] = d.pop("take_profit")
        for col, key in _POSITION_JSON_COLUMNS:
            try:
                d[key] = json.loads(d.pop(col, None) or "{}")
            except (TypeError, ValueError):
                d[key] = {}
        d["initial_stop"] = d.get("initial_stop") or d.get("stop")
        d["opened_ts"] = d.get("ts")
        d.setdefault("exposure_pct", None)
        rows.append(d)
    return rows


def summary(conn: sqlite3.Connection) -> dict[str, float | int]:
    with _guard(conn):
        r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl FROM trades "
                         "WHERE result != 'open' AND pnl IS NOT NULL").fetchone()
        e = conn.execute("SELECT equity FROM equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    return {"closed_trades": int(r["n"]), "realized_pnl": float(r["pnl"]),
            "latest_equity": float(e["equity"] if e else CFG.starting_capital)}


def peak_equity(conn: sqlite3.Connection) -> float | None:
    with _guard(conn):
        r = conn.execute("SELECT MAX(equity) FROM equity_curve").fetchone()
    return float(r[0]) if r and r[0] is not None else None


def recent_closed_pnls(conn: sqlite3.Connection, limit: int = 500) -> list[float]:
    with _guard(conn):
        rows = conn.execute("SELECT pnl FROM trades WHERE result != 'open' AND pnl IS NOT NULL "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [float(r[0]) for r in reversed(rows)]


def equity_series(conn: sqlite3.Connection, limit: int = 500) -> list[tuple[float, float]]:
    with _guard(conn):
        rows = conn.execute("SELECT ts,equity FROM equity_curve ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [(float(r[0]), float(r[1])) for r in reversed(rows)]


def recent_trades(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    with _guard(conn):
        rows = conn.execute("SELECT ts,symbol,action,price,size,result,pnl,exchange_id FROM trades "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows][::-1]


def recent_decisions(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    with _guard(conn):
        rows = conn.execute("SELECT ts,symbol,action,confidence,approved,reason,llm_note FROM decision_log "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows][::-1]
