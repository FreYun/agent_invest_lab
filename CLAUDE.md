# Agent Invest Lab — 开发参考

> 全程用中文回复（代码/命令/标识符除外）。这是硬性要求。

## 0. 这是什么

**大类资产配置 LLM Agent 回测实验室**。让多个 bot agent 在历史 A 股行情上「逐日交易」，验证一套资产配置方法论，产出可复现的净值曲线与择时能力评估。

- 方法论目标：年化超额 ~10%，最大回撤控制在 5% 以内
- 资产范围：A股基金 / 债券基金 / 黄金基金 / 货币现金 四类
- bot 分两类：`bot1~bot20` 单基金择时策略；`bot101~bot103` 多基金大类资产配置
- 核心机制：**PIT（Point-In-Time）时点数据** + **模拟交易系统**，物理隔离未来函数

权威文档（按重要性）：
- [大类资产配置Agent投资方法论_v2.0.md](大类资产配置Agent投资方法论_v2.0.md) — 战略中枢：配置区间、风险闸门
- [FUND-SCREENING-METHODOLOGY.md](FUND-SCREENING-METHODOLOGY.md) — 选基硬门槛 + 三维加权打分（业绩50%/规模30%/费率20%）
- [docs/world-backtest-overview.md](docs/world-backtest-overview.md) — world 回测系统架构
- [record.md](record.md) — Rust/TS runner 的工具热加载机制（`/reload`）

## 1. 架构与数据流

```
Bot 层 (bots/botN)                LLM agent，按 METHODOLOGY 逐日决策
   │  MCP 调用（经 world 的 proxy 注入/抹除 simulated_datetime）
   ▼
数据层（两个独立 MCP）
   ├─ ttjj-data-pit  (:18078)   历史行情时点快照（消除未来函数）
   └─ fund-portfolio-mcp        模拟交易系统（账户/下单/持仓/巡检/NAV）
        readonly :28071 · bot-only :28172 · admin（dashboard 用）
   │
   ▼
存储层
   ├─ data/fund.db (SQLite, ~184M)   持仓/订单/NAV/MD 元数据
   └─ world/runtime/runs/            每日 bot 对话记录
   │  系统侧 scripts/fund_md_to_db.py 解析 bot 输出的 5 份 MD 落库
   ▼
分析层 research/  →  可视化 world 的 backtest-dashboard
```

### PIT（时点数据）是核心
- [ttjj_data_pit_mcp.py](ttjj_data_pit_mcp.py)：每个工具首参强制为 `simulated_today`，server 端只返回该日及之前的数据，哪怕库里有更新的也截断。
- world 的 simworld-proxy 在进程内把 schema 里的时间字段抹掉，调用时自动注入「世界当前日期 + 15:00（A股收盘）」。**bot 看不到、改不了、也不知道** 这个日期的存在。
- 同理 `run_id` 由 proxy 强制注入，bot 看不到 → 保证多 run 隔离。

## 2. 目录结构

| 目录 | 用途 |
|------|------|
| `bots/` | 23 个 bot 工作区。每个含 `METHODOLOGY.md` `AGENTS.md` `SOUL.md` `IDENTITY.md` `TOOLS.md` `MEMORY.md` `USER.md` + `config/` `memory/` `skills/` |
| `world/` | **日度滚动回测系统（TS，Node>=22.6）**：`main.ts` 调度器、`src/`（simworld-proxy、fund-portfolio-proxy、backtest-dashboard、strategy-server）、`config/*.yaml` 回测配置、`runtime/` 日历 |
| `fund-portfolio-mcp/` | 基金交易 MCP（Python）：`server.py`（admin/readonly/bot-only 三模式）、`db.py`、`cli_tools.py`（系统侧账户初始化/每日结算）、`test_*.py` |
| `scripts/` | 数据脚本：`fund_md_to_db.py`（MD→DB 落库）、`fund-bootstrap.py`、`fund-fetch-fees.py`、`fund-backfill-nav.py`、`fund-pool-ingest.py`、`fund_capability_lib.py` |
| `research/` | 因子挖掘与报告（13 个子目录 + `SOP_factor_mining.md`、`s1-s8_final_report_*.md`） |
| `strategies/` | 择时因子研究（如 index-products 指数行业轮动） |
| `data/` | `fund.db`（核心库）、`buyable/`（各轮可买基金列表）、`user/`（用户数据快照） |
| `runtime/` `logs/` `tests/` `skills/` `docs/` | 运行时输出 / 日志 / 项目级测试 / 技能库 / 文档 |

## 3. 启动与运行

### MCP 服务
```bash
./restart.sh                    # ttjj-data-pit          → :18078
./restart-fund-mcp.sh           # fund-portfolio readonly → :28071
./restart-fund-mcp-bot-only.sh  # fund-portfolio bot-only → :28172
```
日志统一在 `/tmp/*.log`（`ttjj-data-pit-mcp.log`、`fund-portfolio-mcp-readonly.log`、`fund-portfolio-mcp-bot-only.log`）。

### 回测（world/）
```bash
cd world
npm start -- --config config/world-1day.yaml   # = node --experimental-strip-types main.ts
npm run backtest                                # 启动 backtest-dashboard（src/backtest-dashboard/server.ts）
npm test                                        # node --test test/*.test.ts
```
配置文件：`world/config/` 下 `world.yaml`、`world-1day.yaml`、`world-10day.yaml`、`world-all-bots.yaml`、`world-multi-asset-allocation-*.yaml` 等是正式配置；大量 `world-tmp-dash-*.yaml` 是 dashboard 临时生成的，可忽略/清理。

## 4. 环境与依赖（务必看）

- **Python 必须用 `/usr/bin/python3.12`**。裸 `python3` 在本机 vscode/claude shell 会被解析到 uv 3.11（缺 `mcp` 包，秒挂）。
- **fund-portfolio-mcp bot-only 端用 `/opt/MCP/.venv/bin/python`**（脚本默认值；可用 `FUND_MCP_PYTHON` 覆盖，启动前会自检 `import mcp`）。
- Python 依赖：`requirements.txt`（mcp/requests/pytest）；`fund-portfolio-mcp/pyproject.toml`（mcp[cli]、requests，requires-python>=3.10）。
- world 依赖：Node >= 22.6.0，仅 `yaml`（用 `--experimental-strip-types` 直接跑 .ts，无需编译）。
- 上游数据：天天基金 API（已全量切到 ttjj-api，见最近 commit）。
- **测 MCP 不要并发**：上游会 reset，并发的「失败」是伪阳性。

## 5. `.openclaw` 符号链接

`.openclaw -> ../.openclaw`，指向主平台库 `/home/rooot/.openclaw`（多 agent 运营平台，另有其 CLAUDE.md）。本项目通过它复用共享资源（如 research-loop）。两者是同一份库的不同视角，不是拷贝。注意：主 `.openclaw` 下也有一个独立的 `data/fund.db`（19M，与本项目 184M 的那个不是同一个文件）。

## 6. git

- 当前分支 `feat/world-system`（其他：`feat/panyh/agent_invest_lab`、`feat/mcp-watchdog`、`master`）。
- 数据库写入路径：bot 下单 → fund-portfolio-mcp 写 `fund.db`；系统侧 `fund_md_to_db.py` 再解析 bot 的 5 份 MD（投资框架/市场/选基/持仓/巡检）补写 allocation_run、selection_run、bot_reviews、bot_actions。
