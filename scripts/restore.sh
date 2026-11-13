#!/usr/bin/env bash
# Restore one database from a backup file:  ./scripts/restore.sh <database> backups/<file>.sql.gz
# Example: ./scripts/restore.sh cheapedeal backups/cheapedeal-20261101-031500.sql.gz
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <database> <backup.sql.gz>"; exit 1; }
cd "$(dirname "$0")/.."
set -a; source .env; set +a
read -r -p "This OVERWRITES database '$1'. Type YES to continue: " ok
[ "$ok" = "YES" ] || exit 1
gunzip -c "$2" | docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1
echo "restored $1 from $2"
