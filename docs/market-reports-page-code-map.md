# `market-reports.html` 前端页面代码与依赖清单

整理时间：2026-07-22

页面地址：

```text
http://172.31.41.105:48080/market-reports.html
```

本页是 `world` 回测看板服务的一部分，核心用途是展示：

- 全局市场报告：行情判断、市场主线、主线轮动、日度主线、日度轮动、宏观资讯。
- 固定 OOS bot：`bot101 / bot102 / bot103` 的净值曲线、持仓快照、当日订单、当日思考。
- `bot101` 右下角实时对话框：基于当前页面数据时点，允许 bot101 调只读工具回答问题。

## 入口与运行服务

| 类型 | 文件/服务 | 说明 |
|---|---|---|
| 前端入口 | `world/src/backtest-dashboard/market-reports.html` | 单文件 HTML，内嵌 CSS 和 JS，无前端构建产物 |
| 后端服务 | `world/src/backtest-dashboard/server.ts` | Node HTTP server，负责静态页面、JSON API、SSE chat |
| systemd 服务 | `world-backtest-dashboard.service` | 常驻 48080，`WorkingDirectory=/home/rooot/agent_invest_lab/world` |
| 启动命令 | `node --experimental-strip-types src/backtest-dashboard/server.ts --host 0.0.0.0` | 默认端口 `48080`，默认 DB `../data/fund.db` |
| npm 脚本 | `world/package.json` -> `npm run backtest` | 等价于 `node --experimental-strip-types src/backtest-dashboard/server.ts` |

当前 systemd 配置：

```ini
WorkingDirectory=/home/rooot/agent_invest_lab/world
ExecStartPre=-/usr/bin/python3.12 /home/rooot/agent_invest_lab/world/bin/kill-48080-stale.py
ExecStart=/usr/bin/node --experimental-strip-types src/backtest-dashboard/server.ts --host 0.0.0.0
StandardOutput=append:/tmp/world-backtest-48080.log
StandardError=append:/tmp/world-backtest-48080.log
```

## 前端文件结构

`world/src/backtest-dashboard/market-reports.html` 是一个完整单页：

| 区块 | 代码位置/标识 | 作用 |
|---|---|---|
| 样式 | `<style>` 顶部 | 页面布局、研报卡片、OOS 卡片、净值曲线、持仓卡片、bot101 对话框 |
| 页面骨架 | `<div class="wrap">...` | 左侧日期列表，右侧 headline、报告卡、OOS bot 卡、当日思考 |
| 主页面 JS | 第一段 `<script>` | 拉取报告/OOS/反思 API，渲染报告、净值曲线、持仓、订单、思考 |
| bot101 对话框样式 | 第二段 `<style>` | 右下角浮动对话框 UI |
| bot101 对话框 JS | 第二段 `<script>` IIFE | 调 `/api/bot101/chat`，处理 SSE token、工具调用进度和 trace |

前端不依赖 React/Vue，也不依赖外部 CDN。图表是手写 SVG，Markdown 是页面内简易解析器。

## 前端状态与固定配置

页面内固定报告类型：

```js
var TYPES=[
  ['market_context','市场行情判断'],
  ['market_mainline','市场主线'],
  ['market_mainline_daily','市场主线（日度）'],
  ['mainline_rotation','主线 Rotation'],
  ['mainline_rotation_daily','主线 Rotation（日度）'],
  ['macro_news','宏观资讯要点']
]
```

固定 OOS bot：

```js
var OOS_BOTS=[
  {botId:'bot101',runId:'oos-bot101-daily'},
  {botId:'bot102',runId:'oos-bot102-daily'},
  {botId:'bot103',runId:'oos-bot103-daily'}
]
```

核心页面状态：

```js
var state={
  payload:null,
  oosByBot:{},
  reflectionsByBot:{},
  intraday:null,
  selectedDate:'',
  query:'',
  collapsed:{...}
}
```

## 前端主要函数

| 函数 | 作用 |
|---|---|
| `load(date)` | 页面主入口。先拉 `/api/market-reports`，再并发拉三个 bot 的 `/api/oos/:botId` 和 `/api/backtest/bot-reflection` |
| `render()` | 统一渲染日期列表、headline、报告卡、OOS 卡、当日思考 |
| `renderDates()` | 左侧日期列表，显示每个日期的 `report_count/3` |
| `renderHeadline()` | 顶部摘要 chips：行情、Risk、主线、日度主线、Rotation、日度动作 |
| `renderReports()` / `card()` | 渲染六类报告卡；日度主线卡会叠加盘中板块快照 |
| `renderMarkdown()` | 简易 Markdown 渲染，支持标题、列表、表格、代码块、粗体 |
| `renderOos()` / `renderOosCard()` | 渲染 `bot101/102/103` 的账户结果 |
| `navChart()` | SVG 净值曲线，支持回测前史线、每日 OOS 线、买卖点、鼠标 hover 日期/净值 |
| `positionChart()` | SVG 持仓权重堆叠图 |
| `holdingCards()` | 右侧持仓快照卡片 |
| `orderTable()` | 当日订单表 |
| `renderThink()` / `renderThinkCard()` | 读取 `reply.json` 决策总结，显示三个 bot 的当日思考 |
| bot101 chat `send()` | 调 `/api/bot101/chat`，stream 模式下处理 `meta/step/token/tool_call/tool_result/done/error` |

## 前端调用的 API

| API | 调用方 | 后端函数 | 作用 |
|---|---|---|---|
| `GET /api/market-reports?date=YYYY-MM-DD` | `load(date)` | `loadMarketReports()` | 返回日期列表、选中日、各类报告正文和结构化摘要 |
| `GET /api/oos/:botId?date=YYYY-MM-DD&run_id=...` | `load(date)` | `loadOosBot()` | 返回某个 bot 的 OOS 净值、持仓、订单、报告镜像、扩展曲线 |
| `GET /api/backtest/bot-reflection?bot_id=&run_id=&trade_date=` | `load(date)` | `loadReflection()` | 读取 runtime 下当日 `reply.json` / `sent.md`，用于“当日思考” |
| `GET /api/market-reports/intraday-boards` | `load(date)` 只在选中最新日期时调用 | `fetchIntradayBoards()` | 盘中实时板块快照，叠加到日度主线卡 |
| `POST /api/bot101/chat` | 右下角对话框 | `createBot101ChatEngine()` | bot101 对话，支持 JSON 和 SSE 流式 |

## 后端服务代码

主文件：`world/src/backtest-dashboard/server.ts`

### 服务启动配置

默认路径：

| 变量 | 默认值 |
|---|---|
| `DEFAULT_DB` | `<repo>/data/fund.db` |
| `DEFAULT_WORLD_ROOT` | `<repo>/world/runtime` |
| `DEFAULT_MARKET_REPORTS_HTML` | `world/src/backtest-dashboard/market-reports.html` |
| host | `127.0.0.1`，systemd 启动时覆盖为 `0.0.0.0` |
| port | `48080` |

参数：

```text
--host <host>
--port <port>
--db <fund.db path>
--world-root <world/runtime path>
```

### `/api/market-reports`

函数：`loadMarketReports(dbPath, requestedDate)`

读取 `data/fund.db`：

| 表 | 用途 |
|---|---|
| `market_reports` | 读取市场报告正文、结构化 JSON、生成时间 |
| `oos_bot_daily_snapshots` | 日期列表补齐：即使某天报告缺失，只要 OOS 净值存在，也让日期可选 |
| `fund_nav` | 限制 OOS 日期只到 `MAX(fund_nav.nav_date)`，避免基金净值未发布时提前展示 |

日期列表口径：

- 只用三份日报类型计数：`market_context`, `market_mainline`, `mainline_rotation`。
- `macro_news` 不参与日期列表计数，因为它可能提前生成未来日期；选中某日后按 PIT 取 `<= selectedDate` 的最近一期。
- 选中日如果有 `market_mainline_daily` / `mainline_rotation_daily`，也会被返回到 `reports` 对象，供前端六类卡片渲染。

返回结构：

```json
{
  "selectedDate": "2026-07-22",
  "dates": [{"as_of_date":"2026-07-22","report_count":3,"generated_at":"..."}],
  "reportTypes": ["market_context","market_mainline","mainline_rotation","macro_news"],
  "reports": {
    "market_context": {"content_md":"...","structured":{}},
    "market_mainline_daily": {"content_md":"...","structured":{}}
  }
}
```

### `/api/oos/:botId`

函数：`loadOosBot(dbPath, botId, runId, requestedDate)`

只允许：

```text
bot101 -> oos-bot101-daily
bot102 -> oos-bot102-daily
bot103 -> oos-bot103-daily
```

读取 `data/fund.db`：

| 表 | 用途 |
|---|---|
| `oos_bot_daily_snapshots` | 当前 OOS 账户净值、总资产、现金仓位、曲线 live 段 |
| `oos_bot_position_snapshots` | 当前持仓快照和全历史持仓权重 |
| `oos_bot_orders` | 选中日订单 |
| `oos_bot_actions` | 选中日动作、全历史买卖点 |
| `oos_market_report_status` | 选中日三份报告镜像状态 |
| `fund_info` | 补基金名称、主题 |
| `fund_nav` | 限制展示到已发布净值日期 |
| `fund_bot_daily_snapshots` | 回测前史段，用来把 4 月以来曲线接到 OOS live 段 |

前史映射：

```ts
const OOS_HISTORY_START = '2026-04-01'
const OOS_BACKTEST_HISTORY_RUNS = {
  bot101: 'dash-2026-06-23T08-47-44',
  bot102: 'dash-2026-06-23T08-48-10',
  bot103: 'dash-2026-06-15T06-30-57',
}
```

曲线拼接逻辑：

- `fund_bot_daily_snapshots` 里的历史段从 `2026-04-01` 开始，首点归一到 1.0，标记 `segment='backtest'`。
- `oos_bot_daily_snapshots` 里的每日 OOS 段按首个 OOS 净值等比缩放接到历史段末点，标记 `segment='daily_oos'`。
- 如果没有历史段，则只返回 OOS 段。

返回结构重点字段：

```json
{
  "runId": "oos-bot101-daily",
  "botId": "bot101",
  "selectedDate": "2026-07-22",
  "accountDate": "2026-07-21",
  "navMaxDate": "2026-07-21",
  "series": [],
  "extendedSeries": [],
  "snapshot": {},
  "positions": [],
  "orders": [],
  "actions": [],
  "reports": [],
  "actionsAll": [],
  "holdingsByDate": {}
}
```

`accountDate` 可能早于 `selectedDate`。这是正常行为：当选中日的 `fund_nav` 尚未发布时，前端展示最近可用账户快照，但订单和当日思考仍按 `selectedDate` 展示。

### `/api/backtest/bot-reflection`

函数：`loadReflection(worldRoot, runId, botId, tradeDate)`

读取 runtime 文件，不读 DB：

```text
world/runtime/runs/<run_id>/<trade_date>/<bot_id>/reply.json
world/runtime/runs/<run_id>/<trade_date>/<bot_id>/sent.md
```

用途：

- `reply.json` -> 前端“当日思考”的决策总结。
- `sent.md` -> 交易记忆窗口，折叠显示。

参数会做白名单校验，防止路径穿越。

### `/api/market-reports/intraday-boards`

后端文件：

- `world/src/intraday-boards.ts`
- `scripts/intraday_board_snapshot.py`

作用：

- 只对服务端“今天”返回 `applicable=true`。
- 给 `market_mainline_daily` / `mainline_rotation_daily` 卡片叠加盘中实时板块快照。
- 只读，不写库，不改变日度主线状态机。

读取/调用：

| 来源 | 用途 |
|---|---|
| `data/market.db.board_trend_daily` | 板块全集和 T-1 排名 |
| `data/fund.db.market_reports` | 最近一期 `market_mainline_daily` 的核心/卫星骨架 |
| `ttjj_data_pit_mcp.market_realtime_quote` | 盘中实时板块行情 |

`world/src/intraday-boards.ts` 有 60 秒短缓存，避免每次刷新都打实时接口。

### `/api/bot101/chat`

前端：

- 右下角“问 bot101”对话框。
- 默认 `stream:true`，走 SSE。
- 页面实时显示 token、工具调用中/成功/失败状态、最终 trace。

后端：

- 路由在 `server.ts`。
- 引擎在 `world/src/backtest-dashboard/bot101-chat.ts`。

数据时点：

- 前端传当前页面日期。
- 后端用 `resolveChatAsOf()` 夹到 `<= 请求日` 且已有完整每日决策的最近交易日。
- 注入 `formatBot101ChatContext()` 生成的页面上下文：当日研报、bot101 账户、持仓、动作。

bot101 chat 引擎依赖：

| 依赖 | 默认地址/路径 | 用途 |
|---|---|---|
| LLM 配置 | `world/runtime/runs/oos-bot101-daily/workspaces/bot101/config/research-loop.yaml` | 读取 bot101 当前实跑用的 base_url/model/api_key |
| bot 人格 | `bots/bot101/IDENTITY.md`, `SOUL.md`, `USER.md` | system prompt 人格块 |
| 方法论 | 优先 `world/runtime/runs/oos-bot101-daily/workspaces/bot101/METHODOLOGY.md`，否则 `bots/bot101/METHODOLOGY.md` | 注入当前生效方法论，最长截断到 9000 chars |
| simworld MCP | `http://127.0.0.1:18078/mcp` | 行情/估值/因子/宏观等 PIT 数据 |
| fund MCP bot-only | `http://127.0.0.1:28172/mcp` | bot101 自己账户的只读基金工具 |
| 本地工具 | `get_market_report` | 直查 `fund.db.market_reports`，支持历史 as_of_date，PIT 不穿越 |

安全约束：

- simworld 工具列表里抹掉 `simulated_datetime`，调用时自动注入 `<asOf> 15:00:00`。
- fund 工具列表里抹掉 `run_id`，调用时自动注入 `oos-bot101-daily`。
- fund 写工具不暴露：`portfolio_place_buy_order`, `portfolio_place_sell_order`。
- 申赎原始接口隐藏：`fund_subscription_redemption_summary`, `fund_index_subscription_redemption`。
- 工具调用串行执行，最多 6 轮工具往返。

## 数据生产链路依赖

页面本身只读。它依赖以下任务提前把数据写入 `fund.db` / runtime。

| 数据 | 生产入口 | 写入位置 | 页面消费 |
|---|---|---|---|
| 三份市场日报 | `scripts/run-oos-market-reports-daily.sh` -> `world/src/market-reports/prepass-driver.ts` | `fund.db.market_reports` | `/api/market-reports` |
| 日度主线报告 | `scripts/run-mainline-daily.sh` -> `world/src/market-reports/backfill-mainline-daily.ts` | `fund.db.market_reports` | `/api/market-reports` |
| OOS bot 决策账本 | `scripts/run-oos-bot101-daily.sh` -> `world/src/oos-daily-driver.ts` | `fund_bot_*` | 通过镜像后展示 |
| OOS 前端镜像 | `scripts/oos-bot101-mirror.lib.sh` | `oos_market_report_status`, `oos_bot_daily_snapshots`, `oos_bot_position_snapshots`, `oos_bot_orders`, `oos_bot_actions` | `/api/oos/:botId` |
| OOS NAV/持仓刷新 | `scripts/refresh-oos-bot101-nav.sh` | `fund_bot_*` + `oos_*` | `/api/oos/:botId` |
| bot 当日思考 | `world/src/oos-daily-driver.ts` 运行产物 | `world/runtime/runs/<run>/<date>/<bot>/reply.json`, `sent.md` | `/api/backtest/bot-reflection` |
| 基金净值上限 | `scripts/fund-lab-daily-refresh.sh` | `fund.db.fund_nav` | `/api/market-reports`, `/api/oos/:botId` 用于日期/NAV 截断 |
| 盘中板块快照 | `scripts/intraday_board_snapshot.py` 即时生成 | 不落库，仅返回 JSON | `/api/market-reports/intraday-boards` |

## 关键数据库表

### `fund.db`

| 表 | 页面用途 |
|---|---|
| `market_reports` | 报告正文、结构化摘要、macro_news PIT 回填、日度主线 |
| `oos_market_report_status` | OOS run 当日是否镜像到报告 |
| `oos_bot_daily_snapshots` | OOS 账户净值、总资产、现金仓位、曲线 live 段 |
| `oos_bot_position_snapshots` | OOS 持仓快照、持仓权重堆叠图 |
| `oos_bot_orders` | 当日订单表 |
| `oos_bot_actions` | 当日动作、曲线买卖点 |
| `fund_bot_daily_snapshots` | 曲线回测前史段 |
| `fund_info` | 基金名称、主题 |
| `fund_nav` | 最新净值日期上限，防止同日净值未发布时前端提前显示 |

### `market.db`

| 表 | 页面用途 |
|---|---|
| `board_trend_daily` | 盘中板块快照的板块全集和 T-1 排名 |

## 相关代码文件

| 文件 | 角色 |
|---|---|
| `world/src/backtest-dashboard/market-reports.html` | 页面 UI、图表、数据加载、bot101 chat 前端 |
| `world/src/backtest-dashboard/server.ts` | 48080 后端服务、API 路由、SQLite 查询 |
| `world/src/backtest-dashboard/bot101-chat.ts` | bot101 对话引擎，LLM + MCP 工具调用 + SSE |
| `world/src/intraday-boards.ts` | 盘中板块快照 API 包装、短缓存、Python 子进程 |
| `scripts/intraday_board_snapshot.py` | 盘中板块实时快照生成 |
| `scripts/sql/oos_bot101_daily.sql` | `oos_*` 镜像表 DDL |
| `scripts/oos-bot101-mirror.lib.sh` | 从 `fund_bot_*` 镜像到 `oos_*` |
| `scripts/run-oos-bot101-daily.sh` | 14:30 OOS 三 bot 决策入口 |
| `scripts/refresh-oos-bot101-nav.sh` | 08:00 OOS 净值/持仓刷新入口 |
| `scripts/run-oos-market-reports-daily.sh` | 每日三份市场报告 prepass |
| `scripts/run-mainline-daily.sh` | 日度主线报告补漏 |
| `world/src/market-reports/prepass-driver.ts` | reporter agent 生成 `market_context/market_mainline/mainline_rotation` |
| `world/src/market-reports/backfill-mainline-daily.ts` | 生成 `market_mainline_daily/mainline_rotation_daily` |

## 常见排查点

| 现象 | 优先检查 |
|---|---|
| 页面打不开 | `systemctl --user status world-backtest-dashboard.service`，日志 `/tmp/world-backtest-48080.log` |
| 日期列表没有当天 | `fund.db.market_reports` 是否有三份日报，或 `oos_bot_daily_snapshots` 是否已有当日且 `trade_date <= MAX(fund_nav.nav_date)` |
| 报告卡缺失 | `market_reports WHERE as_of_date=<date>` 是否有对应 `report_type` |
| OOS 净值曲线不到当天 | `fund_nav.MAX(nav_date)` 是否已经到当天；`oos_bot_daily_snapshots` 是否已有当日 |
| 选中今天但持仓显示昨天 | 正常情况下是 `accountDate` 被 `fund_nav.MAX(nav_date)` 截断；等 NAV 入库后 08:00 refresh 或手动 refresh 会更新 |
| 当日订单不显示 | 查 `oos_bot_orders WHERE live_run_id=? AND bot_id=? AND order_date=?` |
| 当日思考为空 | 查 `world/runtime/runs/<run_id>/<date>/<bot>/reply.json` 是否存在 |
| bot101 对话失败 | 检查 `simworld-data-mcp.service`、`lab-fund-bot-only.service`，以及 `oos-bot101-daily` workspace 下 `research-loop.yaml` |
| 盘中板块快照不显示 | 只对服务端今天显示；检查 `scripts/intraday_board_snapshot.py --fund-db data/fund.db` 输出 |

## 修改注意事项

- 改 `market-reports.html` 后无需构建；`server.ts` 对 HTML 是启动时读取，`market-reports.html` 改动通常需要重启 `world-backtest-dashboard.service` 才生效。
- 改 `server.ts` / `bot101-chat.ts` / `intraday-boards.ts` 必须重启服务。
- `oos_*` 表是展示层镜像，源账本仍是 `fund_bot_*`；排查交易逻辑时先看 `fund_bot_*`，排查前端展示时看 `oos_*`。
- 页面日期和 OOS NAV 都受 `fund_nav.MAX(nav_date)` 约束，这是为了避免基金净值未发布时展示未来/伪同日净值。
