# ZORA v1.2.1 Fix Report

## Fixed in this pass
- `optimize.py`: report serialization previously assumed every metric was a scalar. `compute_metrics()` includes a nested `monte_carlo` object, which caused `TypeError: must be real number, not dict` at the end of optimization. Serialization is now recursive and converts non-finite numbers to JSON `null`.

## Verification
- Dependency-independent tests: **218 passed**.
- Full pytest collection still requires the declared `ccxt` dependency; this environment reports `ModuleNotFoundError: No module named 'ccxt'` for exchange-dependent modules.
- Do not consider exchange/live execution validated until dependencies are installed and sandbox smoke tests are run.
