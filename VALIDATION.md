# ZORA production validation (v0.9.2)

This release adds a **validation layer**, not a profit guarantee.

## 1. Long-duration paper stress validation

```bash
python bot.py validate
python bot.py validate --days 90
```

The harness runs the same pure backtest engine with fees, slippage and latency,
using deterministic synthetic 15-minute stress data. It reports:

- number of candles/trades
- final equity and return
- max drawdown
- profit factor
- risk kill-switch state
- native-protection contract result
- `warnings`: edge vanishing under high costs, profit factor < 1 at 2x costs, or too few trades to mean anything

Synthetic data is deliberately labelled as such. For a real validation, feed a
long historical OHLCV dataset into `backtester.simulate` and compare in-sample,
walk-forward and untouched holdout periods.

## 2. Cost sensitivity

The harness compares baseline costs, a high-cost case and a 2x-cost case. This
is intended to expose strategies that only work when fees/slippage/latency are
unrealistically low.

## 3. Exchange smoke test

```bash
python bot.py exchange-smoke
python bot.py exchange-smoke --private-check
```

This loads public market metadata for Binance, Bybit, OKX and HTX through CCXT
and reports order/OHLCV/native-protection capabilities. It **never submits an
order**. `--private-check` only checks whether exchange-specific environment
variables exist; it does not authenticate or trade.

For a real sandbox/private integration test, use each exchange's official test
environment with dedicated test keys and a separate `.env`. Keep
`LIVE_TRADING=false` until the full test report is reviewed.

## 4. Native SL/TP verification

`validation.native_protection_contract()` uses a mock exchange and verifies that
both stop-loss and take-profit native orders are armed. The live executor still
refuses an entry when native protection is required but the exchange adapter
cannot confirm it.

## 5. Restart/reconciliation

The existing SQLite open-position store remains the source of local intent, but
live execution must reconcile exchange truth before new entries. A restart test
should verify:

1. persisted position exists;
2. exchange position/order state is fetched;
3. missing/unknown protection causes a safe stop rather than a new entry;
4. no duplicate close order is sent after a native stop/take fills, and the surviving sibling order is cancelled.

Items 1-4 are implemented in `Trader.startup`, `LiveExecutor.reconcile/protection_triggered` and covered by
`tests/test_trader.py` against a fake exchange; they still need a real sandbox run before you rely on them.

## What this does not prove

It does **not** prove future profitability, latency under a live network, exchange
sandbox parity, or absence of exchange-specific API behaviour. Those require a
real test environment and extended forward-paper testing.


## 6. Exchange sandbox smoke

```bash
python bot.py exchange-sandbox
python bot.py exchange-sandbox --private-check
```

This calls CCXT's sandbox switch where available and loads public markets only. It reports whether the sandbox method is available and explicitly reports `orders_submitted: 0`. HTX and other exchanges can have different testnet availability; the harness does not pretend otherwise.
