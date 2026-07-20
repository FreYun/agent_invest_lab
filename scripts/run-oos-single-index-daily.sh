#!/usr/bin/env bash
set -euo pipefail

# 单指数 bot 逐日回补 / 续跑脚本。
# 对一个已有账本的 (bot, run_id),从其最后一个 NAV 快照的下一交易日起,
# 逐个交易日调用 oos-daily-driver.ts 前推到 TO_DATE(默认今天),
# 全程 fundInitReset:false(driver 默认),只往 fund.db 账本追加,绝不 reset。
#
# 用法:
#   scripts/run-oos-single-index-daily.sh <bot> <run_id>
# 可选环境变量:
#   CONFIG      驱动配置(相对 world/,默认 config/world-tmp-<run_id>.yaml)
#   TO_DATE     回补终点(默认 date +%F);实际终点取 calendar 中 <= 该日的最大交易日
#   FROM_DATE   回补起点(默认 last_snap 的下一交易日)
#   MAX_DRIVER_SECONDS  单日驱动超时(默认 1200)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"
DB_PATH="${FUND_DB_PATH:-$ROOT_DIR/data/fund.db}"
CALENDAR_PATH="${CALENDAR_PATH:-$WORLD_DIR/runtime/calendar.json}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
SQLITE3_BIN="${SQLITE3:-sqlite3}"
MAX_DRIVER_SECONDS="${MAX_DRIVER_SECONDS:-1200}"

export OPENCLAW_ROOT="${OPENCLAW_ROOT:-$ROOT_DIR}"
export FUND_DB_PATH="$DB_PATH"

BOT="${1:-}"
RUN_ID="${2:-}"
if [[ -z "$BOT" || -z "$RUN_ID" ]]; then
  echo "用法: $0 <bot> <run_id>" >&2
  exit 2
fi
if [[ ! "$BOT" =~ ^[A-Za-z0-9._:-]+$ ]]; then echo "bad bot id: $BOT" >&2; exit 2; fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9._:-]+$ ]]; then echo "bad run id: $RUN_ID" >&2; exit 2; fi

CONFIG="${CONFIG:-config/world-tmp-$RUN_ID.yaml}"
if [[ ! -f "$WORLD_DIR/$CONFIG" ]]; then
  echo "config not found: $WORLD_DIR/$CONFIG (可用 CONFIG=... 覆盖)" >&2
  exit 2
fi

TO_INPUT="${TO_DATE:-$(date '+%F')}"

# calendar 中 <= 参照日的最大交易日
latest_trade_date() {
  node -e 'const c=require(process.argv[1]).trading_days;const t=process.argv[2];for(let i=c.length-1;i>=0;i--){if(c[i]<=t){console.log(c[i]);process.exit(0)}}process.exit(1)' "$CALENDAR_PATH" "$1"
}

TO="$(latest_trade_date "$TO_INPUT")" || { echo "no trading day <= $TO_INPUT in $CALENDAR_PATH" >&2; exit 2; }

LAST_SNAP="$($SQLITE3_BIN "$DB_PATH" "SELECT COALESCE(MAX(trade_date),'') FROM fund_bot_daily_snapshots WHERE bot_id='$BOT' AND run_id='$RUN_ID';")"
if [[ -z "$LAST_SNAP" ]]; then
  echo "拒绝: $BOT/$RUN_ID 在账本里没有任何 NAV 快照(不是续跑场景,应先建仓)" >&2
  exit 2
fi

FROM="${FROM_DATE:-}"
# 缺失交易日列表:(FROM 或 last_snap 的下一交易日) .. TO
readarray -t MISSING < <(node -e '
const c=require(process.argv[1]).trading_days;
const last=process.argv[2], to=process.argv[3], from=process.argv[4]||"";
const lo = from ? from : null;
for (const d of c) {
  if (from) { if (d < from) continue; }
  else { if (d <= last) continue; }
  if (d > to) break;
  console.log(d);
}
' "$CALENDAR_PATH" "$LAST_SNAP" "$TO" "$FROM")

mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_FILE:-$LOG_DIR/oos-single-${BOT}-${RUN_ID}.log}"
LOCK="${LOCK:-/tmp/oos-single-${BOT}-${RUN_ID}.lock}"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] 另一个回补进程正在跑 $BOT/$RUN_ID;跳过" >&2
  exit 0
fi

exec > >(tee -a "$LOG_FILE") 2>&1

echo "[$(date '+%F %T')] 单指数续跑 bot=$BOT run_id=$RUN_ID config=$CONFIG"
echo "  last_snap=$LAST_SNAP  to=$TO  待回补=${#MISSING[@]} 天: ${MISSING[*]:-<无>}"
if [[ "${#MISSING[@]}" -eq 0 ]]; then
  echo "[$(date '+%F %T')] 已到最新交易日,无需回补"
  exit 0
fi

# 上游(dd-ai-api)偶发 503「no healthy upstream」抖动。对同一决策日做重试退避,
# 扛过瞬时抖动;连续 DAY_MAX_ATTEMPTS 次仍未推进快照才中止(不向前跳,零空洞)。
DAY_MAX_ATTEMPTS="${DAY_MAX_ATTEMPTS:-5}"
DAY_RETRY_SLEEP="${DAY_RETRY_SLEEP:-30}"

overall_rc=0
prev_snap="$LAST_SNAP"
for d in "${MISSING[@]}"; do
  day_done=0
  for ((att=1; att<=DAY_MAX_ATTEMPTS; att++)); do
    echo "[$(date '+%F %T')] --> $BOT $RUN_ID 决策日 $d (尝试 $att/$DAY_MAX_ATTEMPTS)"
    driver_rc=0
    (
      cd "$WORLD_DIR"
      OOS_BOTS="$BOT" TRADE_DATE="$d" TODAY="$d" timeout --kill-after=60s "${MAX_DRIVER_SECONDS}s" \
        node --experimental-strip-types src/oos-daily-driver.ts \
        --date "$d" \
        --bot-id "$BOT" \
        --run-id "$RUN_ID" \
        --config "$CONFIG" \
        --world-dir runtime
    ) || driver_rc=$?
    new_snap="$($SQLITE3_BIN "$DB_PATH" "SELECT COALESCE(MAX(trade_date),'') FROM fund_bot_daily_snapshots WHERE bot_id='$BOT' AND run_id='$RUN_ID';")"
    echo "[$(date '+%F %T')] <-- $BOT $d 完成 rc=$driver_rc 最新快照=$new_snap"
    if [[ "$new_snap" == "$d" ]]; then day_done=1; prev_snap="$new_snap"; break; fi
    echo "[$(date '+%F %T')] WARN: $BOT $d 未推进(rc=$driver_rc);${DAY_RETRY_SLEEP}s 后重试" >&2
    sleep "$DAY_RETRY_SLEEP"
  done
  if [[ "$day_done" != "1" ]]; then
    echo "[$(date '+%F %T')] STOP: $BOT $d 连续 $DAY_MAX_ATTEMPTS 次未推进快照(prev=$prev_snap),中止本轮以防空洞;稍后重跑" >&2
    overall_rc=1
    break
  fi
done

echo
echo "===== 单指数续跑结束 bot=$BOT run_id=$RUN_ID overall_rc=$overall_rc ====="
"$SQLITE3_BIN" -header -column "$DB_PATH" "
  SELECT trade_date, ROUND(total_value,2) AS total_value, ROUND(net_value,6) AS net_value,
         ROUND(cumulative_return_pct,4) AS cum_ret_pct, ROUND(cash_weight*100,2) AS cash_pct
    FROM fund_bot_daily_snapshots
   WHERE bot_id='$BOT' AND run_id='$RUN_ID' AND trade_date > '$LAST_SNAP'
   ORDER BY trade_date;" || true

exit "$overall_rc"
