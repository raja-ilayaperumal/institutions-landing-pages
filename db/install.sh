#!/usr/bin/env bash
# Bootstrap the local clema_landing DB from scratch.
#
# Idempotent: drops + recreates the DB. CAUTION on shared machines.
#
# Usage:
#   bash db/install.sh                          # creates clema_landing
#   DB_NAME=clema_landing_dev bash db/install.sh # custom name
set -euo pipefail

DB_NAME="${DB_NAME:-clema_landing}"
DB_USER="${DB_USER:-$USER}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "==> Dropping (if exists) and creating $DB_NAME"
dropdb --if-exists -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"
createdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"

echo "==> Installing FDW (mirrors ipeds + score_card from existing ipeds DB)"
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$ROOT/setup_fdw.sql"

echo "==> Applying landing schema"
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -f "$ROOT/schema.sql"

echo "==> Object counts"
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "
  SELECT 'tables'             AS what, COUNT(*) FROM information_schema.tables WHERE table_schema='landing' AND table_type='BASE TABLE'
  UNION ALL
  SELECT 'foreign tables ipeds',       COUNT(*) FROM information_schema.tables WHERE table_schema='ipeds' AND table_type='FOREIGN'
  UNION ALL
  SELECT 'foreign tables score_card',  COUNT(*) FROM information_schema.tables WHERE table_schema='score_card' AND table_type='FOREIGN'
  UNION ALL
  SELECT 'views',                       COUNT(*) FROM information_schema.views  WHERE table_schema='landing'
  UNION ALL
  SELECT 'materialized views',          COUNT(*) FROM pg_matviews              WHERE schemaname='landing';
"

echo
echo "Done. To use this DB, set in .env:"
echo "  POSTGRES_DB=$DB_NAME"
