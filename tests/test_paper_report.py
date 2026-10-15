import sqlite3
import time

import config
import db
import paper_report


def test_paper_report_gate_reads_trailing_window(tmp_path, monkeypatch):
    monkeypatch.setattr(config.CFG, "db_path", str(tmp_path / "zora.db"))
    monkeypatch.setattr(config.CFG, "starting_capital", 1000.0)
    conn = db.connect()
    now = time.time()
    for i in range(35):
        conn.execute("INSERT INTO trades(ts,symbol,action,price,size,mode,result,pnl) VALUES(?,?,?,?,?,?,?,?)",
                     (now - (34-i)*60, "BTC/USDT", "BUY", 100+i, 1, "paper", "take", 1.0))
        conn.execute("INSERT INTO equity_curve(ts,equity) VALUES(?,?)", (now - (34-i)*60, 1000+i))
    conn.commit(); conn.close()
    report = paper_report.build_report(30)
    assert report["trades"] == 35
    assert report["metrics"]["expectancy"] > 0
    assert report["passed"] is True
