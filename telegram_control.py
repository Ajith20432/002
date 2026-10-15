"""Telegram alerts and remote control. Only messages from admin chat IDs are ever acted on."""
from __future__ import annotations

import requests

from config import CFG

BASE = "https://api.telegram.org/bot{}"
MAX_MESSAGE = 4000  # Telegram rejects >4096 chars; a long AI note used to make the whole alert silently vanish

COMMANDS = [
    ("start", "Bot status / welcome"), ("status", "Full bot status"), ("signal", "Latest signals"),
    ("btc", "BTC market context"), ("ai", "AI provider health"), ("brain", "Learning brain weights"),
    ("pnl", "Trading PnL summary"), ("positions", "Open positions"),
    ("metrics", "Win rate, profit factor, Sharpe, drawdown"), ("guards", "Safety guards and market context"),
    ("pause", "Pause new entries"), ("resume", "Resume entries"), ("kill", "Emergency kill switch"),
    ("help", "Command list"),
]


def enabled() -> bool:
    return bool(CFG.telegram_bot_token and CFG.telegram_chat_id)


def _admins() -> set[str]:
    ids = set(CFG.telegram_admin_chat_ids)
    if CFG.telegram_chat_id:
        ids.add(str(CFG.telegram_chat_id))
    return ids


def is_admin(chat_id: str | int) -> bool:
    return str(chat_id) in _admins()


def _api(method: str, **kwargs):
    if not enabled():
        return None
    return requests.post(BASE.format(CFG.telegram_bot_token) + f"/{method}", timeout=10, **kwargs)


def send(text: str, parse_mode: str | None = None) -> None:
    if not enabled():
        return
    if len(text) > MAX_MESSAGE:
        text = text[: MAX_MESSAGE - 1] + "…"
    try:
        payload = {"chat_id": CFG.telegram_chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = _api("sendMessage", json=payload)
        if r is not None:
            r.raise_for_status()
    except Exception:
        pass    # alerts must never take the trading loop down


def setup_commands() -> None:
    """Show the command menu in Telegram."""
    try:
        _api("setMyCommands", json={"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
    except Exception:
        pass


class CommandPoller:
    def __init__(self) -> None:
        self.offset: int | None = None
        self.initialized = False

    def _get_updates(self) -> list:
        params = {"timeout": 0, "allowed_updates": ["message"]}
        if self.offset is not None:
            params["offset"] = self.offset
        r = requests.get(BASE.format(CFG.telegram_bot_token) + "/getUpdates", params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])

    def drain(self) -> int:
        """
        Discard commands queued while the bot was offline. Without this a stale "/kill" or "/resume" sent
        yesterday would execute the moment the bot restarts. Returns how many were dropped.
        """
        if not enabled():
            return 0
        dropped = 0
        try:
            while True:
                updates = self._get_updates()
                if not updates:
                    break
                for u in updates:
                    self.offset = u.get("update_id", 0) + 1
                dropped += len(updates)
                if len(updates) < 100:
                    break
        except Exception:
            pass
        self.initialized = True
        return dropped

    def poll(self) -> list[str]:
        if not enabled():
            return []
        try:
            updates = self._get_updates()
        except Exception:
            return []
        commands = []
        for u in updates:
            self.offset = u.get("update_id", 0) + 1
            msg = u.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if text:
                self.initialized = True
            if not is_admin(chat_id) or not text.startswith("/"):
                continue
            head, *rest = text.split()
            commands.append(head.lower() + (" " + " ".join(rest) if rest else ""))
        return commands
