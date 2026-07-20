#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"
DB_PATH="${FUND_DB_PATH:-$ROOT_DIR/data/fund.db}"
CALENDAR_PATH="${CALENDAR_PATH:-$WORLD_DIR/runtime/calendar.json}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
RUN_ID="${OOS_BOT101_RUN_ID:-oos-bot101-daily}"
LOCK="${LOCK:-/tmp/oos-bot101-daily.lock}"
SQLITE3_BIN="${SQLITE3:-sqlite3}"
OOS_TABLES_SQL="${OOS_TABLES_SQL:-$ROOT_DIR/scripts/sql/oos_bot101_daily.sql}"
REQUIRE_PREV_TRADING="${REQUIRE_PREV_TRADING:-1}"
REQUIRE_TARGET_TRADING="${REQUIRE_TARGET_TRADING:-1}"
# 14:30 盘中实盘决策模式：默认目标日取系统当天；非交易日仍由 REQUIRE_TARGET_TRADING 跳过。
OOS_BOT101_INTRADAY="${OOS_BOT101_INTRADAY:-0}"
# 护栏：驱动单次 wall-clock 上限(超时强杀，防 node 收尾偶发卡死)；flock 被占超过 MAX_LOCK_AGE 秒
# 视为卡死 run（正常 run ~7-15min），连根杀掉抢占——避免一个僵尸 run 无声饿死之后每天的 run。
MAX_DRIVER_SECONDS="${MAX_DRIVER_SECONDS:-1200}"
MAX_LOCK_AGE="${MAX_LOCK_AGE:-3600}"

# SIGKILL 一个进程及其全部后代。卡死 run 的 node 子进程会继承 flock 的 fd 9，只杀父 bash
# 不够（node 仍持锁），必须连根杀才能真正释放锁。
kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do kill_tree "$child"; done
  kill -9 "$pid" 2>/dev/null || true
}

today_date() {
  date '+%F'
}

# calendar 里 <= 给定日 的最新交易日。simworld 报价"15:00 收盘后可见"→ calendar 最新只到
# 昨天收盘，系统当天通常不在其中。oos 在 T+1 早晨用此函数取"数据已就绪的最新交易日 L"作为
# 要决策的 T 日（而非系统当天——当天数据还没出、也不在 calendar，driver 会直接报错）。
latest_trade_date() {
  node -e 'const fs=require("fs");const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days;const t=process.argv[2];let r="";for(let i=c.length-1;i>=0;i--){if(c[i]<=t){r=c[i];break}}if(r)console.log(r);else process.exit(1)' "$CALENDAR_PATH" "$1"
}

# calendar 里严格早于给定日的最新交易日（前一交易日）。周一→上周五，正确跨过周末/节假日。
prev_trade_date() {
  node -e 'const fs=require("fs");const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days;const t=process.argv[2];let r="";for(let i=c.length-1;i>=0;i--){if(c[i]<t){r=c[i];break}}if(r)console.log(r);else process.exit(1)' "$CALENDAR_PATH" "$1"
}

is_trading_day() {
  node -e 'const fs=require("fs"); const cal=new Set(JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days); process.exit(cal.has(process.argv[2]) ? 0 : 1)' "$CALENDAR_PATH" "$1"
}

is_weekday() {
  node -e 'const d=new Date(`${process.argv[1]}T00:00:00+08:00`); const day=d.getDay(); process.exit(day >= 1 && day <= 5 ? 0 : 1)' "$1"
}

make_intraday_calendar() {
  local target="$1"
  local tmp_dir="$2"
  local tmp_calendar="$tmp_dir/calendar.json"
  node -e '
const fs = require("fs");
const src = process.argv[1], dst = process.argv[2], target = process.argv[3];
const doc = JSON.parse(fs.readFileSync(src, "utf8"));
const days = Array.isArray(doc.trading_days) ? [...doc.trading_days] : [];
if (!days.includes(target)) {
  days.push(target);
  days.sort();
}
doc.trading_days = days;
fs.writeFileSync(dst, JSON.stringify(doc, null, 2) + String.fromCharCode(10));
' "$CALENDAR_PATH" "$tmp_calendar" "$target"
  echo "$tmp_calendar"
}

make_intraday_config() {
  local tmp_dir="$1"
  local tmp_calendar="$2"
  local src_config="$3"
  local src_dir src_base tmp_config
  src_dir="$(dirname "$src_config")"
  src_base="$(basename "${src_config%.yaml}")"
  tmp_config="$src_dir/.${src_base}.intraday.$$.$RANDOM.yaml"
  node -e '
const fs = require("fs");
const src = process.argv[1], dst = process.argv[2], cal = process.argv[3];
const lines = fs.readFileSync(src, "utf8").split(/\r?\n/);
let done = false;
for (let i = 0; i < lines.length; i++) {
  if (/^calendar:\s*/.test(lines[i])) {
    lines[i] = `calendar: ${JSON.stringify(cal)}`;
    done = true;
    break;
  }
}
if (!done) lines.unshift(`calendar: ${JSON.stringify(cal)}`);
fs.writeFileSync(dst, lines.join(String.fromCharCode(10)));
' "$src_config" "$tmp_config" "$tmp_calendar"
  echo "$tmp_config"
}

# 无显式日期时：
#   - 旧早盘/T+1 模式取 calendar 最新交易日（数据已就绪的 T 日）
#   - 14:30 盘中模式取系统当天；当天 NAV 尚未出，订单由 awaiting_nav → 净值刷新后定价
TRADE_DATE="${1:-${TRADE_DATE:-}}"
if [[ -z "$TRADE_DATE" ]]; then
  if [[ "$OOS_BOT101_INTRADAY" == "1" ]]; then
    TRADE_DATE="$(today_date)"
  else
    TRADE_DATE="$(latest_trade_date "$(today_date)")" || { echo "no trading day <= $(today_date) in calendar $CALENDAR_PATH" >&2; exit 2; }
  fi
fi
if [[ ! "$TRADE_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "bad TRADE_DATE: $TRADE_DATE" >&2
  exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  echo "bad OOS_BOT101_RUN_ID: $RUN_ID" >&2
  exit 2
fi

# 需要存在"前一交易日"作为净值基线。用 calendar 找前一交易日，周一会正确跨到上周五，
# 而不是旧逻辑里"日历前一天=周日→每周一误 skip"。
if [[ "$REQUIRE_PREV_TRADING" == "1" ]] && ! prev_trade_date "$TRADE_DATE" >/dev/null; then
  echo "[$(date '+%F %T')] skip: no prior trading day before $TRADE_DATE in calendar"
  exit 0
fi
INTRADAY_TMP_DIR=""
INTRADAY_CONFIG_FILES=""
DRIVER_CONFIG="$WORLD_DIR/config/world-multi-fund-backtest.yaml"
PREPASS_CONFIG="config/world-market-reports.yaml"
if [[ "$REQUIRE_TARGET_TRADING" == "1" ]] && ! is_trading_day "$TRADE_DATE"; then
  if [[ "$OOS_BOT101_INTRADAY" == "1" ]]; then
    if ! is_weekday "$TRADE_DATE"; then
      echo "[$(date '+%F %T')] skip: intraday target day $TRADE_DATE is weekend and absent from calendar"
      exit 0
    fi
    INTRADAY_TMP_DIR="$(mktemp -d /tmp/oos-bot101-intraday-calendar.XXXXXX)"
    trap '[[ -n "${INTRADAY_TMP_DIR:-}" ]] && rm -rf "$INTRADAY_TMP_DIR"; [[ -n "${INTRADAY_CONFIG_FILES:-}" ]] && rm -f $INTRADAY_CONFIG_FILES' EXIT
    INTRADAY_CALENDAR_PATH="$(make_intraday_calendar "$TRADE_DATE" "$INTRADAY_TMP_DIR")"
    DRIVER_CONFIG="$(make_intraday_config "$INTRADAY_TMP_DIR" "$INTRADAY_CALENDAR_PATH" "$WORLD_DIR/config/world-multi-fund-backtest.yaml")"
    PREPASS_CONFIG="$(make_intraday_config "$INTRADAY_TMP_DIR" "$INTRADAY_CALENDAR_PATH" "$WORLD_DIR/config/world-market-reports.yaml")"
    INTRADAY_CONFIG_FILES="$DRIVER_CONFIG $PREPASS_CONFIG"
    echo "[$(date '+%F %T')] WARN: intraday target day $TRADE_DATE absent from close-data calendar; using temporary calendar $INTRADAY_CALENDAR_PATH"
  else
    echo "[$(date '+%F %T')] skip: target day $TRADE_DATE is not a trading day"
    exit 0
  fi
fi

mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_FILE:-$LOG_DIR/oos-bot101-daily-${TRADE_DATE}.log}"

exec 9>"$LOCK"
if ! flock -n 9; then
  # 锁被占。读 holder 边车(PID + 起始 epoch)判定是否陈旧——注意不能用 $LOCK 的 mtime，
  # 因为每次 skip 的 attempt 也会 exec 9>"$LOCK" 截断刷新 mtime（这正是上次死锁时 lock 文件
  # mtime 显示当天而非真实 holder 起始时间的原因）。只有真正拿到锁的 holder 才写边车。
  holder_pid=""; holder_start=0
  read -r holder_pid holder_start < "${LOCK}.holder" 2>/dev/null || { holder_pid=""; holder_start=0; }
  lock_age=$(( $(date +%s) - ${holder_start:-0} ))
  if [[ "$lock_age" -gt "$MAX_LOCK_AGE" && -n "$holder_pid" ]] && kill -0 "$holder_pid" 2>/dev/null; then
    echo "[$(date '+%F %T')] WARN: 锁被 PID $holder_pid 持有 ${lock_age}s (>${MAX_LOCK_AGE}s 阈值)，判定卡死 run，连根 SIGKILL 抢占" >&2
    kill_tree "$holder_pid"
    sleep 3
    if ! flock -n 9; then
      echo "[$(date '+%F %T')] 抢占后仍拿不到锁；放弃本次" >&2
      exit 0
    fi
  else
    echo "[$(date '+%F %T')] previous oos bot101 daily run is still active (age=${lock_age}s holder=${holder_pid:-?}); skip" >&2
    exit 0
  fi
fi
# 记录本次持有者(PID 起始epoch)，供下次陈旧判定/抢占。
printf '%s %s\n' "$$" "$(date +%s)" > "${LOCK}.holder"

exec > >(tee -a "$LOG_FILE") 2>&1

# ensure_oos_tables / mirror_oos_results_for_date 共享实现（refresh-oos-bot101-nav.sh 同源）。
source "$ROOT_DIR/scripts/oos-bot101-mirror.lib.sh"

mirror_oos_results() {
  ensure_oos_tables
  mirror_oos_results_for_date "$TRADE_DATE"
}

summary() {
  local status="$1"
  echo
  echo "===== OOS bot101 daily health summary ====="
  echo "finished_at: $(date '+%F %T %z')"
  echo "status: $status"
  echo "prepass_market_reports_rc: ${prepass_rc:-NA}"
  echo "report_date: $TRADE_DATE"
  echo "bot_id: bot101"
  echo "live_run_id: $RUN_ID"
  echo "tables: oos_market_report_status, oos_bot_daily_snapshots, oos_bot_position_snapshots, oos_bot_orders, oos_bot_actions"
  echo
  echo "-- reports"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT report_type, as_of_date, generated_at, chars FROM oos_market_report_status WHERE live_run_id='$RUN_ID' AND as_of_date='$TRADE_DATE' ORDER BY report_type;" || true
  echo
  echo "-- bot101 orders"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT order_date, order_type, fund_code, ROUND(order_amount,2) AS amount, ROUND(reference_nav,4) AS nav, status, substr(coalesce(action_reason,''),1,80) AS reason FROM oos_bot_orders WHERE bot_id='bot101' AND live_run_id='$RUN_ID' AND order_date='$TRADE_DATE' ORDER BY source_order_id;" || true
  echo
  echo "-- bot101 settled actions"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT action_date, action_type, fund_code, ROUND(amount,2) AS amount, ROUND(shares,4) AS shares, ROUND(nav_used,4) AS nav, substr(coalesce(reason,''),1,80) AS reason FROM oos_bot_actions WHERE bot_id='bot101' AND live_run_id='$RUN_ID' AND action_date='$TRADE_DATE' ORDER BY source_action_id;" || true
  echo
  echo "-- bot101 positions"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT trade_date, fund_code, ROUND(weight * 100, 2) AS weight_pct, ROUND(market_value, 2) AS market_value, ROUND(shares, 4) AS shares FROM oos_bot_position_snapshots WHERE bot_id='bot101' AND live_run_id='$RUN_ID' AND trade_date='$TRADE_DATE' ORDER BY weight DESC, fund_code;" || true
  echo
  echo "-- bot101 nav"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT trade_date, ROUND(total_value,2) AS total_value, ROUND(net_value,6) AS net_value, ROUND(cumulative_return_pct,4) AS cumulative_return_pct, ROUND(cash_weight * 100, 2) AS cash_weight_pct FROM oos_bot_daily_snapshots WHERE bot_id='bot101' AND live_run_id='$RUN_ID' AND trade_date='$TRADE_DATE';" || true
  echo "===== end summary ====="
}

status=failed
trap '[[ -n "${INTRADAY_TMP_DIR:-}" ]] && rm -rf "$INTRADAY_TMP_DIR"; [[ -n "${INTRADAY_CONFIG_FILES:-}" ]] && rm -f $INTRADAY_CONFIG_FILES; summary "$status"' EXIT

ensure_oos_tables

echo "[$(date '+%F %T')] OOS bot101 daily start date=$TRADE_DATE live_run_id=$RUN_ID log=$LOG_FILE"
# prepass(市场研报) 非致命：某份研报上游(LLM/数据)瞬时抖动失败时，只告警不 abort。
# 研报有午夜批预生成兜底在库，且 bot 走 get_market_report 的 PIT 提取（取 as_of<=世界日的最新一期），
# 缺当日某份最多回退到前一日；bot 的「当日决策」才是难复算的核心产物，绝不能被一份研报连坐掐掉。
prepass_rc=0
RUN_ID="market-reports-daily-${TRADE_DATE}" CALENDAR_PATH="${INTRADAY_CALENDAR_PATH:-$CALENDAR_PATH}" CONFIG_PATH="$PREPASS_CONFIG" "$ROOT_DIR/scripts/run-oos-market-reports-daily.sh" "$TRADE_DATE" || prepass_rc=$?
if [[ "$prepass_rc" != "0" ]]; then
  echo "[$(date '+%F %T')] WARN: prepass(market-reports) rc=$prepass_rc —— 某份研报可能缺/未更新；继续跑 bot 决策（bot 走 PIT 读 DB，午夜批兜底）" >&2
fi

# 判"bot 当天是否真决策过"用 driver 跑痕迹（runtime/runs/<run>/<date> 目录），而不是
# fund_bot_daily_snapshots 有没有快照——nav-sync 会给未决策日造 mark-to-market 快照，
# 用快照判会让 daily 永远以为"已决策"而 skip，bot 决策永远跑不起来。driver 跑过才有该目录，
# nav-sync 不碰 runtime → 干净区分"真决策" vs "mark-to-market 估值"。
if [[ "${FORCE_BOT:-0}" != "1" ]] && [[ -d "$WORLD_DIR/runtime/runs/$RUN_ID/$TRADE_DATE" ]]; then
  mirror_oos_results
  echo "[$(date '+%F %T')] bot101 already ran a real decision for $TRADE_DATE (run dir exists); skip bot stage (set FORCE_BOT=1 to rerun)"
  status=ok
  exit 0
fi

# wall-clock 护栏：driver 收尾偶发卡死(node 进程跑完决策却不退出)。timeout 到点先 TERM 再 KILL，
# 保证脚本一定能继续往下 mirror 并退出释放锁，不再让卡死 run 饿死后续每天的 run。
driver_rc=0
(
  cd "$WORLD_DIR"
  OOS_BOT101_RUN_ID="$RUN_ID" TRADE_DATE="$TRADE_DATE" timeout --kill-after=60s "${MAX_DRIVER_SECONDS}s" \
    node --experimental-strip-types src/oos-daily-driver.ts \
    --date "$TRADE_DATE" \
    --run-id "$RUN_ID" \
    --config "$DRIVER_CONFIG" \
    --world-dir runtime
) || driver_rc=$?
if [[ "$driver_rc" == "124" || "$driver_rc" == "137" ]]; then
  echo "[$(date '+%F %T')] WARN: oos-daily-driver 超 ${MAX_DRIVER_SECONDS}s 被强杀(rc=$driver_rc)；bot 决策可能已完成只是进程没退出，继续 mirror 并干净退出" >&2
elif [[ "$driver_rc" != "0" ]]; then
  echo "[$(date '+%F %T')] WARN: oos-daily-driver 失败 rc=$driver_rc；继续 mirror 并干净退出(释放锁)" >&2
fi
mirror_oos_results
if [[ "$driver_rc" == "0" ]]; then status=ok; else status="driver_rc_${driver_rc}"; fi
echo "[$(date '+%F %T')] OOS bot101 daily done date=$TRADE_DATE live_run_id=$RUN_ID driver_rc=$driver_rc"
