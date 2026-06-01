<!-- TOOLS_COMMON:START -->

---

## System Admin — Strictly Forbidden

**Only HQ (mag1) may execute these. All sub-bots are prohibited:**

- `openclaw gateway restart/stop/start`, `kill/pkill/killall`, `systemctl/service`
- `rm -rf`, `trash` on system directories or other bots' files

**Infrastructure issues (timeout, connection failure) → report to HQ, do not troubleshoot yourself.**

---

## Inter-Agent Communication (Message Bus)

### Rules

1. **Only channel**: `send_message` / `reply_message` / `forward_message` — no CLI calls, legacy `message()`, or shell scripts
2. **Every message must include `trace`** (provenance chain); `reply_message` auto-routes based on trace
3. **Strict single-round**: request → process → `reply_message` → **done**. One request = one reply. Put all data in the reply — never split into multiple messages

### Tools

| Tool | Purpose |
|------|---------|
| `send_message` | Start a new conversation/request |
| `reply_message` | Return results (defaults to Feishu user; add `also_notify_agent: true` to also wake upstream agent) |
| `forward_message` | Forward to another agent (trace auto-appended) |
| `get_message` / `list_messages` | Query message details / inbox |

### Trace Construction

```
send_message(to: "target_agent", content: "...", trace: [{
  agent: "your_account_id", session_id: "current_session_id",
  reply_channel: "feishu", reply_to: "ou_xxx", reply_account: "your_account_id"
}])
```

`reply_channel/reply_to/reply_account`: only set at the origin hop. Intermediate forwards auto-append trace.

### Incoming `[MSG:xxx]` Messages

`xxx` is the message_id → process → call `reply_message(message_id: "xxx", content: "all results here")` → done.

**Never use `[[reply_to_current]]` or plain text replies** — the sender won't receive them. Always `reply_message`, whether success or failure.

---

## Image Generation: image-gen-mcp

生图用 `image-gen-mcp.generate_image(style, content)`。模型可选 `banana`（默认）或 `banana2`。

```
npx mcporter call 'image-gen-mcp.generate_image(style: "扁平插画风", content: "一只猫在看股票K线图")'
```

---

## Memory Recall: mem0_search

语义记忆检索——跨历史会话、日记、发帖、研究报告做语义搜索。当你需要回忆"我之前对 X 说过/想过/做过什么"时调用，替代手动 grep 文件。

| 参数 | 说明 |
|------|------|
| `query` | 自然语言检索词 |
| `scope` | `self`（默认，仅查自己的记忆）/ `all`（跨 bot 查询） |

```
mem0_search(query: "黄金ETF写过哪些角度", scope: "self")
```

典型场景：发文前查重、承接上篇话题、回忆过往研究结论、避免重复踩坑。

---

## Tool Priority

1. **memory** → check history first, update incrementally
2. **research-mcp** → financial data
3. **browser** → Xueqiu, EastMoney research reports, etc.
4. **MCP search** → supplementary search, overseas data
5. **xiaohongshu-mcp** → note management, interactions
6. **message bus** → inter-agent communication
<!-- TOOLS_COMMON:END -->










# TOOLS.md - 工具使用规范

## 联网搜索

- 联网搜索通过 browser 工具访问搜索引擎或目标站点（使用前先读 `skills/browser-base/SKILL.md`）

## 网页浏览

**必须使用：OpenClaw 自带 browser 工具**

- 严禁使用 Chrome 插件或任何浏览器扩展
- 需要登录或 JS 渲染的页面用 browser 工具处理

### 投研必访站点（每次行业研究必须覆盖至少2个）

**雪球 xueqiu.com** — 投资者讨论、机构观点、实时情绪
- 行业/股票搜索：`https://xueqiu.com/search?q={关键词}`
- 个股主页：`https://xueqiu.com/S/SH{6位代码}` 或 `SZ{6位代码}`（含置顶讨论和最新观点）
- 重点看：置顶帖、近期高赞帖子、机构研究员发文

**东方财富 eastmoney.com** — 研报、新闻、股吧
- 行业研报（最重要）：`https://data.eastmoney.com/report/industry.jshtml`
- 个股研报：`https://data.eastmoney.com/report/stock.jshtml?code={ts_code}`
- 行业新闻：`https://so.eastmoney.com/news/s?keyword={行业名称}`
- 股吧（舆情参考）：`https://guba.eastmoney.com/list,{6位代码}.html`

**同花顺 10jqka.com.cn** — 新闻聚合、行业动态
- 行业新闻：`https://news.10jqka.com.cn/`搜索行业关键词
- 个股资讯：`https://stockpage.10jqka.com.cn/{6位代码}/`

### browser 使用原则

- 每次行业研究（sector-pulse）：**必须**访问东方财富研报 + 雪球相关讨论
- 每次个股研究（research-stock）：访问个股雪球主页 + 东方财富个股研报
- 浏览后注意时间核验：页面上的"今天"是网站服务器时间，发帖/发布时间才是内容时间

## 金融数据：Tushare 工具（直接调用，无需搜索）

A 股量化数据优先用 Tushare 工具，不要去搜索：

| 工具名 | 用途 |
|--------|------|
| `tushare_stock_basic` | 股票列表、基本信息 |
| `tushare_daily` | 日线行情（OHLCV） |
| `tushare_daily_basic` | 估值数据（PE/PB/PS/市值） |
| `tushare_income` | 利润表 |
| `tushare_balancesheet` | 资产负债表 |
| `tushare_cashflow` | 现金流量表 |
| `tushare_fina_indicator` | 财务指标（ROE/ROA/毛利率等） |
| `tushare_forecast` | 业绩预告 |
| `tushare_moneyflow_hsgt` | 北向/南向资金日汇总 |
| `tushare_hsgt_top10` | 沪深港通十大成交股 |
| `tushare_top10_holders` | 前十大股东 |
| `tushare_index_daily` | 指数日线（沪深300、创业板等） |
| `tushare_trade_cal` | 交易日历 |

股票代码格式：`600519.SH`（上交所）、`000001.SZ`（深交所）。日期格式：`YYYYMMDD`。

## 研究工具优先级

1. **memory（QMD）** → 先检索历史研究，有则增量更新
2. **Tushare 工具** → A 股行情、财务、北向资金（结构化数据）
3. **browser 工具** → 雪球、东方财富研报、同花顺（**必须执行，不是可选**）
4. **browser 搜索** → 补充搜索、验证、海外数据

## 投研技能地图

| 技能 | 触发场景 |
|------|---------|
| `/sector-pulse` | 行业深度研究（旗舰） |
| `/industry-earnings` | 财报季行业横向比较 |
| `/flow-watch` | 北向资金行业轮动 |
| `/market-environment-analysis` | 全球宏观环境：risk-on/risk-off、美股/汇率/大宗/VIX |
| `/research-stock` | 个股快速数据查询（估值/财务/资金） |
| `/technical-analyst` | 用户提供图表时的技术面分析 |
| `/news-factcheck` | 核查资讯/研报观点的真实性和时效性 |
| `/stock-watcher` | 管理个人自选股列表，查看持仓行情摘要 |
| `/record` | 研究完成后保存结论到记忆系统 |
| `/self-review` | 定期复盘 + 自我进化 |

## 投研常用信息源

- 行业研报：东方财富 `data.eastmoney.com/report/`、同花顺研报
- 投资者讨论：雪球 `xueqiu.com`
- 公司公告：巨潮资讯 `cninfo.com.cn`、上交所 `sse.com.cn`、深交所 `szse.cn`
- 行业数据：IDC、Gartner、公司 IR 页面
- 深度报道：36kr、晚点 LatePost、路透中文、财新

---

## 📡 Media Data Pack (MDP) MCP

- **名称**：`media-data-pack`
- **URL**：`http://localhost:18075/mcp`（streamable-http）
- **工具**：
  - `search_weixin(query, top_k, fields, score_threshold)` — 财经公众号文章语义检索
  - `search_xiaohongshu(query, source, top_k, fields, score_threshold, min_likes)` — 小红书 + 抖音笔记检索

### 使用规则

- **source 参数**：传 `"抖音"` 才是拿爆款视频脚本；传 `"小红书"` 是图文笔记
- **min_likes**：行情类场景用 10000，科普类用 5000
- **score_threshold**：0.5-0.7 是中强相关，用来过滤噪声
- **fields 白名单**：不要全字段返回——不需要 CONTENT 时只取 title/user/showtime 省带宽

### 铁律

- 不要用 MDP 做通用 web 检索；通用热点/新闻用 `web_search` / `research-mcp`
- 每次检索结果要落到 `.material.md`，脚本里引用要挂 INFOCODE

详见：`skills/voiceover-pack/MDP-检索策略.md`
