#!/usr/bin/env bash
set -euo pipefail

# Backfill monthly market reports on the first trading day of each month.
#
# This intentionally reuses world/src/market-reports/prepass-driver.ts instead
# of duplicating reporter orchestration. The driver is idempotent: existing
# (report_type, as_of_date, scope) rows are skipped; missing rows are generated.
# market_mainline submissions are normalized by strategy-server through the
# deterministic v5_mainline_plan.py path before being written to fund.db.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"

FROM_DATE="${1:-2025-01-01}"
TO_DATE="${2:-2026-05-29}"
RUN_ID="${RUN_ID:-market-reports-monthly-backfill-${FROM_DATE}_${TO_DATE}}"
OUT_DIR="${OUT_DIR:-runtime-market-reports-backfill}"
RETRIES="${RETRIES:-2}"

cd "$WORLD_DIR"

exec node --experimental-strip-types src/market-reports/prepass-driver.ts \
  --config config/world-market-reports.yaml \
  --from "$FROM_DATE" \
  --to "$TO_DATE" \
  --run-id "$RUN_ID" \
  --out-dir "$OUT_DIR" \
  --retries "$RETRIES"
