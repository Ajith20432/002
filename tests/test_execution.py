import ccxt

import execution


def test_paper_executor_open_returns_the_given_price():
    ex = execution.PaperExecutor()
    assert ex.open(None, "BTC/USDT", "BUY", 1.0, 100.0) == 100.0


def test_paper_executor_close_returns_the_given_price():
    ex = execution.PaperExecutor()
    assert ex.close(None, "BTC/USDT", "BUY", 1.0, 105.0) == 105.0


def test_paper_executor_is_not_marked_live():
    assert execution.PaperExecutor.live is False


def test_live_executor_is_marked_live():
    assert execution.LiveExecutor.live is True


class _ExOk:
    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.001}}}

    def amount_to_precision(self, symbol, amount):
        return round(amount, 6)

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        assert type_ == "market"
        assert params and "clientOrderId" in params
        return {"average": 105.5}


def test_live_executor_successful_open_returns_fill_price_and_alerts():
    alerts = []
    ex = execution.LiveExecutor(telegram_alert=alerts.append)
    fill = ex.open(_ExOk(), "BTC/USDT", "BUY", 0.05, 105.0)
    assert fill == 105.5
    assert any("OPENED" in a for a in alerts)


def test_live_executor_open_uses_buy_side_close_uses_sell_side_for_a_buy_position():
    seen_sides = []

    class _ExRecordsSide:
        def market(self, symbol):
            return {"limits": {"amount": {"min": 0.001}}}

        def amount_to_precision(self, symbol, amount):
            return round(amount, 6)

        def create_order(self, symbol, type_, side, amount, price=None, params=None):
            seen_sides.append(side)
            return {"average": 100.0}

    ex = execution.LiveExecutor()
    fake = _ExRecordsSide()
    ex.open(fake, "BTC/USDT", "BUY", 1.0, 100.0)
    ex.close(fake, "BTC/USDT", "BUY", 1.0, 100.0)
    assert seen_sides == ["buy", "sell"]


def test_live_executor_close_uses_buy_side_for_closing_a_sell_position():
    seen_sides = []

    class _ExRecordsSide:
        def market(self, symbol):
            return {"limits": {"amount": {"min": 0.001}}}

        def amount_to_precision(self, symbol, amount):
            return round(amount, 6)

        def create_order(self, symbol, type_, side, amount, price=None, params=None):
            seen_sides.append(side)
            return {"average": 100.0}

    ex = execution.LiveExecutor()
    ex.close(_ExRecordsSide(), "ETH/USDT", "SELL", 1.0, 100.0)
    assert seen_sides == ["buy"]


class _ExBelowMin:
    def market(self, symbol):
        return {"limits": {"amount": {"min": 1.0}}}

    def amount_to_precision(self, symbol, amount):
        return round(amount, 6)

    def create_order(self, *a, **kw):
        raise AssertionError("must never place an order below the exchange minimum")


def test_live_executor_skips_orders_below_the_exchange_minimum():
    alerts = []
    ex = execution.LiveExecutor(telegram_alert=alerts.append)
    fill = ex.open(_ExBelowMin(), "BTC/USDT", "BUY", 0.0001, 105.0)
    assert fill is None
    assert any("below the exchange minimum" in a for a in alerts)


class _ExNetworkThenOk:
    def __init__(self):
        self.calls = 0

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.001}}}

    def amount_to_precision(self, symbol, amount):
        return round(amount, 6)

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls += 1
        if self.calls == 1:
            raise ccxt.NetworkError("simulated timeout")
        return {"average": 200.0}


def test_live_executor_retries_exactly_once_on_a_pure_network_error(monkeypatch):
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)  # skip the real backoff in tests
    monkeypatch.setattr(execution.CFG, "exec_max_attempts", 2)
    fake = _ExNetworkThenOk()
    ex = execution.LiveExecutor()
    fill = ex.close(fake, "ETH/USDT", "BUY", 0.5, 199.0)
    assert fill == 200.0
    assert fake.calls == 2


class _ExAlwaysNetworkError:
    def __init__(self):
        self.calls = 0

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.001}}}

    def amount_to_precision(self, symbol, amount):
        return round(amount, 6)

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls += 1
        raise ccxt.NetworkError("simulated persistent outage")


def test_live_executor_gives_up_after_one_retry_and_alerts(monkeypatch):
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)
    monkeypatch.setattr(execution.CFG, "exec_max_attempts", 2)
    alerts = []
    fake = _ExAlwaysNetworkError()
    ex = execution.LiveExecutor(telegram_alert=alerts.append)
    fill = ex.open(fake, "BTC/USDT", "BUY", 0.05, 100.0)
    assert fill is None
    assert fake.calls == 2  # attempted once, retried once, then stopped
    assert any("FAILED after retry" in a for a in alerts)


class _ExRejects:
    def __init__(self):
        self.calls = 0

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.001}}}

    def amount_to_precision(self, symbol, amount):
        return round(amount, 6)

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls += 1
        raise ccxt.ExchangeError("insufficient balance")


def test_live_executor_never_retries_a_hard_rejection():
    alerts = []
    fake = _ExRejects()
    ex = execution.LiveExecutor(telegram_alert=alerts.append)
    fill = ex.open(fake, "BTC/USDT", "SELL", 0.02, 100.0)
    assert fill is None
    assert fake.calls == 1  # must NOT retry a non-network rejection
    assert any("rejected" in a for a in alerts)


def test_live_executor_falls_back_to_the_last_price_when_the_order_has_no_average_field():
    class _ExNoAverage:
        def market(self, symbol):
            return {"limits": {"amount": {"min": 0.001}}}

        def amount_to_precision(self, symbol, amount):
            return round(amount, 6)

        def create_order(self, symbol, type_, side, amount, price=None, params=None):
            return {}  # some exchanges omit average/price on certain order types

    ex = execution.LiveExecutor()
    fill = ex.open(_ExNoAverage(), "BTC/USDT", "BUY", 1.0, 123.45)
    assert fill == 123.45


def test_sized_amount_falls_back_to_the_raw_amount_when_market_lookup_fails():
    class _ExBrokenMarket:
        def market(self, symbol):
            raise KeyError("unknown market")

        def amount_to_precision(self, symbol, amount):
            raise KeyError("unknown market")

    ex = execution.LiveExecutor()
    # best-effort fallback — let the exchange itself reject it if truly invalid
    assert ex._sized_amount(_ExBrokenMarket(), "BTC/USDT", 0.5) == 0.5

def test_native_protection_requires_exchange_capability(monkeypatch):
    alerts=[]
    ex=execution.LiveExecutor(alerts.append)
    class Fake:
        def featureValue(self, *args):
            return False
    monkeypatch.setattr(execution.CFG, 'native_protection_required', True)
    assert ex.place_protection(Fake(), 'ETH/USDT', 'BUY', 1, 90, 120) is None
    assert any('native' in a.lower() for a in alerts)

def test_native_protection_uses_stop_and_take_prices():
    calls=[]
    ex=execution.LiveExecutor()
    class Fake:
        def featureValue(self, symbol, kind, feature):
            return feature in ('stopLossPrice', 'takeProfitPrice')

        def create_order(self, *args):
            calls.append(args)
            return {'id': str(len(calls))}
    out=ex.place_protection(Fake(), 'ETH/USDT', 'BUY', 1, 90, 120)
    assert out and len(calls)==2
    assert calls[0][-1]['stopLossPrice']==90
    assert calls[1][-1]['takeProfitPrice']==120


def test_reconcile_uses_the_balance_for_spot_and_never_calls_fetch_positions():
    class Spot:
        has = {"fetchPositions": True, "fetchBalance": True}

        def market(self, symbol):
            return {"spot": True}

        def fetch_open_orders(self, symbol):
            return []

        def fetch_positions(self, symbols):
            raise AssertionError("spot markets have no positions endpoint")

        def fetch_balance(self):
            return {"total": {"ETH": 1.5}}

    r = execution.LiveExecutor().reconcile(Spot(), "ETH/USDT")
    assert r["safe"] and r["position"] == {"contracts": 1.5, "spot_balance": True}


def test_reconcile_is_unsafe_when_open_orders_cannot_be_read():
    class Broken:
        def fetch_open_orders(self, symbol):
            raise RuntimeError("boom")

    assert execution.LiveExecutor().reconcile(Broken(), "ETH/USDT")["safe"] is False


def test_live_open_exposes_the_exchange_rounded_amount():
    class Ex:
        options = {}

        def market(self, symbol):
            return {"limits": {"amount": {"min": 0.001}}}

        def amount_to_precision(self, symbol, amount):
            return f"{amount:.3f}"

        def create_order(self, *a):
            return {"average": 100.5}

    ex = execution.LiveExecutor()
    assert ex.open(Ex(), "ETH/USDT", "BUY", 0.12345, 100.0) == 100.5
    assert ex.last_amount == 0.123
