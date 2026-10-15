# ZORA v1.2.1 Hosting Release

This package is based on ZORA v1.2.1 FIXED.

## Safety additions
- PAPER mode forced by `deploy.sh`.
- Docker healthcheck and restart policy.
- Persistent SQLite/brain/log volumes.
- Nginx reverse-proxy example.
- Backup/update/health scripts.
- AI rate-limit guard: configured AI unavailable/rate-limited => new entries blocked.
- Existing positions remain under normal risk/SL/TP management.

## Verification
- Python syntax compilation: PASS.
- AI 429 safety simulation: PASS.
- AI recovery simulation: PASS.
- Dedicated AI guard pytest: PASS.
- Full pytest cannot collect four exchange-dependent modules in this build environment because `ccxt` is not installed.
- Docker runtime validation cannot be performed in this environment because Docker is not installed.
