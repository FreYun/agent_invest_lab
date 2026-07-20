#!/usr/bin/env bash
set -euo pipefail

# Make agent_invest_lab/data/market.db the canonical market database while
# keeping /home/rooot/database/market.db as a compatibility symlink for older
# .openclaw scripts that still use the legacy absolute path.
#
# This script writes outside the repository. Run it only after the project-local
# database has been created with:
#   sqlite3 /home/rooot/database/market.db ".backup /home/rooot/agent_invest_lab/data/market.db"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DB="${PROJECT_DB:-$ROOT_DIR/data/market.db}"
LEGACY_DB="${LEGACY_DB:-/home/rooot/database/market.db}"
LEGACY_DIR="$(dirname "$LEGACY_DB")"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [[ ! -s "$PROJECT_DB" ]]; then
  echo "[market-db-cutover] project db is missing or empty: $PROJECT_DB" >&2
  exit 2
fi

project_real="$(readlink -f "$PROJECT_DB")"
if [[ -L "$LEGACY_DB" && "$(readlink -f "$LEGACY_DB")" == "$project_real" ]]; then
  echo "[market-db-cutover] already cut over: $LEGACY_DB -> $project_real"
  exit 0
fi

echo "[market-db-cutover] validating project db"
sqlite3 "$PROJECT_DB" "PRAGMA quick_check;"

if [[ -e "$LEGACY_DB" && ! -L "$LEGACY_DB" ]]; then
  echo "[market-db-cutover] checkpointing and validating legacy db"
  sqlite3 "$LEGACY_DB" "PRAGMA wal_checkpoint(FULL); PRAGMA quick_check;"
  mv "$LEGACY_DB" "$LEGACY_DIR/market.db.before-agent-invest-lab-$STAMP"
fi

for suffix in wal shm; do
  sidecar="$LEGACY_DB-$suffix"
  if [[ -e "$sidecar" || -L "$sidecar" ]]; then
    mv "$sidecar" "$LEGACY_DIR/market.db-$suffix.before-agent-invest-lab-$STAMP"
  fi
done

ln -s "$PROJECT_DB" "$LEGACY_DB"
ln -s "$PROJECT_DB-wal" "$LEGACY_DB-wal"
ln -s "$PROJECT_DB-shm" "$LEGACY_DB-shm"

echo "[market-db-cutover] verifying legacy compatibility path"
sqlite3 "$LEGACY_DB" "SELECT 'daily', max(trade_date), count(*) FROM daily;"
echo "[market-db-cutover] done: $LEGACY_DB -> $PROJECT_DB"
