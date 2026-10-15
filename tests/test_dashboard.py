import json
import threading
import urllib.error
import urllib.request

import pytest

import config
import dashboard
import db


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setattr(config.CFG, "db_path", str(tmp_path / "d.db"))
    monkeypatch.setenv("BRAIN_STATE_PATH", str(tmp_path / "brain.json"))
    conn = db.connect()
    db.log_trade(conn, "ETH/USDT", "BUY", 100.0, 1.0, 95.0, 110.0, "paper", result="tp", pnl=5.0)
    db.log_trade(conn, "ETH/USDT", "BUY", 100.0, 1.0, 95.0, 110.0, "paper", result="sl", pnl=-2.0)
    db.log_equity(conn, 1003.0)
    db.log_equity(conn, 1001.0)
    conn.close()
    server = dashboard.make_server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode(), r.headers


def test_state_endpoint_reports_trades_and_metrics(served):
    status, body, headers = _get(served + "/api/state")
    data = json.loads(body)
    assert status == 200
    assert data["summary"]["closed_trades"] == 2
    assert data["metrics"]["win_rate_pct"] == 50.0
    assert len(data["equity"]) == 2
    assert headers["Cache-Control"] == "no-store"


def test_page_is_served_and_never_uses_innerhtml(served):
    status, body, _ = _get(served + "/")
    assert status == 200 and "ZORA" in body
    assert "innerHTML" not in body


def test_dashboard_is_read_only(served):
    for method in ("POST", "PUT", "DELETE"):
        req = urllib.request.Request(served + "/api/state", data=b"{}", method=method)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 405


def test_unknown_path_is_404(served):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(served + "/etc/passwd")
    assert e.value.code == 404


def test_published_live_values_are_exposed(served):
    dashboard.publish(signal="BUY 61%")
    data = json.loads(_get(served + "/api/state")[1])
    assert data["live"]["signal"] == "BUY 61%"


def test_missing_database_returns_503_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(config.CFG, "db_path", str(tmp_path / "nope" / "x.db"))
    server = dashboard.make_server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(f"http://127.0.0.1:{server.server_address[1]}/api/state")
        assert e.value.code == 503
    finally:
        server.shutdown()
        server.server_close()


def test_start_background_is_off_when_port_is_zero(monkeypatch):
    monkeypatch.setattr(config.CFG, "dashboard_port", 0)
    assert dashboard.start_background() is None
