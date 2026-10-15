#!/usr/bin/env bash
set -euo pipefail
curl -fsS http://127.0.0.1:8787/api/health >/dev/null
docker compose ps --status running | grep -q '^zora' || docker compose ps --status running | grep -q zora
echo 'ZORA health: OK'
