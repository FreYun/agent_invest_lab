# bot101 每日运行流程与工具边界

> 当前口径：bot101 的生产 run_id 为 `oos-bot101-daily`。系统每天早上刷新净值曲线，工作日下午 14:30 盘中执行决策。本文覆盖“公共报告如何注入、bot101 自己决策时能调用哪些工具、下单如何在 T 日 NAV 未出时成交”。

## 1. 当前 cron 时序

| 时间 | 任务 | 脚本 / 命令 | 作用 |
|---|---|---|---|
| 08:00 每天 | bot101 净值曲线刷新 | `scripts/refresh-oos-bot101-nav.sh` | 在 fund_nav 入库后，结算待确认订单、重算最近窗口快照、镜像到 `oos_*` 看板表 |
| 08:30 工作日 | 公共宏观/策略研判 | `skill-res1..res5-*` + `mainline-daily` | 生成 res1/2/3/4/5 与日度主线报告 |
| 09:00 工作日 | 行业/主题/技术公共报告 | `skill-res6..res12`、`skill-res14` | 生成行业、主题、指数技术等公共研判 |
| 09:45 工作日 | QC 报告 | `skill-res13-qc-ops` | 质量控制/校验报告 |
| 10:00 / 11:00 / 13:30 / 14:00 / 14:30 工作日 | res14 盘中指数技术刷新 | `res14-intraday.sh` | 盘中更新指数技术研判 |
| 14:30 工作日 | bot101 盘中决策 | `OOS_BOT101_INTRADAY=1 scripts/run-oos-bot101-daily.sh` | 用当天日期作为交易日，读取报告 + 实时行情，执行下单/持有决策 |

要点：
- 14:30 的 bot101 run 使用系统当天作为 `TRADE_DATE`；非交易日由脚本 guard 自动跳过。
- 08:00 的 NAV sync 是净值曲线更新入口，不跑 LLM 决策。
- 14:30 的决策当时通常还没有 T 日基金 NAV；订单先受理，次日 NAV 入库后按 T 日 NAV 定价并回写快照。

## 2. 这轮优化了什么

1. **决策时点从早盘/T+1 改为盘中 T 日 14:30**
   - `run-oos-bot101-daily.sh` 增加 `OOS_BOT101_INTRADAY=1` 模式。
   - 无显式日期时，盘中模式取系统当天，而不是 calendar 最新已收盘日。

2. **新增盘中实时行情注入**
   - `world/src/intraday-market.ts` 从已注入报告中提取宽基与主线相关标的。
   - 调用 `ttjj_data_pit_mcp.market_realtime_quote` 预取实时行情。
   - 注入范围只包括宽基指数和 A 股主线 ETF 代理；跨市场资产不纳入。
   - 联接基金本身没有实时行情时，用同跟踪指数或报告指定载体的场内 ETF 作为盘中代理信号。

3. **订单支持“先接单，后按 T 日 NAV 定价”**
   - `fund_bot_orders` 增加 `pricing_status / pricing_nav_date / priced_at`。
   - 买单：T 日 NAV 缺失时 `pricing_status='awaiting_nav'`，先冻结现金。
   - 卖单：T 日 NAV 缺失时先冻结 `pending_sell_shares`，不立即消耗 lot。
   - NAV 入库后 `settle_pending_fund_orders` 严格读取 `order_date` 的 T 日 NAV，写回 `reference_nav` 后成交。

4. **净值刷新后会回算快照**
   - `refresh-oos-bot101-nav.sh` 从“每天 settle + close”改为三段式：
     1. 先统一 settle 可收口的 pending 单；
     2. 再统一重算窗口内每日快照；
     3. 最后统一镜像到 `oos_*`。
   - 这样 T+1 早上 NAV 入库后，T 日下单动作会写成 T 日 action，T 日净值曲线也会被重算。

5. **报告和数据尽量预注入，减少 bot 重复调用**
   - 账户、持仓、绩效、持仓基金 NAV、五大指数 MA、公共市场报告、盘中行情都在 prompt 里预置。
   - bot101 日常只在需要验证 thesis、查新信息、或执行交易时调用工具。

## 3. 14:30 bot101 决策流程

`scripts/run-oos-bot101-daily.sh` 调 `world/src/oos-daily-driver.ts`，driver 做以下事情：

1. **启动本地 MCP 服务**
   - memory server
   - simworld-data proxy
   - fund-portfolio proxy
   - strategy-server
   - bot101 rust/agent server

2. **系统侧账户前置**
   - pin 本 run 的可买基金池到 `data/buyable/oos-bot101-daily.json`
   - 初始化/校验 bot101 账户
   - 在 bot chat 前 settle 历史 pending 单

3. **读取并注入上下文**
   - 账户 / 持仓 / 绩效 / 已平仓 P&L
   - 持仓基金近 20 日 NAV
   - 五大宽基指数 MA60/120/200 与趋势标签
   - 本 bot 可买基金池摘要
   - 公共报告：`market_context`、`market_mainline_daily`、`mainline_rotation_daily`、`macro_news`
   - 四研判室背景：res1 市场策略、res2 政策分析、res4 国际关系、res5 跨市场联动
   - 盘中实时行情：宽基 + 主线相关 ETF 代理
   - history window 与 belief 校准块
   - bot101 当前 `METHODOLOGY.md`

4. **bot101 执行决策**
   - 先读注入内容，不重复调已预取的数据。
   - 若需要验证持仓消息面或 thesis，调 simworld-data 研究/行情工具。
   - 若需要调整仓位，调 fund-portfolio 下单工具。
   - 每日写 mem0/belief，留下当日判断。

5. **系统侧 close + mirror**
   - bot chat 完成后，driver 调 `close_my_day` 落当日快照。
   - shell 层把订单、动作、持仓、净值、报告状态镜像到 `oos_*` 表。
   - 对 14:30 新下的 awaiting NAV 订单，快照会在次日 08:00 NAV sync 后被回算修正。

## 4. 注入报告明细

bot101 14:30 决策时，不是只读三份报告。当前注入分三层：

| 层级 | 报告 | 来源 | 用途 |
|---|---|---|---|
| 核心市场报告 | `market_context` | market_reports | regime / risk_state / 市场温度 / 风险预算 |
| 核心主线报告 | `market_mainline_daily`，缺失时回退 `market_mainline` | market_reports | 主线识别、主线板块、可投基金映射 |
| 核心轮动报告 | `mainline_rotation_daily`，缺失时回退 `mainline_rotation` | market_reports | 核心/卫星组合骨架、计数器、今日动作 |
| 宏观资讯 | `macro_news` | market_reports | 政策、会议、监管、地缘、汇率、大宗等事件面 |
| 背景研判 | res1 / res2 / res4 / res5 | res_reports / skill cron | 校准风险预算，不覆盖主线与组合骨架 |
| 盘中行情 | 宽基 + 主线 ETF 代理 | `market_realtime_quote` | 14:30 实时强弱、量比、成交额、5/20 日表现 |

使用原则：
- `market_context / market_mainline_daily / mainline_rotation_daily` 是操作性主线结论。
- res1/2/4/5 是背景研判，不直接改写组合骨架。
- 盘中实时行情只作为 T 日执行时的强弱校准，不改变可买池。

## 5. bot101 自己可调用的工具

### 5.1 memory 工具

| 工具 | 用途 | 日常是否需要 |
|---|---|---|
| `mcp__mem0__mem0_search` | 搜索历史记忆、上次 belief、上次执行理由 | 需要时 |
| `mcp__mem0__mem0_add` | 写入当日决策、belief、证伪触发点 | 每日必须写 |

### 5.2 fund-portfolio 工具

bot101 连接的是 fund-portfolio 的 bot-only 端口。proxy 会把 `run_id=oos-bot101-daily` 强制注入，bot 看不到也不能伪造 `run_id`。

| 工具 | 可见性 | 用途 | 注意 |
|---|---|---|---|
| `mcp__fund_portfolio_mcp__portfolio_place_buy_order` | 可见 | 申购基金 | fund_code 必须在本 bot 可买池；T NAV 缺失时进入 `awaiting_nav` 并冻结现金 |
| `mcp__fund_portfolio_mcp__portfolio_place_sell_order` | 可见 | 赎回基金份额 | T NAV 缺失时冻结份额；NAV 入库后按 T NAV 消耗 lot |
| `mcp__fund_portfolio_mcp__portfolio_get_my_history` | 可见 | 查账户/持仓/订单 | 日常已预注入，不建议重复查 |
| `mcp__fund_portfolio_mcp__portfolio_get_my_trades` | 可见 | 查成交/交易历史 | 日常已预注入，不建议重复查 |
| `mcp__fund_portfolio_mcp__portfolio_get_my_performance` | 可见 | 查绩效/区间收益/P&L | 日常已预注入，不建议重复查 |
| `mcp__fund_portfolio_mcp__portfolio_get_buyable_funds` | 可见 | 查完整可买池 | 可买池变化低，通常读 prompt 摘要即可 |
| `mcp__fund_portfolio_mcp__get_fund_detail` | 可见 | 查基金主题、风格、业绩、排名等细节 | 选新载体或替代品时使用 |

bot101 看不到的 fund-portfolio 系统工具：
- `init_fund_account`
- `settle_pending_orders`
- `close_my_day`
- admin / migration / 手工写库类工具

这些由 driver 或 cron 执行，bot101 不负责账户生命周期。

### 5.3 simworld-data 工具

simworld-data proxy 会：
- 从 `tools/list` 隐藏原始申赎接口；
- 在 `tools/call` 强制注入 `simulated_datetime=<TRADE_DATE> 15:00:00`；
- 防止 bot101 自己指定时间造成未来函数。

bot101 常用的 simworld-data 工具类型：

| 工具/类别 | 用途 | 当前约束 |
|---|---|---|
| `mcp__simworld_data__research_search` | 搜索新闻、研报、政策、持仓消息面 | 用于验证 thesis、查利空/催化；不要替代已注入主线报告 |
| `mcp__simworld_data__market_index_quote` | 查指数历史行情/技术数据 | 五大宽基已预注入；额外指数才需要查 |
| `mcp__simworld_data__fund_nav` | 查基金 NAV 历史 | 持仓基金近 20 日已预注入；新基金才需要查 |
| 其它行情/因子/行业工具 | 补充研究新行业、新基金、资金面、宏观因子 | 只在方法论需要且 prompt 未覆盖时调用 |

bot101 看不到/不能调用：
- `fund_subscription_redemption_summary`
- `fund_index_subscription_redemption`

### 5.4 strategy-server 工具

| 工具 | 用途 | 日常是否需要 |
|---|---|---|
| `mcp__strategy_mcp__get_market_report` | 手工读取共享市场报告 | 报告已预注入；缺失或需要复查时用 |
| `mcp__strategy_mcp__get_my_strategy` / `get_active_strategy` | 读取当前 active methodology | system prompt 已注入；通常不用 |
| `mcp__strategy_mcp__update_my_strategy` | 完整替换自己的 methodology | 只有强制复盘判定方法论失效时用 |
| `mcp__strategy_mcp__list_strategies` | 查看共享策略 catalog | 研究策略库时用 |
| `mcp__strategy_mcp__get_strategy` | 读取某个共享策略全文 | 参考其它产品策略时用 |

bot101 不应调用：
- `submit_market_report`：仅 reporter agent 使用。
- `update_my_user / get_my_user`：生产 bot101 默认不暴露。

### 5.5 discovery / 其它

| 工具 | 用途 | 约束 |
|---|---|---|
| `discover_tools` | 发现工具 | 正常不需要；simworld tools 已 probe + whitelist 预激活 |
| raw shell / sqlite | 不属于 bot101 可调用工具 | 仅 driver / 系统脚本使用 |

## 6. bot101 日常决策时的推荐调用顺序

1. 先读 prompt 注入块：账户、持仓、报告、盘中行情、history、methodology。
2. 如果今日动作为 `hold` 且无明显冲击：通常只需少量 research 验证 + `mem0_add`。
3. 如果报告触发新进/剔除/晋升：
   - 用 `get_fund_detail` 或 simworld-data 补查候选基金；
   - 用 `portfolio_place_buy_order` / `portfolio_place_sell_order` 执行；
   - 写 `mem0_add` 记录目标权重、执行理由、belief。
4. 不要重复调用已预注入的账户/绩效/持仓 NAV/五大宽基 MA。
5. 不要自己重跑主线识别；主线和组合骨架以 `market_mainline_daily` 与 `mainline_rotation_daily` 为准。

## 7. 下单与净值确认语义

### 买入

14:30 下买单时：
- 若 T 日 NAV 已存在：订单直接写 `reference_nav`，`pricing_status='priced'`。
- 若 T 日 NAV 不存在：订单写 `pricing_status='awaiting_nav'`，`reference_nav=NULL`，现金冻结到 `cash_in_transit`。

次日 NAV 入库后：
- `settle_pending_fund_orders` 读取 `fund_nav(fund_code, order_date)`；
- 写回 `reference_nav / pricing_nav_date / priced_at`；
- 计算申购费和到账份额；
- 写 T 日 `ADD` action；
- 释放 `cash_in_transit`。

### 卖出

14:30 下卖单时：
- 若 T 日 NAV 已存在：立即按 T NAV 消耗 lot，proceeds 进入 `cash_receivable`，T+1 转 cash。
- 若 T 日 NAV 不存在：先增加 `pending_sell_shares`，不消耗 lot，不写 `REDUCE` action。

次日 NAV 入库后：
- settle 读取 T 日 NAV；
- FIFO 消耗 lot；
- 写 T 日 `REDUCE` action；
- 释放 `pending_sell_shares`；
- 确认订单金额。

### 快照

T 日 14:30 后的即时快照可能还没有 T NAV 定价后的最终成交效果。每天 08:00 NAV sync 会：
1. settle 所有可收口 pending；
2. 重算窗口内 daily snapshots / position snapshots；
3. 镜像到 `oos_bot_daily_snapshots`、`oos_bot_position_snapshots`、`oos_bot_orders`、`oos_bot_actions`。

## 8. 最近 bot101 决策耗时统计（2026-06-29 ~ 2026-07-08）

统计口径：只统计 bot101 自己的 chat 决策段，即 `world/runtime/runs/oos-bot101-daily/<交易日>/bot101/status.json` 里的 `started_at -> finished_at`。这段包含 bot101 读取 prompt、LLM 思考、多轮工具调用、生成 reply 与写 status；不包含 shell 启动、公共报告 prepass、MCP 服务启动、账户 init/settle、close、mirror 和日志 summary。

| 交易日 | 决策开始时间 | 决策完成时间 | bot101 决策耗时 | 状态 | 迭代数 | usage | 工具调用 | 错误 | 订单 |
|---|---|---|---:|---|---:|---:|---:|---:|---:|
| 2026-06-29 | 2026-06-30 08:06:26 | 2026-06-30 08:07:52 | 1m25s | ok | 3 | 398892 | 5 | 0 | 0 |
| 2026-06-30 | 2026-07-01 08:04:43 | 2026-07-01 08:06:05 | 1m21s | ok | 3 | 414662 | 5 | 0 | 0 |
| 2026-07-01 | 2026-07-02 08:06:43 | 2026-07-02 08:11:22 | 4m38s | ok | 10 | 1486209 | 31 | 5 | 8 |
| 2026-07-02 | 2026-07-03 08:08:40 | 2026-07-03 08:09:58 | 1m17s | ok | 2 | 287446 | 2 | 0 | 0 |
| 2026-07-03 | 2026-07-04 20:33:15 | 2026-07-04 20:34:28 | 1m12s | ok | 2 | 280449 | 3 | 0 | 0 |
| 2026-07-06 | 2026-07-07 08:10:02 | 2026-07-07 08:11:20 | 1m18s | ok | 2 | 280980 | 1 | 0 | 0 |
| 2026-07-07 | 2026-07-08 08:06:51 | 2026-07-08 08:07:49 | 0m58s | ok | 2 | 277460 | 1 | 0 | 0 |
| 2026-07-08 | 2026-07-09 08:08:47 | 2026-07-09 08:11:09 | 2m22s | ok | 5 | 342008 | 5 | 0 | 1 |

汇总：
- 样本数：8 个真实完成会话。
- bot101 自己决策平均耗时：1m49s；中位数：1m20s；最短：0m58s；最长：4m38s。
- 常规 hold / 轻决策日通常在 1 到 1.5 分钟；7/8 因新增买入动作与 mem0 检索，耗时升到 2m22s。
- 重换仓日明显更慢：2026-07-01 有 10 轮迭代、31 次工具调用、8 笔订单，bot101 决策段耗时 4m38s。
- 如果要监控 14:30 盘中生产运行，应该同时保留两类指标：`bot101/status.json` 的纯决策耗时，以及 shell 日志的端到端耗时。本文第 8 节只记录前者。

## 9. 仍需注意的边界

- 14:30 决策依赖早上公共报告；除 res14 intraday 外，主线/宏观报告不会在 14:30 自动重新生成。
- 场外基金真实成交仍按基金公司 T 日 NAV；盘中 ETF 行情只是执行参考，不是成交价。
- `refresh-oos-bot101-nav.sh` 是净值曲线最终收敛入口；如果 08:00 任务失败，pending NAV 订单和 T 日快照会延后到下一次刷新才收敛。
- 当前仓库工作区可能还有其它未提交改动；本文只描述 bot101 每日链路，不代表其它实验分支状态。
