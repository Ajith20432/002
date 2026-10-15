# ZORA deployment

## VPS / Docker
1. Copy `.env.example` to `.env`. Keep `LIVE_TRADING=false` until paper validation is complete.
2. `docker compose up -d --build` starts two containers sharing one SQLite volume (`zora_data`):
   - `zora-dashboard` - read-only dashboard + `/api/health`, published on **127.0.0.1:8787 only**.
   - `zora-trader` - the paper-trading loop (`python bot.py run`). The container never runs `live`
     (it needs an interactive confirmation by design).
3. The dashboard has **no login**. Reach it through an SSH tunnel (`ssh -L 8787:127.0.0.1:8787 user@vps`) or put
   HTTPS + authentication in a reverse proxy first. Never publish port 8787 directly.
4. Health: `/api/health`. API docs: `/docs`.

## Termux / phone
`bash setup_termux.sh`, edit `.env`, then `python bot.py learn` and `python bot.py run`. Set `DASHBOARD_PORT=8787`
to serve the stdlib dashboard from inside the trading process (no FastAPI needed).

## Multi-exchange
Supported adapters: Binance, Bybit, OKX and HTX. Set `EXCHANGE_ID` plus `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET`
(or the per-exchange `HTX_API_KEY`... variables). OKX also needs a password.

## Native protection
`NATIVE_PROTECTION_REQUIRED=true` means a live entry is refused unless the exchange confirms conditional stop and
take-profit orders, and the position is closed again at once if arming them fails. When one leg fills the bot cancels
the other. Some spot exchanges lock the whole balance for the first conditional order and reject the second: test in
the exchange sandbox (`python bot.py exchange-sandbox`) before trusting live mode.
