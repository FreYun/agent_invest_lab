# 新数据库 agent 结果表命名规范

> 生成日期：2026-07-15  
> 范围：`.openclaw/data` 与 `agent_invest_lab/data` 两个项目的数据目录。  
> 最新口径：只写回 agent 自己产出的投资结果；新库按业务拆成 **基金库** 和 **投顾库**。

## 1. 总体结论

现在不再按项目完整迁移，也不全量合并两个 `fund.db`。

推荐新库结构：

| 新库 | 来源 | 写回内容 |
|---|---|---|
| 基金库 | `.openclaw/data/fund.db` + `agent_invest_lab/data/fund.db` | `.openclaw` 基金 agent 结果 + `agent_invest_lab` bot101 每日 OOS 结果 |
| 投顾库 | `.openclaw/data/tougu.db` | `.openclaw` 投顾 agent 结果 |

当前需要建表合计 **28 张**：

- 基金库：18 张。
  - `.openclaw/data/fund.db`：14 张。
  - `agent_invest_lab/data/fund.db`：4 张，只保留 bot101 每天跑的 OOS/live 核心结果。
- 投顾库：10 张。

## 2. 为什么两个 fund.db 可以这样合并

两个 `fund.db` 不能做全量直接合并，因为同名表 schema 和主键已经分叉：

- `agent_invest_lab` 的 `fund_bot_*` 表比 `.openclaw` 多 `run_id`、`order_run_id`、`pricing_status`、`cash_receivable` 等字段。
- `fund_invest_bot_orders.order_id`、`fund_invest_bot_actions.action_id`、`fund_invest_bot_holdings.holding_id` 等自增主键大量冲突。
- `bot_id` 也有重叠，如 `bot1`、`bot2`、`bot4`、`bot5`、`bot6`、`bot7`、`bot11`、`bot12`。

但如果 `agent_invest_lab` 只写回 bot101 每日结果，就不需要合并 lab 的历史回测 `fund_bot_*` 表。bot101 日跑结果落在 `oos_*` 表，和 `.openclaw` 的 `fund_bot_*` 表天然不冲突，所以可以放入同一个基金业务库。

## 3. 建表规则

每张新表的 schema 直接复制原表：

- 原表字段完整保留。
- 原表字段名完整保留。
- 原表字段类型完整保留。
- 原表主键、唯一约束、索引建议同步复制。
- 原表数据按新表名导入。

新表名统一按投资业务前缀命名：基金投资表用 `fund_invest_`，投顾投资表用 `strategy_invest_`。bot101 的 OOS 表属于基金投资结果，也使用 `fund_invest_` 前缀。


## 3.1 短命名规则

| 来源 | 新表名前缀 | 说明 |
|---|---|---|
| `.openclaw/data/fund.db` | `fund_invest_` | 基金投资 agent 结果 |
| `agent_invest_lab/data/fund.db` bot101 OOS | `fund_invest_` | bot101 每日基金投资结果 |
| `.openclaw/data/tougu.db` | `strategy_invest_` | 投顾投资 agent 结果 |

命名原则：

- 基金投资表统一使用 `fund_invest_`。
- 投顾投资表统一使用 `strategy_invest_`。
- 不再使用 `openclaw_`、`agent_invest_lab_` 这种长项目前缀。
- 同一个业务库内不冲突即可。
- 原表结构不变，只改新表名。
- `daily_snapshots`、`position_snapshots` 这类词保留，因为它们表达表粒度。

## 4. 基金库写回表

### 4.1 `.openclaw/data/fund.db` 基金 agent 结果表

这些表是 `.openclaw` 基金业务 agent 产生的订单、持仓、快照、复盘和运行结果。

| 原表名 | 推荐新表名 | 说明 |
|---|---|---|
| `fund_allocation_runs` | `fund_invest_alloc_runs` | agent 资产配置运行结果 |
| `fund_bot_accounts` | `fund_invest_bot_accounts` | bot 账户状态 |
| `fund_bot_actions` | `fund_invest_bot_actions` | bot 决策动作 |
| `fund_bot_daily_snapshots` | `fund_invest_bot_daily_snapshots` | bot 每日账户快照 |
| `fund_bot_holdings` | `fund_invest_bot_holdings` | bot 当前持仓 |
| `fund_bot_orders` | `fund_invest_bot_orders` | bot 订单流水 |
| `fund_bot_position_snapshots` | `fund_invest_bot_position_snapshots` | bot 持仓每日快照 |
| `fund_bot_reviews` | `fund_invest_bot_reviews` | bot 复盘结果 |
| `fund_capability_circle` | `fund_invest_bot_capability` | bot 能力圈结果/配置 |
| `fund_capability_runs` | `fund_invest_bot_capability_runs` | bot 能力评估运行结果 |
| `fund_paradigm_runs` | `fund_invest_bot_paradigm_runs` | bot 范式切换/运行结果 |
| `fund_repair_log` | `fund_invest_bot_repair_log` | agent 结果修复记录 |
| `fund_selection_runs` | `fund_invest_select_runs` | agent 基金选择运行结果 |
| `fund_system_runs` | `fund_invest_system_runs` | agent 系统运行记录 |

不写回：`fund_info`、`fund_nav`、`fund_performance`、`fund_style`、`fund_industry`、`fund_top_stocks`、`fund_fact_pack_daily`。这些是公共输入或事实包，不是 agent 最终结果。

### 4.2 `agent_invest_lab/data/fund.db` bot101 每日 OOS 核心结果表

这些表就是 bot101 当前每天跑出来的 live/OOS 核心结果。当前数据特征：

- `live_run_id = 'oos-bot101-daily'`
- `bot_id = 'bot101'`
- 日期范围：2026-06-11 到 2026-07-15

| 原表名 | 推荐新表名 | 写回过滤条件 | 说明 |
|---|---|---|---|
| `oos_bot_daily_snapshots` | `fund_invest_bot101_daily_snapshots` | `live_run_id='oos-bot101-daily' and bot_id='bot101'` | bot101 每日账户快照，含净值、现金、仓位权重 |
| `oos_bot_position_snapshots` | `fund_invest_bot101_position_snapshots` | `live_run_id='oos-bot101-daily' and bot_id='bot101'` | bot101 每日持仓快照 |
| `oos_bot_orders` | `fund_invest_bot101_orders` | `live_run_id='oos-bot101-daily' and bot_id='bot101'` | bot101 OOS 订单 |
| `oos_bot_actions` | `fund_invest_bot101_actions` | `live_run_id='oos-bot101-daily' and bot_id='bot101'` | bot101 OOS 决策动作 |

不写回：`oos_market_report_status`、`market_reports`。这次只要 bot101 OOS 的账户、持仓、订单、动作四类核心结果。

不写回 `agent_invest_lab` 的历史回测表：`fund_bot_accounts`、`fund_bot_orders`、`fund_bot_actions`、`fund_bot_daily_snapshots`、`fund_bot_position_snapshots`、`fund_bot_performance` 等。它们包含大量历史 run，不是“bot101 当前每天跑”的最小结果。

也不写回公共输入表：`fund_info`、`fund_nav`、`fund_performance`、`flow_*`、`industry_*`、`news_item`、`real_user_*`、`market.db` 中的 `daily`、`index_daily`、`board_trend_daily`。

## 5. 投顾库写回表

来源：`.openclaw/data/tougu.db`。

| 原表名 | 推荐新表名 | 说明 |
|---|---|---|
| `allocation_runs` | `strategy_invest_alloc_runs` | agent 资产配置运行结果 |
| `bot_accounts` | `strategy_invest_bot_accounts` | bot 账户状态 |
| `bot_daily_snapshots` | `strategy_invest_bot_daily_snapshots` | bot 每日账户快照 |
| `bot_holdings` | `strategy_invest_bot_holdings` | bot 当前持仓 |
| `bot_pending_orders` | `strategy_invest_bot_pending_orders` | bot 待结算订单 |
| `bot_position_snapshots` | `strategy_invest_bot_position_snapshots` | bot 持仓每日快照 |
| `bot_rebalance_actions` | `strategy_invest_bot_rebalance_actions` | bot 调仓动作 |
| `bot_reviews` | `strategy_invest_bot_reviews` | bot 复盘结果 |
| `portfolio_plans` | `strategy_invest_portfolio_plans` | agent 组合计划 |
| `system_runs` | `strategy_invest_system_runs` | agent 系统运行记录 |

不写回：`tougu_info`、`tougu_nav`、`tougu_performance`、`tougu_portfolio`、`tougu_equity_analysis`、`tougu_products_legacy`、`tougu_product_daily_metrics_legacy`。这些是投顾产品公共资料、净值、组合或历史公共数据。

## 6. 执行建议

1. 新建基金业务库。
2. 在基金库里建 `.openclaw/data/fund.db` 的 14 张 agent 结果表。
3. 在基金库里建 `agent_invest_lab/data/fund.db` 的 4 张 bot101 OOS 核心结果表。
4. 新建投顾业务库。
5. 在投顾库里建 `.openclaw/data/tougu.db` 的 10 张 agent 结果表。
6. 所有公共输入表暂不写回。新系统需要公共行情/净值时，建议从公共数据源或原始数据层另行读取。

每张表迁移后校验：

- 新旧表字段数量一致。
- 新旧表字段名、字段类型一致。
- 新旧表主键字段一致。
- 新旧表行数一致，或者符合过滤条件后的行数一致。
- 关键表抽样数据一致。

## 7. 关键判断

这次“合并”不是把两个 `fund.db` 的同名表硬合成一张，而是按业务库归并：

- `.openclaw` 基金 agent 历史结果进入基金库。
- `agent_invest_lab` bot101 每日 OOS 核心结果也进入基金库。
- `.openclaw` 投顾 agent 结果进入投顾库。

这样刚好形成两个业务数据库：**基金库** 和 **投顾库**。风险比全量合并两个 `fund.db` 小得多，也符合现在只需要写回 bot101 日跑结果的目标。

## 8. 写入链路和代码位置

### 8.1 总体写入模式

这几个库的 agent 结果不是都由 bot 直接写数据库。当前主要有两种模式：

- `.openclaw` 基金/投顾：bot 先写 markdown 文件，cron 脚本校验后调用 admin MCP 写工具入库。
- `agent_invest_lab` bot101 OOS：world 先写 live 执行账本 `fund_bot_*`，再由 mirror 脚本把 bot101 当日结果同步到 `oos_*` 四张结果表。

所以后面重构写回程序时，不能只改 bot prompt；真正需要改表名的位置主要在 daily 脚本、MD 解析脚本、MCP 写工具和 OOS mirror SQL。

### 8.2 基金库：`.openclaw/data/fund.db`

日跑总入口是 `/home/rooot/.openclaw/scripts/fund-daily-refresh.py`，目标库是 `/home/rooot/.openclaw/data/fund.db`。它按下面顺序写结果表：

| 写入步骤 | 主要表 | 调度/解析代码 | 真正写表位置 |
|---|---|---|---|
| Phase A 执行记录 | `fund_system_runs` | `/home/rooot/.openclaw/scripts/fund-phase-a.py`，由 `fund-daily-refresh.py` 调用 | `/home/rooot/MCP/fund-portfolio-mcp/server.py` 的 `save_system_run`；`fund-phase-a.py` 也有本地 `write_system_run` |
| T+1 在途单收口 | `fund_bot_orders`、`fund_bot_actions`、`fund_bot_holdings`、`fund_bot_accounts` | `fund-daily-refresh.py` 的 `run_phase_settle()` | `/home/rooot/MCP/fund-portfolio-mcp/server.py` 的 `settle_pending_fund_orders` |
| 范式选择 | `fund_paradigm_runs` | `fund-daily-refresh.py` 的 `run_phase_b1()`；手动入口 `/home/rooot/.openclaw/scripts/fund-phase-b1.py` | `/home/rooot/MCP/fund-portfolio-mcp/server.py` 的 `select_all_fund_paradigms` |
| 市场判断落库 | `fund_allocation_runs` | `/home/rooot/.openclaw/scripts/fund_md_to_db.py` 的 `process_bot()` 解析 `市场环境判断.md` | `/home/rooot/MCP/fund-portfolio-mcp/server.py` 的 `save_allocation_run` |
| 选基结果落库 | `fund_selection_runs` | `fund_md_to_db.py` 的 `process_bot()` 解析 `个性化基金选择.md` | `/home/rooot/MCP/fund-portfolio-mcp/server.py` 的 `save_selection_run` |
| 巡检、调仓、挂单 | `fund_bot_reviews`、`fund_bot_orders`、`fund_bot_holdings` | `fund_md_to_db.py` 的 `process_bot()` 解析 `基金巡检记录.md` + `当前基金持仓.md` | `/home/rooot/MCP/fund-portfolio-mcp/server.py` 的 `apply_fund_review_and_rebalance` |
| 每日收益快照 | `fund_bot_daily_snapshots`、`fund_bot_position_snapshots`、`fund_bot_holdings` | `/home/rooot/.openclaw/scripts/fund-phase-d.py`，由 `fund-daily-refresh.py` 的 `run_phase_d()` 调用 | `/home/rooot/MCP/fund-portfolio-mcp/server.py` 的 `record_fund_snapshot` / `record_all_fund_snapshots` |
| 初始建仓 | `fund_bot_accounts`、`fund_bot_orders`、`fund_bot_holdings` | `/home/rooot/.openclaw/scripts/fund-bootstrap.py` | 本脚本直连 SQLite 写入，不是每日 cron 主链路 |
| 能力圈声明 | `fund_capability_circle` | `/home/rooot/.openclaw/scripts/update-capability-circle.py` | `/home/rooot/MCP/fund-portfolio-mcp/server.py` 的 `save_capability_circle` |
| 能力评估版本 | `fund_capability_runs` | `/home/rooot/.openclaw/scripts/fund-capability-bootstrap.py` | 本脚本直连 SQLite 写入 |
| 市场观点修复/辅助链路 | `fund_repair_log`、`fund_allocation_runs` | `/home/rooot/.openclaw/scripts/fund-market-view-refresh-v2.py` | 本脚本直连 SQLite 写入；属于辅助/历史入口，不覆盖完整日跑链路 |

基金主链路的关键点：

- bot 不应该直接调用写库工具；`fund-daily-refresh.py` 唤起 bot 后等待 5 份基金 MD 写出。
- `fund_md_to_db.process_bot()` 是基金 MD 到 DB 的核心转换入口。
- `apply_fund_review_and_rebalance` 当日主要写巡检和 pending 在途单；真实现金、份额和成交动作由下一轮 `settle_pending_fund_orders` 收口。
- `record_all_fund_snapshots` 是基金账户/持仓快照的统一计算入口。

### 8.3 基金库：`agent_invest_lab/data/fund.db` bot101 OOS

bot101 当前每天跑的 OOS 结果入口是 `/home/rooot/agent_invest_lab/scripts/run-oos-bot101-daily.sh`，目标库是 `/home/rooot/agent_invest_lab/data/fund.db`。

| 写入步骤 | 主要表 | 代码位置 | 说明 |
|---|---|---|---|
| 建 OOS 表 | `oos_bot_daily_snapshots`、`oos_bot_position_snapshots`、`oos_bot_orders`、`oos_bot_actions` | `/home/rooot/agent_invest_lab/scripts/sql/oos_bot101_daily.sql` | 只迁移前四张；`oos_market_report_status` 不在本次写回范围 |
| 单日 bot101 决策 | live `fund_bot_*` 执行账本 | `/home/rooot/agent_invest_lab/world/src/oos-daily-driver.ts`，由 `run-oos-bot101-daily.sh` 调用 | 固定 `bots: ['bot101']`，默认 `run_id=oos-bot101-daily` |
| OOS 结果镜像 | 四张 `oos_bot_*` 表 | `/home/rooot/agent_invest_lab/scripts/oos-bot101-mirror.lib.sh` 的 `mirror_oos_results_for_date()` | 对每个交易日先 `DELETE` 再 `INSERT`，从 live `fund_bot_*` 复制到 `oos_*`，幂等可重跑 |
| 日跑入口 | 四张 `oos_bot_*` 表 | `/home/rooot/agent_invest_lab/scripts/run-oos-bot101-daily.sh` | 跑完 driver 后调用 `mirror_oos_results` |
| 净值刷新入口 | 四张 `oos_bot_*` 表 | `/home/rooot/agent_invest_lab/scripts/refresh-oos-bot101-nav.sh` | 复用同一个 mirror lib，用于刷新估值/快照 |

bot101 每日思考内容和调仓理由主要落在：

- `oos_bot_actions.reason`：最重要，来自 live `fund_bot_actions.reason`。
- `oos_bot_orders.action_reason`：订单层面的下单理由，来自 live `fund_bot_orders.action_reason`。

新库写回四张表时继续使用过滤条件：

```sql
live_run_id = 'oos-bot101-daily'
AND bot_id = 'bot101'
```

### 8.4 投顾库：`.openclaw/data/tougu.db`

投顾日跑总入口是 `/home/rooot/.openclaw/scripts/tougu-daily-refresh.py`，目标库是 `/home/rooot/.openclaw/data/tougu.db`。它现在拆成两个子命令：

- `tougu-daily-refresh.py data`：只刷新公共数据并写 `system_runs`。
- `tougu-daily-refresh.py bots`：跑 Phase B-1、T+1 收口、bot 巡检、快照和 MD 反写。

| 写入步骤 | 主要表 | 调度/解析代码 | 真正写表位置 |
|---|---|---|---|
| Phase A 执行记录 | `system_runs` | `tougu-daily-refresh.py` 的 `phase_a_run()` | `/home/rooot/MCP/tougu-portfolio-mcp/server.py` 的 `save_system_run` |
| T+1 挂单收口 | `bot_pending_orders`、`bot_rebalance_actions`、`bot_holdings`、`bot_accounts` | `tougu-daily-refresh.py` 的 `phase_settle()` | `/home/rooot/MCP/tougu-portfolio-mcp/server.py` 的 `settle_pending_orders` |
| 市场判断落库 | `allocation_runs` | `/home/rooot/.openclaw/scripts/tougu_md_to_db.py` 的 `process_bot()` 解析 `市场环境判断.md` | `/home/rooot/MCP/tougu-portfolio-mcp/server.py` 的 `save_allocation_run` |
| 组合计划落库 | `portfolio_plans` | `tougu_md_to_db.py` 的 `process_bot()` 解析 `tougu/个性化投顾产品选择.md` | `/home/rooot/MCP/tougu-portfolio-mcp/server.py` 的 `save_portfolio_plan` |
| 巡检和本轮挂单 | `bot_reviews`、`bot_pending_orders` | `tougu_md_to_db.py` 的 `process_bot()` 解析 `tougu/投顾巡检记录.md` + `tougu/当前投顾持仓.md` | `/home/rooot/MCP/tougu-portfolio-mcp/server.py` 的 `apply_review_and_rebalance` |
| 每日账户快照 | `bot_daily_snapshots`、`bot_holdings` | `tougu-daily-refresh.py` 的 `phase_d()` 调 `/home/rooot/.openclaw/scripts/tougu_phase_d.py` | 当前生产代码为 `tougu_phase_d.snapshot_bot()` 本地直写 |
| 历史产品级快照 | `bot_position_snapshots` | 旧链路/文档中的 `record_daily_snapshot` | `/home/rooot/MCP/tougu-portfolio-mcp/server.py` 的 `record_daily_snapshot` 会写 `bot_daily_snapshots` + `bot_position_snapshots`；但当前 `tougu_phase_d.py` 没有继续写 `bot_position_snapshots` |
| 中枢同步 | `bot_accounts.central_allocation_json` | `/home/rooot/.openclaw/scripts/tougu-central-sync.py` | 本脚本直连 SQLite 更新 `bot_accounts` |

投顾主链路的关键点：

- bot 只写 5 份投顾 MD；`tougu_md_to_db.process_bot()` 是投顾 MD 到 DB 的核心转换入口。
- `apply_review_and_rebalance` 当日只写巡检和 pending 挂单，不立即改真实持仓和现金。
- 下一交易日 `settle_pending_orders` 才按净值成交，并写 `bot_rebalance_actions`、更新 `bot_holdings` 和 `bot_accounts.cash`。
- 当前生产 Phase D 用 `tougu_phase_d.snapshot_bot()` 写 `bot_daily_snapshots` 并同步 `bot_holdings` 现态；`bot_position_snapshots` 是历史结果表，若新系统还要继续生成产品级每日快照，需要把 `record_daily_snapshot` 的产品级写入逻辑迁过来，或在 `tougu_phase_d.py` 中补同等逻辑。

