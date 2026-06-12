#!/usr/bin/env bash
set -euo pipefail

# Backfill only market_context reports with the LLM reporter.
#
# This is intentionally separate from the deterministic market_mainline /
# mainline_rotation backfill. It runs reporter-context via prepass-driver.ts
# using config/world-market-reports-context-only.yaml, which pins qwen3.6-plus.
#
# Defaults are for completing weekly market reports. Existing
# (report_type, as_of_date, scope) rows are skipped by prepass-driver, so reruns
# resume from missing market_context dates.
#
# Usage:
#   scripts/backfill-market-context.sh [FROM_DATE] [TO_DATE]
#
# Env overrides:
#   FREQ=weekly|monthly         default: weekly
#   RUN_ID=<id>                 default: market-context-<freq>-backfill-<from>_<to>
#   OUT_DIR=<dir>               default: runtime-market-context-<freq>-backfill
#   RETRIES=<n>                 default: 2

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"

FROM_DATE="${1:-2025-01-01}"
TO_DATE="${2:-$(/usr/bin/python3.12 -c "import json; print(json.load(open('$WORLD_DIR/runtime/calendar.json'))['trading_days'][-1])")}"
FREQ="${FREQ:-weekly}"
RUN_ID="${RUN_ID:-market-context-${FREQ}-backfill-${FROM_DATE}_${TO_DATE}}"
OUT_DIR="${OUT_DIR:-runtime-market-context-${FREQ}-backfill}"
RETRIES="${RETRIES:-2}"

case "$FREQ" in
  weekly|monthly) ;;
  *) echo "FREQ must be weekly or monthly, got: $FREQ" >&2; exit 2 ;;
esac

cd "$WORLD_DIR"

exec node --experimental-strip-types src/market-reports/prepass-driver.ts \
  --config config/world-market-reports-context-only.yaml \
  --freq "$FREQ" \
  --from "$FROM_DATE" \
  --to "$TO_DATE" \
  --run-id "$RUN_ID" \
  --out-dir "$OUT_DIR" \
  --retries "$RETRIES"
