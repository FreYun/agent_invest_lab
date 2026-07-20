# `fund_bot_orders` 表说明

> 数据库：`data/fund.db`（SQLite，核心库）
> 文档生成日期：2026-06-16 · 表内当前行数 **3397**（buy 1897 / sell 1500）· 订单日期跨度 **2023-01-03 ~ 2026-06-11**

## 1. 这是什么

`fund_bot_orders` 是**模拟交易系统的订单流水表**——记录每个 bot 在历史回测中每一笔基金申购/赎回的下单与确认明细。它是「下单意图 → T+1 确认成交」全过程的落地，持仓（份额/成本）和净值曲线都由这张表的确认结果推算。

在数据流中的位置：

```
bot 决策 → fund-portfolio-mcp 下单 → 写入 fund_bot_orders (status=pending)
                                          │  系统侧每日结算 (cli_tools.py)
                                          ▼
                                  T+1 用 PIT 收盘净值确认 → status=confirmed
                                  → 更新持仓 / NAV / 净值曲线
```

写入方：`fund-portfolio-mcp/`（bot 下单 + 系统侧结算）。**不是** `scripts/fund_md_to_db.py`（那条链路写的是 allocation_run / selection_run / bot_reviews / bot_actions 等元数据表）。

## 2. 字段总表

| 列名 | 类型 | 可空 | 含义 |
|------|------|------|------|
| `order_id` | INTEGER | PK | 主键，自增。订单唯一标识 |
| `review_id` | INTEGER | 是 | 预留的复盘关联字段，**当前全表为 NULL，实际未启用** |
| `bot_id` | TEXT | 否 | 下单的 bot，如 `bot19`、`bot101` |
| `fund_code` | TEXT | 否 | 基金代码，6 位（场外基金/ETF 联接），如 `020829` |
| `fund_name` | TEXT | 是 | 基金名称，如 `东财北证50指数发起式C` |
| `order_type` | TEXT | 否 | 订单方向：`buy`（申购）/ `sell`（赎回） |
| `order_date` | TEXT | 否 | 下单日（T 日），`YYYY-MM-DD` |
| `confirm_date` | TEXT | 是 | 确认日（A 股基金 T+1），pending 时为空 |
| `order_amount` | REAL | 是 | **语义随方向不同**：buy=申购金额（元）；sell=赎回份额（份）。详见 §3 |
| `reference_nav` | REAL | 是 | 下单时参考净值。回测中等于 `confirm_nav`（PIT 收盘价） |
| `confirm_nav` | REAL | 是 | 确认成交净值（T+1 当日 PIT 收盘净值），pending 时为空 |
| `confirmed_shares` | REAL | 是 | 确认份额（份） |
| `confirmed_amount` | REAL | 是 | 确认金额（元）。**仅卖出填写**=赎回到账金额；买入恒为空 |
| `fee` | REAL | 是 | 手续费（元）。申购费/赎回费，可为 0 也可非 0 |
| `action_reason` | TEXT | 是 | bot 给出的下单理由（自然语言，可较长，含触发器/信号依据） |
| `status` | TEXT | 是 | 订单状态：`pending`（已下单待确认）/ `confirmed`（已确认成交）。默认 `pending` |
| `created_at` | TEXT | 是 | 记录写入时间戳，默认 `datetime('now')` |
| `order_run_id` | TEXT | 是 | 下单所属回测 run，如 `dash-2026-06-16T02-26-32`。用于多 run 隔离 |
| `settle_run_id` | TEXT | 是 | 结算所属回测 run。通常与 `order_run_id` 相同 |

## 3. 关键语义（容易踩坑，务必看）

1. **`order_amount` 在买卖中含义不同（语义重载）**
   - **买入**：`order_amount` = 申购投入**金额（元）**，由 bot 决定。
   - **卖出**：`order_amount` = 赎回**份额（份）**，数值与 `confirmed_shares` 完全相等。
   - ⚠️ 卖出行里 `order_amount` 不是金额。要看卖出到账金额请用 `confirmed_amount`。

2. **金额/份额勾稽公式**
   - 买入：`confirmed_shares = (order_amount − fee) / confirm_nav`（手续费从申购金额中扣除）
   - 卖出：`confirmed_amount = confirmed_shares × confirm_nav − fee`（赎回费从到账金额中扣除）

3. **`confirmed_amount` 只在卖出填写**，买入恒为 NULL（买入投入额就是 `order_amount`）。

4. **`confirm_nav` 是 PIT（时点）收盘净值**，T+1 确认。`reference_nav` 与之相等（已确认行 3390/3390 全等），因为回测按收盘价撮合，无盘中偏差。

5. **`review_id` 当前未启用**，全表 NULL。需要把订单关联到某次复盘时，靠 `order_run_id` + `order_date` + `bot_id` 拼接，而非 `review_id`。

6. **run 隔离**：`order_run_id` / `settle_run_id` 决定订单属于哪一次回测。统计某个 run 的成交时务必带上 run 过滤，否则会混入历史 run 的订单。

7. **`status='pending'`** 表示当日下单、尚未到 T+1 结算（confirm_date/confirm_nav/confirmed_shares 均为空）。全表仅 7 行 pending，其余均已 confirmed。

## 4. 索引

| 索引名 | 字段 | 用途 |
|--------|------|------|
| `idx_fund_orders_bot_status` | `(bot_id, status)` | 按 bot 查未结算/已结算订单 |
| `idx_fund_orders_bot_date` | `(bot_id, order_date)` | 按 bot 查某交易日订单 |
| `idx_fund_orders_order_run` | `(bot_id, order_date, order_run_id)` | 按 run 精确定位某日下单 |
| `idx_fund_orders_settle_run` | `(bot_id, settle_run_id)` | 按 run 查结算 |

## 5. 实例数据

> 取自真实表数据。`action_reason` 过长，表中省略（见 §6 单独示例）。卖出行的 `order_amount` 即赎回份额。

| order_id | bot_id | fund_code | fund_name | order_type | order_date | confirm_date | order_amount | confirm_nav | confirmed_shares | confirmed_amount | fee | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3475 | bot19 | 020829 | 东财北证50指数发起式C | buy | 2025-12-15 | 2025-12-16 | 400000.00 | 1.8134 | 220580.13 | — | 0.00 | confirmed |
| 3447 | bot101 | 015336 | 嘉实中证芯片产业指数发起式A | buy | 2026-05-06 | 2026-05-07 | 140000.00 | 1.6667 | 83914.41 | — | 139.86 | confirmed |
| 3474 | bot20 | 005693 | 广发中证军工ETF联接C | sell | 2026-05-18 | 2026-05-19 | 418897.19 | 0.943393 | 418897.19 | 395184.68 | 0.00 | confirmed |
| 3457 | bot18 | 020829 | 东财北证50指数发起式C | sell | 2026-06-01 | 2026-06-02 | 450160.05 | 1.5915 | 450160.05 | 714776.33 | 1653.39 | confirmed |
| 451 | bot11 | 510300 | （沪深300ETF） | buy | 2026-05-14 | — | 100000.00 | — | — | — | — | pending |

**逐行读法**

- **3475（买入·零费）**：bot19 申购 40 万元，T+1 以净值 1.8134 确认，得 220580.13 份。`confirmed_shares × confirm_nav ≈ 400000`。
- **3447（买入·有费）**：bot101 申购 14 万元，扣申购费 139.86 元后按 1.6667 确认 → `(140000−139.86)/1.6667 ≈ 83914.41` 份。
- **3474（卖出·零费）**：bot20 赎回 418897.19 份（注意 order_amount 这里是份额），按 0.943393 确认，到账 `confirmed_amount = 395184.68` 元。
- **3457（卖出·有费）**：bot18 赎回 450160.05 份，按 1.5915 计 716429.7 元，扣赎回费 1653.39 元 → 到账 714776.33 元。
- **451（pending）**：bot11 下单申购 10 万元，尚未到 T+1，confirm_* 与 fee 全空。

**`action_reason` 完整示例**（order_id 3474）：

> V2 卖出信号触发：1) 中证军工指数 7 日 -7.23% 至 14075 点，跌破 MA60 临界位（估计 14200-14300），趋势破位；2) 虽 VIX 从 20.48 回落至 18.82<20，但 V2 框架中趋势优先级高于风险偏好；3) 历史案例 03-23 显示破位后清仓可规避进一步回撤。执行清仓至 0%。

## 6. 常用查询示例

```sql
-- 某 bot 在某 run 的全部成交（按时间）
SELECT order_date, order_type, fund_name, order_amount, confirm_nav, confirmed_shares, confirmed_amount, fee
FROM fund_bot_orders
WHERE bot_id='bot101' AND order_run_id='dash-2026-06-16T02-26-32' AND status='confirmed'
ORDER BY order_date, order_id;

-- 卖出实际到账金额汇总（confirmed_amount 才是金额）
SELECT bot_id, COUNT(*) AS sell_cnt, ROUND(SUM(confirmed_amount),2) AS total_redeemed
FROM fund_bot_orders
WHERE order_type='sell' AND status='confirmed'
GROUP BY bot_id ORDER BY total_redeemed DESC;

-- 买入投入金额汇总（buy 用 order_amount）
SELECT bot_id, ROUND(SUM(order_amount),2) AS total_invested
FROM fund_bot_orders
WHERE order_type='buy' AND status='confirmed'
GROUP BY bot_id ORDER BY total_invested DESC;

-- 待结算订单
SELECT order_id, bot_id, fund_code, order_type, order_date, order_amount
FROM fund_bot_orders WHERE status='pending';
```

## 7. 待确认字段和业务口径 → 我方答复

> 本节对应对接方迁移文档的「第 9 节」(原始截图 `docs/咚咚图片_20260618134216.png`)。
> 对接方系统口径偏券商/资管核心(客户号、组合号、子账户、`C_BUSINTYPE` 业务码),列出 10 项「单看 `fund_bot_orders` 一表无法确定、需我方业务/研究侧补充」的待确认项。
> 下方先**忠实转录**对接方原始清单,再给出**我方逐条答复**(取数截至 2026-06-18,数据库 `data/fund.db`)。

### 7.1 对接方原始清单(转录)

| 待确认项 | 为什么需要 | 对接方建议提供方式 |
|---|---|---|
| `bot_id` 对应哪个员工号/客户号 | 我方(对接方)须按客户号和组合号落库 | 提供 `bot_id`, `employee_no/customer_no`, `sub_account_no`, `portfolio_name` 映射表 |
| 一个 bot 是否只对应一个组合 | 决定组合号生成和数据隔离 | 明确一对一或一对多规则 |
| 初始现金/初始本金 | 回放组合净值和现金仓位需要分母 | 提供每个 bot/run 的初始资产 |
| 是否存在现金仓位 | 持仓比例合计小于 100% 时需要解释 | 提供现金处理规则 |
| 业务码最终映射 | `buy/sell` 需要映射对接方 `C_BUSINTYPE` | 确认申购/赎回枚举(当前推断买卖常用 22/24) |
| 手续费是否全部体现在 `fee` | 成本和收益计算依赖费用口径 | 明确申购费、赎回费是否都在 `fee` |
| 是否允许导入 `pending` | pending 没有确认净值和份额 | 建议历史迁移不导 pending |
| 迁移目标日期范围 | 决定净值回放区间 | 提供开始日期、结束日期、目标 run |
| 净值来源 | 回放每日资产需要历史净值 | 确认使用对接方基金净值表还是研究 PIT 净值 |
| 组合净值初始值 | 常见为 1.0000,但需确认 | 每个组合提供初始净值或统一口径 |

### 7.2 我方逐条答复

| # | 待确认项 | 状态 | 我方答复 |
|---|---|---|---|
| 1 | `bot_id` ↔ 员工号/客户号 | ⚠ 需业务侧 | 我方为 LLM Agent 回测,**无真实员工/客户**。当前共 **25 个 bot**(`bot1`~`bot20` 单基金择时、`bot101`~`bot103` 多基金配置、`multi_asset_allocation_bot`/`bot_multi` 多资产)。建议直接用 `bot_id` 作为客户/组合标识,或由业务侧给定合成映射规则。 |
| 2 | 一 bot 是否对一组合 | ✅ 已坐实 | **一对多**。组合唯一键是 **`(bot_id, order_run_id)`**,不是 `bot_id`——同一 bot 在不同回测 run 下是互相隔离的独立组合(订单层 258 个 run,净值层 282 个 run)。对接方「组合号」应由 `(bot_id, run_id)` 生成。`fund_bot_accounts` 仅保留每个 bot **最近一个 run** 的现金状态(25 行)。 |
| 3 | 初始现金/初始本金 | ✅ 已坐实 | 统一 **1,000,000 元**(25 个账户 `initial_capital` 恒为 100 万,无例外)。逐日快照 `fund_bot_daily_snapshots.initial_capital` 亦携带此分母。 |
| 4 | 是否存在现金仓位 | ✅ 已坐实 | **存在,现金是显式第四类资产**。`fund_bot_accounts.cash` 即现金;每日快照含 `equity_weight`/`bond_weight`/`gold_weight`/**`cash_weight`** 四类权重,合计 100%。持仓基金只占股/债/金,**残差即现金**。持仓表 `fund_bot_holdings` 不含现金行,现金只落在 `cash` 列与快照 `cash_weight`。 |
| 5 | 业务码 `buy/sell` → `C_BUSINTYPE` | ✅ 已坐实 | `order_type` **仅两枚:`buy`=申购、`sell`=赎回**,无其他类型。即标准开放式基金申购/赎回,`22/24` 映射由对接方按其枚举确认即可。 |
| 6 | 手续费是否全在 `fee` | ✅ 已坐实 | **是**。申购费与赎回费**都落在单列 `fee`(元)**。买入 `confirmed_shares=(order_amount−fee)/confirm_nav`;卖出 `confirmed_amount=shares×nav−fee`。`fee` 可为 0(免费份额),无其他费用列。 |
| 7 | 是否导入 `pending` | ✅ 建议不导 | **建议不导**。当前仅 **6 行** pending(`bot11` 旧 run 尾 2 笔 + `multi_asset_allocation_bot` 3 笔),均无 `confirm_nav`/`confirmed_shares`,是未结算尾巴。历史迁移只取 `status='confirmed'`。 |
| 8 | 迁移目标日期范围 | ✅ 已坐实 | 全表 `order_date` 跨 **2023-01-03 ~ 2026-06-15**(随回测推进会更新)。但区间是 **per-run** 的——**迁移须先定目标 `order_run_id`**,再取该 run 实际起止,**勿跨 run 混取**。 |
| 9 | 净值来源 | ✅ 已坐实 | `confirm_nav` = **PIT(时点)收盘净值**,T+1 确认,源自 ttjj-data-pit,按收盘价撮合(`reference_nav≡confirm_nav`)。**强烈建议对接方不要从订单流水自行回放组合净值**,直接用我方 `fund_bot_daily_snapshots.net_value`(已逐日算好的组合净值 + 四类权重 + 现金),订单表仅用于核对成交明细。 |
| 10 | 组合净值初始值 | ✅ 已坐实 | 统一 **1.0000**(`net_value = total_value/initial_capital`,首日为 1)。每日快照 `net_value` 即组合单位净值,对接方无需另行设定。 |

### 7.3 给迁移工程的关键建议

1. **净值回放优先用 `fund_bot_daily_snapshots`,而非订单流水。** 它已逐日给出 `net_value`(组合净值)、`total_value`、`cash`、四类权重(equity/bond/gold/cash)、`cumulative_return_pct`、`max_drawdown_pct`,共 **40154 行 / 282 run**,是组合层回放的真源;`fund_bot_orders` 只承担成交明细核对。
2. **一切按 run 归组。** 组合 = `(bot_id, run_id)`。注意 `order_run_id` 与 `settle_run_id` 有 **155 行不一致**(跨改造点续跑/重结算所致):**按 `order_run_id` 归组组合,用 `confirm_date` 取成交日**。
3. **业务码/费用/净值口径均已统一,无歧义项;真正待业务侧拍板的只有第 1 项(bot↔客户号映射规则)。** 其余 9 项均可按本节直接落库。
