# bot101 每日 cron 流程拆解（driver 段）

> 依据：08:00 `run-oos-bot101-daily.sh` 单次运行的实测数据（近 15 次样本，2026-06-16 ~ 2026-07-07）。
> **本文只覆盖 `oos-daily-driver` 起服务及之后**——即"prepass 三份研报生成完之后、bot101 真正做决策 + 下单"这一段。
> 前面 prepass 段（~9m40s，占总耗时 84%）不在本文范围。

## driver 段一览（4 步）

| # | 流程段 | 谁执行 | 输出 | 平均耗时 | 是否常态 |
|---|---|---|---|---|---|
| 1 | oos-daily-driver 起服务 | node --experimental-strip-types | memory / simworld-proxy / fund-portfolio-proxy / strategy-server / bot101 rust server 5 进程起 + 60 tools probe | ~3s | 每天 |
| 2 | 账户前置（pin buyable + init + settle） | Python fund-portfolio-mcp | 759 只白名单 JSON、账户校验、昨日 settle | ~1s | 每天 |
| 3 | **bot101 chat（LLM 决策 + 下单）** | LLM agent（Kimi via rust rl server） | reply.md + 下单 + belief 块 | **~1m 30s（hold 日） / ~5m（换仓日）** | 每天 |
| 4 | close + mirror + summary | Python + bash | position 快照 + oos_* 镜像 + sqlite 打印 | ~1s | 每天 |

driver 段总耗时中位数 **~1m 50s**（hold 日）；换仓日到 **~5m**。

## 各步细节

### 步 1 · oos-daily-driver 起服务（~3s）

执行者：`node --experimental-strip-types world/src/oos-daily-driver.ts --date T --run-id oos-bot101-daily --config config/world-multi-fund-backtest.yaml`

起 5 个本地进程 + probe 工具：

| 服务 | 端口（示例） | 上游 | 作用 |
|---|---|---|---|
| memory server | 37163 | — | bot mem0 存储 |
| simworld-data proxy | 35489 | 18078（ttjj_data_pit_mcp） | 时点数据代理，进程内注入 simulated_datetime |
| fund-portfolio proxy | 36719 | 28172（fund-portfolio bot-only） | 交易 MCP 代理，注入 run_id |
| strategy-server | 39283 | — | 方法论 / market_report 提供 |
| bot101 rust server | — | 火山网关 Kimi | LLM agent 本体 |

日志典型行：
```
simworld-data proxy at http://127.0.0.1:35489/mcp (upstream http://127.0.0.1:18078/mcp); 60 tools probed
simworld_tools whitelist active (34 entries) → tools.always_load 预激活
fund-portfolio proxy at http://127.0.0.1:36719/mcp (upstream http://127.0.0.1:28172/mcp, run_id=oos-bot101-daily)
strategy-server at http://127.0.0.1:39283/mcp
bot bot101: server ready
```

`60 tools probed` = simworld 全量工具 probe；`simworld_tools whitelist active (34 entries)` = 白名单预激活 `tools.always_load`（rust 侧配置），bot 不用每天 discover_tools 试错风暴。

工具调用：无 LLM，纯本地进程 spawn + JSON-RPC 握手。

### 步 2 · 账户前置（~1s）

Python 侧一次性完成三件：

1. **pin buyable**：把 `world/config/world-multi-fund-backtest.yaml` 里的 fund_pool（约 759 只场外 C 份额）落到 `data/buyable/oos-bot101-daily.json`，供 bot chat 读入。
2. **fund init**：`portfolio_init_bot(bot_id=bot101, initial_capital=1_000_000)` — 已存在账户则返回 `已有账户`（`existing.cash` = 昨日收盘现金），不重建。
3. **fund settle**：`portfolio_settle_pending(bot_id=bot101, as_of_date=T)` — 把 T-1 及以前的 pending 单结算掉；无 pending 时 `settled=[] skipped=[]`。

工具调用：3 次 fund-portfolio-mcp（`portfolio_init_bot` / `portfolio_settle_pending` / 白名单写文件）。**bot 尚未 chat**，这些是 driver 直接调用。

### 步 3 · bot101 chat（LLM 决策 + 下单，核心，~1m 30s hold 日 / ~5m 换仓日）

**这是唯一有 LLM 决策 + 下单的一步**。timeout=600s。

driver 组装 daily prompt 发给 bot101 rust server：
- **顶部预注入**：持仓 NAV / 账户绩效 / 峰值回撤 / 主要指数 5 个（MA60/120/200 + 趋势标签）/ 可买池 759 只按 theme 聚合的 top 5 / 四份 market_report（context + mainline_daily + rotation_daily + macro_news）+ 四大研判室子段（res1/2/4/5）
- **中部**：history window（近 4-6 个决策日的 reply.md，14000–15000 字）
- **底部**：METHODOLOGY.md 全文（`strategies/index-products/multi-equity-high.md` 覆盖注入）+ 每 5 交易日强制复盘 block

bot 按方法论走 7 小步：

#### 3.1 读预注入（0 次工具调用）

只读 daily prompt 顶部，不重复拉数据。

#### 3.2 持仓消息面扫描（1-2 次 `research_search`，串行）

对当前 2-4 只持仓合并成 1 条检索词（如 `"半导体 存储芯片 CPO 利空"`），串行调 `mcp__simworld_data__research_search(search_type='news', top_k≤5, search_days=7-14)`。命中重大利空进「口子 A：提前减仓」。

方法论要求"必做"，实际 hold 日 bot 有时会跳过（07-06 / 07-07 就是 0 次）。

#### 3.3 读 market_context 定 regime（0 次工具调用）

从预注入直接抄 `risk_state / market_regime / valuation_anchor`。不重跑 4 维度评分。

#### 3.4 读 mainline_rotation_daily 的『今日动作』（0 次工具调用）

日度状态机的输出，四种：
- `今日动作：hold` → 维持
- `今日动作：新进[卫星] BKxxxx` → 从 fund_pool 选载体建仓
- `今日动作：剔除 BKxxxx` → 卖出对应持仓
- `今日动作：晋升核心 BKxxxx` → 调标签

bot **不重算天数计数器**（40 日 top5 / 3 日破 MA60 等）——直接读报告『④ 计数器与触发距离』表。

#### 3.5 过 3 个 override 口子（0-1 次工具调用）

三个允许 bot 覆盖日度动作的口子：
- **A · 持仓消息面证伪**（3.2 步命中） → 提前减仓
- **B · 极端恐慌逆向闸门**（VIX z≥1.5 + 单日≤-4% + 温度≤5 + macro_news 一次性冲击） → 扛住 + 受限逆向
- **C · 账户回撤闸门**（≥6% 降档 / ≥10% defensive review） → 压低总仓位

三个都没触发 → 100% 照办日度动作。

#### 3.6 执行下单（0 - 数十次 `place_buy_order` / `place_sell_order`）

按日度动作 × 总仓位档 → 目标权重，调 fund-portfolio-mcp 下单。

多数日子这一步 = 0 次调用（hold 日）。

#### 3.7 写 mem0（1 次 `mem0_add`，每日必写）

内容按输出范式必答：风险状态 / 主线判定 / 日度动作执行状态（未执行明写覆盖来源）/ 持仓消息面 / 资讯研判 / **belief 块**（未来 20 交易日 p_up + 假设 + 证伪触发点，每日必写，否则被 belief-validate 拦截）。

#### 步 3 实测工具调用画像

按最近 5 次采样：

| trade_date | 步 3 耗时 | tool_use 总数 | 明细 |
|---|---|---|---|
| 2026-07-02 | ~1m 48s | 4 | 4× research_search（消息面扫描，无下单） |
| 2026-07-03 | ~1m 45s | 6 | 6× research_search（同上） |
| 2026-07-06 | ~1m 49s | 2 | 2× mem0_add（连消息面都没扫，直接收工） |
| 2026-07-07 | ~1m 28s | 2 | 2× mem0_add（同上） |
| **2026-07-01（月决策日重构）** | ~5m 08s | **62** | 5× research_search + 21× sell_order（含 5 次 error 重试）+ 6× buy_order + 2× discover_tools + 2× mem0_add |

**核心观察**：日度化改造前的 07-01 是"月决策日全组合重构"典型——62 次工具调用堆出 5 分钟。改造完成后，bot 应该按日度状态机的『今日动作』每日小步微调，单次调用数会降到 5-15 次量级、不再有 62 次这种爆点。

### 步 4 · close + mirror + summary（~1s）

driver 侧调 `portfolio_close_trading_day(bot_id=bot101, as_of_date=T)` 落 `fund_bot_position_snapshots` + `fund_bot_daily_snapshots`。

driver 退出后 shell 侧 `mirror_oos_results_for_date` 把 T 日的快照/订单/actions/reports 镜像到 `oos_*` 表（供 48080 backtest-dashboard 展示）。

最后打印 summary（sqlite 查 reports / orders / positions / nav）到日志 tail。典型行：
```
-- bot101 positions
trade_date  fund_code  weight_pct  market_value  shares
----------  ---------  ----------  ------------  ----------
2026-07-06  007818     40.14       411062.0      98727.5945
...
-- bot101 nav
2026-07-06  1024029.72   1.02403    2.403                  52.47
```

工具调用：1 次 fund-portfolio-mcp（close） + 若干次 sqlite（mirror + summary）。

## driver 段时长汇总（近 15 次样本）

| trade_date | driver 段总耗时 | 备注 |
|---|---|---|
| 06-16 | 1m56s | hold |
| 06-17 | 2m05s | hold |
| 06-18 | 1m48s | hold |
| 06-22 | 1m33s | hold |
| 06-24 | 1m36s | hold |
| 06-25 | ~3m46s | 日志未收尾 |
| 06-26 | 3m21s | hold（当日 prepass 已缓存，脚本前期跳过） |
| 06-29 | 1m57s | hold |
| 06-30 | 1m52s | hold |
| **07-01** | **5m08s** | **月决策日重构 62 次工具** |
| 07-02 | 1m48s | hold + 消息面扫描 4 次 |
| 07-03 | 1m45s | hold + 消息面扫描 6 次 |
| 07-06 | 1m49s | hold 极简 |
| 07-07 | 1m28s | hold 极简 |

**中位数 ~1m 50s，剔除换仓日后 hold 日稳定在 1m30s – 2m。**

driver 段耗时几乎全在**步 3 bot chat**——起服务/账户前置/close 合计不到 5 秒，剩下 100+ 秒全是 LLM 一轮或多轮工具调用循环。
