import telegram_control
from config import CFG


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _configure(monkeypatch, token="FAKETOKEN", chat_id="999888"):
    monkeypatch.setattr(CFG, "telegram_bot_token", token)
    monkeypatch.setattr(CFG, "telegram_chat_id", chat_id)


def test_disabled_when_token_missing(monkeypatch):
    _configure(monkeypatch, token="", chat_id="999888")
    assert telegram_control.enabled() is False


def test_disabled_when_chat_id_missing(monkeypatch):
    _configure(monkeypatch, token="FAKETOKEN", chat_id="")
    assert telegram_control.enabled() is False


def test_enabled_when_both_are_set(monkeypatch):
    _configure(monkeypatch)
    assert telegram_control.enabled() is True


def test_send_is_a_silent_no_op_when_disabled(monkeypatch):
    _configure(monkeypatch, token="", chat_id="")

    def fail_if_called(*a, **kw):
        raise AssertionError("requests.post must not be called when Telegram is disabled")

    monkeypatch.setattr(telegram_control.requests, "post", fail_if_called)
    telegram_control.send("should never be sent")  # must not raise


def test_send_swallows_network_errors(monkeypatch):
    _configure(monkeypatch)

    def raise_network_error(*a, **kw):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(telegram_control.requests, "post", raise_network_error)
    telegram_control.send("this should not crash the caller")  # must not raise


def test_poll_returns_empty_list_when_disabled(monkeypatch):
    _configure(monkeypatch, token="", chat_id="")
    assert telegram_control.CommandPoller().poll() == []


def test_poll_only_accepts_commands_from_the_configured_chat(monkeypatch):
    _configure(monkeypatch)
    updates = {
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 999888}, "text": "/pause"}},
            {"update_id": 2, "message": {"chat": {"id": 111111}, "text": "/kill"}},  # a stranger
            {"update_id": 3, "message": {"chat": {"id": 999888}, "text": "/status"}},
        ]
    }
    monkeypatch.setattr(telegram_control.requests, "get", lambda url, params=None, timeout=None: _FakeResponse(updates))

    commands = telegram_control.CommandPoller().poll()
    assert commands == ["/pause", "/status"]  # the stranger's /kill must never appear


def test_poll_ignores_non_command_text(monkeypatch):
    _configure(monkeypatch)
    updates = {"result": [{"update_id": 1, "message": {"chat": {"id": 999888}, "text": "hey what's up"}}]}
    monkeypatch.setattr(telegram_control.requests, "get", lambda url, params=None, timeout=None: _FakeResponse(updates))
    assert telegram_control.CommandPoller().poll() == []


def test_poll_advances_offset_so_the_same_update_is_not_reprocessed(monkeypatch):
    _configure(monkeypatch)
    updates = {"result": [{"update_id": 5, "message": {"chat": {"id": 999888}, "text": "/status"}}]}
    monkeypatch.setattr(telegram_control.requests, "get", lambda url, params=None, timeout=None: _FakeResponse(updates))

    poller = telegram_control.CommandPoller()
    poller.poll()
    assert poller.offset == 6


def test_poll_is_case_insensitive_on_the_command(monkeypatch):
    _configure(monkeypatch)
    updates = {"result": [{"update_id": 1, "message": {"chat": {"id": 999888}, "text": "/PAUSE"}}]}
    monkeypatch.setattr(telegram_control.requests, "get", lambda url, params=None, timeout=None: _FakeResponse(updates))
    assert telegram_control.CommandPoller().poll() == ["/pause"]


def test_poll_returns_empty_on_request_failure_instead_of_raising(monkeypatch):
    _configure(monkeypatch)

    def raise_error(url, params=None, timeout=None):
        raise ConnectionError("simulated outage")

    monkeypatch.setattr(telegram_control.requests, "get", raise_error)
    assert telegram_control.CommandPoller().poll() == []
