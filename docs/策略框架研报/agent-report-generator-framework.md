# Agent 化策略研报「蒸馏 + 自生成」系统 —— 设计与落地框架

> 目标：让 LLM Agent 学会券商策略研究员的分析套路，能每周（甚至每日）自动产出结构完整、有观点、可回测的策略研报。
> 参考对标：招商证券策略团队周报、广发证券策略专题、Stanford AI Index 式的产业跟踪。

---

## 0. 一句话概括

**用「PIT 数据 MCP + 结构化模板 + 多角色 Agent + 回测反馈」四件套，把策略研究员写周报的隐性方法论显性化、参数化、可复现化。**

---

## 1. 目标与非目标

### 1.1 目标
1. **蒸馏**：把已有 7 份研报（后续可扩展至 100+）的**分析框架**提取成机器可读的模板与规则库。
2. **生成**：Agent 能在每周固定节点，输入当周市场/宏观/产业数据，自动输出一份 20–40 页的策略研报（Markdown → PDF）。
3. **可评估**：报告里的每个观点都能与后续市场走势对照，产出「观点胜率」指标，形成闭环。
4. **PIT 严格性**：所有数据点必须带时间戳，`simulated_today` 之后的数据零泄漏（与项目现有 world 系统一致）。

### 1.2 非目标（第一版不做）
- 不做实时（分钟级）策略推送，只做**周度/事件驱动**。
- 不做个股推荐，只做**行业与大类资产层面**的观点。
- 不追求超越券商研究员，追求**成本 <1/50、时效 24×7、可回测**。

---

## 2. 关键洞察：这些研报到底在做什么

拆解 7 份研报后可以看出，一份「合格的策略周报」几乎都由 **5 个固定模块** 拼装而成：

| 模块 | 招商证券叫法 | 广发证券叫法 | 数据密度 | Agent 可复现度 |
|---|---|---|---|---|
| **复盘·内观** | 本周市场涨跌 | 位置 | 高（结构化） | ⭐⭐⭐⭐⭐ |
| **中观·景气** | 行业景气数据 | 景气 | 高 | ⭐⭐⭐⭐⭐ |
| **资金·众寡** | 融资/ETF/公募 | 资金面 | 中 | ⭐⭐⭐⭐ |
| **主题·风向** | 产业催化事件 | 叙事 | 低（非结构化） | ⭐⭐⭐ |
| **数据·估值** | PE/PB 分位表 | 估值分位 | 高（结构化） | ⭐⭐⭐⭐⭐ |

**结论**：4/5 模块是"数据 + 固定表述模板"，Agent 完全可复刻；真正体现分析师水平的是 **主题·风向** 那一段（把新闻串成叙事）和 **总纲** 那一段（结论/方向选择）。这两块是 Agent 需要重点投入 prompt 工程与知识库的地方。

---

## 3. 系统架构总览

```
┌────────────────────────────────────────────────────────────────────┐
│                     报告生成 Agent 系统                             │
│                                                                    │
│  ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌──────────┐ │
│  │  数据层     │──▶│ 蒸馏层      │──▶│ 分析层      │──▶│ 生成层    │ │
│  │  Data MCP  │   │ Framework  │   │ Multi-Agent│   │ Report   │ │
│  │  Cluster   │   │ Extractor  │   │ Analyst    │   │ Writer   │ │
│  └─────┬──────┘   └────────────┘   └─────┬──────┘   └────┬─────┘ │
│        │                                  │                │       │
│        │  PIT 时点隔离                     │                │       │
│        ▼                                  ▼                ▼       │
│  ┌───────────────────────────────────────────────────────────────┐│
│  │              评估层  Evaluation & Feedback                     ││
│  │   · 生成质量评分  · 观点胜率回测  · 与人写报告的差距分析          ││
│  └───────────────────────────────────────────────────────────────┘│
│                                                                    │
│  ┌───────────────────────────────────────────────────────────────┐│
│  │              运营层  Scheduler / Versioning / Publish          ││
│  └───────────────────────────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────────────┘
```

**复用现有项目基础设施**（这不是从 0 开始）：
- `ttjj-data-pit` → 行情/基金/指数 PIT 数据 ✅ 已有
- `fund-portfolio-mcp` → 模拟交易 & 净值验证观点收益率 ✅ 已有
- world/ simworld-proxy 模式 → 强制注入 `simulated_today`，杜绝未来函数 ✅ 已有
- SQLite (`fund.db`) → 观点/报告/评估结果落库 ✅ 可扩表

---

## 4. 分层设计

### 4.1 数据层（Data Layer）

#### 4.1.1 已覆盖 vs 待补
| 数据域 | 现状 | 补齐动作 |
|---|---|---|
| A 股指数/基金/行情 | `ttjj-data-pit` 已有 | 增加**行业指数**（申万一级 30 个）、**风格指数**（大盘价值/消费龙头/上证50/中证100/沪深300/上证指数/大盘成长/中小100/创业板指/北证50 + 必选/金融/医药生物/可选/周期/TMT）、**估值分位算子**（10y rolling） |
| 资金流（融资/ETF/公募新发/股东） | 无 | 挂到 `macro-data-pit-mcp` 里（口径与人写研报一致：融资取前 4 交易日、ETF 拆宽基+行业、公募只算偏股类） |
| 中观景气（工业细分产量/进出口/销售） | 无 | 挂到 `macro-data-pit-mcp`，**必须内置 3M 滚动同比算子**（招商体的固定表达） |
| 宏观（GDP/CPI/PMI/社融/LPR） | 无 | **新建 `macro-data-pit-mcp`** |
| 海外宏观（美 CPI/非农/联邦基金利率/美债/汇率） | 无 | 挂到上面同一个 MCP，源用 FRED |
| AI 产业景气（Arena/OpenRouter/ECI/Silicon Data Token Index） | 无 | **新建 `ai-industry-pit-mcp`**（招商 **7-19** §主题·风向"Kimi K3 登顶开源"章节 4 源全部命中，见下方实证核对） |
| 新闻催化流（财联社/科创板日报） | 无 | **新建 `news-pit-mcp`**（须支持"国内 10 + 海外 10"聚类，与人写研报量级对齐） |
| 政策文件全文（发改委/央行/证监会/工信部/国务院） | 无 | 挂到 news-pit-mcp 里的 `policy_docs` 子域，样本里 3 周出现的部委：发改委 / 国家药监局 / 商务部 / 央行 / 工信部 / 国务院 / 证监会 |
| 港股/黄金/大宗（原油） | 部分（黄金基金已有） | 港股需补；原油周度价格用于 §观策·论市 归因段，指数 API 用 akshare 兜底 |

> **实证核对（2026-07-29 用 3 份招商证券 A 股周报回填 · 2026-07-30 交叉核实修订）**：
> - `ttjj-data-pit` 覆盖：`get_index_weekly` `get_industry_weekly`（申万一级） `get_index_pe` `get_industry_pe_pct` `get_etf_flow` —— 是 §复盘·内观 / §资金·众寡（ETF 部分）/ §数据·估值 三段的全部数据来源。
> - `macro-data-pit-mcp` 覆盖：`get_margin_flow` `get_new_fund_issuance` `get_shareholder_change` `get_industry_output`（智能手机/集成电路/工业机器人/机床/汽车/商品房销售） `get_macro_indicator`（CPI/PPI/PMI/GDP/M2/社融/LPR） `get_us_macro`（非农/CPI/联邦基金利率） —— 是 §中观·景气 / §资金·众寡（融资+新发+股东部分） 与 §观策·论市 归因段的必备源。
> - `ai-industry-pit-mcp` 覆盖：`get_arena_rank`（Text/Frontend Code/Vision Arena，招商 **7-19 图 53/54/55**（p27-28）直接用到 Text Arena 榜 1486 分排名 + Frontend Code Arena Ranked #1） `get_eci_history`（Epoch Capabilities Index，**7-19 图 56**（p28）直接用到）`get_openrouter_share`（**7-19 图 59**（p29）52.6 万亿 token/周） `get_silicon_data_token_index`（**7-19 图 58**（p29），Bloomberg 源需接入或 OCR） —— 4 个源全部被 7-19 §主题·风向"Kimi K3 登顶全球开源模型第一"章节直接消费。7-12 §主题·风向 主题为"商业航天"（p20-23，图 45-49），只在 p6 图 11 顺带用了 OpenRouter 累计 42.6T；7-05 §主题·风向 主题为"氟化工"，未消费 AI 类源。**MCP 必要性验证：4/4 全命中（1 份报告），跨报告命中 1-4/4 视主题而定**。
> - `news-pit-mcp` 覆盖：`search_news`（按国内/海外分区）`get_hot_topics` `get_policy_docs` —— §主题·风向 段"国内 10 条 + 海外 10 条"的固定输出结构，3 份样本雷打不动。<br>
> **新识别的必备算子**：3M 滚动同比（中观景气段的固定表达）、10y rolling PE 分位（估值段的固定表达）、风格指数封装（复盘段固定要素）—— 全部落到 `ttjj-data-pit` 增量迭代里，不新增 MCP。

#### 4.1.2 三个新 MCP 的骨架

```
macro-data-pit-mcp/          (:18080)
├── server.py
├── db.py                    # 独立 SQLite: macro.db
├── sources/
│   ├── stats_gov_cn.py      # 国家统计局
│   ├── pboc.py              # 央行
│   ├── fred.py              # 美联储数据（免费 API key）
│   └── customs.py           # 海关数据（进出口）
└── tools/
    ├── get_gdp(simulated_today, freq)
    ├── get_cpi(simulated_today, country, freq)
    ├── get_pmi(simulated_today, type)   # 制造业/非制造业/财新
    ├── get_social_finance(simulated_today)
    ├── get_us_rates(simulated_today)
    └── ...

ai-industry-pit-mcp/         (:18081)
├── server.py
├── db.py                    # ai_industry.db
├── crawlers/
│   ├── lmarena.py           # Text/Vision/Code Arena（每天抓一次）
│   ├── openrouter.py        # 官方 API，各模型 token 份额
│   ├── epoch_eci.py         # CSV 定时下载
│   └── model_releases.py    # HuggingFace + arXiv 新模型
└── tools/
    ├── get_arena_rank(simulated_today, category, top_n)
    ├── get_openrouter_share(simulated_today, window)
    ├── get_eci_history(simulated_today, open_or_closed)
    └── list_new_models(since, until)

news-pit-mcp/                (:18082)
├── server.py
├── db.py                    # news.db，全文检索用 FTS5
├── ingestors/
│   ├── cls_telegram.py      # 财联社电报
│   ├── stcn_daily.py        # 科创板日报
│   ├── ndrc_policy.py       # 发改委政策
│   └── csrc_announce.py     # 证监会公告
└── tools/
    ├── search_news(simulated_today, keywords, days_back, top_n)
    ├── get_hot_topics(simulated_today, window)   # 主题聚类
    ├── get_policy_docs(simulated_today, agency, keywords)
    └── summarize_events(simulated_today, sector)
```

#### 4.1.3 PIT 铁律（沿用 world 系统的做法）

- **所有工具首参强制 `simulated_today`**，server 端硬截断。
- 在 Agent 与 MCP 之间放一个 **`report-agent-proxy`**（仿 `simworld-proxy` 的写法）：
  1. schema 里删掉 `simulated_today` 字段 → Agent 不知道它的存在。
  2. 调用时自动注入当前"报告日期 + 15:00"（对齐 A 股收盘）。
  3. 强制注入 `run_id`（多轮实验隔离）。
- **分位数、rolling 指标**必须"用截至 T 日的历史数据计算"，禁止用完整历史算完再切片。

---

### 4.2 蒸馏层（Framework Extractor）

**目的**：把「研究员脑子里的模板」提取到磁盘上，让 Agent 有骨架可套。

#### 4.2.1 蒸馏三件套

```
distillation/
├── templates/               # 报告模板骨架
│   ├── weekly_a_share.yaml           # 招商 A 股周报体
│   ├── industry_comparison.yaml      # 广发行业比较框架
│   ├── mid_term_asset.yaml           # 港股 & 大类资产中期策略
│   └── sector_deep_dive.yaml         # 服务业投资框架
│
├── rules/                   # 分析规则/触发条件
│   ├── valuation_rules.yaml          # PE 分位 >80% → "估值高位需警惕"
│   ├── flow_rules.yaml               # 融资净流出 + ETF 净申购 → "结构分化"
│   ├── macro_rules.yaml              # CPI < 前值 且 PMI > 50 → "复苏无通胀"
│   └── narrative_rules.yaml          # 政策催化 + 龙头股上涨 → "主题确立"
│
└── examples/                # few-shot 语料库（从 7 份 PDF 抽段落）
    ├── good_openings.md              # 好的开篇句 200 条
    ├── transition_phrases.md         # 逻辑衔接词
    ├── valuation_narratives.md       # 估值段的固定套话
    └── conclusion_patterns.md        # 「方向选择」段的推理链
```

#### 4.2.2 蒸馏怎么做

**流程**：
1. **PDF → 结构化**：用 `pdfplumber` + LLM（Claude/GPT-4o）把每份 PDF 切成 `{章节标题, 段落, 表格, 图表 caption}` 的 JSON。
2. **模板提取**：让另一个 LLM 读 5+ 份同类型报告，抽出**共同骨架**（哪些一级标题必出现、每段固定说什么）。
3. **规则挖掘**：把"数据 → 结论"的映射用 LLM 标注（例：`"融资净流出851.6亿"` → `"资金面偏紧"`），沉淀到 rules yaml。
4. **人工校对**：模板 + 规则必须过一遍人工，防止 LLM 幻觉污染知识库。

**关键点**：
- 模板不是硬编码文字，是**「章节 + 变量插槽 + 触发条件」**的组合。
- 每条规则要带 `confidence` 和 `source`（来自哪份报告哪一页），方便后续 A/B。

---

### 4.3 分析层（Multi-Agent Analyst）

**目的**：把一份研报的写作过程拆成多个专业 Agent 分工，仿真研究团队。

#### 4.3.1 Agent 编制（参考券商研究所结构）

```
report-agent-team/
├── chief_strategist/        # 首席：出总纲、方向选择、风险提示
│   └── AGENTS.md
├── market_analyst/          # 市场：涨跌复盘、板块表现
│   └── AGENTS.md
├── macro_analyst/           # 宏观：国内外经济数据、政策
│   └── AGENTS.md
├── industry_analyst/        # 行业：景气、比较、拥挤度
│   └── AGENTS.md
├── fund_flow_analyst/       # 资金：融资/ETF/公募/北向
│   └── AGENTS.md
├── theme_analyst/           # 主题：新闻聚合、催化提炼
│   └── AGENTS.md
└── editor/                  # 编辑：整合、润色、图表配文
    └── AGENTS.md
```

**每个 Agent 是一个独立 LLM 会话**，可以：
- 用不同的 MCP 工具集（macro_analyst 只连 macro-data-pit，theme_analyst 只连 news-pit）；
- 有自己的 SOUL/IDENTITY/METHODOLOGY.md（参考现有 bots/botN 的写法）；
- 通过 SQLite 表共享中间产物：`report_draft_sections`。

#### 4.3.2 编排（Orchestration）

**推荐用 world 的 TS 调度器模式**，新建 `world/report_orchestrator.ts`：

```ts
// 伪代码
async function generateWeeklyReport(date: string) {
  const runId = createRun(date);

  // 并行跑数据类 Agent（互不依赖）
  const [market, macro, industry, flow] = await Promise.all([
    runAgent('market_analyst', { date, runId }),
    runAgent('macro_analyst', { date, runId }),
    runAgent('industry_analyst', { date, runId }),
    runAgent('fund_flow_analyst', { date, runId }),
  ]);

  // 串行：主题依赖前面的数据类结果
  const theme = await runAgent('theme_analyst', {
    date, runId, context: { market, macro, industry }
  });

  // 首席拿全部结果出总纲
  const chief = await runAgent('chief_strategist', {
    date, runId, context: { market, macro, industry, flow, theme }
  });

  // 编辑整合
  const finalReport = await runAgent('editor', {
    date, runId, sections: { market, macro, industry, flow, theme, chief }
  });

  return finalReport;
}
```

#### 4.3.3 每个 Agent 内部循环

1. **拉数据**：调 MCP 工具，只拿本周相关数据。
2. **匹配模板**：从 `templates/` 里选骨架。
3. **触发规则**：跑 `rules/` 判断哪些"结论"应被触发。
4. **写段落**：LLM 填充模板变量 + 引用 few-shot 例句。
5. **自检**：数字对不对得上、图表 caption 是否一致。

---

### 4.4 生成层（Report Writer）

#### 4.4.1 输出格式栈

```
Markdown 主体 ──┬──▶ Pandoc + LaTeX ──▶ PDF（研报排版）
                ├──▶ HTML + Chart.js  ──▶ Dashboard 内嵌
                └──▶ 富文本 JSON     ──▶ 微信公众号/飞书文档
```

#### 4.4.2 图表生成
- **Python matplotlib + 招商证券配色** 出静态图（跟原报告风格一致）。
- 每张图配一个 `chart_spec.json`（数据 + 图类型），便于二次修改。
- 图表 caption 由生成图的 Agent 附带写好（"图1：本周申万一级行业涨跌"）。

#### 4.4.3 引用与去幻觉
- **每段结论必须带 `sources` 数组**：`[{tool: "get_pe_percentile", args: {...}, value: 65.7}]`
- 编辑 Agent 最后一道校验：**遍历所有数字，如果没 sources 就打回重写**。
- 防止 LLM 编造：**Agent 不允许自己"记忆"数字**，所有数字必须来自本轮 MCP 调用。

---

### 4.5 评估层（Evaluation）

#### 4.5.1 三个维度

| 维度 | 指标 | 怎么算 |
|---|---|---|
| **形似度** | 结构完整率、表格数量、图表数量、字数、章节覆盖 | 与真报告做 diff |
| **神似度** | 语义相似度（BERTScore）、关键论点重合率 | LLM-as-Judge，5 分制打分 |
| **实战胜率** | 观点后 1 周/2 周/1 月的胜率 | 把观点抽取成 `{标的, 方向, 时间窗}` 三元组，回测 |

#### 4.5.2 观点结构化（关键）

**每份报告生成时必须同时产出一份 `views.jsonl`**：
```jsonl
{"id": "20260728-01", "type": "sector", "target": "电子", "direction": "underweight", "window_days": 14, "reason": "估值分位98.4%", "confidence": 0.7}
{"id": "20260728-02", "type": "index", "target": "沪深300", "direction": "neutral", "window_days": 7, "reason": "回调消化中"}
{"id": "20260728-03", "type": "theme", "target": "国产算力", "direction": "overweight", "window_days": 30, "reason": "昇腾950+Kimi K3双催化"}
```

**回测规则**（复用 `fund-portfolio-mcp`）：
- 每条 view 生成一笔虚拟仓位（sector 用行业 ETF、index 用指数基金、theme 用主题 ETF）
- 到期日计算相对基准（沪深 300）的超额收益
- 累积每个 Agent 的**观点胜率**、**信息比率**、**衰减曲线**

这样就有了「哪个 Agent 更值得听」的量化答案。

---

### 4.6 运营层（Operations）

```
scheduler/
├── weekly_report.cron        # 每周日 20:00 触发下周初报告
├── event_driven.py           # 重大事件（CPI 公布、Fed 议息）触发临时报告
├── daily_snapshot.py         # 每晚 21:00 抓当日数据入库
└── health_check.py           # MCP 服务存活、数据延迟监控
```

**版本管理**：
- 每份报告 → git commit（分支 `reports/YYYYMMDD-weekly`）
- 模板/规则更新 → 走 PR，人工 review 后合入
- Agent prompt 版本化 → 存 `prompts/` 目录，改动必带 changelog

**分发**：
- Dashboard（复用 world 的 backtest-dashboard 加一个 tab）
- 生成 PDF 存 S3 或本地 `runtime/reports/`
- 可选：webhook 推送到飞书群

---

## 5. 落地路线图（4 个 Milestone）

### M1（2 周）：数据地基
- [ ] 补齐 3 个新 MCP（macro / ai-industry / news）**只做最小可用**：
  - macro：只做 GDP/CPI/PMI/美 CPI/美联邦基金利率
  - ai-industry：只做 LMArena（Text/Frontend Code/Vision 三类）+ OpenRouter；对标 cms_20260719 §主题·风向 章节 Kimi K3 段（p27-29，图 53-59）
  - news：只做财联社电报 + 关键词检索
- [ ] 增加行业估值分位算子到 `ttjj-data-pit`
- [ ] 在 world 里加 `report-agent-proxy`（PIT 注入）

**验收**：能通过 MCP 拉出招商这份 7-19 报告里的所有数字（含 Arena 分数、ECI 曲线、OpenRouter token 份额、行业 PE 分位表 30 行）。

### M2（3 周）：单 Agent MVP
- [ ] 只用 1 个 Agent（先不分工）复现 **招商 A 股周报的"数据·估值"章节**
- [ ] 输出 Markdown，跟原报告 diff，通过率 >70% 算过关
- [ ] 建立第一版 `templates/weekly_a_share.yaml`

**验收**：Agent 能自动生成一段"本周整体 A 股估值水平下行……"的完整文字 + 数据表。

### M3（4 周）：多 Agent 全流程
- [ ] 拆出 6 个专业 Agent + 1 个 editor
- [ ] 跑通端到端：给定日期 → 输出完整 20 页 MD → 转 PDF
- [ ] 建立 views.jsonl 结构化观点输出
- [ ] LLM-as-Judge 评分链路搭建

**验收**：生成一份完整周报，人评价 3.5/5 以上（对标真研报的 4.5/5）。

### M4（持续）：闭环 + 优化
- [ ] 观点胜率回测跑通，能画"Agent 声誉曲线"
- [ ] Prompt/规则的 A/B 系统
- [ ] 加入 广发行业比较框架、大类资产中期策略 两种报告模板
- [ ] 事件驱动的临时报告（跟"回调复盘"这种 ad-hoc 场景）

**验收**：连续 8 周稳定产出，观点胜率显著优于随机。

---

## 6. 与现有项目的集成

| 项目模块 | 集成方式 |
|---|---|
| `bots/botN` | 报告 Agent 复用 bot 的目录结构（SOUL/IDENTITY/METHODOLOGY.md），但独立于交易 bot |
| `ttjj-data-pit` | 增加 3 个行业估值工具即可 |
| `fund-portfolio-mcp` | 用作观点回测：每个 view 开一个虚拟账户跑 |
| `world/` | 新增 `report_orchestrator.ts`，复用 simworld-proxy 的 PIT 机制 |
| `fund.db` | 加表：`generated_reports`、`report_sections`、`report_views`、`view_backtest_results` |
| `scripts/` | 加 `report_md_to_db.py`（仿 `fund_md_to_db.py`，落库） |
| `research/` | 挖掘出的规则/模板，作为研究产出归档 |
| `.openclaw/research-loop` | 蒸馏阶段可直接复用其 LLM 循环封装 |

---

## 7. 主要风险与坑

### 7.1 数据侧
- **上游 API 不稳定**：财联社/东财都有反爬，需要做**多源冗余 + 缓存 + 失败降级**。
- **PIT 泄漏**：新闻的**发布时间戳**要精确到分钟，回测时用"当日 15:00 之前发布的"截断，否则等于开外挂。
- **分位数陷阱**：一定要 rolling 分位，不能全序列分位。

### 7.2 模型侧
- **数字幻觉**：LLM 最爱瞎编数字。**硬约束：所有数字必须挂 sources，编辑 Agent 一票否决**。
- **模板僵化**：过分依赖模板会让报告读起来像小学生填空。留出 20% 篇幅让 chief_strategist 自由发挥。
- **成本**：一份完整报告估计 100-300 万 token（多 Agent + 长上下文），要提前预估钱包。Claude Haiku 做数据类、Opus/Sonnet 做首席。

### 7.3 评估侧
- **观点胜率短期噪音大**：单周胜率没意义，至少累积 3 个月才可比。
- **形似 ≠ 神似**：BERTScore 高不代表观点对。**必须有人 review 环节**（每月抽查 4 篇）。
- **对标偏差**：不是每份人写报告都对，别把"跟原报告一致"当成唯一目标。

### 7.4 组织侧
- **知识库腐化**：模板 / 规则 / few-shot 需要专人维护，不然 3 个月后没人敢改。建议**每周 15 分钟 review 一次**。
- **Prompt drift**：Agent 输出会漂移，需要有**回归测试集**（固定 5 个历史日期，跑完看输出是否稳定）。

---

## 8. 附录 A：完整数据源清单

### A.1 市场估值
| 数据 | 来源 | 获取方式 | 是否 PIT | 备注 |
|---|---|---|---|---|
| A 股指数 PE/PB(TTM) 及分位 | Wind / akshare / tushare | akshare `stock_a_all_pb` | ✅ 自建 rolling 分位 | 免费 |
| 申万一级行业估值 | Wind / 中证指数官网 | akshare `index_value_hist_funddb` | ✅ | 部分需付费 |
| 沪深 300 / 中证 500 / 创业板 PE(TTM) | 中证/深交所每日文件 | HTTP GET | ✅ | 一手数据 |
| 基金历史净值 | 天天基金 | `ttjj-data-pit`（已有） | ✅ | 已接入 |

### A.2 宏观
| 数据 | 来源 | 获取方式 | 频率 |
|---|---|---|---|
| GDP / 工业增加值 / 社零 | 国家统计局 | 官方 API 或 akshare | 月/季 |
| CPI / PPI / PMI | 国家统计局 / 财新 | 官方 API / akshare | 月 |
| M2 / 社融 / LPR | 央行 | pboc.gov.cn / akshare | 月 |
| 美国 CPI / 非农 / 失业率 / 联邦基金利率 | 美国 BLS + Fed | **FRED API（免费首选）** | 月/日 |
| 汇率 / 美债收益率 | 各国央行 / Investing | akshare / Yahoo | 日 |

### A.3 AI 产业景气（本项目差异化）
| 数据 | 来源 | 获取方式 | 频率 |
|---|---|---|---|
| Text/Vision/Code Arena 排行 | LMArena (原 LMSYS) | lmarena.ai 页面 + HF dataset 镜像 | 日 |
| OpenRouter 各模型 token 份额 | OpenRouter | 官方 API（免费） | 日/周 |
| Epoch Capabilities Index | Epoch AI | epoch.ai 数据下载页 CSV | 月 |
| Stanford AI Index | Stanford HAI | 年度 PDF 报告解析 | 年 |
| Silicon Data LLM Token Index | Silicon Data (via Bloomberg) | 无免费源，需订阅或 OCR | 周 |
| 新模型发布 | HuggingFace + arXiv | HF API + arXiv API | 日 |
| 台积电/ASML 财报 | 官方 IR | 财报 PDF 解析 + 财联社 | 季 |

### A.4 资金面
| 数据 | 来源 | 获取方式 |
|---|---|---|
| 融资余额/融资买入 | 交易所每日文件 | akshare `stock_margin_sse` |
| ETF 净申赎 / 份额变动 | 交易所 + Wind | akshare `fund_etf_hist_em` |
| 公募新发基金份额 | 中基协 / Wind | akshare `fund_new_found_em` |
| 北向资金 | 港交所 | akshare `stock_hsgt_north_net_flow_in_em` |
| 重要股东增减持 | 上市公司公告 | 巨潮 / akshare |

### A.5 新闻催化
| 数据 | 来源 | 获取方式 |
|---|---|---|
| 财联社电报 | cls.cn | 电报页爬虫（注意反爬） |
| 科创板日报 | 财联社系 | 同上 |
| 东方财富新闻 | eastmoney | akshare `stock_info_global_em` |
| 证监会公告 | csrc.gov.cn | RSS + PDF 解析 |
| 交易所公告 | sse/szse/hkex | 官网 announcement API |

### A.6 政策文件
| 数据 | 来源 | 获取方式 |
|---|---|---|
| 发改委政策 | ndrc.gov.cn | 政策文件页爬取 + 全文入库 |
| 央行货币政策报告 | pbc.gov.cn | 季度报告 PDF |
| 中央经济工作会议纪要 | xinhua.net | 新华社通稿 |
| 部委行动计划 / 白皮书 | 各部委官网 | 关键词监控 |

---

## 9. 附录 B：目录结构建议

```
agent_invest_lab/
├── report_agent/                      # ★ 新增：报告生成系统根目录
│   ├── CLAUDE.md                      # 子项目说明
│   ├── orchestrator.ts                # 端到端调度（或放 world/）
│   ├── agents/
│   │   ├── chief_strategist/
│   │   ├── market_analyst/
│   │   ├── macro_analyst/
│   │   ├── industry_analyst/
│   │   ├── fund_flow_analyst/
│   │   ├── theme_analyst/
│   │   └── editor/
│   ├── distillation/
│   │   ├── templates/
│   │   ├── rules/
│   │   └── examples/
│   ├── generation/
│   │   ├── chart_specs/
│   │   ├── pandoc_template.tex
│   │   └── pdf_builder.py
│   ├── evaluation/
│   │   ├── judge_prompts/
│   │   ├── view_extractor.py
│   │   └── backtest_views.py
│   ├── scheduler/
│   └── runtime/
│       ├── reports/YYYYMMDD/
│       └── views.jsonl
│
├── macro-data-pit-mcp/                # ★ 新增
├── ai-industry-pit-mcp/               # ★ 新增
├── news-pit-mcp/                      # ★ 新增
│
├── ttjj-data-pit/                     # 现有，加行业估值算子
├── fund-portfolio-mcp/                # 现有，用于观点回测
├── world/                             # 现有，可承接编排
└── data/
    ├── fund.db                        # 现有，加 report 相关表
    ├── macro.db                       # ★ 新增
    ├── ai_industry.db                 # ★ 新增
    └── news.db                        # ★ 新增
```

---

## 10. 最小可用版（MVP）路径

如果先只做一件事验证可行性，建议：

**先写「一个 Agent + 招商周报的"数据·估值"章节」**。

理由：
- 数据全在 A 股，`ttjj-data-pit` 现成能用
- 章节结构最规范，好评估（跟原报告 diff）
- 不涉及叙事，幻觉风险最低
- 2 周内出结果，能判断整套架构是否值得投入

跑通后再逐章扩展：估值 → 复盘 → 资金 → 景气 → 主题 → 首席。

---

> **总结**：这不是"训一个大模型写研报"，而是"用一堆小 Agent + PIT 数据 + 显式模板"把研究员的方法论工程化。**难点不在 LLM，在数据 PIT 严格性 + 观点结构化 + 评估闭环**。做完你会有个 24×7 的策略研究员，还是个能回测胜率的那种。
