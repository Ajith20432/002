/* ===== ZORA Dashboard — Demo Data (matches actual bot data structures) ===== */

// Equity curve: 90 data points simulating 90 days of trading
const EQUITY_DATA = (() => {
  const data = [];
  let equity = 1000;
  const trades = [
    { day: 3, pnl: 12.5 }, { day: 7, pnl: -8.2 }, { day: 10, pnl: 18.3 },
    { day: 14, pnl: -5.1 }, { day: 18, pnl: 22.7 }, { day: 22, pnl: -15.4 },
    { day: 26, pnl: 8.9 }, { day: 30, pnl: 31.2 }, { day: 34, pnl: -12.6 },
    { day: 38, pnl: 19.8 }, { day: 42, pnl: -7.3 }, { day: 46, pnl: 25.1 },
    { day: 50, pnl: -18.9 }, { day: 54, pnl: 14.2 }, { day: 58, pnl: 28.5 },
    { day: 62, pnl: -10.7 }, { day: 66, pnl: 21.4 }, { day: 70, pnl: -6.8 },
    { day: 74, pnl: 33.1 }, { day: 78, pnl: -14.5 }, { day: 82, pnl: 19.7 },
    { day: 86, pnl: 24.3 }, { day: 88, pnl: -8.1 }, { day: 89, pnl: 9.4 },
  ];
  let tradeIdx = 0;
  for (let day = 0; day <= 90; day++) {
    // Small daily drift
    equity += (Math.random() - 0.48) * 3;
    // Apply trade PnL
    if (tradeIdx < trades.length && trades[tradeIdx].day === day) {
      equity += trades[tradeIdx].pnl;
      tradeIdx++;
    }
    equity = Math.max(850, equity);
    const date = new Date();
    date.setDate(date.getDate() - (90 - day));
    data.push({
      date: date.toISOString().slice(0, 10),
      equity: parseFloat(equity.toFixed(2)),
    });
  }
  return data;
})();

// Open positions (matching execution.py / db.py structure)
const OPEN_POSITIONS = [
  {
    symbol: "ETH/USDT",
    side: "BUY",
    entry_price: 2385.20,
    current_price: 2418.50,
    size: 0.0945,
    notional: 228.55,
    stop_loss: 2331.40,
    take_profit: 2512.00,
    unrealized_pnl: 3.14,
    pnl_pct: 1.40,
    opened: "2026-10-05 14:23",
    opened_hours: 17.2,
  },
  {
    symbol: "ETH/USDT",
    side: "BUY",
    entry_price: 2401.80,
    current_price: 2418.50,
    size: 0.0472,
    notional: 114.15,
    stop_loss: 2356.20,
    take_profit: 2530.00,
    unrealized_pnl: 0.79,
    pnl_pct: 0.69,
    opened: "2026-10-06 02:45",
    opened_hours: 4.8,
  },
];

// Recent signals (matching strategy.py Signal structure)
const RECENT_SIGNALS = [
  { time: "07:32:14", symbol: "ETH/USDT", action: "BUY", confidence: 72, reason: "4h EMA stack bullish, MACD positive, RSI 58 (room to run)" },
  { time: "07:30:02", symbol: "ETH/USDT", action: "HOLD", confidence: 45, reason: "15m timeframe showing weakness, waiting for confirmation" },
  { time: "07:15:33", symbol: "ETH/USDT", action: "BUY", confidence: 68, reason: "News: ETH ETF inflows hit record high, sentiment bullish" },
  { time: "06:58:21", symbol: "ETH/USDT", action: "SELL", confidence: 52, reason: "4h RSI overbought (72), possible pullback" },
  { time: "06:45:09", symbol: "ETH/USDT", action: "HOLD", confidence: 38, reason: "BTC/ETH correlation broken (0.28), entries sized down" },
  { time: "06:30:55", symbol: "ETH/USDT", action: "BUY", confidence: 65, reason: "Bullish engulfing on 1h, volume spike 2.1x, pattern confirmed" },
  { time: "06:15:02", symbol: "ETH/USDT", action: "BUY", confidence: 71, reason: "AI panel: Groq + OpenRouter bullish, judge synthesized BUY" },
  { time: "06:00:18", symbol: "ETH/USDT", action: "HOLD", confidence: 41, reason: "Stale data guard: 15m candle delayed 3.2 periods" },
];

// Closed trades (matching backtester.py SimResult structure)
const CLOSED_TRADES = [
  { date: "2026-10-05", symbol: "ETH/USDT", side: "BUY", entry: 2362.10, exit: 2389.40, size: 0.0945, pnl: 2.58, r_multiple: 1.12, hold_time: "6h 23m", exit_reason: "Take Profit" },
  { date: "2026-10-04", symbol: "ETH/USDT", side: "BUY", entry: 2398.50, exit: 2371.20, size: 0.0472, pnl: -1.29, r_multiple: -0.56, hold_time: "4h 15m", exit_reason: "Stop Loss" },
  { date: "2026-10-03", symbol: "ETH/USDT", side: "BUY", entry: 2341.80, exit: 2389.40, size: 0.0945, pnl: 4.50, r_multiple: 1.95, hold_time: "12h 40m", exit_reason: "Take Profit" },
  { date: "2026-10-02", symbol: "ETH/USDT", side: "BUY", entry: 2375.30, exit: 2401.80, size: 0.0472, pnl: 1.25, r_multiple: 0.54, hold_time: "8h 20m", exit_reason: "Trailing Stop" },
  { date: "2026-10-01", symbol: "ETH/USDT", side: "BUY", entry: 2401.20, exit: 2375.30, size: 0.0472, pnl: -1.22, r_multiple: -0.53, hold_time: "5h 30m", exit_reason: "Stop Loss" },
  { date: "2026-09-30", symbol: "ETH/USDT", side: "BUY", entry: 2338.50, exit: 2412.30, size: 0.0945, pnl: 6.97, r_multiple: 3.02, hold_time: "18h 15m", exit_reason: "Take Profit" },
  { date: "2026-09-29", symbol: "ETH/USDT", side: "SELL", entry: 2415.80, exit: 2389.20, size: 0.0472, pnl: 1.26, r_multiple: 0.55, hold_time: "7h 45m", exit_reason: "Take Profit" },
  { date: "2026-09-28", symbol: "ETH/USDT", side: "BUY", entry: 2398.40, exit: 2371.10, size: 0.0472, pnl: -1.29, r_multiple: -0.56, hold_time: "3h 50m", exit_reason: "Stop Loss" },
  { date: "2026-09-27", symbol: "ETH/USDT", side: "BUY", entry: 2362.50, exit: 2415.80, size: 0.0945, pnl: 5.04, r_multiple: 2.19, hold_time: "15h 20m", exit_reason: "Take Profit" },
  { date: "2026-09-26", symbol: "ETH/USDT", side: "BUY", entry: 2389.10, exit: 2401.20, size: 0.0472, pnl: 0.57, r_multiple: 0.25, hold_time: "6h 10m", exit_reason: "Trailing Stop" },
  { date: "2026-09-25", symbol: "ETH/USDT", side: "BUY", entry: 2356.40, exit: 2338.50, size: 0.0472, pnl: -0.85, r_multiple: -0.37, hold_time: "4h 05m", exit_reason: "Stop Loss" },
  { date: "2026-09-24", symbol: "ETH/USDT", side: "BUY", entry: 2345.20, exit: 2389.10, size: 0.0945, pnl: 4.15, r_multiple: 1.80, hold_time: "14h 30m", exit_reason: "Take Profit" },
];

// Brain weights (matching brain.py DEFAULT_EXPERT_WEIGHTS / DEFAULT_FACTOR_WEIGHTS)
const EXPERT_WEIGHTS = {
  "15m": 0.12,
  "1h": 0.28,
  "4h": 0.32,
  "AI": 0.14,
  "News": 0.08,
  "FnG": 0.06,
};

const FACTOR_WEIGHTS = {
  "Trend (EMA)": 32,
  "Momentum (MACD)": 14,
  "RSI": 22,
  "Mean Rev (BB)": 8,
  "Volume": 9,
  "Pattern": 15,
};

const BRAIN_STATE = {
  "expert_weights": {
    "15m": 0.12,
    "1h": 0.28,
    "4h": 0.32,
    "ai": 0.14,
    "news": 0.08,
    "fng": 0.06,
    "ml": 0.10,
    "tv": 0.10
  },
  "factor_weights": {
    "trend": 32,
    "momentum": 14,
    "rsi": 22,
    "meanrev": 8,
    "volume": 9,
    "pattern": 15
  },
  "trades_learned_from": 24
};

// AI Panel providers (matching multi_ai.py structure)
const AI_PROVIDERS = [
  {
    name: "Groq",
    model: "openai/gpt-oss-20b",
    status: "active",
    lean: "BUY",
    confidence: 72,
    note: "4h trend strongly bullish, RSI has room to run, volume confirms",
    latency: "1.2s",
  },
  {
    name: "OpenRouter",
    model: "openrouter/free",
    status: "active",
    lean: "BUY",
    confidence: 65,
    note: "EMA stack positive on 1h and 4h, MACD histogram rising",
    latency: "2.8s",
  },
  {
    name: "Gemini",
    model: "gemini-flash-latest",
    status: "cooldown",
    lean: "HOLD",
    confidence: 48,
    note: "Rate limited (429), cooldown: 42s remaining",
    latency: "—",
  },
  {
    name: "Anthropic",
    model: "claude-sonnet-4-6",
    status: "offline",
    lean: "—",
    confidence: 0,
    note: "No API key configured",
    latency: "—",
  },
  {
    name: "OpenAI",
    model: "gpt-4o-mini",
    status: "offline",
    lean: "—",
    confidence: 0,
    note: "No API key configured",
    latency: "—",
  },
];

const JUDGE_INFO = {
  provider: "Groq (auto-selected)",
  enabled: true,
  synthesis: "BUY (confidence: 70)",
  method: "LLM-as-judge: weighed Groq's bullish lean (72%) higher than OpenRouter's (65%) due to stronger confidence and lower latency. Gemini was excluded from synthesis due to cooldown.",
  fallback: "If judge unavailable: confidence-weighted vote averaging → BUY (69%)",
};

// AI verdicts table
const AI_VERDICTS = [
  { time: "07:32:14", symbol: "ETH/USDT", provider: "Groq", lean: "BUY", confidence: 72, note: "4h trend strongly bullish", latency: "1.2s" },
  { time: "07:32:16", symbol: "ETH/USDT", provider: "OpenRouter", lean: "BUY", confidence: 65, note: "EMA stack positive, MACD rising", latency: "2.8s" },
  { time: "07:32:19", symbol: "ETH/USDT", provider: "Gemini", lean: "HOLD", confidence: 48, note: "Rate limited (429)", latency: "—" },
  { time: "07:32:22", symbol: "ETH/USDT", provider: "Judge", lean: "BUY", confidence: 70, note: "Synthesized: 2 providers bullish", latency: "1.5s" },
  { time: "07:15:33", symbol: "ETH/USDT", provider: "Groq", lean: "BUY", confidence: 68, note: "News sentiment: ETF inflows record", latency: "1.1s" },
  { time: "07:15:35", symbol: "ETH/USDT", provider: "OpenRouter", lean: "BUY", confidence: 61, note: "Bullish engulfing on 1h confirmed", latency: "2.5s" },
  { time: "07:15:38", symbol: "ETH/USDT", provider: "Judge", lean: "BUY", confidence: 65, note: "Synthesized: 2 providers bullish", latency: "1.3s" },
  { time: "06:58:21", symbol: "ETH/USDT", provider: "Groq", lean: "SELL", confidence: 52, note: "RSI overbought (72), possible pullback", latency: "1.0s" },
  { time: "06:58:23", symbol: "ETH/USDT", provider: "OpenRouter", lean: "HOLD", confidence: 45, note: "Mixed signals, waiting", latency: "2.9s" },
  { time: "06:58:26", symbol: "ETH/USDT", provider: "Judge", lean: "SELL", confidence: 50, note: "Groq bearish, OpenRouter neutral", latency: "1.4s" },
];

// Export for use in app.js
if (typeof window !== 'undefined') {
  window.ZORA_DATA = {
    EQUITY_DATA,
    OPEN_POSITIONS,
    RECENT_SIGNALS,
    CLOSED_TRADES,
    EXPERT_WEIGHTS,
    FACTOR_WEIGHTS,
    BRAIN_STATE,
    AI_PROVIDERS,
    JUDGE_INFO,
    AI_VERDICTS,
  };
}
