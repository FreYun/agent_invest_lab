# fund-portfolio-mcp: read-side run_id 隔离

> 状态：设计中
> 日期：2026-05-17
> 上游 commit：`8f9ae44 feat: route bots through per-run fund/simworld proxies; add run_id to portfolio tables`

## 背景

commit `8f9ae44` 给 `fund-portfolio-mcp` 加了 per-run 隔离，但只做了一半：

- **写侧**：`run_id` 作为审计字段写入 7 张执行表；proxy 强制注入 run_id 到所有 `tools/call`。
- **读侧**：大约一半的 `WHERE bot_id=? AND status='active'` 查询带 `AND run_id=?`，另一半裸跑——所以新 run 的 bot 通过 daily prompt 看到所有历史 run 的 active 持仓。

实测 bot11：fund_bot_holdings 表里有 4 个 run_id 的 active 持仓共存（共 7 行），新 run init 后 cash=60 万、持仓 40 万（真实），但 bot 看到 cash 60 万 + 跨 run 持仓 ~256 万 + 当前 40 万 = ~356 万，触发"持仓继承前 run 300 万"现象。

## 目标

bot 的 read 路径只看到本 run 的状态。admin / dashboard 路径仍能跨 run 回看。

## 非目标

- 不动 schema（accounts/holdings 表 PK 不变）。
- 不修 `portfolio_init_my_account` 的 `--reset` 清表路径（保留"每 run 数据独立存档"语义；dead 行靠读过滤隐藏）。
- 不迁移 NULL `order_run_id` 的 6 行历史订单（strict 过滤后不可见，影响零）。
- 不动 backtest-dashboard 的查询逻辑。

## 范围划分

### 严格层（必须带 run_id，空 = `_require_run_id` 报错）

bot 自助 + system prep 路径。bot 调用走 fund-portfolio-proxy 自动注入 run_id；CLI 调用必须显式 `--run-id`。

| 工具 | 文件 | 当前签名缺啥 |
|---|---|---|
| `portfolio_get_my_history` | server.py:1547 | 加 `run_id: str = ""` + strict check |
| `portfolio_get_my_performance` | server.py:1747 | 加 `run_id: str = ""` + strict check |
| `portfolio_get_my_trades` | server.py:2197 | 已有 `run_id`，改"空=跨 run 全量"为 strict |
| CLI `get_my_history` | cli_tools.py:86 | 加 `--run-id required=True`，传给 portfolio fn |
| CLI `get_my_performance` | cli_tools.py:91 | 加 `--run-id required=True`，传给 portfolio fn |

### 松散层（run_id 可选；空 = 跨 run 全量）

admin / dashboard 路径。不在 BOT_ONLY 白名单 → bot 永远看不到。

| 工具 | 文件 | 改法 |
|---|---|---|
| `get_fund_holdings` | server.py:1071 | 加 `run_id: str = ""`，非空才追加 `AND run_id=?` |
| `get_fund_curve` | server.py:3684 | 同上 |
| `get_fund_position_snapshots` | server.py:3705 | 同上 |
| `get_fund_review_history` | server.py:3168 | 同上 |

### 内部 helper（writer 内部读 active 持仓）

写工具自身已有 `run_id` 参数，把它透给内部 SELECT 即可。当前 server.py 里这类 SELECT 约 15 处，一半已带 `AND run_id=?`（如 [server.py:654](../../fund-portfolio-mcp/server.py#L654), [:1262](../../fund-portfolio-mcp/server.py#L1262), [:2445](../../fund-portfolio-mcp/server.py#L2445), [:2650](../../fund-portfolio-mcp/server.py#L2650)），另一半（[:1165](../../fund-portfolio-mcp/server.py#L1165), [:1975](../../fund-portfolio-mcp/server.py#L1975) 等）漏改。本次补齐"严格层"3 个 read 工具 reachable 的所有 SELECT。

## 调用链

```
bot Claude Code
  │  tool_use: portfolio_get_my_history(bot_id="bot11", as_of_date="2026-01-15")
  ▼
fund-portfolio-proxy  ── 注入 args.run_id = "dash-2026-05-17T..." ─▶
  ▼
fund-portfolio-mcp (BOT_ONLY 端口)
  │  portfolio_get_my_history(bot_id, as_of_date, limit, fund_code, run_id="dash-...")
  │  _require_run_id(run_id) ✓
  │  SQL: WHERE bot_id=? AND status='active' AND run_id=?
  ▼
SQLite
```

```
world setup (run.ts) ──── runId="dash-..." ────────────────────────▶
  ▼
daily-context.ts
  │  runFundCli(fundMcpCli, 'get_my_history', ['--bot-id', botId, '--run-id', runId, ...])
  ▼
cli_tools.py
  │  portfolio_get_my_history(args.bot_id, args.limit, args.fund_code, args.run_id)
  │  (内部走同一段函数；strict check 通过；同一 SQL 路径)
  ▼
SQLite
```

## 实现要点

### 1. server.py 严格层签名

```python
@mcp.tool()
async def portfolio_get_my_history(
    bot_id: str,
    limit: int = 30,
    fund_code: str = "",
    run_id: str = "",
) -> str:
    err = _require_run_id(run_id)
    if err:
        return err
    # ... 所有 'WHERE bot_id=? AND status="active"' SELECT 后面加 ' AND run_id=?'
```

`portfolio_get_my_performance` / `portfolio_get_my_trades` 同样处理。`portfolio_get_my_trades` 把现有 `if run_id: sql += " AND (order_run_id=? OR settle_run_id=?)"` 改成无条件追加（strict 后 run_id 一定非空）。

### 2. cli_tools.py

```python
p_hist = sub.add_parser("get_my_history")
p_hist.add_argument("--bot-id", required=True)
p_hist.add_argument("--run-id", required=True)  # 新增
p_hist.add_argument("--limit", type=int, default=30)
p_hist.add_argument("--fund-code", default="")

p_perf = sub.add_parser("get_my_performance")
p_perf.add_argument("--bot-id", required=True)
p_perf.add_argument("--run-id", required=True)  # 新增
p_perf.add_argument("--as-of-date", required=True)
p_perf.add_argument("--daily-series-limit", type=int, default=120)

# dispatch:
if args.cmd == "get_my_history":
    return await portfolio_get_my_history(args.bot_id, args.limit, args.fund_code, args.run_id)
if args.cmd == "get_my_performance":
    return await portfolio_get_my_performance(args.bot_id, args.as_of_date, args.daily_series_limit, args.run_id)
```

### 3. world/src/daily-context.ts

`runFundCli(... 'get_my_history' ...)` 和 `... 'get_my_performance' ...` 的 args 数组加 `'--run-id', opts.runId`。`opts` 类型已有 `runId`（从 run.ts 调用处传入），verify 调用点签名。

### 4. server.py 松散层签名

```python
@mcp.tool()
async def get_fund_holdings(bot_id: str, run_id: str = "") -> str:
    # ...
    sql = "SELECT ... WHERE bot_id=? AND status='active'"
    args = [bot_id]
    if run_id:
        sql += " AND run_id=?"
        args.append(run_id)
    # ...
```

其余 3 个 admin 工具同模式。

### 5. fund-portfolio-proxy 不动

[fund-portfolio-proxy/server.ts](../../world/src/fund-portfolio-proxy/server.ts) 已经无条件给所有 `tools/call` 注入 `run_id`，tools/list 的 inputSchema 自动 strip `run_id`。新加的 `run_id` 参数会被 proxy 接管，bot 视野不可见。

## 测试

### 单元（pytest）

`fund-portfolio-mcp/test_run_id_isolation.py`（新增）：
- 准备：同 bot 两个 run（runA、runB）各 insert active 持仓与订单
- `portfolio_get_my_history(bot_id, run_id=runA)` → 只返回 runA 持仓
- `portfolio_get_my_history(bot_id, run_id="")` → 报错（strict）
- `portfolio_get_my_performance(bot_id, as_of_date, run_id=runA)` → holdings_performance 列表只含 runA
- `portfolio_get_my_trades(bot_id, as_of_date, run_id=runA)` → orders 列表只含 runA
- `get_fund_holdings(bot_id)` → 返回 runA+runB 全部（松散层默认跨 run）
- `get_fund_holdings(bot_id, run_id=runA)` → 只返回 runA

### 集成（vitest in world/）

`world/test/run-id-isolation.test.ts`（新增）：
- 起 run1（runId1），bot 通过 portfolio_place_buy_order 建仓
- close run1
- 起 run2（runId2），同 bot，**不**做任何买入
- 在 run2 调用 portfolio_get_my_performance（through CLI from daily-context），assert holdings_performance 是空数组、cash = initial_capital
- assert DB 里 fund_bot_holdings 仍有 run1 的 active 行（保留语义）

### 回归

跑现有 `world/test/run.test.ts`、`world/test/resume.test.ts` 确认未挂。fund-portfolio-mcp 现有测试（`test_bot_performance.py`、`test_fund_nav_performance.py`、`test_redeem_t1_settlement.py`）verify 不挂——它们应该已经传 run_id（commit 8f9ae44 之后写的）。

## 失败模式

- **proxy 漏注入** → bot 拿到空 run_id → `_require_run_id` 返错 JSON → bot 看到 `{"success": false, "message": "run_id 缺失..."}`，比静默看错数据安全。
- **同 bot 同 run_id 跨 day 多次 run（resume）** → 同 run_id 下持仓都看见，行为正确。
- **NULL run_id 的 6 行老 orders** → strict 模式下不可见，影响零。

## 不变量

- 数据库行不被本次改动删除/迁移。
- accounts 表仍是 (bot_id) PK；current run 的 accounts 状态读 latest 行；历史 run 账户态走 `daily_snapshots`（PK 含 run_id）。
- proxy 接口契约不变。
- admin / dashboard 跨 run 查询能力保留。
