## 1.2.1

- Fixed optimizer report serialization for nested metrics and non-finite numeric values.
- Verified dependency-independent test suite and Python compilation.

## 1.2.0

- Added local historical OHLCV cache for reproducible replay/backtests.
- Portfolio backtests now include an equity curve in JSON output.
- Added self-contained HTML replay report with portfolio curve and symbol breakdown.
- Added `replay-report` CLI command.
- Kept live trading safety gates unchanged; replay/reporting is read-only.

## 1.1.0
- Added multi-symbol portfolio historical replay using the shared backtester and configured execution-cost model.
- Added read-only paper-performance gate for a configurable trailing window (default 30 days).
- Added Monte Carlo P95 drawdown and expectancy checks to the paper gate.
- Added `portfolio-backtest` and `paper-report` CLI commands.

# Changelog

## 1.0.0
- Added recovery factor and non-annualized Calmar performance metrics.
- Added deterministic Monte Carlo trade-order drawdown estimates (P50/P95/P99).
- Added chronological 3-fold walk-forward validation to the production validation harness.
- Validation now includes the native stop-loss/take-profit contract and walk-forward gate.
- Made the execution adapter import-safe for paper/replay/contract tests when `ccxt` is not installed; live execution still requires the dependency.
- Dashboard now surfaces recovery factor, Calmar, and maximum loss streak alongside existing KPIs.

## 0.9.3
- Hardened offline validation/replay imports: `validation.py` now loads the live `ccxt` execution adapter only when the native-protection contract is actually executed.
- Kept live trading, risk limits, exchange routing, and native protection behavior unchanged.

## 0.9.2 - audit and hardening release

### Fixed
- **ML expert look-ahead leak**: each feature row contained the very return it was asked to predict, so held-out
  accuracy came out near 100% on pure noise. Features now use only data before the predicted candle (regression test added).
- **`bot.py` was a 39-line scaffold**: `backtest`, `learn`, `brain`, `run`, `live`, `status`, `metrics`, `optimize`, `dca`,
  `grid`, `ai-health` and `ai-test` did not exist. The trading loop, replay commands and Telegram control are implemented.
- `bot.py validate` / `exchange-smoke` / `exchange-sandbox` crashed on their own flags (argv was never forwarded).
- **Risk engine exposure** counted position notional while the backtester, restore logic and tests counted risk budget;
  one definition is now used everywhere, and every decision reports the exposure it consumes.
- `execution.py` used nested same-quote f-strings (Python 3.12 only) although CI/pyproject target 3.10+.
- Native stop/take-profit are two independent orders: after one fills, the other is now cancelled instead of being left
  to fire later and open an unwanted opposite position.
- Spot reconciliation no longer calls `fetch_positions` (not valid on spot), which previously made restart checks fail.
- Live orders now record the exchange-rounded amount, not the unrounded request.
- Blank per-exchange keys (`HTX_API_KEY=`) overrode `EXCHANGE_API_KEY`, leaving live mode without credentials.
- `fear_greed.py` was a stub missing `fetch/get_index/score_from_index`; its tests failed.
- TradingView alert notes now include the action; the stdlib dashboard refuses oversized webhook bodies and rejects PUT/DELETE/PATCH.
- `pyproject.toml` was empty; `requirements-dev.txt`, `setup_termux.sh` and `CHANGELOG.md` were referenced but missing.
- FastAPI is now an optional import, so the rest of the bot installs on Termux without it.
- Docker: runs as a non-root user, the dashboard is published on localhost only, compose runs trader + dashboard.
- `.env.example` no longer points `DB_PATH` at `/data` and the dashboard at `0.0.0.0` for local runs.

### Added
- `bot.Trader`: paper/live loop with stale-data, market-quality, degradation and BTC-correlation guards, restart
  recovery, exchange reconciliation, native-protection checks, trailing stops, Telegram commands and heartbeat.
- `validate` now reports warnings (edge vanishing under high costs, too few trades).
- 55+ new tests (end-to-end paper loop, live path with a fake exchange, replay look-ahead, regressions).

## 0.9.1
Production validation harness, multi-exchange smoke tests, native protection contract.

## v1.2.1 — Reliability patch
- Fixed optimizer report serialization for nested Monte Carlo metrics and non-finite numeric values.
- Optimizer reports now remain strict-JSON-safe (`Infinity`/`NaN` become `null`) instead of crashing after successful trials.
- Verified dependency-independent suite: 218 tests passed. Exchange-dependent tests still require installing `requirements.txt` (notably `ccxt`).
