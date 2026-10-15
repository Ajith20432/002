# ZORA v1.2.1 — VPS Hosting Package

## Deploy
1. Ubuntu 22.04/24.04 VPS; 2 vCPU / 4 GB RAM recommended.
2. Install Docker + Compose plugin.
3. Copy project to `/opt/zora`.
4. `cp .env.example .env`; add only required keys.
5. Keep `LIVE_TRADING=false` and `I_UNDERSTAND_THE_RISK=false`.
6. Run `./deploy.sh`.
7. Verify `./health-check.sh` and `docker compose logs -f zora`.

## HTTPS
Use Nginx in front of `127.0.0.1:8787` and obtain a TLS certificate with your ACME client. Do not expose port 8787 publicly.

## AI limit safety
With one or more AI keys configured and `AI_ENTRY_BLOCK_ON_UNAVAILABLE=true`, ZORA blocks **new entries** when AI capacity is rate-limited/unavailable. Existing positions continue under the normal risk/SL/TP engine. If no AI key is configured, technical-only mode remains available.

HTTP 429, provider failures and all-provider exhaustion trigger a cooldown. `AI_DAILY_CALL_LIMIT=0` disables the local daily cap; set it to a positive number for a local budget ceiling.

## Live
Do not enable live trading until paper validation is complete. Live requires the existing multi-condition lock plus interactive confirmation. Exchange API keys should have trading permissions only; never enable withdrawals.

## Commands
`docker compose up -d` · `docker compose logs -f zora` · `./health-check.sh` · `./update.sh` · `./backup.sh` · `docker compose down`
