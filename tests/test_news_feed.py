import news_feed


def _seed_cache(monkeypatch, headlines):
    monkeypatch.setitem(news_feed._cache, "headlines", headlines)
    monkeypatch.setitem(news_feed._cache, "ts", 10**15)  # far future -> cache reads as fresh


def test_positive_headline_scores_above_zero(monkeypatch):
    _seed_cache(monkeypatch, ["Bitcoin surges to new all-time high after ETF inflow"])
    score, matched = news_feed.news_sentiment("BTC/USDT")
    assert score > 0
    assert len(matched) == 1


def test_negative_headline_scores_below_zero(monkeypatch):
    _seed_cache(monkeypatch, ["Ethereum hack drains millions from DeFi protocol"])
    score, matched = news_feed.news_sentiment("ETH/USDT")
    assert score < 0


def test_unrelated_headline_is_not_matched(monkeypatch):
    _seed_cache(monkeypatch, ["Random unrelated headline about tomatoes"])
    score, matched = news_feed.news_sentiment("SOL/USDT")
    assert score == 0
    assert matched == []


def test_mixed_headlines_net_out(monkeypatch):
    # one clearly positive, one clearly negative mention of the same coin -> should roughly cancel
    _seed_cache(monkeypatch, [
        "Bitcoin rally continues as adoption grows",
        "Bitcoin exchange hacked, funds stolen in exploit",
    ])
    score, matched = news_feed.news_sentiment("BTC/USDT")
    assert len(matched) == 2
    assert -25 <= score <= 25  # not a strong lean either way once both sides are counted


def test_score_is_clamped_to_valid_range(monkeypatch):
    # stack up way more positive keyword hits than the +/-100 range would allow
    spammy = "Bitcoin surge rally soar gain bullish adoption approval partnership upgrade breakthrough"
    _seed_cache(monkeypatch, [spammy])
    score, _ = news_feed.news_sentiment("BTC/USDT")
    assert -100 <= score <= 100


def test_symbol_matching_is_case_insensitive_and_uses_known_aliases(monkeypatch):
    _seed_cache(monkeypatch, ["ETHEREUM surges on network upgrade news"])
    score, matched = news_feed.news_sentiment("ETH/USDT")
    assert len(matched) == 1
    assert score > 0


def test_unknown_base_currency_falls_back_to_matching_its_own_lowercase_name(monkeypatch):
    _seed_cache(monkeypatch, ["Pepecoin rally sends PEPE to new highs"])
    score, matched = news_feed.news_sentiment("PEPE/USDT")
    assert len(matched) == 1


def test_get_headlines_caches_and_does_not_refetch_within_the_cache_window(monkeypatch):
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return [f"fetch number {calls['n']}"]

    monkeypatch.setattr(news_feed, "_fetch_headlines", fake_fetch)
    monkeypatch.setitem(news_feed._cache, "headlines", [])
    monkeypatch.setitem(news_feed._cache, "ts", 0)

    first = news_feed.get_headlines()
    second = news_feed.get_headlines()  # should hit the cache, not fetch again
    assert calls["n"] == 1
    assert first == second


def test_get_headlines_force_refetches_even_within_the_cache_window(monkeypatch):
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return [f"fetch number {calls['n']}"]

    monkeypatch.setattr(news_feed, "_fetch_headlines", fake_fetch)
    monkeypatch.setitem(news_feed._cache, "headlines", ["stale"])
    monkeypatch.setitem(news_feed._cache, "ts", 10**15)

    news_feed.get_headlines(force=True)
    assert calls["n"] == 1


def test_one_feed_failing_does_not_prevent_the_other_from_being_read(monkeypatch):
    class _FakeResponse:
        def __init__(self, xml_bytes):
            self.content = xml_bytes
        def raise_for_status(self):
            pass

    good_xml = b"<rss><channel><item><title>Bitcoin partnership announced</title></item></channel></rss>"

    def fake_get(url, timeout=None, headers=None):
        if "coindesk" in url:
            raise ConnectionError("simulated feed outage")
        return _FakeResponse(good_xml)

    monkeypatch.setattr(news_feed.requests, "get", fake_get)
    headlines = news_feed._fetch_headlines()
    assert any("partnership" in h for h in headlines)
