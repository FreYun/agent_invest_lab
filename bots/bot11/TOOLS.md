# TOOLS.md - bot11（小奶龙）工具配置（agent_invest_lab 基金链路精简版）

> 本文件是 lab 版的精简 TOOLS.md，**只列基金投资全流程会用到的工具**。
> 生产环境的小红书、公众号、短线策略 MCP、image-gen、inter-agent 通讯等在 lab 里都不挂载，
> 不要尝试调用它们。

## Bot 专属配置

- **account_id**: `bot11`
- **lab 模式**: backtest / 时点回放（time-pinned），所有数据带 `simulated_today` 参数

---

## 数据源 1: ttjj_data_pit_mcp（时点版基金/市场数据）

天天基金 API 的"时点版薄代理"——所有工具必须传 `simulated_today=YYYY-MM-DD`，**只返回该日期当天及之前的数据**,杜绝未来函数。

- **端口**: `http://127.0.0.1:18078`
- **协议**: streamable-http
- **启动**: `agent_invest_lab/restart.sh`

### 核心工具

| 工具 | 用途 | 必填参数 |
|---|---|---|
| `fund_basic_info` | 基金基本信息（名称/类型/规模/成立日等） | `simulated_today`, `fund_codes` |
| `fund_nav` | 基金净值历史 | `simulated_today`, `fund_code`, `start_date`, `end_date` |
| `fund_index_return` | 基金区间收益 | `simulated_today`, `fund_code` |
| `fund_bonus` | 基金分红记录 | `simulated_today`, `fund_code` |
| `fund_abnormal_movement` | 基金异动 | `simulated_today`, `fund_code` |
| `market_index_quote` | 指数行情（上证/沪深300/创业板等） | `simulated_today`, `index_code`, `start_date`, `end_date` |
| `commodity_market` | 商品行情（黄金/原油等） | `simulated_today` |
| `bond_yield_curve` | 债券收益率曲线 | `simulated_today` |
| `stock_market` | 个股行情 | `simulated_today`, `stock_codes` |
| `stock_capital_flow` | 个股资金流向 | `simulated_today` |
| `stock_alpha` | 个股 alpha 指标 | `simulated_today` |
| `stock_events` | 个股事件 | `simulated_today` |
| `macro_data` | 宏观数据 | `simulated_today` |
| `research_view` | 研究观点检索 | `simulated_today`, 关键词 |
| `ttjj_research_search` | 天天基金研报搜索 | `simulated_today` |

参考文档：`skills/research-mcp/SKILL.md` 列出了 production 的 research-mcp 工具大全，lab 里对应能力由 `ttjj_data_pit_mcp` 提供，工具名/参数可能略有差异（lab 优先以此 TOOLS.md 为准）。

---

## 数据源 2: fund-portfolio-mcp（基金组合状态，**只读**）

bot 自己持仓/订单/快照/巡检历史/范式 run 的查询入口。

- **端口**: `http://127.0.0.1:28071`
- **模式**: READONLY（写工具未注册，调用会得到 `tool not found`）
- **协议**: streamable-http
- **启动**: `agent_invest_lab/restart-fund-mcp.sh`

### 可读工具（只读端可见）

| 工具 | 用途 |
|---|---|
| `get_fund_md_schema` | 取本 bot 写 5 份基金 MD 的 frontmatter schema（写 MD 前先看） |
| `validate_fund_bot_md` | 写完 5 份 MD 后自检（不入库，只验证 schema） |
| `get_fund_pool` | 查基金核心池 |
| `get_fund_detail` | 查单只基金详情 + 绩效 + 主题 |
| `get_fund_perf` | 查基金多区间绩效 |
| `get_bot_account` | 查自己的账户（initial_capital / cash） |
| `get_fund_holdings` | 查自己当前活跃持仓 |
| `get_pending_orders` | 查 T 日待结算订单 |
| `get_fund_curve` | 查自己的净值曲线 |
| `get_fund_position_snapshots` | 查持仓快照历史 |
| `get_latest_paradigm` | 查当日 / 最近一次范式 run |
| `get_review_history` | 查巡检历史 |

### 写工具（**lab 里全部未注册，不要调**）

`save_*` / `apply_*` / `init_*` / `record_*` / `upsert_*` / `settle_*` / `rollback_*` 这些都是 admin 端工具。
落库由 `agent_invest_lab/scripts/fund_md_to_db.py` 在 bot 写完 MD 之后直连 DB 完成，**bot 自己不写库**。

---

## 工具优先级

1. **memory** → 先翻自己 `memory/portfolio/` 已有的 MD（投资框架 / 能力圈宣告 / 个性化基金选择 / 市场环境判断），别重复劳动
2. **fund-portfolio-mcp（readonly）** → 查自己的当前持仓 / 订单 / 范式 / 巡检历史
3. **ttjj_data_pit_mcp** → 拉基金净值 / 指数行情 / 宏观 / 研报等市场数据

---

## 写产出 = 5 份 MD（系统会解析入库）

按 `skills/portfolio/_shared_docs/fund-investment/基金MD-frontmatter-schema.md` 规范写到 `memory/portfolio/fund/` 下：

1. `投资框架.md`（Phase B-1 写）
2. `能力圈宣告.md`（季度更新，每日跑只读不改）
3. `市场环境判断.md`（Phase B0 写，由 `skills/portfolio/market-context/SKILL.md` 指导）
4. `个性化基金选择.md`（Phase B 写，由 `skills/portfolio/fund-match/SKILL.md` 指导）
5. `基金巡检记录.md`（Phase C 写，由 `skills/portfolio/fund-review/SKILL.md` 指导）

写 MD 前先调 `get_fund_md_schema`，照 schema 写；写完调 `validate_fund_bot_md` 自检。落库 / T+1 结算 / 快照入库都是系统侧的事，bot 不管。
