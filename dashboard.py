"""ZORA read-only dashboard backed by the real SQLite database.

Two interchangeable front ends serve the same JSON: the FastAPI app (`python bot.py dashboard`, the
production interface) and a tiny stdlib server (make_server / start_background) that runs inside the
trading process on Termux without extra dependencies. No HTTP endpoint can place a trade; the only
write path is the optional, secret-protected TradingView webhook, which can only cast one expiring vote.
"""
# NOTE: no `from __future__ import annotations` here: FastAPI resolves route annotations (e.g. Request) at def time.
import json
import logging
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import db
import tv_signal
from config import CFG
from metrics import compute_metrics

logger = logging.getLogger("zora.dashboard")
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "0"

SHARED: dict = {}
_LOCK = threading.Lock()


def publish(**values):
    """Called by the trading loop to expose live (non-persisted) state such as the latest signal."""
    with _LOCK:
        SHARED.update(values)


def _shared_copy():
    with _LOCK:
        return dict(SHARED)


def _open_readonly(path):
    return db.connect_readonly(path)


def _open_readonly_legacy(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _finite(v):
    return None if isinstance(v, float) and v in (float("inf"), float("-inf")) else v


def _ai_safety_status():
    try:
        import multi_ai
        return multi_ai.ai_safety_status()
    except Exception as e:
        return {"configured": False, "entry_blocked": False, "reason": f"unavailable: {e}"}


def snapshot_state(conn, brain_path="brain_state.json", shared=None):
    closed = db.recent_closed_pnls(conn, 500)
    equity = db.equity_series(conn, 500)
    try:
        positions = db.load_open_positions(conn)
    except sqlite3.DatabaseError:
        positions = []
    try:
        with open(brain_path) as f:
            brain = json.load(f)
    except (OSError, ValueError):
        brain = {}
    metrics = {k: _finite(v) for k, v in compute_metrics(closed, [e for _, e in equity] or None, CFG.starting_capital).items()}
    return {
        "generated": time.time(),
        "summary": db.summary(conn),
        "metrics": metrics,
        "equity": equity,
        "positions": [{k: p.get(k) for k in ("symbol", "action", "entry", "size", "stop", "take", "mode")} for p in positions],
        "trades": db.recent_trades(conn, 20),
        "decisions": db.recent_decisions(conn, 15),
        "brain": {"expert_weights": brain.get("expert_weights", {}), "factor_weights": brain.get("factor_weights", {}),
                  "trades_learned_from": brain.get("trades_learned_from", 0)},
        "live": shared or {},
        "ai_safety": _ai_safety_status(),
        "config": {"symbol": CFG.symbol, "symbols": list(CFG.symbols), "exchange": CFG.exchange_id,
                   "supported_exchanges": list(CFG.supported_exchanges), "risk_per_trade_pct": CFG.risk_per_trade_pct,
                   "max_exposure_pct": CFG.max_exposure_pct, "max_drawdown_pct": CFG.max_drawdown_pct,
                   "max_daily_loss_pct": CFG.max_daily_loss_pct, "mode": "live" if CFG.live_allowed() else "paper"},
    }


def _state_json():
    conn = _open_readonly(CFG.db_path)
    try:
        return snapshot_state(conn, os.getenv("BRAIN_STATE_PATH", "brain_state.json"), _shared_copy())
    finally:
        conn.close()


PAGE='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ZORA Dashboard</title><style>body{font:14px system-ui;margin:20px;background:#f5f6f8;color:#111}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px}.big{font-size:24px;font-weight:700}table{width:100%;border-collapse:collapse}td,th{padding:6px;border-bottom:1px solid #eee;text-align:left}#status{color:#666}</style></head><body><h1>ZORA</h1><div id="status">Loading…</div><div class="grid" id="kpis"></div><div class="card"><h3>Open Positions</h3><table id="positions"></table></div><div class="card"><h3>Recent Trades</h3><table id="trades"></table></div><div class="card"><h3>Recent Decisions</h3><table id="decisions"></table></div><script>const $=x=>document.getElementById(x);const fmt=x=>x==null?'—':Number(x).toFixed(2);function table(id,h,rows){let t=$(id);t.replaceChildren();let tr=document.createElement('tr');h.forEach(x=>{let e=document.createElement('th');e.textContent=x;tr.append(e)});t.append(tr);rows.forEach(r=>{let x=document.createElement('tr');r.forEach(v=>{let e=document.createElement('td');e.textContent=v;x.append(e)});t.append(x)})}async function load(){try{let s=await fetch('/api/state',{cache:'no-store'}).then(r=>r.json());$('status').textContent=`${s.config.exchange} · ${s.config.mode} · ${new Date(s.generated*1000).toLocaleString()} · SQLite`;let k=[['Equity',s.summary.latest_equity],['Realized PnL',s.summary.realized_pnl],['Win rate',s.metrics.win_rate_pct+'%'],['Profit factor',s.metrics.profit_factor],['Max DD',s.metrics.max_drawdown_pct+'%'],['Recovery',s.metrics.recovery_factor],['Calmar',s.metrics.calmar],['Loss streak',s.metrics.max_consecutive_losses],['Trades',s.summary.closed_trades]];$('kpis').replaceChildren(...k.map(([a,b])=>{let d=document.createElement('div');d.className='card';let h=document.createElement('h3');h.textContent=a;let p=document.createElement('div');p.className='big';p.textContent=fmt(b);d.append(h,p);return d}));table('positions',['Symbol','Side','Entry','Size','SL','TP','Mode'],s.positions.map(p=>[p.symbol,p.action,fmt(p.entry),fmt(p.size),fmt(p.stop),fmt(p.take),p.mode]));table('trades',['Time','Symbol','Side','Price','Size','Result','PnL','Exchange'],s.trades.map(t=>[new Date(t.ts*1000).toLocaleString(),t.symbol,t.action,fmt(t.price),fmt(t.size),t.result,fmt(t.pnl),t.exchange_id]));table('decisions',['Time','Symbol','Signal','Confidence','Approved','Reason'],s.decisions.map(d=>[new Date(d.ts*1000).toLocaleString(),d.symbol,d.action,fmt(d.confidence),d.approved?'yes':'no',d.reason||'']));}catch(e){$('status').textContent='Dashboard error: '+e}}load();setInterval(load,10000)</script></body></html>'''


def build_app():
    """Create the FastAPI app lazily so installs without fastapi (e.g. Termux) can still use everything else."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="ZORA Dashboard", version=VERSION, docs_url="/docs", redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "zora-dashboard", "time": time.time(), "exchange": CFG.exchange_id}

    @app.get("/api/state")
    def state():
        try:
            return JSONResponse(_state_json(), headers={"Cache-Control": "no-store"})
        except (OSError, sqlite3.Error) as e:
            raise HTTPException(503, f"database unavailable: {e}") from e

    @app.get("/api/exchanges")
    def exchanges():
        from exchange_manager import MANAGER
        out = {}
        for eid in CFG.supported_exchanges:
            try:
                out[eid] = MANAGER.capabilities(eid)
            except Exception as e:
                out[eid] = {"id": eid, "error": str(e)}
        return out

    @app.get("/api/config")
    def config_view():
        return {"exchange": CFG.exchange_id, "symbols": CFG.symbols, "paper_mode": not CFG.live_allowed(),
                "native_protection_required": CFG.native_protection_required}

    @app.post("/webhook/tradingview")
    async def tradingview(request: Request):
        raw = await request.body()
        try:
            score, note = tv_signal.handle_payload(raw)
        except tv_signal.WebhookError as e:
            raise HTTPException(e.status, str(e)) from e
        return {"ok": True, "score": score, "note": note}

    return app


class _Handler(BaseHTTPRequestHandler):
    """Stdlib read-only server used inside the trading process."""

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._send(200, json.dumps(_state_json()).encode(), "application/json")
            except Exception as e:
                self._send(503, json.dumps({"error": str(e)}).encode(), "application/json")
        elif path == "/api/health":
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/webhook/tradingview":
            self._send(405, b"Method Not Allowed", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n > tv_signal.MAX_BODY:       # never read an attacker-sized body into memory
            self._send(413, b'{"error":"payload too large"}', "application/json")
            return
        raw = self.rfile.read(n)
        try:
            score, note = tv_signal.handle_payload(raw)
            self._send(200, json.dumps({"ok": True, "score": score, "note": note}).encode(), "application/json")
        except tv_signal.WebhookError as e:
            self._send(e.status, json.dumps({"error": str(e)}).encode(), "application/json")

    def _reject(self):
        self._send(405, b"Method Not Allowed", "text/plain")

    do_PUT = do_DELETE = do_PATCH = _reject


def make_server(host, port):
    return ThreadingHTTPServer((host, port), _Handler)


def start_background():
    if not CFG.dashboard_port:
        return None
    server = make_server(CFG.dashboard_host, CFG.dashboard_port)
    threading.Thread(target=server.serve_forever, name="zora-dashboard", daemon=True).start()
    return server


def run_fastapi(host=None, port=None):
    try:
        import uvicorn
    except ImportError as e:
        raise SystemExit("The FastAPI dashboard needs: pip install fastapi uvicorn") from e
    uvicorn.run(build_app(), host=host or CFG.dashboard_host, port=port or CFG.dashboard_port or 8787, log_level="info")


def serve_forever(host=None, port=None):
    return run_fastapi(host, port)
