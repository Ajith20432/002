#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker compose build --pull
docker compose up -d
docker image prune -f >/dev/null 2>&1 || true
./health-check.sh
