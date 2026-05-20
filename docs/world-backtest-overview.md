# World 回测系统总览（汇报版）

> 路径: [world/](world/) · 看板: [http://localhost:48080/](http://localhost:48080/) · 更新: 2026-05-18

## 1. 这是什么

**World** 是一套日度滚动的金融世界回测系统，让 LLM 驱动的 bot agent 在历史 A 股行情上「逐日交易」，并把每只 bot 的净值、动作、持仓沉淀下来做横向对比。

一次回测的最小闭环：

```
world.yaml (bots + 日期区间 + loop)
   ↓ world run
runtime/runs/<run_id>/<YYYY-MM-DD>/<bot>/{sent.md, reply.json, status.json}
   ↓
fund-portfolio-mcp 落库（每笔买卖、每日 NAV）
   ↓
backtest-dashboard :48080 实时可视化
```

## 2. 为什么做

- **训练/评估闭环**：bot 的策略好不好，光看 prompt 跑不出来，要让它在真实历史行情里连续做几十天决策才看得出净值曲线、回撤、择时能力。
- **横向对比**：同一段行情下，不同 bot（不同人设、不同 prompt、不同模型）跑完直接看排行榜，决策"哪条线值得继续投入"。
- **可复现**：每次 run 都用 `run_id` 隔离 state / 持仓 / 记忆，跑废了不污染历史数据，可以反复重放调参。

## 3. 当前能力

| 能力 | 状态 | 说明 |
|---|---|---|
| 单 bot 单日跑通 | done | `world-1day.yaml` 配置可在 ~5 分钟内出结果 |
| 单 bot 多月连跑 | done | 最长已跑通 96 个交易日（约 4 个月行情）|
| 多 bot 并行 | done | 单 run 内 `concurrency` 控制；跨 run 走 per-run 隔离 |
| 并发多 run | done | 87 个历史 run 共存，state 互不污染（[concurrent-runs spec](docs/superpowers/specs/2026-05-15-concurrent-world-runs-design.md)）|
| 中断恢复 | done | `world resume --run-id <id>` 从 cursor 续跑 |
| 持仓/资金正确性 | done | T+0/T+1 现金应收、跨日结算已覆盖回归测试 |
| 看板可视化 | done | 净值曲线、买卖标记、持仓快照、基准对比 |
| 基准对比（alpha）| done | 与沪深 300ETF (510300) 同区间对比 |

## 4. 两个 MCP：bot 看到的世界

World 本身只是一个调度器，bot 真正"看世界 + 做交易"靠两个独立的 MCP server，加 world 进程内两层薄 proxy。

```
┌─────────────────────────────────────────────────────────────┐
│  bot (LLM)                                                  │
│   │ MCP 调用：query_xxx / portfolio_xxx                     │
│   ▼                                                         │
│  ┌──────────────────┐    ┌─────────────────────┐            │
│  │ simworld-proxy   │    │ fund-portfolio-proxy│  ← world 进程内
│  │ 注 simulated_dt  │    │ 注 run_id           │            │
│  └────────┬─────────┘    └──────────┬──────────┘            │
│           ▼                         ▼                       │
│  ┌──────────────────┐    ┌─────────────────────┐            │
│  │ ttjj-data-pit    │    │ fund-portfolio-mcp  │  ← 独立 Python
│  │ :18078           │    │ :28172 (bot-only)   │     server
│  │ 历史数据时点快照 │    │ 交易/持仓/巡检       │            │
│  └──────────────────┘    └─────────────────────┘            │
└─────────────────────────────────────────────────────────────┘
```

### 4.1 ttjj-data-pit — 历史数据时点快照（"看世界"）

[ttjj_data_pit_mcp.py](ttjj_data_pit_mcp.py)，默认监听 `127.0.0.1:18078`。

- **职责**：把天天基金的真实历史数据（基金净值、指数行情、宏观、个股财务、公告、研报…）按"时点"切片暴露给 bot。
- **核心约束**：每个 tool 第一个参数都是 `simulated_today` (YYYY-MM-DD)，server 端**只返回该日期当天及之前的数据**，哪怕数据库里有更新的也强制截断 ⇒ 物理上消除未来函数。
- **覆盖面**：~25 个 tool，覆盖基金（nav/分红/异动）、行情（指数/股票/商品/债券）、资金面、个股基本面、宏观、研报检索、实时行情；详见文件头注释。
- **设计取舍**：剔除了"无法锚定到日期的工具"——基金筛选/经理画像/费率、以及只有报告期没披露日的持仓字段都不开放，避免泄漏。
- **bot 看不到 `simulated_today`**：world 在进程内起了 `simworld-proxy`（[world/src/simworld-proxy/server.ts](world/src/simworld-proxy/server.ts)），从 `tools/list` 的 inputSchema 里把 `simulated_datetime` 字段抹掉，`tools/call` 时强制注入当前世界日 + `15:00:00`（A股收盘）。bot 既不能设，也不知道这个参数存在。

### 4.2 fund-portfolio-mcp — 模拟交易系统（"做交易"）

[fund-portfolio-mcp/server.py](fund-portfolio-mcp/server.py)，bot 端默认监听 `127.0.0.1:28172`（BOT_ONLY 模式）。

- **职责**：bot 的"券商账户" + "组合管理后台"。负责开户、下单、撮合、T+0/T+1 现金结算、每日 NAV 快照、巡检流水、绩效计算。
- **三种 mode**（同一份代码，环境变量切端口）：
  - `admin`（:28173）— 全部 tool 注册，给 dashboard / 调试用。
  - `READONLY`（:28171）— 写工具屏蔽，纯查询。
  - `BOT_ONLY`（:28172）— bot 用的极简端：只暴露 5 个 `portfolio_*` 工具
    - 写：`place_buy_order`, `place_sell_order`
    - 读：`get_my_history`, `get_my_trades`, `get_my_performance`
    - 隐藏：`init_my_account`, `close_my_day`（账户生命周期由 world 系统侧通过 `cli_tools.py` 直接触发，bot 看不到）。
- **可买池强校验**：`buyable_fund_codes`（world.yaml 配置）在 server 端校验 `place_buy_order`，bot 乱输代码会直接被拒。
- **bot 看不到 `run_id`**：world 进程内起 `fund-portfolio-proxy`（[world/src/fund-portfolio-proxy/server.ts](world/src/fund-portfolio-proxy/server.ts)），抹掉 schema 里的 `run_id` 字段，`tools/call` 时强制注入当前 run 的 id。所有 bot 写入都自动带 run 标签，审计追溯不依赖 bot 自觉。
- **读 tool 也强制 run_id**：`portfolio_get_my_history / get_my_trades / get_my_performance` 在 server 端拒绝不带 `run_id` 的调用，保证 bot 跨 run 看不到对方持仓（[run_id 读隔离 spec](docs/superpowers/specs/2026-05-17-fund-portfolio-run-id-read-isolation-design.md)）。

### 4.3 为什么要 proxy 一层

直接让 bot 连原始 MCP server 也能跑，但有两个问题躲不开：

1. **未来函数泄漏**：如果 `simulated_datetime` 暴露给 bot，bot 偷偷把它设成回测期之外的日期就能"偷看未来"，回测全废。proxy 在 schema 层就把这个参数抹掉。
2. **跨 run 审计**：同一个 bot 同时跑多个 run（不同人设/策略），写入必须自动带 `run_id` 才能事后区分，靠 bot 自觉填一定会乱。proxy 强制注入。

两件事都是"在 bot 与 MCP 之间加一道闸"，让物理隔离做掉合规问题，不靠 prompt 约束。

## 5. 怎么跑

**启动一次回测**（项目根目录）：

```bash
cd world
npm start -- run --config config/world-1day.yaml      # 1 个交易日，最快
npm start -- run --config config/world-10day.yaml     # 10 个交易日
npm start -- run --config config/world.yaml           # 完整区间（2026-01-01 → 05-14）
```

**查看进度 / 续跑 / 优雅停止**：

```bash
npm start -- status --run-id <id>
npm start -- resume --config config/world.yaml --run-id <id>
npm start -- stop --run-id <id>
```

**看板**（已托管到 `.openclaw/dashboard/start.sh`，开机自起）：

```
http://localhost:48080/
```

入口：[world/main.ts](world/main.ts) → [world/src/cli.ts](world/src/cli.ts) → [world/src/run.ts](world/src/run.ts)

## 6. 看板能看到什么

打开 :48080，每只 bot 一张卡片，点进去：

- **顶部 pills**：净值、累计收益、alpha（vs 基准）、最大回撤、总资产、买卖次数
- **账户净值曲线**：bot vs 基准（沪深 300ETF）双线 + 买卖点标注
- **最新持仓**：每只基金的金额/份额/占比
- **指标对比**：区间内胜率、夏普、回撤等
- **买卖动作流水**：按日的完整 trade log
- **最新巡检**：bot 自我评估的 regime / decision / turnover_ratio

数据接口：
- `GET /api/backtest/data` — 所有 bot 排行（最新 run）
- `GET /api/backtest/bot?bot_id=&run_id=` — 单 bot 全量明细

## 7. 关键设计决策

1. **per-run 全隔离**：每个 `run_id` 在 `runtime/runs/<run_id>/` 下自管 state / 持仓 / pi-sessions / memory，dashboard 通过扫盘拿活动 run 列表，不再依赖全局 `state.json`。
2. **fund-portfolio MCP 读写都强制 run_id**：bot 在不同 run 之间看不到对方的历史/持仓/收益，杜绝跨 run 数据污染（[run_id read isolation spec](docs/superpowers/specs/2026-05-17-fund-portfolio-run-id-read-isolation-design.md)）。
3. **两种 loop 可切换**：`research-loop`（轻量、纯研究决策）与 `openclaw-pi`（完整 pi runner，可装 plugin / mem0），用同一份 world.yaml 切。
4. **系统侧 vs bot 侧 tool 分离**：`init_fund_account` / `close_my_day` 这些生命周期 tool 在 BOT_ONLY 端口隐藏，只能由 world 进程在 setup / 每日收盘触发，bot 不能调。
5. **可买基金 curated**：白名单写在 world.yaml 的 `buyable_fund_codes`，server 端校验 buy 订单，避免 bot 乱买。

## 8. 已知问题 / 下一步

- `world/config/` 下堆了 100+ 个 `world-tmp-dash-*.yaml`，是看板「新建回测」每次写盘留下的，需要定期清理或改为 in-memory。
- 看板的「区间对比」目前只有沪深 300 一条基准，后续可以加可配置基准。
- 多 bot 并跑时 LLM token 用量会线性涨，需要做并发预算限制（目前靠 `concurrency` 字段卡死）。
- bot 决策日志（sent.md / reply.json）目前只能逐文件翻，缺一个跨日 timeline 视图。

## 9. 相关文档

- [agent-invest-lab world 总设计](docs/superpowers/specs/2026-05-11-agent-invest-lab-world-design.md)
- [openclaw-pi loop 接入设计](docs/superpowers/specs/2026-05-13-world-openclaw-pi-loop-design.md)
- [并发 run 设计](docs/superpowers/specs/2026-05-15-concurrent-world-runs-design.md)
- [bot 卡片基准对比设计](docs/superpowers/specs/2026-05-16-bot-card-benchmark-design.md)
- [fund-portfolio run_id 读隔离设计](docs/superpowers/specs/2026-05-17-fund-portfolio-run-id-read-isolation-design.md)
