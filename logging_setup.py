"""
logging_setup.py — one place that configures logging for the whole bot.

Writes to both the console (so Termux still shows live status) and a size-capped rotating log file.

Security: every line that reaches a handler - including formatted tracebacks - passes through
:func:`redact`, which masks (1) the exact values of every configured credential and (2) common
credential shapes (Telegram bot tokens inside URLs, Bearer tokens, ``api_key=...``, ``sk-...``, ...).
``requests`` exceptions embed the full URL, and Telegram puts the bot token in the URL path, so
without this a single logged ``HTTPError`` would leak the token into zora.log.

Call configure_logging() once, at the very start of bot.py's main().
"""
from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from types import TracebackType

LOG_FILE = os.getenv("LOG_FILE", "zora.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_BYTES = 2 * 1024 * 1024   # 2 MB per file
BACKUP_COUNT = 3              # keep 3 rotated files -> ~8 MB ceiling total

_REDACTED = "<redacted>"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot" + _REDACTED),                    # Telegram: /bot<id>:<token>/
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1" + _REDACTED),              # Authorization: Bearer ...
    (re.compile(r"(?i)((?:api[_-]?key|apikey|secret|token|passphrase|password|signature|x-goog-api-key|x-api-key)"
                r"[\"']?\s*[:=]\s*[\"']?)[^\s&\"',}]{6,}"), r"\1" + _REDACTED),             # key=..., "secret": "..."
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "sk-" + _REDACTED),                       # OpenAI / Anthropic style
    (re.compile(r"\bgsk_[A-Za-z0-9]{16,}"), "gsk_" + _REDACTED),                        # Groq
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}"), "hf_" + _REDACTED),                          # Hugging Face
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "AIza" + _REDACTED),                     # Google
)

_configured = False


def redact(text: str) -> str:
    """Mask credentials in `text`. Never raises: logging must not be able to take the bot down."""
    try:
        from config import CFG
        for value in CFG.secret_values():
            text = text.replace(value, _REDACTED)
    except Exception:  # noqa: BLE001 - redaction is best-effort on the exact-match side
        pass
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Scrubs the *final* string (message + formatted traceback), so nothing can slip past via exc_info."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _log_unhandled(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger("zora").critical("unhandled exception", exc_info=(exc_type, exc, tb))


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    fmt = RedactingFormatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        try:
            os.chmod(LOG_FILE, 0o600)       # trade history is private
        except OSError:
            pass
    except OSError:
        root.warning("Could not open log file %s; continuing with console logging only.", LOG_FILE)

    # urllib3/requests DEBUG lines contain full request URLs; keep them out even if LOG_LEVEL=DEBUG.
    for noisy in ("urllib3", "requests", "ccxt"):
        logging.getLogger(noisy).setLevel(max(root.level, logging.INFO))

    logging.captureWarnings(True)
    sys.excepthook = _log_unhandled
    _configured = True
