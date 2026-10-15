import ai_guard


def test_rate_limit_blocks_new_entries_and_recovers(monkeypatch):
    monkeypatch.setattr(ai_guard, "configured", lambda: True)
    monkeypatch.setattr(ai_guard.CFG, "ai_entry_block_on_unavailable", True)
    monkeypatch.setattr(ai_guard.CFG, "ai_provider_cooldown_seconds", 60)
    ai_guard.record_available()
    assert ai_guard.before_cycle()[0] is True
    ai_guard.record_unavailable("provider 429")
    ok, reason = ai_guard.before_cycle()
    assert ok is False
    assert "429" in reason
    ai_guard.record_available()
    assert ai_guard.before_cycle()[0] is True
