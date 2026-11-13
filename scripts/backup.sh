#!/usr/bin/env bash
# Daily backup of both databases (Cheape Deal + n8n). Run from the project folder.
# Cron example (every day 03:15):  15 3 * * * cd /opt/cheapedeal && ./scripts/backup.sh >> backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
for db in "$POSTGRES_DB" n8n; do
  docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$db" --clean --if-exists | gzip > "backups/${db}-${stamp}.sql.gz"
  echo "$(date -Is) wrote backups/${db}-${stamp}.sql.gz"
done
find backups -name '*.sql.gz' -mtime +14 -delete   # keep 14 days
echo "REMINDER: also keep a copy of N8N_ENCRYPTION_KEY and .env somewhere safe (not in git)."
