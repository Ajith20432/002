"""Generate a self-contained HTML report for a historical portfolio replay."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


def _svg_curve(values: list[float], width=1000, height=320) -> str:
    if not values:
        return '<svg viewBox="0 0 1000 320"><text x="20" y="40">No equity data</text></svg>'
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    pts = []
    for i, value in enumerate(values):
        x = 20 + (width - 40) * i / max(1, len(values) - 1)
        y = 20 + (height - 40) * (hi - value) / span
        pts.append(f"{x:.1f},{y:.1f}")
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Equity curve">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="3" points="{" ".join(pts)}"/>'
            f'<text x="20" y="{height-5}">Start {values[0]:.2f}</text>'
            f'<text x="{width-170}" y="20">End {values[-1]:.2f}</text></svg>')


def build_html(report: dict) -> str:
    m = report.get("metrics", {})
    symbols = ", ".join(map(str, report.get("symbols", [])))
    rows = []
    for item in report.get("symbols_report", []):
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(item.get(k, '')))}</td>"
                     for k in ("symbol", "candles", "return_pct", "trades", "profit_factor", "max_drawdown_pct", "kill_switch")) + "</tr>")
    payload = json.dumps(report, default=str).replace("</", "<\\/")
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZORA Historical Replay</title><style>body{{font:15px system-ui;background:#0b1020;color:#e8edf7;margin:0;padding:24px}}.wrap{{max-width:1100px;margin:auto}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card{{background:#131a2d;border:1px solid #29324a;border-radius:12px;padding:16px}}.big{{font-size:25px;font-weight:700}}svg{{width:100%;height:auto;background:#0f1526;border-radius:10px;margin-top:12px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border-bottom:1px solid #29324a;text-align:left}}.ok{{color:#6ee7a5}}.warn{{color:#fbbf69}}small{{color:#9aa7bd}}</style></head><body><div class="wrap">
<h1>ZORA Historical Replay</h1><p><small>{html.escape(symbols)} · {report.get('candles', 0)} candles · Historical simulation only</small></p>
<div class="grid">{''.join(f'<div class="card"><small>{k}</small><div class="big">{v}</div></div>' for k,v in [('Final equity',f"{report.get('final_equity',0):.2f}"),('Return',f"{m.get('return_pct',0):+.2f}%"),('Trades',m.get('trades',0)),('Win rate',f"{m.get('win_rate_pct',0):.1f}%"),('Profit factor',m.get('profit_factor')),('Max DD',f"{m.get('max_drawdown_pct',0):.2f}%"),('Sharpe',f"{m.get('sharpe',0):.2f}"),('Sortino',f"{m.get('sortino',0):.2f}")])}</div>
<div class="card"><h2>Portfolio equity curve</h2>{_svg_curve(report.get('equity_curve', []))}</div>
<div class="card"><h2>Symbol breakdown</h2><table><tr><th>Symbol</th><th>Candles</th><th>Return %</th><th>Trades</th><th>PF</th><th>Max DD %</th><th>Kill switch</th></tr>{''.join(rows)}</table></div>
<div class="card"><b class="{'ok' if report.get('passed') else 'warn'}">Gate: {'PASS' if report.get('passed') else 'REVIEW'}</b><p>{html.escape(report.get('disclaimer',''))}</p></div>
<script type="application/json" id="report">{payload}</script></div></body></html>'''


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create a self-contained ZORA replay HTML report")
    ap.add_argument("input", help="portfolio-backtest JSON file")
    ap.add_argument("--output", default="zora_replay_report.html")
    args = ap.parse_args(argv)
    report = json.loads(Path(args.input).read_text())
    Path(args.output).write_text(build_html(report), encoding="utf-8")
    print(f"Replay report written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
