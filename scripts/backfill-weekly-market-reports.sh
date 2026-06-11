#!/usr/bin/env bash
set -euo pipefail

# Backfill weekly market reports on the first trading day of each ISO week.
#
# Two stages, mirroring the monthly backfill split:
#   1. market_mainline / mainline_rotation —— deterministic v5 engine
#      (world/src/market-reports/backfill-deterministic.ts, no LLM)
#   2. market_context —— LLM reporter via prepass-driver.ts with
#      config/world-market-reports-context-only.yaml (qwen3.6-plus)
#
# Both stages are idempotent: existing (report_type, as_of_date, scope) rows
# (including the 18 monthly first-trading-day rows) are skipped; only missing
# weekly rows are generated. Interrupt + rerun resumes automatically.
#
# TO_DATE defaults to the latest trading day in world/runtime/calendar.json
# (kept fresh by refresh-calendar.ts + systemd timer) — never hardcode it.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"

FROM_DATE="${1:-2025-01-01}"
TO_DATE="${2:-$(/usr/bin/python3.12 -c "import json; print(json.load(open('$WORLD_DIR/runtime/calendar.json'))['trading_days'][-1])")}"
RUN_ID="${RUN_ID:-market-reports-weekly-backfill-${FROM_DATE}_${TO_DATE}}"
OUT_DIR="${OUT_DIR:-runtime-market-reports-weekly-backfill}"
RETRIES="${RETRIES:-2}"
STAGE="${STAGE:-all}"   # all | deterministic | context

cd "$WORLD_DIR"

if [[ "$STAGE" == "all" || "$STAGE" == "deterministic" ]]; then
  echo "=== stage 1/2: deterministic weekly backfill (market_mainline + mainline_rotation) ==="
  node --experimental-strip-types src/market-reports/backfill-deterministic.ts \
    --freq weekly \
    --from "$FROM_DATE" \
    --to "$TO_DATE" \
    --run-id "${RUN_ID}-det"
fi

if [[ "$STAGE" == "all" || "$STAGE" == "context" ]]; then
  echo "=== stage 2/2: LLM weekly backfill (market_context, qwen3.6-plus) ==="
  node --experimental-strip-types src/market-reports/prepass-driver.ts \
    --config config/world-market-reports-context-only.yaml \
    --freq weekly \
    --from "$FROM_DATE" \
    --to "$TO_DATE" \
    --run-id "${RUN_ID}-ctx" \
    --out-dir "$OUT_DIR" \
    --retries "$RETRIES"
fi
