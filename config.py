"""
config.py — loads settings from .env
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


ENV_PROBLEMS: list[str] = []   # unparseable / non-finite env values seen while loading CFG; surfaced by Config.validate()
_RECORDING = True              # switched off once the CFG singleton exists, so later ad-hoc _f() calls can't pollute it


def _env_problem(name: str, raw: str, default: object) -> None:
    if _RECORDING:
        ENV_PROBLEMS.append(f"{name}={raw!r} is not a valid number; using the default {default}")


def _f(name: str, default: float) -> float:
    """Float env var. 'nan'/'inf' parse as floats in Python and would silently disable every limit comparison
    (nan < x is always False), so they are rejected like any other garbage."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _env_problem(name, raw, default)
        return float(default)
    if not math.isfinite(value):
        _env_problem(name, raw, default)
        return float(default)
    return value


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        _env_problem(name, raw, default)
        return int(default)


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _secret(name: str) -> str:
    """Credential from the environment, stripped of the whitespace/quotes that .env copy-pastes tend to add."""
    return os.getenv(name, "").strip().strip("\"'").strip()


def _s(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


@dataclass
class Config:
    exchange_id: str = os.getenv("EXCHANGE_ID", "htx")
    supported_exchanges: tuple[str, ...] = ("binance", "bybit", "okx", "htx")
    symbol: str = os.getenv("SYMBOL", "ETH/USDT")          # used by backtest/learn by default
    symbols: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.getenv("SYMBOLS", os.getenv("SYMBOL", "ETH/USDT")).split(",") if s.strip()
    ))                                                       # traded together by `run` / `live`
    timeframes: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.getenv("TIMEFRAMES", "15m,1h,4h").split(",") if s.strip()
    ))
    # ETH/USDT is the only traded market. BTC/USDT is context-only: its trend
    # is supplied to the AI panel so ETH decisions can respect BTC regime moves.
    btc_context_symbol: str = os.getenv("BTC_CONTEXT_SYMBOL", "BTC/USDT")

    # capital / risk
    starting_capital: float = _f("STARTING_CAPITAL", 1000)
    risk_per_trade_pct: float = _f("RISK_PER_TRADE_PCT", 0.5)     # % of equity risked per trade
    max_exposure_pct: float = _f("MAX_EXPOSURE_PCT", 30)          # % of equity allowed in open positions
    max_daily_loss_pct: float = _f("MAX_DAILY_LOSS_PCT", 6)       # kill switch (backstop above the 4% circuit breaker)
    max_drawdown_pct: float = _f("MAX_DRAWDOWN_PCT", 15)          # kill switch from peak equity
    atr_stop_mult: float = _f("ATR_STOP_MULT", 2.0)
    atr_take_mult: float = _f("ATR_TAKE_MULT", 3.5)
    max_position_notional_pct: float = _f("MAX_POSITION_NOTIONAL_PCT", 30)  # one position may never exceed this % of equity (no hidden leverage)
    allow_shorts: bool = _b("ALLOW_SHORTS", False)                # live SELL entries (shorts) are refused unless explicitly enabled; paper always simulates them

    # position management (paper / software-protected positions only; native exchange orders are never modified)
    trailing_stop_enabled: bool = _b("TRAILING_STOP_ENABLED", True)
    breakeven_at_r: float = _f("BREAKEVEN_AT_R", 1.0)             # move stop to entry once profit reaches this many R
    trail_atr_mult: float = _f("TRAIL_ATR_MULT", 2.0)             # after breakeven, trail the stop this many ATR behind price

    # market / data safety guards
    stale_data_max_candles: float = _f("STALE_DATA_MAX_CANDLES", 2.5)  # refuse to trade on data older than this many candle periods
    max_spread_bps: float = _f("MAX_SPREAD_BPS", 15.0)            # live entries refused when bid/ask spread is wider
    min_quote_volume: float = _f("MIN_QUOTE_VOLUME", 1_000_000)   # 24h quote volume floor for live entries
    degradation_window: int = _i("DEGRADATION_WINDOW", 20)        # closed trades considered by the degradation guard
    degradation_min_profit_factor: float = _f("DEGRADATION_MIN_PF", 0.7)  # pause entries if rolling profit factor falls below this
    correlation_window: int = _i("CORRELATION_WINDOW", 50)        # candles of BTC/ETH returns used for the correlation read
    correlation_break_threshold: float = _f("CORRELATION_BREAK", 0.3)  # below this the BTC/ETH link is "broken": entries are sized down
    correlation_size_mult: float = _f("CORRELATION_SIZE_MULT", 0.5)
    fear_greed_enabled: bool = _b("FEAR_GREED_ENABLED", True)

    # dashboard (read-only, local by default)
    dashboard_port: int = _i("DASHBOARD_PORT", 0)                 # 0 = off
    dashboard_host: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")

    # execution realism / safety
    fee_rate: float = _f("FEE_RATE", 0.001)  # 0.10% per side default; override with your HTX tier
    slippage_bps: float = _f("SLIPPAGE_BPS", 5.0)
    execution_latency_ms: int = _i("EXECUTION_LATENCY_MS", 250)
    latency_bps: float = _f("LATENCY_BPS", 1.0)
    native_protection_required: bool = _b("NATIVE_PROTECTION_REQUIRED", True)
    reconcile_on_start: bool = _b("RECONCILE_ON_START", True)
    paper_validation_days: int = _i("PAPER_VALIDATION_DAYS", 30)

    # live execution hardening (see execution.py)
    exec_max_attempts: int = _i("EXEC_MAX_ATTEMPTS", 3)                 # total tries per order on network errors / timeouts
    exec_backoff_seconds: float = _f("EXEC_BACKOFF_SECONDS", 1.0)       # first retry delay; doubles each retry (+jitter)
    exec_order_style: str = _s("EXEC_ORDER_STYLE", "market")            # market | limit_ioc (entries become price-capped)
    max_slippage_bps: float = _f("MAX_SLIPPAGE_BPS", 30.0)              # entry price band / slippage alarm; 0 disables
    exec_fill_poll_attempts: int = _i("EXEC_FILL_POLL_ATTEMPTS", 3)     # re-check an order that came back not yet filled
    exec_fill_poll_seconds: float = _f("EXEC_FILL_POLL_SECONDS", 1.0)
    native_take_profit_required: bool = _b("NATIVE_TAKE_PROFIT_REQUIRED", True)   # false: native stop only, software take-profit

    # risk sizing
    risk_include_costs: bool = _b("RISK_INCLUDE_COSTS", True)           # size so a stop-out loses ~risk% INCLUDING fees + slippage
    min_stop_bps: float = _f("MIN_STOP_BPS", 10.0)                      # never place a stop closer than this to the entry

    # hedge-fund style capital preservation (see risk.py)
    risk_min_pct: float = _f("RISK_MIN_PCT", 1.0)                       # per-trade risk at the judge's minimum confidence
    risk_max_pct: float = _f("RISK_MAX_PCT", 2.0)                       # per-trade risk at 100% judge confidence
    circuit_breaker_pct: float = _f("CIRCUIT_BREAKER_DAILY_PCT", 4.0)   # daily drawdown that locks NEW entries; 0 disables
    circuit_breaker_hours: float = _f("CIRCUIT_BREAKER_HOURS", 24.0)
    tiered_exits_enabled: bool = _b("TIERED_EXITS_ENABLED", False)      # TP1/TP2/runner scale-out (live/paper loop)
    tp1_r: float = _f("TP1_R", 1.5)
    tp2_r: float = _f("TP2_R", 3.0)
    tp1_fraction: float = _f("TP1_FRACTION", 0.50)                      # share of the ORIGINAL size closed at TP1
    tp2_fraction: float = _f("TP2_FRACTION", 0.25)                      # the remainder is the ATR-trailed runner

    # strategy mode + hybrid triggering (see strategy.py / bot.py)
    strategy_mode: str = _s("STRATEGY_MODE", "technical")               # technical | ai_council
    ai_fallback_to_technical: bool = _b("AI_FALLBACK_TO_TECHNICAL", False)   # council unavailable: false = HOLD (fail closed)
    hybrid_trigger_enabled: bool = _b("HYBRID_TRIGGER_ENABLED", False)  # 10s intra-candle breakout polling
    fast_poll_seconds: int = _i("FAST_POLL_SECONDS", 10)
    breakout_volume_mult: float = _f("BREAKOUT_VOLUME_MULT", 2.5)       # forming-candle volume vs 20-candle average
    breakout_level_buffer_bps: float = _f("BREAKOUT_LEVEL_BUFFER_BPS", 5.0)   # how far beyond the 24h high/low counts as a break
    fast_track_cooldown_seconds: int = _i("FAST_TRACK_COOLDOWN_SECONDS", 300)  # min gap between breakout-triggered AI runs per symbol

    # signal generation
    signal_on_closed_candles: bool = _b("SIGNAL_ON_CLOSED_CANDLES", True)   # live parity with the backtester (no forming candle)
    signal_threshold: float = _f("SIGNAL_THRESHOLD", 25.0)              # |weighted score| needed for BUY/SELL
    ai_deadline_seconds: float = _f("AI_DEADLINE_SECONDS", 60.0)        # hard wall-clock cap on one AI consultation
    live_confirmation: str = _s("LIVE_CONFIRMATION", "")                # non-interactive go-live: set to "I ACCEPT THE RISK"

    # extra experts (optional; each is just one more vote the brain learns to weigh — never a trade trigger)
    ml_enabled: bool = _b("ML_ENABLED", True)
    ml_min_holdout_accuracy: float = _f("ML_MIN_HOLDOUT_ACCURACY", 0.53)  # below this the model has no proven edge -> score 0
    tv_webhook_secret: str = field(default_factory=lambda: _secret("TV_WEBHOOK_SECRET"), repr=False)           # blank = TradingView webhook disabled
    tv_signal_ttl_minutes: int = _i("TV_SIGNAL_TTL_MINUTES", 60)
    tv_signal_path: str = os.getenv("TV_SIGNAL_PATH", "tv_signal.json")

    # DCA / grid modes (historical paper simulation). Fixed order sizes + hard caps — never martingale.
    dca_base_order_pct: float = _f("DCA_BASE_ORDER_PCT", 5.0)       # % of equity per buy
    dca_interval_hours: float = _f("DCA_INTERVAL_HOURS", 24.0)
    dca_dip_pct: float = _f("DCA_DIP_PCT", 3.0)                    # extra safety buy per this % below avg entry
    dca_max_safety_orders: int = _i("DCA_MAX_SAFETY_ORDERS", 3)
    dca_take_profit_pct: float = _f("DCA_TAKE_PROFIT_PCT", 4.0)
    dca_stop_loss_pct: float = _f("DCA_STOP_LOSS_PCT", 15.0)       # a DCA cycle is closed at this loss from average entry
    grid_levels: int = _i("GRID_LEVELS", 10)
    grid_range_pct: float = _f("GRID_RANGE_PCT", 8.0)               # +/- around the start price
    grid_capital_pct: float = _f("GRID_CAPITAL_PCT", 20.0)          # % of equity the grid may use
    grid_stop_pct: float = _f("GRID_STOP_PCT", 3.0)                 # price this far below the grid floor liquidates and stops it

    # loop
    poll_seconds: int = _i("POLL_SECONDS", 60)

    # mode — LIVE_TRADING must be explicitly and deliberately enabled.
    live_trading: bool = _b("LIVE_TRADING", False)
    i_understand_the_risk: bool = _b("I_UNDERSTAND_THE_RISK", False)

    # exchange API (only needed for LIVE_TRADING)
    api_key: str = field(default_factory=lambda: _secret("EXCHANGE_API_KEY"), repr=False)
    api_secret: str = field(default_factory=lambda: _secret("EXCHANGE_API_SECRET"), repr=False)
    api_password: str = field(default_factory=lambda: _secret("EXCHANGE_API_PASSWORD"), repr=False)

    # ---- Multi-AI expert panel (all optional, advisory-only — see multi_ai.py) ----
    # Leave any key blank to skip that provider entirely; nothing breaks.
    groq_api_key: str = field(default_factory=lambda: _secret("GROQ_API_KEY"), repr=False)
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    groq_fallback_models: tuple[str, ...] = field(default_factory=lambda: tuple(
        m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b").split(",") if m.strip()
    ))
    groq_auto_fallback: bool = _b("GROQ_AUTO_FALLBACK", True)

    openrouter_api_key: str = field(default_factory=lambda: _secret("OPENROUTER_API_KEY"), repr=False)
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "openrouter/free")

    gemini_api_key: str = field(default_factory=lambda: _secret("GEMINI_API_KEY"), repr=False)
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

    # 5-AI council members beyond Groq/OpenRouter (plain HTTPS via requests; keys optional)
    mistral_api_key: str = field(default_factory=lambda: _secret("MISTRAL_API_KEY"), repr=False)
    mistral_model: str = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
    huggingface_api_key: str = field(default_factory=lambda: _secret("HUGGINGFACE_API_KEY"), repr=False)
    huggingface_model: str = os.getenv("HUGGINGFACE_MODEL", "Qwen/Qwen2.5-72B-Instruct")

    # optional paid extras — off unless keyed
    anthropic_api_key: str = field(default_factory=lambda: _secret("ANTHROPIC_API_KEY"), repr=False)
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    openai_api_key: str = field(default_factory=lambda: _secret("OPENAI_API_KEY"), repr=False)
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # judge: which provider synthesizes the individual AI verdicts into one call.
    # Blank = auto-pick whichever configured provider answers first each cycle.
    ai_judge_provider: str = os.getenv("AI_JUDGE_PROVIDER", "")
    # agentic tool use: lets Anthropic/Groq/OpenRouter/OpenAI call get_price_series
    # themselves before answering. Off saves tokens/latency at the cost of less context.
    ai_tool_use_enabled: bool = _b("AI_TOOL_USE_ENABLED", True)
    ai_max_concurrent_providers: int = _i("AI_MAX_CONCURRENT_PROVIDERS", 3)
    ai_judge_enabled: bool = _b("AI_JUDGE_ENABLED", True)
    ai_cycle_min_seconds: int = _i("AI_CYCLE_MIN_SECONDS", 45)
    ai_min_confidence: int = _i("AI_MIN_CONFIDENCE", 35)
    council_min_members: int = _i("COUNCIL_MIN_MEMBERS", 2)           # council answers required before the judge is consulted
    ai_judge_min_confidence: int = _i("AI_JUDGE_MIN_CONFIDENCE", 75)   # judge confidence needed to authorize an entry
    # AI safety: when at least one provider is configured, exhausted/rate-limited
    # AI capacity blocks NEW entries. Existing positions remain managed by risk/SL/TP.
    ai_entry_block_on_unavailable: bool = _b("AI_ENTRY_BLOCK_ON_UNAVAILABLE", True)
    ai_provider_cooldown_seconds: int = _i("AI_PROVIDER_COOLDOWN_SECONDS", 60)
    ai_daily_call_limit: int = _i("AI_DAILY_CALL_LIMIT", 0)  # 0 = disabled
    ai_minute_call_limit: int = _i("AI_MINUTE_CALL_LIMIT", 0)  # 0 = provider/API limit not known

    db_path: str = os.getenv("DB_PATH", "zora.db")

    # FastAPI dashboard
    dashboard_reload_seconds: int = _i("DASHBOARD_RELOAD_SECONDS", 10)

    # ---- Telegram alerts + remote control (both optional) ----
    telegram_bot_token: str = field(default_factory=lambda: _secret("TELEGRAM_BOT_TOKEN"), repr=False)
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_admin_chat_ids: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "").split(",") if s.strip()
    ))
    telegram_heartbeat_minutes: int = _i("TELEGRAM_HEARTBEAT_MINUTES", 15)

    def live_allowed(self) -> bool:
        return self.live_trading and self.i_understand_the_risk and bool(self.api_key) and bool(self.api_secret)

    def validate(self) -> list[str]:
        """Return human-readable problems with the current settings (empty list = fine)."""
        problems: list[str] = list(ENV_PROBLEMS)
        if self.exchange_id not in self.supported_exchanges:
            problems.append(f"EXCHANGE_ID={self.exchange_id} unsupported; use {', '.join(self.supported_exchanges)}")
        if not 0 < self.risk_per_trade_pct <= 5:
            problems.append(f"RISK_PER_TRADE_PCT={self.risk_per_trade_pct} should be in (0, 5]")
        if not 0 < self.max_exposure_pct <= 100:
            problems.append(f"MAX_EXPOSURE_PCT={self.max_exposure_pct} should be in (0, 100]")
        if self.max_exposure_pct < self.risk_per_trade_pct:
            problems.append("MAX_EXPOSURE_PCT is smaller than RISK_PER_TRADE_PCT: no trade could ever be approved")
        if self.atr_stop_mult <= 0 or self.atr_take_mult <= 0:
            problems.append("ATR_STOP_MULT and ATR_TAKE_MULT must be positive")
        elif self.atr_take_mult < self.atr_stop_mult:
            problems.append("ATR_TAKE_MULT < ATR_STOP_MULT: reward/risk below 1, strategy needs a very high win rate")
        if self.starting_capital <= 0:
            problems.append("STARTING_CAPITAL must be positive")
        if self.poll_seconds < 5:
            problems.append("POLL_SECONDS below 5 will hit exchange rate limits")
        if self.fee_rate < 0 or self.slippage_bps < 0:
            problems.append("FEE_RATE and SLIPPAGE_BPS cannot be negative")
        if not self.timeframes:
            problems.append("TIMEFRAMES is empty")
        if self.live_trading and not self.live_allowed():
            problems.append("LIVE_TRADING is on but I_UNDERSTAND_THE_RISK / API keys are missing: live stays locked")
        if self.dashboard_port and self.dashboard_host not in ("127.0.0.1", "localhost"):
            problems.append("DASHBOARD_HOST is not local: the dashboard has no login, anyone on the network can read your trades")
        if self.tv_webhook_secret and len(self.tv_webhook_secret) < 16:
            problems.append("TV_WEBHOOK_SECRET is shorter than 16 characters: use a long random string")
        if self.tv_webhook_secret and not self.dashboard_port:
            problems.append("TV_WEBHOOK_SECRET is set but DASHBOARD_PORT is 0: the webhook is served by the dashboard server")
        if not 0 < self.dca_base_order_pct * (1 + self.dca_max_safety_orders) <= 100:
            problems.append("DCA_BASE_ORDER_PCT * (1 + DCA_MAX_SAFETY_ORDERS) must be in (0, 100]% of equity")
        if self.grid_levels < 2 or self.grid_range_pct <= 0:
            problems.append("GRID_LEVELS must be >= 2 and GRID_RANGE_PCT > 0")
        for name, value in (("MAX_DAILY_LOSS_PCT", self.max_daily_loss_pct), ("MAX_DRAWDOWN_PCT", self.max_drawdown_pct),
                            ("MAX_POSITION_NOTIONAL_PCT", self.max_position_notional_pct)):
            if not 0 < value <= 100:
                problems.append(f"{name}={value} should be in (0, 100]")
        if self.exec_max_attempts < 1 or self.exec_max_attempts > 6:
            problems.append("EXEC_MAX_ATTEMPTS should be between 1 and 6")
        if self.exec_backoff_seconds < 0 or self.max_slippage_bps < 0 or self.min_stop_bps < 0:
            problems.append("EXEC_BACKOFF_SECONDS, MAX_SLIPPAGE_BPS and MIN_STOP_BPS cannot be negative")
        if self.exec_order_style not in ("market", "limit_ioc"):
            problems.append(f"EXEC_ORDER_STYLE={self.exec_order_style} unsupported; use market or limit_ioc")
        if not 0 < self.risk_min_pct <= self.risk_max_pct <= 5:
            problems.append("RISK_MIN_PCT <= RISK_MAX_PCT <= 5 must hold (percent of equity risked per trade)")
        if self.circuit_breaker_pct < 0 or self.circuit_breaker_hours < 0:
            problems.append("CIRCUIT_BREAKER_DAILY_PCT and CIRCUIT_BREAKER_HOURS cannot be negative")
        if self.circuit_breaker_pct and self.max_daily_loss_pct <= self.circuit_breaker_pct:
            problems.append("MAX_DAILY_LOSS_PCT (kill switch) should be above CIRCUIT_BREAKER_DAILY_PCT, or the kill switch fires first")
        if self.tiered_exits_enabled and not (0 < self.tp1_r < self.tp2_r and 0 < self.tp1_fraction and 0 < self.tp2_fraction
                                              and self.tp1_fraction + self.tp2_fraction < 1):
            problems.append("tiered exits need 0 < TP1_R < TP2_R and TP1_FRACTION + TP2_FRACTION < 1 (a runner must remain)")
        if self.strategy_mode not in ("technical", "ai_council"):
            problems.append(f"STRATEGY_MODE={self.strategy_mode} unsupported; use technical or ai_council")
        if self.strategy_mode == "ai_council" and not (self.groq_api_key or self.openrouter_api_key or self.mistral_api_key
                                                       or self.huggingface_api_key):
            problems.append("STRATEGY_MODE=ai_council but no council provider key (GROQ/OPENROUTER/MISTRAL/HUGGINGFACE) is set")
        if self.strategy_mode == "ai_council" and not {"15m", "1h"} <= set(self.timeframes):
            problems.append("STRATEGY_MODE=ai_council needs TIMEFRAMES to include 15m and 1h (the dossier timeframes); "
                            "without them no entry can ever be evaluated")
        if self.strategy_mode == "ai_council" and self.ai_judge_provider != "gemini" and not self.ai_judge_provider:
            problems.append("STRATEGY_MODE=ai_council expects AI_JUDGE_PROVIDER=gemini (the supreme judge)")
        if self.strategy_mode == "ai_council" and self.ai_judge_provider == "gemini" and not self.gemini_api_key:
            problems.append("AI_JUDGE_PROVIDER=gemini but GEMINI_API_KEY is empty: no entries can be authorized")
        if self.hybrid_trigger_enabled and self.fast_poll_seconds < 5:
            problems.append("FAST_POLL_SECONDS below 5 will hit exchange rate limits")
        if not 0 < self.signal_threshold < 100:
            problems.append("SIGNAL_THRESHOLD should be in (0, 100)")
        if self.live_trading:
            for label, value in (("EXCHANGE_API_KEY", self.api_key), ("EXCHANGE_API_SECRET", self.api_secret)):
                if value and (len(value) < 8 or any(ch.isspace() for ch in value)):
                    problems.append(f"{label} looks malformed (too short or contains whitespace)")
            if not self.native_protection_required:
                problems.append("NATIVE_PROTECTION_REQUIRED=false: live positions may rely on software stops only (a crash = no stop)")
        return problems

    def secret_values(self) -> list[str]:
        """Every configured credential value (>= 6 chars), longest first, for log redaction."""
        names = tuple(n for n in self.__dataclass_fields__ if n.endswith(("_api_key", "_api_secret", "_api_password", "_token"))
                      or n in ("api_key", "api_secret", "api_password", "tv_webhook_secret"))
        values = {str(getattr(self, n, "") or "") for n in names}
        return sorted((v for v in values if len(v) >= 6), key=len, reverse=True)


def env_file_warnings(path: str = ".env") -> list[str]:
    """Group/world-readable .env files leak API keys to other local users (POSIX only). Used by `bot.py doctor`."""
    out: list[str] = []
    if os.name != "posix":
        return out
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return out
    if mode & 0o077:
        out.append(f"{path} is readable by other users (mode {mode:o}); run: chmod 600 {path}")
    return out


CFG = Config()
_RECORDING = False
