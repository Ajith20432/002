#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo 'Docker is required.'; exit 1; }
[ -f .env ] || cp .env.example .env
sed -i 's/^LIVE_TRADING=.*/LIVE_TRADING=false/' .env
sed -i 's/^I_UNDERSTAND_THE_RISK=.*/I_UNDERSTAND_THE_RISK=false/' .env
sed -i 's/^DASHBOARD_HOST=.*/DASHBOARD_HOST=0.0.0.0/' .env
sed -i 's/^DASHBOARD_PORT=.*/DASHBOARD_PORT=8787/' .env
docker compose build
docker compose up -d
echo 'ZORA deployed in PAPER mode.'
echo 'Dashboard: http://127.0.0.1:8787'
docker compose ps
