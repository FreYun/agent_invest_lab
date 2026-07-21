# 盘中实时跑（live intraday run）设计

> 日期：2026-07-21　分支：feat/world-system
> 目标：把看板上「合格」的历史回测 run 转成每日盘中实盘运行，接实时信息、无未来函数泄漏，且不污染宝贵的历史回测数据。

## 1. 背景与目标

现状：`world/` 是日度滚动回测系统，bot 在历史 A 股行情上「逐日交易」，simworld-proxy 注入模拟日期、PIT 截断未来数据以消除未来函数。已把 45 个合格 run（单指数 bot1~bot20，排除多指数 bot101/102/103）逐日推进到覆盖 2026-07-20。

需求：对这些合格 run 设置一个**盘中 cron**，每交易日让 bot 在**真实当下**做一次决策：

- 必须 **15:00 收盘前提交订单**（A 股场内基金规则：15:00 前提交按当日收盘 NAV 成交，之后顺延 T+1）。
- 除 simworld 的 PIT 历史快照外，让 bot 拿到**实时信息**：实时行情、新闻/情绪、资金面/宏观，以及 research/ 研究成果。实时跑在「数据前沿」上，天然无未来函数泄漏。
- **不直接在原 run 上续跑**——历史回测数据宝贵，且盘中口径与回测收盘口径不一致（回撤/净值会混）。要**接着已有持仓与记忆**继续，但物理隔离历史。

方法论目标不变：年化超额 ~10%、最大回撤 ≤5%。

## 2. 关键决策（已与用户确认）

| 决策点 | 选择 |
|--------|------|
| 历史 vs 实盘 | **派生新 live run**：每个合格 run 派生一个新 run_id，继承持仓快照+记忆，历史回测 run 冻结不动，两套数据物理隔离。 |
| 决策与成交口径 | **盘中快照决策 + 当日收盘 NAV 成交**：尾盘触发，bot 用「昨收 + 当日盘中实时快照」决策，15:00 前提交订单，按当日收盘 NAV 成交。 |
| 触发时间 / 并发 | **14:00 触发、并发 5**（先试）。留 60 分钟缓冲；45 run / 5 并发 ≈ 9 批，最坏 ~45 分钟。 |
| 实时信息范围 | 实时行情（已有）+ 新闻/情绪 + 资金面/宏观 + research 成果。纯新闻情绪文本源列为第二期。 |
| 防泄漏 | 保留 PIT 注入，把注入日期指向「真实今天」，今天之后的数据照常截断。 |

## 3. 架构与隔离边界

新增一个 `live/` 编排层，复用现有 world 引擎，不改回测逻辑。三个物理隔离面：

- **历史回测 run（`dash-*`）**：只读冻结，永不写。
- **live run（`live-<bot>-<源run摘要>`）**：新 run_id，所有实盘读写都落这里。
- **PIT 前沿**：simworld-proxy 注入日期指向真实今天，今天之后的数据被截断 → 实时且无泄漏。

关键机制（已核实）：

- 账户按 `(bot_id, run_id)` 键控。`cli_tools.py init_fund_account --run-id` 建账；`run.ts` 读持仓按自身 run_id 从 `fund_bot_position_snapshots` 取 `MAX(trade_date)<=worldDate`。
- 成交两步：下单先落 pending，`settle_pending_orders --as-of-date` 才按那天 NAV 结算。→ 14:00 盘中提交时当日收盘 NAV 尚不存在，**必须收盘后再结算**。
- 记忆按 run 存：`runs/<runId>/memory/store.jsonl` + pi agent 会话/人格状态。

由「成交两步」推出核心结论：**系统层拆成两阶段——盘中决策（Phase 1，bot 动一次）+ 盘后结算（Phase 2，纯系统、无 LLM）**。

## 4. 组件设计

### 4.1 一次性派生（seeding，每个合格 run 跑一次）

- **DB 复制 per-run 交易历史（重键到新 run_id，让系统重算，不动 schema）**：核心是复制种子 run 自己的**逐笔行为流** `fund_bot_actions`（BUY/SELL 历史）+ `fund_bot_holdings` / `fund_bot_holding_lots`（未平仓 lots）+ 未结算的 `fund_bot_orders`，全部按新 live run_id 落一份；系统读现金时**不信任** `fund_bot_accounts` 那行缓存（PK=bot_id，跨 run 会被覆盖），而是用 `_replay_fund_account_state(run_id=...)` 按本 run 的 actions+pending 重算 → 现金天然按 run_id 隔离，**无需改主键、无需搬运公用现金、无需 40+ 个 DB 文件**。再在派生日写一条基线 `fund_bot_daily_snapshots` / `fund_bot_position_snapshots`（run 键控）作为净值起点。
- **FS 克隆**：`runs/<源>/memory/store.jsonl` 与 pi agent 会话/人格状态 → 新 live run 目录。
- **配置**：为 live run 生成 `config/world-live-<runId>.yaml`（源自合格 run 的 tmp 配置，改注入日期策略、解开实时工具白名单）。
- **幂等**：live run 已存在则跳过，绝不覆盖。

### 4.2 Phase 1 — 盘中决策（14:00，并发 5）

- 每个 live run 跑「决策驱动器」（在 `oos-daily-driver` 上加 `--phase decide`）：注入今天日期 + 当前盘中时刻，bot 读昨收 + 盘中实时快照 + 新闻/资金面/宏观/research，产出 5 份 MD 并**提交订单（pending）**。
- **绝不结算**：此阶段不调 `settle_pending_orders` / `close_my_day`。

### 4.3 Phase 2 — 盘后结算（当日 NAV 落库后，傍晚）

- 纯系统步骤、无 LLM：对每个 live run `settle_pending_orders --as-of-date 今天` → 按**当日真实收盘 NAV** 成交，再 `close_my_day` 落净值快照。
- 触发时机等 ttjj 当日 NAV 就绪（傍晚或次日开盘前）——现实中 A 股基金当日 NAV 通常当晚才披露。用「NAV 是否可得」做前置门闩，未就绪则自旋等待/顺延。

### 4.4 实时信息暴露

- 在 live 配置的 simworld 工具白名单里**解开**已有 ttjj 工具：`market_realtime_quote`（盘中行情）、`stock_capital_flow`（资金面）、`macro_data`（宏观）、`stock_events`（事件/催化）、`ttjj_research_search` + `research_view`（research 检索）。
- 注入日期 = 真实今天 → 工具返回「截至今天」的真实数据，PIT 仍挡未来。
- research/ 成果：已入库的走 `research_view` / `ttjj_research_search`；未入库的 s1-s8 报告等按需以只读附件注入 bot 上下文。
- 纯新闻情绪文本若现有 `stock_events` 不够，列为**第二期**再接新源，不阻塞上线。

**数据粒度说明**：`market_realtime_quote` 返回的是「**当前时点的现价截面**」（timestamp 恒为「现在」），`include` 可带 `quote`/`order_book`/`capital_flow`/`valuation`/`industry`/`index` 多维度，但每维都是此刻的截面，**不是日内分时/分钟 K 线序列**。ttjj MCP 现无分时/kline 端点。本设计 14:00 定点决策一次，只需现价截面 + 昨收 + 资金面/宏观，不依赖分钟序列。若未来要 bot 看日内走势形态择时下手，需另接分时/kline 上游 → 列为可选第二期增强。

### 4.5 cron 编排、幂等、并发、兜底

- 两个 systemd/crontab 定时器：`live-decide`（工作日 14:00）、`live-settle`（工作日傍晚，带 NAV 就绪门闩）。
- 复用现有编排骨架（仿 `advance_qualified.py`）：flock 防重叠、并发 5、错峰启动、逐 run 写日志 + `summary.tsv`。
- 幂等：决策阶段按「今天是否已产出决策」跳过；结算阶段按「今天是否已 settle」跳过 → 重跑安全。
- **跑不完兜底**：Phase 1 设硬截止 **14:55**——到点仍未完成的 run 记 `MISSED` 并停止提交（宁可今天不动，不越过 15:00 拿 T+1 成交），summary 高亮，人工/次日复盘。
- 非交易日：日历判断，空跑退出。

## 5. 失败处理与观测

- 单 run 失败隔离，不影响其它；失败清单进 summary。
- 决策与结算解耦：即便某天没结算，次日结算门闩按 as-of-date 幂等补上欠的 NAV。
- dashboard 加一个 live 面板：看当日决策/结算状态与净值曲线。

## 6. 测试策略

- 先拿 **1 个 live run** 做端到端演练：派生 → 14:00 决策 → 傍晚结算 → 净值落库。
- 核对账户守恒：`现金 + 持仓市值 + 在途 = 上日总值 ± 当日损益`。
- 通过后再铺到 45 个 run。

## 7. 范围与非目标

- **本期做**：派生、两阶段 cron、解开已有 ttjj 实时工具、兜底与观测、单 run 端到端验证。
- **非目标 / 第二期**：接入全新的新闻情绪文本源；多指数 bot（bot101~103）的实盘化；把 live 结果反哺回测评估口径。
