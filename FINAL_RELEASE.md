# ZORA v1.2.1 — Fixed Release Guide

## Safety
- PAPER mode is the default.
- AI/strategy logic cannot override the risk engine.
- Live mode remains explicitly gated and requires the existing configuration, API keys and typed confirmation.
- Historical replay and reports are read-only.

## Main commands
```bash
python bot.py doctor
python bot.py run --once
python bot.py backtest BTC/USDT 1500
python bot.py portfolio-backtest BTC/USDT ETH/USDT SOL/USDT --candles 1500 --json > portfolio.json
python bot.py replay-report portfolio.json --output zora_replay_report.html
python bot.py paper-report --days 30
python bot.py validate
python bot.py dashboard
```

## Historical cache
The cache is stored in `history_cache/` when explicitly used by the cache module. Cached OHLCV is for reproducible analysis; it must not be treated as live market data.

## Validation path
BACKTEST → WALK-FORWARD → STRESS/COST TEST → MONTE CARLO → PAPER → 30-DAY PAPER GATE → TESTNET/SANDBOX → LIVE REVIEW.

A passing report is not a guarantee of future profitability.
