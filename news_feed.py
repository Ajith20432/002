"""
news_feed.py — pulls recent crypto headlines from public RSS feeds (no API
key, no rate-limit quota to run out of, format unlikely to break overnight —
good properties for something running unattended on a phone) and scores a
simple keyword-based sentiment.

This becomes a "news" expert, exactly like the technical timeframes and the
AI panel — the auto-learning brain decides over time how much to trust it.
The matched headlines are also handed to multi_ai.py so the LLM panel (if
configured) gets real context instead of guessing blind.

This is a blunt keyword heuristic, not real NLP sentiment analysis — it
will misread sarcasm, headlines that mention a bad thing NOT happening, etc.
Treat it as one noisy vote among several, which is exactly how the brain
ends up treating it once it has a few dozen trades to learn from.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

import requests

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]

POSITIVE_WORDS = {
    "surge", "rally", "soar", "gain", "gains", "bullish", "adopt", "adoption",
    "approve", "approval", "approves", "partnership", "upgrade", "record high",
    "all-time high", "inflow", "breakthrough", "integrate", "integration",
    "launch", "launches", "milestone",
}
NEGATIVE_WORDS = {
    "crash", "plunge", "hack", "hacked", "exploit", "lawsuit", "ban", "banned",
    "fraud", "bearish", "sell-off", "selloff", "collapse", "fine", "fined",
    "investigation", "outflow", "liquidation", "delist", "delisted", "scam",
    "halt", "halted", "warning",
}

SYMBOL_ALIASES = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum", "ether"],
    "SOL": ["sol", "solana"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["doge", "dogecoin"],
    "BNB": ["bnb", "binance coin"],
    "ADA": ["ada", "cardano"],
    "LTC": ["ltc", "litecoin"],
}

_cache = {"ts": 0, "headlines": []}
CACHE_SECONDS = 600  # headlines don't change fast enough to refetch every poll cycle


def _fetch_headlines() -> list[str]:
    headlines = []
    for url in FEEDS:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:15]:
                title = (item.findtext("title") or "").strip()
                if title:
                    headlines.append(title)
        except Exception:
            continue  # one feed failing shouldn't block the others
    return headlines


def get_headlines(force: bool = False) -> list[str]:
    now = time.time()
    if force or now - _cache["ts"] > CACHE_SECONDS or not _cache["headlines"]:
        fetched = _fetch_headlines()
        if fetched:
            _cache["headlines"] = fetched
            _cache["ts"] = now
    return _cache["headlines"]


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _word_hits(tokens: list[str], low: str, words: set[str]) -> int:
    """
    Count keyword hits using WHOLE words (plus a plural / past-tense suffix), never raw substrings:
    a substring test made "ban" fire on "bank", "fine" on "define", "gain" on "again" and
    "eth" on "together" — flipping sentiment and symbol matching on perfectly ordinary headlines.
    """
    hits = 0
    for tok in tokens:
        if tok in words or any(tok.endswith(suf) and tok[: -len(suf)] in words for suf in ("s", "es", "ed", "d")):
            hits += 1
    for phrase in (w for w in words if " " in w):
        if re.search(rf"\b{re.escape(phrase)}\b", low):
            hits += 1
    return hits


def _matches_symbol(headline: str, base_currency: str) -> bool:
    aliases = SYMBOL_ALIASES.get(base_currency.upper(), [base_currency.lower()])
    low = headline.lower()
    tokens = set(_tokens(low))
    for a in aliases:
        if " " in a:
            if re.search(rf"\b{re.escape(a)}\b", low):
                return True
        elif a in tokens or a + "s" in tokens:
            return True
    return False


def news_sentiment(symbol: str) -> tuple[float, list[str]]:
    """
    Returns (score, matched_headlines):
      score: -100..100 keyword-sentiment lean across headlines mentioning
             this symbol's base currency, or 0 if no relevant headlines.
      matched_headlines: the headlines that were actually scored — pass
             these to multi_ai.get_ai_consensus(headlines=...) for extra
             context, or just log them.
    """
    base = symbol.split("/")[0]
    headlines = get_headlines()
    matched = [h for h in headlines if _matches_symbol(h, base)]
    if not matched:
        return 0, []

    net = 0
    for h in matched:
        low = h.lower()
        toks = _tokens(low)
        net += _word_hits(toks, low, POSITIVE_WORDS)
        net -= _word_hits(toks, low, NEGATIVE_WORDS)

    score = max(-100, min(100, net * 25))  # each net keyword hit swings the score by 25
    return score, matched
