import costs


def test_buy_slippage_increases_fill(monkeypatch):
    monkeypatch.setattr(costs.CFG, "latency_bps", 0.0)
    c = costs.ExecutionCost(0.001, 10, 0)
    assert c.fill_price(100.0, "buy") == 100.1

def test_sell_slippage_decreases_fill(monkeypatch):
    monkeypatch.setattr(costs.CFG, "latency_bps", 0.0)
    c = costs.ExecutionCost(0.001, 10, 0)
    assert c.fill_price(100.0, "sell") == 99.9

def test_round_trip_fee():
    c = costs.ExecutionCost(0.001, 0, 0)
    assert c.round_trip_fee(100, 110, 2) == 0.42
