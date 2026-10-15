import fear_greed


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def test_score_is_contrarian_with_a_neutral_middle():
    assert fear_greed.score_from_index(10) == 80
    assert fear_greed.score_from_index(90) == -80
    assert fear_greed.score_from_index(50) == 0
    assert fear_greed.score_from_index(40) == 0
    assert fear_greed.score_from_index(0) == 100
    assert fear_greed.score_from_index(100) == -100


def test_fetch_parses_the_api_shape(monkeypatch):
    payload = {"data": [{"value": "23", "value_classification": "Extreme Fear"}]}
    monkeypatch.setattr(fear_greed.requests, "get", lambda *a, **k: _Resp(payload))
    assert fear_greed.fetch() == (23, "Extreme Fear")


def test_fetch_returns_none_on_error_or_garbage(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(fear_greed.requests, "get", boom)
    assert fear_greed.fetch() is None
    monkeypatch.setattr(fear_greed.requests, "get", lambda *a, **k: _Resp({"data": [{"value": "500"}]}))
    assert fear_greed.fetch() is None
    monkeypatch.setattr(fear_greed.requests, "get", lambda *a, **k: _Resp({"nope": 1}))
    assert fear_greed.fetch() is None


def test_get_index_caches_and_survives_an_outage(monkeypatch):
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return (30, "Fear")
    monkeypatch.setattr(fear_greed, "fetch", fake_fetch)
    monkeypatch.setitem(fear_greed._cache, "value", None)
    monkeypatch.setitem(fear_greed._cache, "ts", 0.0)
    assert fear_greed.get_index() == (30, "Fear")
    fear_greed.get_index()
    assert calls["n"] == 1
    monkeypatch.setattr(fear_greed, "fetch", lambda: None)
    assert fear_greed.get_index(force=True) == (30, "Fear")  # keeps the last good value


def test_fear_greed_score_is_none_when_never_available(monkeypatch):
    monkeypatch.setitem(fear_greed._cache, "value", None)
    monkeypatch.setattr(fear_greed, "fetch", lambda: None)
    score, note = fear_greed.fear_greed_score()
    assert score is None and "unavailable" in note
