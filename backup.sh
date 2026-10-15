#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
out="backups/zora-$(date -u +%Y%m%dT%H%M%SZ).tgz"
mkdir -p backups
docker run --rm -v zora_data:/data -v "$PWD/backups":/backup alpine sh -c "tar czf /backup/$(basename "$out") -C /data ."
echo "Backup: $out"
