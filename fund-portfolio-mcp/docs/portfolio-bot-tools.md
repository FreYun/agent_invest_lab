# portfolio_* — Bot 自助交易工具集使用说明

agent_invest_lab 给 bot 用的 7 个 MCP 工具，全部以 `portfolio_` 前缀命名，注册在 fund-portfolio-mcp 的 readonly 端点（默认 `http://127.0.0.1:28171/mcp`）。

> 为什么是 readonly 端点？这套工具虽然会写库（订单、持仓、快照、账户），但**只能写自己的 `bot_id` 名下的行**——admin 端点 (`:28173`) 上的 `init_fund_account` / `apply_fund_review_and_rebalance` / `save_fund_holdings` 等会动其他全局表的工具没暴露给 bot。命名上叫 readonly 其实是历史名字，这里指"bot 自助"。

## 端口与启动

```bash
# Bot 自助端 (bot 用)
FUND_MCP_READONLY=1 \
OPENCLAW_ROOT=/home/rooot/agent_invest_lab \
FUND_DB_PATH=/home/rooot/agent_invest_lab/data/fund.db \
python3 server.py --transport streamable-http --port 28171

# 系统侧 admin 端 (settle / 数据维护)
OPENCLAW_ROOT=/home/rooot/agent_invest_lab \
FUND_DB_PATH=/home/rooot/agent_invest_lab/data/fund.db \
python3 server.py --transport streamable-http --port 28173
```

## 关键不变量

1. **NAV 唯一来源**：所有 portfolio_* 工具需要净值时一律按 `(fund_code, trade_date)` 严格查询 `fund_nav` 表，找不到直接报错——外部 loop 必须保证净值齐全。
2. **T+1 结算（资金/份额侧）**：
   - **BUY**：T 日立即扣 cash → cash_in_transit；T+1 settle 时把 cash_in_transit 释放、加 holding.shares、写 ADD action。
   - **SELL**：T 日只挂 pending + 冻结 `holding.pending_sell_shares`；T+1 settle 时按 reference_nav 完成 FIFO 扣 shares、加 cash、写 REDUCE action（action_date=T 日）。**与 BUY 完全对称**。
   - 两者都用下单时锁的 `reference_nav` 收口，且赎回费按 order_date 那天的持有天数 T 日就锁死。
3. **回测安全**：所有 `as_of_date` 入参都用**严格 `<`**——绝不暴露 `as_of_date >= trade_date` 的快照、订单、持仓数据。给定 trade_date 的当日数据在 `close_my_day` 真正调用前不会被算进任何"历史"查询。
4. **bot 视野隔离**：每个工具都按 `bot_id` 过滤；bot A 的工具调用看不到 bot B 的任何数据。

---

## 8 个工具一览

| 时机 | 工具 | 写库？ |
|---|---|---|
| 首次开账户 / 测试 reset | `portfolio_init_my_account` | ✓ |
| 想买入前先看有什么能买 | `portfolio_get_buyable_funds` | — |
| 想买入 | `portfolio_place_buy_order` | ✓ pending |
| 想卖出 | `portfolio_place_sell_order` | ✓ pending |
| 当日所有操作完成 | `portfolio_close_my_day` | ✓ 写日快照 |
| 任何时刻看当前账户 | `portfolio_get_my_history` | — |
| 任何时刻看历史表现 | `portfolio_get_my_performance` | — |
| 任何时刻看历史操作 | `portfolio_get_my_trades` | — |

---

## 1. `portfolio_init_my_account` — 自助开户

```
portfolio_init_my_account(bot_id, initial_capital, force=False)
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `bot_id` | str | 账户标识（自取） |
| `initial_capital` | float | 起始资金，必须 > 0；全部为可用现金 |
| `force` | bool | False（默认）：已有账户则拒绝；True：清空该 bot 在全部 7 张业务表的所有行，再重建 |

**返回**：`{success, bot_id, initial_capital, cash, cash_in_transit, force, cleaned, note}`

**示例**：
```python
# 第一次开
{"bot_id":"bot7","initial_capital":1000000}
# → {"success":true,"cash":1000000,"cash_in_transit":0,"force":false,"cleaned":null}

# 重复 init 拒绝
{"bot_id":"bot7","initial_capital":999}
# → {"success":false,"message":"bot bot7 已有账户...","existing":{...}}

# 测试场景清空重建
{"bot_id":"bot7","initial_capital":2000000,"force":true}
# → {"success":true,"cash":2000000,"cleaned":{"fund_bot_orders":3,...}}
```

**跟 admin 端的 `init_fund_account` 区别**：不接 allocations_json（bot 自己用 `place_buy_order` 建仓）；不写 INIT review/action（账本只有真实交易，replay 不被干扰）；不锁 fund_code（bot 想买 fund.db 里有的任何基金都行）。

---

## 1.5. `portfolio_get_buyable_funds` — 查可买基金代码清单

```
portfolio_get_buyable_funds()
```

无参。

**数据源**：`fund_nav`（基金净值底表，按 `fund_code × nav_date` 记录每天净值）。返回该表里**所有出现过的 fund_code 去重**，按字典序升序。这就是 bot 能传给 `portfolio_place_buy_order` 的 fund_code 全集——只要 trade_date 那天这个 fund_code 在 fund_nav 有对应行，就能下单成功。

**返回**：
```jsonc
{
  "success": true,
  "count": 640,
  "fund_codes": ["000006", "000045", "000059", ..., "510300", ..., "700002"]
}
```

刻意只返回代码——不带 fund_name / fees / 规模等元数据。bot 想看某只基金详情，调 `get_fund_detail(fund_code)`（admin 端的查询工具）或自己查 fund_info。

**示例**：
```python
codes = portfolio_get_buyable_funds()["fund_codes"]
# → 640 个代码，含 510300、021985 等
chosen = "510300"
assert chosen in codes
portfolio_place_buy_order(bot_id, chosen, 500_000, "2026-05-13")
```

**想买的基金不在列表里怎么办**：admin 端用 `scripts/fund-backfill-nav.py` 把那只基金的净值拉进 `fund_nav` 即可。

---

## 2. `portfolio_place_buy_order` — 申报买入

```
portfolio_place_buy_order(bot_id, fund_code, amount, trade_date, reason="")
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `bot_id` | str | |
| `fund_code` | str | 6 位基金代码，必须在 fund.db 的 fund_info 里 |
| `amount` | float | 申购金额（人民币元，含申购费），必须 > 0 |
| `trade_date` | str | T 日（YYYY-MM-DD），必须在 fund_nav 有该基金的 NAV |
| `reason` | str | 可选，bot 写自己的下单理由，会落到 `action_reason` |

**行为**：
1. 校验账户、基金、当日 NAV、`amount ≤ cash_available`
2. 用 fund_nav(fund_code, trade_date) 的 NAV 作 `reference_nav` 写入订单
3. 立即 `cash -= amount`、`cash_in_transit += amount`（**冻结**避免重复下单超额）
4. 订单 status='pending'，等系统侧 settle 在 T+1 收口

**返回**：`{success, order_id, reference_nav, amount, estimated_fee, estimated_shares, cash_after, cash_in_transit_after, ...}`

**典型拒绝路径**：
- `现金不足：amount=600000 > cash=500000` — 已被冻结的不能再用
- `fund_nav 缺失 (510300, 2026-05-09)` — 周末或停牌
- `基金 XXX 不在 fund_info`

---

## 3. `portfolio_place_sell_order` — 申报卖出

```
portfolio_place_sell_order(bot_id, fund_code, shares, trade_date, reason="")
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `shares` | float | 申报份额（注意是份额不是金额），必须 > 0 |

**行为（T+1 结算，与 BUY 对称）**：
1. 校验账户、有 active 持仓、`shares ≤ holding.shares - pending_sell_shares`
2. 用 trade_date 当日 NAV 作 reference_nav（T 日已在库 → 锁死 `pricing_status='priced'`；T 日 NAV 未出 → `pricing_status='awaiting_nav'`，settle 时按 order_date NAV 定价）
3. **T 日只挂 pending 单** + `holding.pending_sell_shares += shares` 冻结（防重复卖）
4. T 日**不动** `holding.shares` / `amount_invested`、**不写** REDUCE action、**不动** cash / cash_receivable
5. **T+1 settle** 才真正结算：按 order.reference_nav 与 order_date 持有天数从阶梯表算赎回费，FIFO 消耗 lot、扣 shares、close/update holding、释放 pending_sell_shares、逐 lot 写 REDUCE action（action_date=order_date=T 日）、`cash += (gross - fee)`、订单翻 confirmed（confirm_date=as_of_date）

**返回**：`{success, order_id, reference_nav, pricing_status, shares, status='pending', pending_sell_shares_after, estimated_gross, ...}`（`fee` / `confirmed_amount` 等最终数值要到 T+1 settle 后才在 order 上出现）

**注意**：与旧的 T+0 sell 语义不同——T 日看板上卖出基金的份额和市值不会立即减少，卖出款也不会立即进 cash_receivable；效果要 T+1 settle 完成后才可见。这与 BUY（T 日冻结现金、T+1 才加份额）完全对称，也贴合真实 A 股 T+1 确认份额的规则。

**典型拒绝路径**：
- `bot X 无 YYYYYY 的活跃持仓`
- `可卖份额不足：want=500000 sellable=153072 (total=303072, pending_sell=150000)`

---

## 4. `portfolio_close_my_day` — 当日收盘核算

```
portfolio_close_my_day(bot_id, trade_date)
```

bot 当日所有 `place_*_order` 调完后，调一次做收盘核算：

1. 按 trade_date 当日 NAV 估值所有 active 持仓
2. 写 `fund_bot_daily_snapshots` + `fund_bot_position_snapshots`（INSERT OR REPLACE，幂等）
3. 更新 `fund_bot_holdings` 的 `latest_nav` / `market_value` / `actual_weight`
4. 返回 4 块结构化数据

**返回结构**：
```jsonc
{
  "success": true, "bot_id": "...", "trade_date": "...",
  "assets": {
    "initial_capital", "cash_available", "cash_in_transit",
    "market_value", "total_value", "net_value"
  },
  "holdings": [{
    "fund_code", "fund_name", "status", "shares", "pending_sell_shares",
    "amount_invested", "latest_nav", "market_value",
    "unrealized_pnl", "unrealized_pnl_pct", "weight",
    "entry_date", "exit_date", "holding_days", "high_nav"
  }],
  "pnl": {
    "daily_return_pct", "cumulative_return_pct", "max_drawdown_pct"
  },
  "pending": {
    "count", "frozen_cash_total", "frozen_shares_by_fund",
    "orders": [{ "order_id", "type", "fund_code", "order_date",
                  "amount_or_shares", "reference_nav",
                  "estimated_gross_proceeds", "reason" }]
  },
  "asset_allocation_pct": { "equity", "bond", "gold", "cash" },
  "snapshot_written": true
}
```

**幂等**：同 (bot, trade_date) 重复调用会用 INSERT OR REPLACE 覆盖那行 daily snapshot。

---

## 5. `portfolio_get_my_history` — 当前账户视图（无日期过滤）

```
portfolio_get_my_history(bot_id, limit=30, fund_code="")
```

实时读账户、持仓、最近订单，不写库，**不带日期过滤**——看的是"现在"。

**返回结构**：`{success, bot_id, account, holdings, orders, summary}`
- `account`：cash_available / cash_in_transit / market_value / total_value
- `holdings`：所有持仓（active + closed），含 pending_sell_shares
- `orders`：最近 `limit` 单（任何 status，按 order_date desc）
- `summary`：active_holdings 数 / pending_orders 数 / 当前冻结金额 / 冻结份额

可传 `fund_code` 只看那只基金的持仓+订单。

---

## 6. `portfolio_get_my_performance` — 历史投资表现（带日期截止）

```
portfolio_get_my_performance(bot_id, as_of_date, daily_series_limit=120)
```

**严格 `trade_date < as_of_date`** ——回测里禁止偷看未来。

| 参数 | 说明 |
|---|---|
| `as_of_date` | 截止日 |
| `daily_series_limit` | 返回最近 N 天的日序列（默认 120；传 0 = 全量） |

**返回结构**：
```jsonc
{
  "summary": {
    "first_date", "last_date", "trading_days",
    "initial_capital", "latest_total_value", "latest_net_value",
    "total_return_pct", "annualized_return_pct",  // 年化按 252 天
    "max_drawdown_pct", "max_drawdown_date",
    "volatility_pct_annualized", "sharpe_ratio_rf0",  // rf=0 简化
    "win_days", "loss_days", "flat_days",
    "best_day": {"date","return_pct"},
    "worst_day": {"date","return_pct"}
  },
  "trades_summary": {
    "buy_count", "sell_count",
    "total_buy_amount", "total_sell_proceeds", "total_fees",
    "round_trips_count"
  },
  "completed_positions": [{
    "fund_code", "fund_name", "entry_date", "exit_date",
    "holding_days", "total_invested", "total_proceeds",
    "total_fees", "net_pnl", "return_pct"
  }],
  "daily_series": [{ "trade_date", "total_value", "net_value",
                      "daily_return_pct", "cumulative_return_pct",
                      "max_drawdown_pct" }],
  "daily_series_truncated", "daily_series_total",
  "interval_metrics": {
    "as_of_perf_date", "rf_annual_pct", "rf_daily_pct", "trading_days_per_year",
    "metrics": {  // 账户级 5 个 period：1m/3m/6m/1y/since_inception
      "<period>": { "return_pct", "annualized_return_pct", "max_drawdown_pct",
                    "volatility_pct", "sharpe_ratio", "calmar_ratio",
                    "data_points", "window_target_days", "fallback" }
    },
    "holdings_performance": { "<fund_code>": { /* 同结构 metrics + 持仓信息 */ } }
  }
}
```

**口径说明**：
- `completed_positions.total_invested` = 持仓周期内所有 BUY 单 `order_amount` 之和（不是 `holding.amount_invested`，那是被部分卖出按比例摊薄后的剩余成本基）
- `total_proceeds` = 同周期内所有 SELL 单 `confirmed_amount` 之和
- `return_pct` = `(proceeds - invested) / invested × 100`
- `as_of_date` 之前没有任何 daily snapshot 时返回 `summary: null` + 友好 message
- `interval_metrics` 统一年化口径（2026-06-11 起，与 backtest-dashboard 对齐）：
  `annualized_return_pct = (1+return)^(252/区间交易日数) - 1`、
  `volatility_pct = stdev_daily × √252`、`sharpe_ratio = (mean_d - rf_d)/stdev_d × √252`、
  `calmar_ratio = annualized_return_pct / |max_drawdown_pct|`；
  `return_pct` / `max_drawdown_pct` 保持区间原值（回撤不年化）

---

## 7. `portfolio_get_my_trades` — 历史操作流水（带日期截止）

```
portfolio_get_my_trades(bot_id, as_of_date, limit=100, fund_code="")
```

**严格 `order_date < as_of_date`**。返回所有 status 的订单（含 pending），按日期倒序。

**返回结构**：
```jsonc
{
  "summary": {
    "buy_count", "sell_count", "confirmed_count", "pending_count",
    "total_buy_amount", "total_sell_proceeds",
    "total_sell_shares_requested", "total_fees",
    "distinct_funds_traded": ["021985", "510300"]
  },
  "orders": [{
    "order_id", "fund_code", "fund_name",
    "order_type", "status",         // buy/sell × pending/confirmed
    "order_date", "confirm_date",
    "order_amount",                  // BUY=申报金额；SELL=申报份额
    "reference_nav", "confirm_nav",
    "confirmed_shares", "confirmed_amount",
    "fee", "reason"
  }],
  "orders_truncated", "orders_total"
}
```

可传 `fund_code` 只看一只；`limit=0` 返回全量。

---

## 系统侧配套（admin 端 `:28173`，不是 bot 工具，bot 看不到）

### 1. 结算：外部 loop / 系统侧每天 T+1 开始时调一次

```
settle_pending_fund_orders(bot_id, as_of_date)
```

把所有 `order_date < as_of_date` 的 pending 单按各自锁的 `reference_nav` 收口：
- **BUY** → `cash_in_transit -= order_amount`，新增 `holdings.shares`、写一条 ADD action
- **SELL（新机制，与 BUY 对称）** → `order.confirmed_amount IS NULL` 的订单：按 lot FIFO 扣 shares、写 REDUCE action、`cash += (gross - fee)`、释放 pending_sell_shares、close/update holding
- **SELL（老机制兼容）** → `order.confirmed_amount NOT NULL` 的存量订单（旧 T+0 sell 代码遗留）：`cash += order.confirmed_amount`、`cash_receivable -= order.confirmed_amount`；不动 holdings、不再写 actions

这一步**不是 bot 自己干**——bot 只管下单 + 关日，结算由外部 loop 触发。

### 2. 净值底表维护：扩 lab / 拉新基金 / 刷新已有基金净值

数据全部从 `research-mcp.jijinmima.cn/mcp` 自动拉取。两个工具：

```
upsert_fund_from_source(fund_code, start_date="", end_date="")
upsert_funds_from_source(fund_codes_json, start_date="", end_date="")
```

| 项 | 单只版 | 批量版 |
|---|---|---|
| 入参 | `fund_code` (6 位字符串) | `fund_codes_json` JSON 数组字符串 |
| 错误处理 | 失败直接 `success=False` 不动 DB | 错误隔离：成功的正常写、失败的进 `errors` |
| 默认起点 | `research-mcp` 报的成立日；都没有则 5 年前 | 5 年前（不按单只成立日，统一起点） |
| 默认终点 | 今天 | 今天 |

**行为**（两个共用底层逻辑）：
1. 调 `research-mcp.get_fund_info(fund_code)` → INSERT OR REPLACE 进 `fund_info`
2. 分段（每 2 年一段，避免 API timeout）调 `research-mcp.get_fund_nav_and_return(fund_code, start, end)` → INSERT OR REPLACE 进 `fund_nav`，跨段去重
3. ETF 类（基金名/类型含 'ETF'）的 `purchase_fee` 若 research-mcp 报 None，自动落 0；其它字段直落 research-mcp 提供值
4. `redeem_fee_json` 默认留空，要自定义阶梯请另调 `upsert_fund_fees`

**返回（单只版）**：
```jsonc
{
  "success": true, "fund_code": "510300",
  "start_date": "2012-05-04", "end_date": "2026-05-15",
  "result": {
    "fund_code", "fund_name", "fund_type",
    "info_upserted": 1, "nav_upserted": 3407,
    "nav_first": "2012-05-04", "nav_last": "2026-05-14"
  }
}
```

**返回（批量版）**：
```jsonc
{
  "success": true, "start_date":"...", "end_date":"...",
  "requested": 3, "ok_count": 2, "error_count": 1,
  "total_info_upserted": 2, "total_nav_upserted": 116,
  "ok": [{ /* 同单只版的 result 结构 */ }, ...],
  "errors": [{"fund_code":"999999","reason":"RuntimeError: research-mcp 既无元数据也无净值"}]
}
```

**示例**：
```python
# 新增一只 lab 没有的基金（起点用 research-mcp 报的成立日）
upsert_fund_from_source(fund_code="110011")
# → 入库 110011 易方达优质精选混合(QDII), 自 2010 年至今的全部 nav

# 刷新已有基金的最新净值（不指定起点 → 5 年）
upsert_fund_from_source(fund_code="510300")

# 批量加 3 只指定时间窗
upsert_funds_from_source(
    fund_codes_json='["006729","005870","007186"]',
    start_date="2024-01-01"
)
```

**幂等**：两个工具都用 `INSERT OR REPLACE`，重复跑只覆盖同 `(fund_code, nav_date)` 的旧行，不会爆库。

**典型错误**：
- `research-mcp 既无元数据也无净值` — 代码错 / 已清盘 / research-mcp 不收录
- `get_fund_info 失败: ...` — research-mcp 端口超时或代码格式不对
- 网络抖动会被 requests timeout (120s) 截断

**配套环境变量**：
- `RESEARCH_MCP_URL` 默认 `http://research-mcp.jijinmima.cn/mcp`，可覆盖到其它源

---

## 一天的完整生命周期示例

```python
# === 首日（2025-11-17）===
portfolio_init_my_account(bot_id="bot7", initial_capital=1_000_000)
# → cash=1_000_000

# 看 lab 里能买什么
portfolio_get_buyable_funds()
# → ["000006","000045",...,"510300",...] 共 640 个代码；选中 510300

portfolio_place_buy_order(bot_id="bot7", fund_code="510300",
                          amount=500_000, trade_date="2025-11-17",
                          reason="建仓")
# → order_id=..., status="pending", cash_after=500_000, cash_in_transit=500_000

portfolio_close_my_day(bot_id="bot7", trade_date="2025-11-17")
# → assets.total_value=1_000_000 (持仓还没成交), pending.count=1

# === 第二天（2025-11-18）===
# 系统侧自动调 settle_pending_fund_orders(bot7, "2025-11-18")
# → 昨日 BUY 收口：cash_in_transit=0, 持仓 active, shares 到位

portfolio_get_my_history(bot_id="bot7")
# → 看当前持仓 + 现金分布

# 决定加仓
portfolio_place_buy_order(bot_id="bot7", fund_code="510300",
                          amount=200_000, trade_date="2025-11-18")

portfolio_close_my_day(bot_id="bot7", trade_date="2025-11-18")

# === 一段时间后回顾 ===
portfolio_get_my_performance(bot_id="bot7", as_of_date="2025-12-13")
# → 累计收益率 / 年化 / 夏普 / 最大回撤 / 已平仓 round-trips

portfolio_get_my_trades(bot_id="bot7", as_of_date="2025-12-13", limit=50)
# → 最近 50 条订单流水
```

---

## 常见错误信息一览

| 错误 message | 含义 | 解决 |
|---|---|---|
| `bot X 无账户` | 还没 init | 调 `portfolio_init_my_account` |
| `bot X 已有账户...` | 重复 init | 传 `force=True` 或换 bot_id |
| `initial_capital 必须 > 0` | 入参 ≤ 0 | 传正数 |
| `基金 XXX 不在 fund_info` | fund.db 没这只 | admin 侧 `upsert_fund_info` 先入库 |
| `fund_nav 缺失 (XXX, YYYY-MM-DD)` | 周末/停牌/数据未到 | trade_date 必须是有 NAV 的交易日 |
| `现金不足：amount=X > cash=Y` | available cash 不够（含已冻结） | 等待 settle 释放，或减少 amount |
| `bot X 无 YYYYYY 的活跃持仓` | sell 一只没买过的基金 | 先买 |
| `可卖份额不足：want=X sellable=Y` | shares 已被 pending_sell 冻 | 等 settle 或减小 shares |
| `as_of_date=YYYY-MM-DD 之前无任何已完成的日快照` | 截止日早于第一笔 close_my_day | 把 as_of_date 调晚 |

---

## 相关文件

- 工具实现：[fund-portfolio-mcp/server.py](../server.py)（搜 `^async def portfolio_`）
- DB schema：[fund-portfolio-mcp/db.py](../db.py)（`fund_bot_*` 7 张表 + `cash_in_transit`（BUY 冻结）/ `cash_receivable`（老 SELL 存量在途）/ `pending_sell_shares`（新机制 SELL T 日冻结份额）三个延迟现金/份额列）
- BOT_ONLY 模式（老的极简模式，跟新链路不冲突，不推荐）：见 server.py 顶部 `_BOT_ONLY_ALLOWED` 注释
