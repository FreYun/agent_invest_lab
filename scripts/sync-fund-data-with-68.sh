#!/usr/bin/env bash
# sync-fund-data-with-68.sh — 本机回测 fund.db 与 68 (172.31.41.1055) 的「数据侧」表双向对齐
#
# 背景:两台机各自跑回测但共享基金池/行情数据。68 的 agent_invest_lab/data/fund.db 盘前刷新
#       fund_nav 到上一交易日;两边基金池各有独有部分,需要双向对齐。
#
# 同步策略(拉取 upsert + 回推增量,68 在冲突时永远赢):
#   ① 拉取:白名单表(TABLES)从 68 快照 INSERT OR REPLACE 合并进本机——68 的行覆盖本机
#      同主键行,本机独有行保留。无主键表(real_user_txns)REPLACE 会退化成追加导致翻倍,
#      改为整表替换(以 68 为准)。
#   ② 回推:拉取后本机即两边并集;对有主键的表算 EXCEPT 差集(本机有、68 无),
#      推到 68 用 INSERT OR IGNORE 纯增量写入——绝不覆盖 68 现有行。无主键表不回推。
#   - 绝不同步 fund_bot_* / fund_allocation_runs / fund_selection_runs / fund_system_runs /
#     fund_paradigm_runs —— 两台机各自的回测 run 记录,互相覆盖会毁数据。
#   - 本机缺的表(如 market_reports)自动按 68 端 schema 建表后再灌。
#   - 合并前先比对两端列名,schema 漂移时跳过该表并告警,不盲插。
#
# 流程:68 端 sqlite3 .backup 出 WAL 一致快照 → scp 回本机 → 本机 .backup 留底 →
#       ATTACH 快照逐表合并(单事务) → 差集回推 68 → 输出行数/最新日期校验。
#
# 依赖:本机 → 68 的 ssh 免密(~/.ssh/id_ed25519,2026-06-10 配置);sqlite3 CLI。
# 用法:手动跑 `bash scripts/sync-fund-data-with-68.sh`;cron 每天 06:50(68 盘前刷新后)。
set -euo pipefail

REMOTE=rooot@172.31.41.1055
REMOTE_DB=/home/rooot/agent_invest_lab/data/fund.db
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DB="$HERE/data/fund.db"
SNAP=/tmp/fund-sync-from-68.db
PUSH=/tmp/fund-sync-push-to-68.db
BACKUP="$LOCAL_DB.pre-sync.bak"   # 滚动备份,只留最近一次同步前的状态

# cron 的 PATH 没有 linuxbrew;本机没有 /usr/bin/sqlite3
SQLITE3="$(command -v sqlite3 || true)"
[ -x "${SQLITE3:-}" ] || SQLITE3=/home/linuxbrew/.linuxbrew/bin/sqlite3

TABLES=(fund_info fund_nav fund_nav_performance fund_performance fund_style
        fund_top_stocks fund_industry fund_capability_circle
        real_user_cycles real_user_txns real_user_curves market_reports)

log() { echo "[$(date '+%F %T')] $*"; }

log "1/5 在 68 端生成一致性快照 (.backup) ..."
ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE" \
  "rm -f /tmp/fund-sync-export.db && sqlite3 '$REMOTE_DB' \".backup /tmp/fund-sync-export.db\""

log "2/5 拉回快照 ..."
scp -q -o BatchMode=yes "$REMOTE:/tmp/fund-sync-export.db" "$SNAP"
ssh -o BatchMode=yes "$REMOTE" "rm -f /tmp/fund-sync-export.db"

log "3/5 本机库留底 → $BACKUP ..."
"$SQLITE3" "$LOCAL_DB" ".backup '$BACKUP'"

log "4/5 合并白名单表(68 → 本机)..."
for t in "${TABLES[@]}"; do
  remote_cols=$("$SQLITE3" "$SNAP" "SELECT GROUP_CONCAT(name) FROM pragma_table_info('$t');")
  if [ -z "$remote_cols" ]; then
    log "  - $t: 68 端无此表,跳过"
    continue
  fi
  local_cols=$("$SQLITE3" "$LOCAL_DB" "SELECT GROUP_CONCAT(name) FROM pragma_table_info('$t');")
  if [ -z "$local_cols" ]; then
    log "  - $t: 本机无此表,按 68 端 schema 创建"
    "$SQLITE3" "$SNAP" "SELECT sql || ';' FROM sqlite_master WHERE type='table' AND name='$t';" \
      | "$SQLITE3" "$LOCAL_DB"
    local_cols="$remote_cols"
  fi
  if [ "$local_cols" != "$remote_cols" ]; then
    log "  ⚠️ $t: 两端列不一致,跳过(本机:$local_cols / 68:$remote_cols)"
    continue
  fi
  # 有主键 → upsert 合并(保留本机独有行);无主键(纯 rowid 表,如 real_user_txns)
  # INSERT OR REPLACE 会退化成追加导致翻倍 → 改为整表替换(以 68 为准)。
  has_pk=$("$SQLITE3" "$LOCAL_DB" "SELECT COUNT(*) FROM pragma_table_info('$t') WHERE pk > 0;")
  if [ "$has_pk" -gt 0 ]; then
    merge_sql="INSERT OR REPLACE INTO main.\"$t\" SELECT * FROM snap.\"$t\";"
    mode="upsert"
  else
    merge_sql="DELETE FROM main.\"$t\"; INSERT INTO main.\"$t\" SELECT * FROM snap.\"$t\";"
    mode="整表替换(无主键)"
  fi
  before=$("$SQLITE3" "$LOCAL_DB" "SELECT COUNT(*) FROM \"$t\";")
  "$SQLITE3" -cmd ".timeout 30000" "$LOCAL_DB" "
    ATTACH '$SNAP' AS snap;
    BEGIN IMMEDIATE;
    $merge_sql
    COMMIT;
    DETACH snap;"
  after=$("$SQLITE3" "$LOCAL_DB" "SELECT COUNT(*) FROM \"$t\";")
  log "  - $t: $before → $after 行 [$mode]"
done

log "5/5 计算差集并回推(本机 → 68,INSERT OR IGNORE 纯增量)..."
rm -f "$PUSH"
push_tables=()
for t in "${TABLES[@]}"; do
  # 只回推有主键、两端 schema 一致的表;差集基于刚拉的快照算,无需再查 68
  has_pk=$("$SQLITE3" "$LOCAL_DB" "SELECT COUNT(*) FROM pragma_table_info('$t') WHERE pk > 0;")
  [ "${has_pk:-0}" -gt 0 ] || continue
  remote_cols=$("$SQLITE3" "$SNAP" "SELECT GROUP_CONCAT(name) FROM pragma_table_info('$t');")
  local_cols=$("$SQLITE3" "$LOCAL_DB" "SELECT GROUP_CONCAT(name) FROM pragma_table_info('$t');")
  [ -n "$remote_cols" ] && [ "$local_cols" = "$remote_cols" ] || continue
  n=$("$SQLITE3" -cmd ".timeout 30000" "$LOCAL_DB" "
    ATTACH '$SNAP' AS snap;
    ATTACH '$PUSH' AS push;
    CREATE TABLE push.\"$t\" AS SELECT * FROM main.\"$t\" EXCEPT SELECT * FROM snap.\"$t\";
    SELECT COUNT(*) FROM push.\"$t\";")
  if [ "$n" -gt 0 ]; then
    push_tables+=("$t")
    log "  - $t: 待回推 $n 行"
  fi
done
rm -f "$SNAP"

if [ ${#push_tables[@]} -gt 0 ]; then
  scp -q -o BatchMode=yes "$PUSH" "$REMOTE:/tmp/fund-sync-push.db"
  for t in "${push_tables[@]}"; do
    ssh -o BatchMode=yes "$REMOTE" "sqlite3 -cmd '.timeout 60000' '$REMOTE_DB' \"
      ATTACH '/tmp/fund-sync-push.db' AS push;
      BEGIN IMMEDIATE;
      INSERT OR IGNORE INTO main.\\\"$t\\\" SELECT * FROM push.\\\"$t\\\";
      COMMIT;
      DETACH push;\""
    log "  - $t: 已回推"
  done
  ssh -o BatchMode=yes "$REMOTE" "rm -f /tmp/fund-sync-push.db"
  remote_check=$(ssh -o BatchMode=yes "$REMOTE" "sqlite3 '$REMOTE_DB' 'SELECT COUNT(*) FROM fund_info;'")
  log "  68 端 fund_info 现为 $remote_check 只"
else
  log "  无差集,无需回推"
fi
rm -f "$PUSH"

log "校验:fund_info $("$SQLITE3" "$LOCAL_DB" 'SELECT COUNT(*) FROM fund_info;') 只 | fund_nav 最新 $("$SQLITE3" "$LOCAL_DB" 'SELECT MAX(nav_date) FROM fund_nav;')"
log "完成 ✅"
