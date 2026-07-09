#!/usr/bin/env bash
set -euo pipefail

# Generate exactly the three daily market reports for one trading day and verify
# that all of them landed in fund.db. If all three already exist, exit without
# starting reporter agents.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"
DB_PATH="${FUND_DB_PATH:-$ROOT_DIR/data/fund.db}"
CALENDAR_PATH="${CALENDAR_PATH:-$WORLD_DIR/runtime/calendar.json}"
SQLITE3_BIN="${SQLITE3:-sqlite3}"

latest_trade_date() {
  node -e 'const fs=require("fs"); const cal=JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days; const today=(process.env.TODAY||new Date().toISOString().slice(0,10)); const d=[...cal].reverse().find(x=>x<=today); if(!d) process.exit(2); console.log(d)' "$CALENDAR_PATH"
}

missing_reports() {
  "$SQLITE3_BIN" "$DB_PATH" "WITH expected(report_type) AS (VALUES ('market_context'),('market_mainline'),('mainline_rotation')) SELECT group_concat(report_type, ',') FROM expected WHERE NOT EXISTS (SELECT 1 FROM market_reports m WHERE m.report_type=expected.report_type AND m.scope='global' AND m.as_of_date='$TRADE_DATE');"
}

TRADE_DATE="${1:-${TRADE_DATE:-}}"
if [[ -z "$TRADE_DATE" ]]; then
  TRADE_DATE="$(latest_trade_date)"
fi
if [[ ! "$TRADE_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "bad TRADE_DATE: $TRADE_DATE" >&2
  exit 2
fi

RUN_ID="${RUN_ID:-market-reports-daily-${TRADE_DATE}}"
OUT_DIR="${OUT_DIR:-runtime-market-reports-daily}"
RETRIES="${RETRIES:-2}"
CONFIG_PATH="${CONFIG_PATH:-config/world-market-reports.yaml}"

missing="$(missing_reports)"
if [[ -z "$missing" ]]; then
  echo "market reports already ready for $TRADE_DATE: market_context, market_mainline, mainline_rotation"
  exit 0
fi

cd "$WORLD_DIR"
node --experimental-strip-types src/market-reports/prepass-driver.ts \
  --config "$CONFIG_PATH" \
  --from "$TRADE_DATE" \
  --to "$TRADE_DATE" \
  --freq daily \
  --run-id "$RUN_ID" \
  --out-dir "$OUT_DIR" \
  --retries "$RETRIES"

missing="$(missing_reports)"
if [[ -n "$missing" ]]; then
  echo "market report validation failed for $TRADE_DATE: missing $missing" >&2
  exit 1
fi

echo "market reports ready for $TRADE_DATE: market_context, market_mainline, mainline_rotation"
