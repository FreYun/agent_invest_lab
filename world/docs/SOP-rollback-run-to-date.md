# SOP：把某个 world run 回退到指定交易日

> 场景：某个 bot 的 run 在中途做了坏决策（如长期空仓踏空），需要把它**回退到某个历史交易日的收盘状态**，之后 `world resume` 从下一交易日重新决策。
>
> 这是**破坏性、不可逆**操作（删 DB 行 + 删 runtime 文件）。**必须先备份**，且涉及多张表 + 文件系统的协同回退，不能只删 DB 行。
>
> 本 SOP 抽象自一次真实回退：把 `bot12` 的 run `dash-2026-06-01T05-51-08` 回退到 `2025-06-03` 收盘、保留 06-03、从 06-04 续跑。把下文的 `$R`(run_id)、`$CUT`(回退到的"最后保留交易日")替换成你的值即可。

---

## 0. 关键概念（先读，否则会做错）

- **两套日期**：run 目录名里的时间戳（`dash-2026-06-...`）是真实墙钟时间；图表/快照里的 `2025-06-03` 是回测中的**模拟交易日**。回退针对的是模拟交易日。
- **数据分布在 4 层**，都要回退，否则 resume 会不一致 / 前视污染：
  1. **DB（`data/fund.db`）**：账户现金、持仓、lots、逐日快照、actions、orders、performance。
  2. **逐日目录**（`world/runtime/runs/$R/<交易日>/`）：每个交易日一个目录。
  3. **memory**（`world/runtime/runs/$R/memory/store.jsonl`）：agent 跨日携带的记忆，`created_at` = 模拟交易日。
  4. **state.json / world-date**：续跑游标。
- **RL 会话：续跑不需要、但看板会展示，所以清爽回退要删**。`rl-openclaw/agents/<bot>/sessions/*` 是每个世界日**独立 chat session**（`history:[]` 不串联，见 `world/src/run.ts` 注释），不向后传递——所以**对续跑正确性无影响**，跨日记忆只走 `store.jsonl`（PIT 过滤，`world/src/memory-server/store.ts`）。但 **dashboard 的"Day N / events / 时长"时间线面板读的就是这些 session**，不删的话回退后看板仍显示 $CUT 之后的天。要干净回退就按第 6e 步删掉 `> $CUT` 的 session 文件并同步 `sessions.json` 索引。`history-compact/` 不是该面板数据源，可不动。
- **cursor 语义**（`world/src/run.ts` `runLoop`：`for(cursor=fromCursor; ...) date=dates[cursor]`，每天结束 cursor+1）：
  `cursor` = **下一个要跑的交易日索引** = 已完成天数。
  完成到 `$CUT`（索引 `i`）→ resume 从 `i+1` 起 → **`cursor = i+1`**。
- **边界惯例**：通常"回退到 $CUT 的状态" = **保留 $CUT 当天收盘**，删除 `>= $CUT 的下一交易日`。下文统一用"删除日期 `> $CUT`"，即 `>= 次日`。

---

## 1. 定位 run 并摸清状态

```bash
cd /home/rooot/agent_invest_lab
DB=data/fund.db
R=dash-2026-06-01T05-51-08          # ← 你的 run_id

# 1a. 这个 run 里有哪些 bot、日期范围（确认是不是单 bot；多 bot 会一起回退！）
sqlite3 -header -column $DB "SELECT bot_id, MIN(trade_date) mn, MAX(trade_date) mx, COUNT(*) n
  FROM fund_bot_daily_snapshots WHERE run_id='$R' GROUP BY bot_id;"

# 1b. dashboard 默认展示的是 run_id 最新(DESC)的那个 run；确认你回退的就是用户看到的那个
#     (见 server.ts listRunsForBot: ORDER BY run_id DESC，runs[0] 为默认)

# 1c. 确认目标 run 进程已死（不能在活跃 run 上动刀）
PID=$(python3 -c "import json;print(json.load(open('world/runtime/runs/$R/state.json'))['pid'])")
ps -p $PID -o pid,cmd 2>/dev/null || echo "pid $PID NOT running (安全)"

# 1d. 注意：fund.db 是 WAL，可能有别的 run 正在并发写。
#     本 SOP 所有删除都按 run_id 限定，不会动别的 run 的行；但仍用 .timeout。
pgrep -af "main.ts resume" | grep -v grep   # 看还有哪些 run 在跑（确认不是本 R）
```

## 2. 确定 $CUT 的索引 与 目标 cursor

```bash
CUT=2025-06-03                       # ← 回退到的"最后保留交易日"
python3 - "$R" "$CUT" <<'PY'
import json,sys
R,CUT=sys.argv[1],sys.argv[2]
s=json.load(open(f'world/runtime/runs/{R}/state.json'))
td=s['trading_dates']
i=td.index(CUT)
print(f"$CUT={CUT} index={i}")
print(f"次日(待重跑)= td[{i+1}] = {td[i+1]}")
print(f"目标 cursor = {i+1}")
print(f"当前 cursor={s['cursor']} status={s['status']} td_len={len(td)} first={td[0]} last={td[-1]}")
PY
```
记下 `次日`（设为 `$NEXT`，如 `2025-06-04`）和 `目标 cursor`（如 `98`）。

## 3. 取 $CUT 收盘的账户真值（用于重建现金/持仓）

> ⚠️ 不能简单"删行"：`fund_bot_accounts.cash`、`holdings`、`lots.shares_remaining` 都是**期末状态**，不是 $CUT 的状态。要用 $CUT 的快照重建。

```bash
# 3a. $CUT 收盘现金 + 应收（这是续跑的起始现金）
sqlite3 -header -column $DB "SELECT cash, cash_receivable FROM fund_bot_daily_snapshots
  WHERE run_id='$R' AND trade_date='$CUT';"

# 3b. $CUT 当天持仓快照（判断 $CUT 收盘是空仓还是有持仓）
sqlite3 -header -column $DB "SELECT fund_code, shares, market_value, weight FROM fund_bot_position_snapshots
  WHERE run_id='$R' AND trade_date='$CUT';"

# 3c. 是否有跨界在途单（order_date<=$CUT 但 confirm_date 在 $CUT 之后 / 未确认）
#     若有，$CUT 收盘 cash_in_transit / cash_receivable 非 0，需对应保留并据快照重建。
sqlite3 -header -column $DB "SELECT order_id, order_type, order_date, confirm_date, order_amount, status
  FROM fund_bot_orders WHERE (order_run_id='$R' OR settle_run_id='$R')
  AND order_date<='$CUT' AND (confirm_date>'$CUT' OR confirm_date IS NULL OR status!='confirmed');"

# 3d. $CUT 收盘仍 open 的持仓 / lots（决定哪些 holdings/lots 要保留 vs 删）
#     holdings 表通常每 run 只留"当前 open"那条；若它的 entry_date > $CUT，说明 $CUT 时还没建仓 → 删。
sqlite3 -header -column $DB "SELECT holding_id, fund_code, entry_date, status FROM fund_bot_holdings WHERE run_id='$R';"
```

**判定持仓重建方式：**
- **$CUT 收盘空仓**（快照 shares≈0 / cash_weight=1，且无跨界在途单）→ 最简单：删 `entry_date > $CUT` 的 holdings/lots，现金=3a 的 cash，in_transit/receivable=0。`entry_date <= $CUT` 的 lots 维持现状（它们若现在 closed 且 $CUT 已空仓，说明 $CUT 前已平，状态一致）。
- **$CUT 收盘有持仓**（罕见、更复杂）→ 不能只删行：需把 open 的 holding/lot 的 `shares_remaining/cost_remaining/market_value/latest_nav` 改回 $CUT 快照值。**这种情况停下来跟用户确认重建口径**，本 SOP 的简化删除会算错。

## 4. 备份（不可跳过）

```bash
TS=$(date +%Y%m%d-%H%M%S); BK=/tmp/rollback-$R-$TS; mkdir -p "$BK"
sqlite3 $DB ".timeout 10000" ".backup '$BK/fund.db'"          # WAL 安全的在线备份
tar czf "$BK/runtime-$R.tar.gz" -C world/runtime/runs "$R"
echo "$BK"; ls -la "$BK"
```
回滚备份：`cp $BK/fund.db data/fund.db`（先停所有写 fund.db 的 run）；`rm -rf world/runtime/runs/$R && tar xzf $BK/runtime-$R.tar.gz -C world/runtime/runs`。

## 5. DB 回退（单事务）

> 下面是**$CUT 收盘空仓**的标准模板。日期阈值统一用 `$NEXT`（= $CUT 次一交易日），即"删除 >= $NEXT"。

```bash
NEXT=2025-06-04        # ← 第 2 步算出的次日
CASH=1311604.89        # ← 第 3a 步的 cash
sqlite3 $DB <<SQL
.timeout 10000
BEGIN IMMEDIATE;
DELETE FROM fund_bot_daily_snapshots    WHERE run_id='$R' AND trade_date  >= '$NEXT';
DELETE FROM fund_bot_position_snapshots WHERE run_id='$R' AND trade_date  >= '$NEXT';
DELETE FROM fund_bot_actions            WHERE run_id='$R' AND action_date >= '$NEXT';
DELETE FROM fund_bot_reviews            WHERE run_id='$R' AND review_date >= '$NEXT';
DELETE FROM fund_bot_performance        WHERE run_id='$R' AND trade_date  >= '$NEXT';
DELETE FROM fund_bot_orders             WHERE (order_run_id='$R' OR settle_run_id='$R') AND order_date >= '$NEXT';
DELETE FROM fund_bot_holding_lots       WHERE run_id='$R' AND entry_date  >= '$NEXT';
DELETE FROM fund_bot_holdings           WHERE run_id='$R' AND entry_date  >= '$NEXT';
UPDATE fund_bot_accounts SET cash=$CASH, cash_in_transit=0.0, cash_receivable=0.0, updated_at=datetime('now') WHERE run_id='$R';
COMMIT;
SQL
```

**涉及的表清单**（确认无遗漏）：`fund_bot_daily_snapshots`、`fund_bot_position_snapshots`、`fund_bot_actions`、`fund_bot_reviews`、`fund_bot_performance`（按 period 每日多行）、`fund_bot_orders`（`order_run_id` 或 `settle_run_id`）、`fund_bot_holding_lots`、`fund_bot_holdings`、`fund_bot_accounts`。
`fund_system_runs / fund_selection_runs / fund_paradigm_runs / fund_allocation_runs` 有 `run_id` 列，但通常本类 run 不落库（先查一遍 `COUNT(*)`，有才处理）。

## 6. runtime 回退

```bash
D=world/runtime/runs/$R

# 6a. 删除 >= $NEXT 的逐日目录（保留到 $CUT）
deleted=0
for dir in "$D"/2025-* "$D"/2026-*; do
  [ -d "$dir" ] || continue
  day=$(basename "$dir")
  if [[ "$day" > "$CUT" ]]; then rm -rf "$dir"; deleted=$((deleted+1)); fi
done
echo "deleted $deleted day dirs; tail:"; ls -d "$D"/2025-* "$D"/2026-* 2>/dev/null | tail -2

# 6b. 截断 memory store.jsonl 到 created_at <= $CUT
python3 - "$D/memory/store.jsonl" "$CUT" <<'PY'
import json,sys
p,CUT=sys.argv[1],sys.argv[2]
kept=[l.rstrip("\n") for l in open(p) if l.strip() and str(json.loads(l).get("created_at",""))[:10]<=CUT]
open(p,"w").write("\n".join(kept)+("\n" if kept else ""))
print(f"kept={len(kept)} last={json.loads(kept[-1])['created_at'] if kept else None}")
PY

# 6c. state.json：cursor=目标值、current_date=$NEXT、status=paused、保留 trading_dates
python3 - "$D/state.json" "$NEXT" <<'PY'
import json,sys,datetime
p,NEXT=sys.argv[1],sys.argv[2]
s=json.load(open(p)); td=s["trading_dates"]
i=td.index(NEXT)
s["cursor"]=i; s["current_date"]=NEXT; s["status"]="paused"; s.pop("aborted_reason",None)
s["updated_at"]=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
json.dump(s,open(p,"w"),ensure_ascii=False,indent=2); open(p,"a").write("\n")
print("cursor=",s["cursor"],"current_date=",s["current_date"],"td[cursor]=",td[i])
PY

# 6d. world-date 文件 → $NEXT
printf '%s\n' "$NEXT" > "$D/world-date"
# 注：PAUSE/STOP 哨兵不用删，resumeWorld 会自动清。

# 6e. 删 RL sessions（>$CUT）并同步 sessions.json 索引——否则 dashboard 时间线仍显示未来天。
#     sessions.json: key 形如 agent:<bot>:trading-<run>-YYYY-MM-DD → {sessionFile: <uuid>.jsonl}
#     每 run 下每个 bot 各有一份 sessions.json，多 bot 要循环处理。先 dry-run 看数量再删。
python3 - "$D" "$CUT" <<'PY'
import json,os,re,glob
D,CUT=__import__('sys').argv[1],__import__('sys').argv[2]
date_re=re.compile(r"(\d{4}-\d{2}-\d{2})$")
for idxp in glob.glob(f"{D}/rl-openclaw/agents/*/sessions/sessions.json"):
    idx=json.load(open(idxp)); delk=[]; delf=[]
    for k,v in idx.items():
        m=date_re.search(k)
        if m and m.group(1)>CUT:
            delk.append(k)
            sf=v.get("sessionFile") if isinstance(v,dict) else None
            if sf and sf.endswith(".jsonl"): delf+=[sf, sf[:-6]+".state.json"]
    n=sum(os.path.exists(f) and (os.remove(f) or True) for f in delf)
    for k in delk: idx.pop(k,None)
    json.dump(idx,open(idxp,"w"),ensure_ascii=False,indent=2); open(idxp,"a").write("\n")
    print(f"{idxp}: deleted_files={n} kept_keys={len(idx)} max={max((date_re.search(k).group(1) for k in idx),default=None)}")
PY
```

## 7. 校验

```bash
# DB 末日应为 $CUT；holdings 数量符合预期；现金正确
sqlite3 -header -column $DB "SELECT trade_date,net_value,cash,cash_weight FROM fund_bot_daily_snapshots
  WHERE run_id='$R' ORDER BY trade_date DESC LIMIT 1;"
sqlite3 $DB "SELECT COUNT(*) holdings FROM fund_bot_holdings WHERE run_id='$R';"
# resume 前置条件：status=paused/running，trading_dates 首尾/长度未变
python3 -c "import json;s=json.load(open('$D/state.json'));print('status',s['status'],'cursor',s['cursor'],'td_len',len(s['trading_dates']),'first',s['trading_dates'][0],'last',s['trading_dates'][-1])"
# RL session 索引已截断到 $CUT（看板时间线最后一天）
for f in "$D"/rl-openclaw/agents/*/sessions/sessions.json; do
  python3 -c "import json,re,sys;idx=json.load(open(sys.argv[1]));ds=sorted(re.search(r'(\d{4}-\d{2}-\d{2})\$',k).group(1) for k in idx);print(sys.argv[1],'keys',len(idx),'max',ds[-1] if ds else None)" "$f"
done
# 其它并发 run 未被误伤
```

## 8. 续跑（交给用户决定是否启动）

> resume 会拉起一个长时间、耗 LLM token 的 agent 跑批。**不要擅自启动**，把命令给用户，或问一句是否现在跑。

```bash
# config 一般是 dashboard 启动时生成的 per-run 临时 yaml：
ls world/config/ | grep "$R"        # 形如 world-tmp-$R.yaml
cd world && node --experimental-strip-types main.ts resume \
  --config config/world-tmp-$R.yaml --run-id $R
```
resumeWorld（`world/src/run.ts`）会：清 STOP/PAUSE 哨兵；以 `fundInitReset=false` 让 `init_fund_account` 对已存在账户 no-op（**不会重置现金**，保住第 5 步重建的账本）；校验交易日序列未变；翻 status→running、改 pid；从 `cursor` 续跑。

---

## 易错点速查

| 坑 | 后果 | 防 |
|---|---|---|
| 只删 DB 行、不回退 runtime | resume 从错误游标/记忆继续，前视污染 | 4 层都回退 |
| 没重建 `fund_bot_accounts.cash` | 续跑用期末现金，账全错 | 第 5 步 UPDATE |
| `cursor` 设成 $CUT 的索引而非次日 | 重跑 $CUT 当天（与"保留 $CUT"矛盾） | cursor = $CUT索引 + 1 |
| 多 bot run 上回退单个 bot | 误删/漏删 | 第 1a 步先确认 bot 数；多 bot 要么全退要么按 bot_id 再加过滤 |
| $CUT 收盘**有持仓**仍套用空仓模板 | open 持仓的 shares/cost 算错 | 第 3 步判定，有持仓时停下确认重建口径 |
| 漏删 RL sessions | 续跑没问题，但 **dashboard 时间线仍显示 $CUT 之后的天** | 第 6e 步删 session + 同步 sessions.json（多 bot 循环）|
| 直接 `cp` 备份正在被写的 fund.db | 备份损坏 | 用 `sqlite3 .backup` |

## 参考代码位置
- `world/src/run.ts`：`resumeWorld`（续跑语义、fundInitReset=false）、`runLoop`（cursor 语义）、每日独立 session 注释。
- `world/src/memory-server/store.ts`：记忆按 `created_at` PIT 过滤。
- `world/src/backtest-dashboard/server.ts`：`listRunsForBot`（默认展示 run_id DESC 的 runs[0]）、`loadBotForRun`。
