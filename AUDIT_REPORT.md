# ZORA 5-AI Engine — Audit & Upgrade Report

## What changed
| Area | Change |
|---|---|
| `db.py` | Single serialized writer (RLock) + read-only dashboard connection, `BEGIN IMMEDIATE` with busy-retry, `synchronous=FULL`, versioned migrations (v3), write-ahead **order-intent journal**, atomic `record_open/record_close/record_partial_close`, `bot_state` KV for risk baselines. |
| `execution.py` | Idempotent retries via `clientOrderId` lookup before resend, backoff+jitter, `FillResult` (partial fills, base-currency fees, slippage bps), price-band pre-check, optional `limit_ioc`, `close_protected` (cancel legs → verify → close → re-arm), typed errors. |
| `risk.py` | NaN/inf guards, ATR sizing at 1–2% (confidence-scaled), cost-aware sizing, AI stop only accepted inside a safe band, **4% daily circuit breaker (24h lock)** vs kill switch (6%/15%), tiered exits (TP1 50% @1.5R → breakeven, TP2 25% @3R, 25% ATR-trailed runner). Risk always overrides the AI. |
| `multi_ai.py` | Council of Groq / OpenRouter / Mistral / HuggingFace in parallel under one deadline; Gemini supreme judge with strict JSON schema; fail-closed parsing (HOLD on anything malformed; hallucinated prices discarded); quorum; 429/5xx circuit-breaker cooldowns; keys in headers not URLs. |
| `strategy.py` | Market Dossier packager (15m+1h, EMA20/50/200, RSI, MACD, ATR, ADX, S/R zones, 4 regimes), closed-candle only, headline sanitization (prompt-injection), `BreakoutDetector` (vol ≥2.5× 20-avg or 24h high/low break, per-symbol cooldown). |
| `bot.py` | Exits-first loop, hybrid trigger (15m close + 10s ticker), persist-before-protect, pending-write retry, `/unblock /regime /council /risk`, graceful shutdown. |
| `config.py` / `logging_setup.py` | NaN-safe env parsing, secrets `repr=False`, validation, `.env` permission warnings; log redaction of tokens/keys, log file chmod 600. |

## Honest caveats
- An LLM council is **not a guarantee of returns**. Run paper mode for weeks (`PAPER_VALIDATION_DAYS`) before any live capital.
- Defaults in code stay conservative (`technical`, tiers off); `.env.example` opts in to `ai_council`, hybrid trigger and tiered exits.
- AI outage ⇒ **fail closed** (no entry) unless `AI_FALLBACK_TO_TECHNICAL=true`.
- Backtester uses technical signals only (LLM decisions can't be replayed point-in-time); tiers are not simulated there.
- Model names (Mistral, HF, Groq, Gemini) are defaults — verify they exist on your accounts.
- Tests ran against a local ccxt/pytest shim (PyPI was unreachable): exchange paths need a sandbox smoke test (`exchange_smoke.py`) before live.
- Termux: keep `DB_PATH` on internal app storage (not shared storage) so SQLite WAL locking works.
- `position_manager.py` from the brief does not exist in the zip; its job lives in `bot.py` + `risk.py`.

## Final verification pass
- 373 tests pass (risk, tiers, breaker, judge parsing, breakout, redaction, DB intents, council→entry→TP1→breakeven end-to-end, config).
- SQLite stress: 6 writer threads × 150 open/close cycles = 1800/1800 rows, integrity ok, concurrent readers unaffected.
- Council: all providers hung → fails closed to HOLD at the deadline; provider exceptions containing API keys never reach logs.
- Found and fixed: `STRATEGY_MODE=ai_council` silently never traded without `15m` and `1h` in `TIMEFRAMES`; now a startup validation error.
- Known limitation: with a degenerate near-zero ATR the stop floors at MIN_STOP_BPS (set 30 in `.env.example`); position size is also capped by notional, so risk stays within budget.
