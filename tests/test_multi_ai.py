import json
from dataclasses import dataclass

import pytest

import multi_ai


@dataclass
class FakeSignal:
    action: str = "BUY"
    confidence: float = 40.0
    reasons: list = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = ["1h:+45"]


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """_circuit is module-level mutable state — never let one test's recorded
    failures leak into the next test's assertions."""
    multi_ai._circuit.clear()
    yield
    multi_ai._circuit.clear()


class _FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ---------------- parse_verdict / _extract_json_object ----------------

def test_parse_verdict_reads_clean_json():
    lean, confidence, note = multi_ai.parse_verdict('{"lean": "BUY", "confidence": 72, "note": "solid setup"}')
    assert (lean, confidence, note) == ("BUY", 72, "solid setup")


def test_parse_verdict_extracts_json_embedded_in_surrounding_prose():
    text = 'Sure, here you go:\n{"lean": "SELL", "confidence": 60, "note": "overbought"}\nHope that helps!'
    lean, confidence, note = multi_ai.parse_verdict(text)
    assert lean == "SELL"
    assert confidence == 60


def test_parse_verdict_clamps_out_of_range_confidence():
    lean, confidence, _ = multi_ai.parse_verdict('{"lean": "BUY", "confidence": 500, "note": "x"}')
    assert confidence == 100


def test_parse_verdict_defaults_confidence_on_a_garbage_value():
    lean, confidence, _ = multi_ai.parse_verdict('{"lean": "BUY", "confidence": "not-a-number", "note": "x"}')
    assert confidence == 50


def test_parse_verdict_normalizes_an_unknown_lean_to_hold():
    lean, _, _ = multi_ai.parse_verdict('{"lean": "MAYBE", "confidence": 50, "note": "x"}')
    assert lean == "HOLD"


def test_parse_verdict_falls_back_to_legacy_text_format_when_json_is_unparseable():
    lean, confidence, note = multi_ai.parse_verdict("LEAN: SELL\nNOTE: thin volume here")
    assert lean == "SELL"
    assert "thin volume" in note


def test_parse_verdict_legacy_fallback_defaults_to_hold_on_total_garbage():
    lean, _, _ = multi_ai.parse_verdict("the model just rambled with no structure at all")
    assert lean == "HOLD"


def test_extract_json_object_returns_none_for_non_json_text():
    assert multi_ai._extract_json_object("no json here at all") is None


def test_extract_json_object_handles_empty_or_none_input_without_raising():
    assert multi_ai._extract_json_object("") is None
    assert multi_ai._extract_json_object(None) is None


# ---------------- _base_prompt ----------------

def test_base_prompt_includes_headlines_when_given():
    prompt = multi_ai._base_prompt("BTC/USDT", FakeSignal(), headlines=["Bitcoin surges past resistance"])
    assert "Bitcoin surges past resistance" in prompt


def test_base_prompt_omits_headline_section_when_none_given():
    prompt = multi_ai._base_prompt("BTC/USDT", FakeSignal(), headlines=None)
    assert "Recent relevant headlines" not in prompt


def test_base_prompt_states_the_json_schema():
    prompt = multi_ai._base_prompt("BTC/USDT", FakeSignal())
    assert '"lean"' in prompt and '"confidence"' in prompt


# ---------------- _execute_tool ----------------

def test_execute_tool_reports_no_exchange_without_crashing():
    result = json.loads(multi_ai._execute_tool("BTC/USDT", "1h", exchange=None))
    assert "error" in result


def test_execute_tool_returns_real_closes_when_an_exchange_handle_is_given(monkeypatch):
    import pandas as pd
    fake_df = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
    monkeypatch.setattr(multi_ai, "fetch_ohlcv_df",
                         lambda exchange, symbol, timeframe, limit=20: fake_df)
    result = json.loads(multi_ai._execute_tool("BTC/USDT", "1h", exchange=object()))
    assert result == {"timeframe": "1h", "last_10_closes": [100.0, 101.0, 102.0]}


def test_execute_tool_degrades_to_an_error_string_on_fetch_failure(monkeypatch):
    def broken(exchange, symbol, timeframe, limit=20):
        raise RuntimeError("network down")
    monkeypatch.setattr(multi_ai, "fetch_ohlcv_df", broken)
    result = json.loads(multi_ai._execute_tool("BTC/USDT", "1h", exchange=object()))
    assert "error" in result


# ---------------- circuit breaker ----------------

def test_provider_is_not_cooling_down_by_default():
    assert multi_ai._is_cooling_down("groq") is False


def test_record_failure_with_explicit_retry_after_sets_a_cooldown():
    multi_ai._record_failure("groq", retry_after=100)
    assert multi_ai._is_cooling_down("groq") is True


def test_record_success_clears_any_existing_cooldown():
    multi_ai._record_failure("groq", retry_after=100)
    multi_ai._record_success("groq")
    assert multi_ai._is_cooling_down("groq") is False


def test_a_couple_of_failures_below_the_threshold_does_not_trip_the_breaker():
    for _ in range(multi_ai.COOLDOWN_AFTER_FAILURES - 1):
        multi_ai._record_failure("groq")
    assert multi_ai._is_cooling_down("groq") is False


def test_reaching_the_failure_threshold_trips_the_breaker():
    for _ in range(multi_ai.COOLDOWN_AFTER_FAILURES):
        multi_ai._record_failure("groq")
    assert multi_ai._is_cooling_down("groq") is True


def test_cooldown_grows_with_repeated_failures_up_to_the_cap():
    for _ in range(multi_ai.COOLDOWN_AFTER_FAILURES):
        multi_ai._record_failure("groq")
    first_cooldown = multi_ai._circuit["groq"]["cooldown_until"]
    multi_ai._record_failure("groq")
    second_cooldown = multi_ai._circuit["groq"]["cooldown_until"]
    assert second_cooldown > first_cooldown


def test_configured_providers_excludes_one_that_is_currently_cooling_down(monkeypatch):
    monkeypatch.setattr(multi_ai.CFG, "groq_api_key", "key")
    monkeypatch.setattr(multi_ai.CFG, "openrouter_api_key", "")
    monkeypatch.setattr(multi_ai.CFG, "gemini_api_key", "")
    monkeypatch.setattr(multi_ai.CFG, "anthropic_api_key", "")
    monkeypatch.setattr(multi_ai.CFG, "openai_api_key", "")
    multi_ai._record_failure("groq", retry_after=999)
    assert multi_ai._configured_providers() == []


def test_configured_providers_includes_a_keyed_provider_with_a_clean_record(monkeypatch):
    monkeypatch.setattr(multi_ai.CFG, "groq_api_key", "key")
    monkeypatch.setattr(multi_ai.CFG, "openrouter_api_key", "")
    monkeypatch.setattr(multi_ai.CFG, "gemini_api_key", "")
    monkeypatch.setattr(multi_ai.CFG, "anthropic_api_key", "")
    monkeypatch.setattr(multi_ai.CFG, "openai_api_key", "")
    names = [name for name, _ in multi_ai._configured_providers()]
    assert names == ["groq"]


# ---------------- _retry_after_seconds ----------------

def test_retry_after_seconds_parses_a_valid_header():
    class R:
        headers = {"Retry-After": "12.5"}
    assert multi_ai._retry_after_seconds(R()) == 12.5


def test_retry_after_seconds_returns_none_when_the_header_is_missing():
    class R:
        headers = {}
    assert multi_ai._retry_after_seconds(R()) is None


def test_retry_after_seconds_returns_none_on_an_unparseable_header():
    class R:
        headers = {"Retry-After": "not-a-number"}
    assert multi_ai._retry_after_seconds(R()) is None


# ---------------- _call_with_retry ----------------

def test_call_with_retry_succeeds_on_the_first_try():
    def fn(prompt, symbol, exchange):
        return '{"lean": "BUY", "confidence": 80, "note": "clean"}'
    lean, confidence, note, err = multi_ai._call_with_retry("fake", fn, "prompt", "BTC/USDT", None)
    assert (lean, confidence, err) == ("BUY", 80, None)
    assert multi_ai._is_cooling_down("fake") is False


def test_call_with_retry_retries_once_on_a_transient_error_then_succeeds():
    calls = {"n": 0}

    def fn(prompt, symbol, exchange):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient hiccup")
        return '{"lean": "SELL", "confidence": 55, "note": "recovered"}'

    lean, confidence, note, err = multi_ai._call_with_retry("fake", fn, "prompt", "BTC/USDT", None)
    assert (lean, err) == ("SELL", None)
    assert calls["n"] == 2


def test_call_with_retry_gives_up_after_one_retry_and_records_a_failure():
    def fn(prompt, symbol, exchange):
        raise RuntimeError("persistent failure")

    lean, confidence, note, err = multi_ai._call_with_retry("fake", fn, "prompt", "BTC/USDT", None)
    assert lean is None
    assert err is not None
    assert multi_ai._circuit["fake"]["failures"] == 1


def test_call_with_retry_never_retries_a_confirmed_rate_limit(monkeypatch):
    calls = {"n": 0}

    def fn(prompt, symbol, exchange):
        calls["n"] += 1
        raise multi_ai._RateLimited(retry_after=42)

    lean, confidence, note, err = multi_ai._call_with_retry("fake", fn, "prompt", "BTC/USDT", None)
    assert lean is None
    assert "rate limited" in err
    assert calls["n"] == 1  # no retry burned on a confirmed rate limit
    assert multi_ai._is_cooling_down("fake") is True


# ---------------- _synthesize_with_judge ----------------

def test_synthesize_with_judge_skips_when_fewer_than_two_verdicts():
    verdicts = [{"name": "a", "lean": "BUY", "confidence": 70, "note": "x"}]
    score, note = multi_ai._synthesize_with_judge("BTC/USDT", FakeSignal(), verdicts)
    assert score is None
    assert note == ""


def test_synthesize_with_judge_uses_the_configured_judge_provider(monkeypatch):
    def judge_fn(prompt, symbol, exchange):
        assert "reviewers assessed" in prompt
        return '{"lean": "BUY", "confidence": 66, "note": "panel mostly agrees"}'

    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [("groq", judge_fn), ("gemini", judge_fn)])
    monkeypatch.setattr(multi_ai.CFG, "ai_judge_provider", "groq")

    verdicts = [
        {"name": "groq", "lean": "BUY", "confidence": 70, "note": "bullish"},
        {"name": "gemini", "lean": "SELL", "confidence": 40, "note": "cautious"},
    ]
    score, note = multi_ai._synthesize_with_judge("BTC/USDT", FakeSignal(), verdicts)
    assert score == 66
    assert "judge (groq)" in note


def test_synthesize_with_judge_falls_back_to_the_first_available_when_configured_judge_is_absent(monkeypatch):
    def judge_fn(prompt, symbol, exchange):
        return '{"lean": "HOLD", "confidence": 50, "note": "mixed"}'

    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [("gemini", judge_fn)])
    monkeypatch.setattr(multi_ai.CFG, "ai_judge_provider", "some_unconfigured_provider")

    verdicts = [
        {"name": "a", "lean": "BUY", "confidence": 70, "note": "x"},
        {"name": "b", "lean": "SELL", "confidence": 40, "note": "y"},
    ]
    score, note = multi_ai._synthesize_with_judge("BTC/USDT", FakeSignal(), verdicts)
    assert score == 0  # HOLD -> direction 0
    assert "judge (gemini)" in note


def test_synthesize_with_judge_falls_back_gracefully_when_the_judge_call_fails(monkeypatch):
    def broken(prompt, symbol, exchange):
        raise RuntimeError("down")

    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [("groq", broken)])
    monkeypatch.setattr(multi_ai.CFG, "ai_judge_provider", "groq")

    verdicts = [
        {"name": "groq", "lean": "BUY", "confidence": 70, "note": "x"},
        {"name": "gemini", "lean": "SELL", "confidence": 40, "note": "y"},
    ]
    score, note = multi_ai._synthesize_with_judge("BTC/USDT", FakeSignal(), verdicts)
    assert score is None
    assert note == ""


# ---------------- get_ai_consensus (end to end) ----------------

def test_get_ai_consensus_with_no_providers_configured(monkeypatch):
    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [])
    score, notes = multi_ai.get_ai_consensus("BTC/USDT", FakeSignal())
    assert score is None
    assert "no AI providers configured" in notes[0]


def test_get_ai_consensus_uses_judge_synthesis_over_plain_vote_averaging(monkeypatch):
    calls = {"bullish": 0, "bearish": 0}

    def bullish(prompt, symbol, exchange):
        calls["bullish"] += 1
        if "reviewers assessed" in prompt:  # this invocation is the judge-synthesis call
            return '{"lean": "BUY", "confidence": 60, "note": "panel leans bullish overall"}'
        return '{"lean": "BUY", "confidence": 80, "note": "strong trend"}'

    def bearish(prompt, symbol, exchange):
        calls["bearish"] += 1
        return '{"lean": "SELL", "confidence": 40, "note": "overextended"}'

    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [("fake_a", bullish), ("fake_b", bearish)])
    monkeypatch.setattr(multi_ai.CFG, "ai_judge_provider", "fake_a")

    score, notes = multi_ai.get_ai_consensus("BTC/USDT", FakeSignal())

    assert score == 60  # the judge's synthesized call, not a plain vote average
    assert notes[0].startswith("judge (fake_a):")
    assert any(n.startswith("fake_a: BUY 80%") for n in notes)
    assert any(n.startswith("fake_b: SELL 40%") for n in notes)
    assert calls["bullish"] == 2  # once for its own panel verdict, once as judge


def test_get_ai_consensus_falls_back_to_confidence_weighted_average_when_judge_unavailable(monkeypatch):
    def bullish(prompt, symbol, exchange):
        return '{"lean": "BUY", "confidence": 80, "note": "strong trend"}'

    def bearish(prompt, symbol, exchange):
        return '{"lean": "SELL", "confidence": 40, "note": "overextended"}'

    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [("fake_a", bullish), ("fake_b", bearish)])
    monkeypatch.setattr(multi_ai, "_synthesize_with_judge", lambda symbol, signal, verdicts: (None, ""))

    score, notes = multi_ai.get_ai_consensus("BTC/USDT", FakeSignal())
    # confidence-weighted average: (+0.80 + -0.40) / 2 * 100 = 20.0
    assert score == pytest.approx(20.0, abs=0.01)


def test_get_ai_consensus_reports_a_provider_error_without_crashing(monkeypatch):
    def bullish(prompt, symbol, exchange):
        return '{"lean": "BUY", "confidence": 80, "note": "ok"}'

    def broken(prompt, symbol, exchange):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [("fake_a", bullish), ("fake_broken", broken)])
    score, notes = multi_ai.get_ai_consensus("BTC/USDT", FakeSignal())
    assert score is not None  # fake_a still answered
    assert any("fake_broken: error" in n for n in notes)


def test_get_ai_consensus_returns_none_when_every_provider_fails(monkeypatch):
    def broken(prompt, symbol, exchange):
        raise RuntimeError("down")

    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [("fake_a", broken)])
    score, notes = multi_ai.get_ai_consensus("BTC/USDT", FakeSignal())
    assert score is None
    assert any("error" in n for n in notes)


def test_get_ai_consensus_passes_the_exchange_handle_through_to_providers(monkeypatch):
    received = {}

    def fn(prompt, symbol, exchange):
        received["exchange"] = exchange
        return '{"lean": "HOLD", "confidence": 50, "note": "x"}'

    monkeypatch.setattr(multi_ai, "_configured_providers", lambda: [("fake_a", fn)])
    sentinel = object()
    multi_ai.get_ai_consensus("BTC/USDT", FakeSignal(), exchange=sentinel)
    assert received["exchange"] is sentinel


# ---------------- _call_openai_style: the agentic tool-use loop ----------------

def test_call_openai_style_executes_a_tool_call_then_returns_final_content(monkeypatch):
    responses = [
        {"choices": [{"message": {
            "role": "assistant",
            "tool_calls": [{"id": "call_1", "function": {"name": "get_price_series",
                                                           "arguments": '{"timeframe": "5m"}'}}],
        }}]},
        {"choices": [{"message": {"content": '{"lean": "BUY", "confidence": 70, "note": "5m confirms"}'}}]},
    ]
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _FakeResponse(responses[len(calls) - 1])

    monkeypatch.setattr(multi_ai.requests, "post", fake_post)
    monkeypatch.setattr(multi_ai, "_execute_tool", lambda symbol, timeframe, exchange: '{"last_10_closes": [1,2,3]}')
    monkeypatch.setattr(multi_ai.CFG, "ai_tool_use_enabled", True)

    text = multi_ai._call_openai_style(
        "https://fake.example/v1/chat/completions", "key", "fake-model",
        "prompt", "BTC/USDT", exchange=object(), use_json_mode=True, enable_tools=True,
    )

    assert json.loads(text) == {"lean": "BUY", "confidence": 70, "note": "5m confirms"}
    assert len(calls) == 2
    second_request_messages = calls[1]["messages"]
    assert any(m.get("role") == "tool" for m in second_request_messages)


def test_call_openai_style_never_offers_tools_when_the_caller_disables_them(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _FakeResponse({"choices": [{"message": {"content": '{"lean": "HOLD", "confidence": 50, "note": "x"}'}}]})

    monkeypatch.setattr(multi_ai.requests, "post", fake_post)
    multi_ai._call_openai_style("https://fake", "key", "model", "prompt", "BTC/USDT",
                                 None, use_json_mode=True, enable_tools=False)
    assert "tools" not in calls[0]


def test_call_openai_style_raises_rate_limited_on_429_with_the_retry_after_header(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({}, status_code=429, headers={"Retry-After": "17"})

    monkeypatch.setattr(multi_ai.requests, "post", fake_post)

    raised = None
    try:
        multi_ai._call_openai_style("https://fake", "key", "model", "prompt", "BTC/USDT",
                                     None, use_json_mode=True, enable_tools=False)
    except multi_ai._RateLimited as e:
        raised = e
    assert raised is not None
    assert raised.retry_after == 17.0


# ---------------- _call_anthropic: native tool-calling loop ----------------

def test_call_anthropic_returns_verdict_json_when_the_model_calls_submit_verdict(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse({"content": [{"type": "tool_use", "id": "t1", "name": "submit_verdict",
                                            "input": {"lean": "BUY", "confidence": 75, "note": "clean breakout"}}]})

    monkeypatch.setattr(multi_ai.requests, "post", fake_post)
    text = multi_ai._call_anthropic("prompt", "BTC/USDT", exchange=None)
    assert json.loads(text) == {"lean": "BUY", "confidence": 75, "note": "clean breakout"}


def test_call_anthropic_executes_the_price_tool_before_submitting_a_verdict(monkeypatch):
    responses = [
        {"content": [{"type": "tool_use", "id": "p1", "name": "get_price_series", "input": {"timeframe": "5m"}}]},
        {"content": [{"type": "tool_use", "id": "v1", "name": "submit_verdict",
                      "input": {"lean": "SELL", "confidence": 55, "note": "5m weak"}}]},
    ]
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _FakeResponse(responses[len(calls) - 1])

    monkeypatch.setattr(multi_ai.requests, "post", fake_post)
    monkeypatch.setattr(multi_ai, "_execute_tool", lambda symbol, timeframe, exchange: '{"last_10_closes": [1,2]}')
    monkeypatch.setattr(multi_ai.CFG, "ai_tool_use_enabled", True)

    text = multi_ai._call_anthropic("prompt", "BTC/USDT", exchange=object())
    assert json.loads(text) == {"lean": "SELL", "confidence": 55, "note": "5m weak"}
    assert len(calls) == 2


def test_call_anthropic_forces_the_verdict_tool_on_the_final_round(monkeypatch):
    # The model keeps asking for price data past the allowed tool rounds; the
    # loop must still terminate with a forced verdict on the final round.
    responses = [
        {"content": [{"type": "tool_use", "id": "p1", "name": "get_price_series", "input": {"timeframe": "5m"}}]},
        {"content": [{"type": "tool_use", "id": "p2", "name": "get_price_series", "input": {"timeframe": "30m"}}]},
        {"content": [{"type": "tool_use", "id": "v1", "name": "submit_verdict",
                      "input": {"lean": "HOLD", "confidence": 50, "note": "forced final answer"}}]},
    ]
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _FakeResponse(responses[len(calls) - 1])

    monkeypatch.setattr(multi_ai.requests, "post", fake_post)
    monkeypatch.setattr(multi_ai, "_execute_tool", lambda symbol, timeframe, exchange: '{"last_10_closes": [1,2]}')
    monkeypatch.setattr(multi_ai.CFG, "ai_tool_use_enabled", True)

    text = multi_ai._call_anthropic("prompt", "BTC/USDT", exchange=object())
    assert json.loads(text) == {"lean": "HOLD", "confidence": 50, "note": "forced final answer"}
    assert len(calls) == 3
    assert calls[2]["tool_choice"] == {"type": "tool", "name": "submit_verdict"}
    assert calls[2]["tools"] == [multi_ai._ANTHROPIC_VERDICT_TOOL]


def test_groq_healthcheck_reports_missing_key(monkeypatch):
    monkeypatch.setattr(multi_ai.CFG, "groq_api_key", "")
    ok, detail = multi_ai.groq_healthcheck()
    assert ok is False
    assert "not configured" in detail


def test_groq_healthcheck_accepts_current_model(monkeypatch):
    monkeypatch.setattr(multi_ai.CFG, "groq_api_key", "key")
    monkeypatch.setattr(multi_ai.CFG, "groq_model", "openai/gpt-oss-20b")
    monkeypatch.setattr(multi_ai, "_groq_models", lambda: {"openai/gpt-oss-20b"})
    ok, detail = multi_ai.groq_healthcheck()
    assert ok is True
    assert "available" in detail


def test_groq_healthcheck_reports_fallback_for_unavailable_configured_model(monkeypatch):
    monkeypatch.setattr(multi_ai.CFG, "groq_api_key", "key")
    monkeypatch.setattr(multi_ai.CFG, "groq_model", "old-model")
    monkeypatch.setattr(multi_ai.CFG, "groq_fallback_models", ("openai/gpt-oss-20b",))
    monkeypatch.setattr(multi_ai, "_groq_models", lambda: {"openai/gpt-oss-20b"})
    ok, detail = multi_ai.groq_healthcheck()
    assert ok is False
    assert "fallback available" in detail


def test_low_confidence_provider_is_forced_to_hold(monkeypatch):
    def fn(prompt, symbol, exchange):
        return '{"lean": "BUY", "confidence": 10, "note": "weak"}'
    lean, confidence, note, err = multi_ai._call_with_retry("lowconf", fn, "prompt", "BTC/USDT", None)
    assert lean == "HOLD"
    assert confidence == 10
    assert "safety floor" in note
    assert err is None


def test_groq_probe_fails_cleanly_without_key(monkeypatch):
    monkeypatch.setattr(multi_ai.CFG, "groq_api_key", "")
    ok, detail = multi_ai.groq_probe()
    assert ok is False
    assert "not configured" in detail


def test_groq_probe_uses_an_available_fallback(monkeypatch):
    monkeypatch.setattr(multi_ai.CFG, "groq_api_key", "key")
    monkeypatch.setattr(multi_ai.CFG, "groq_model", "old-model")
    monkeypatch.setattr(multi_ai.CFG, "groq_fallback_models", ("openai/gpt-oss-20b",))
    monkeypatch.setattr(multi_ai, "_groq_models", lambda: {"openai/gpt-oss-20b"})
    monkeypatch.setattr(multi_ai, "_call_openai_style",
                        lambda *a, **k: '{"lean":"HOLD","confidence":100,"note":"connection ok"}')
    ok, detail = multi_ai.groq_probe()
    assert ok is True
    assert "openai/gpt-oss-20b" in detail
