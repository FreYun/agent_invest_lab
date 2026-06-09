# 市场研究报告预生成系统（market-reports）设计

> 把三套**通用、复杂、对各 bot 一致**的市场分析流程（market-context / market-mainline /
> mainline-rotation）从「每 bot 每天重算」抽出来，由系统**预生成**为研报、存库、**全局共享**，
> 回测时各 bot 经工具**读报告**再结合自身账户/人设决策。

## 1. 为什么这么做

- 这三套流程端到端要调几十次 MCP、做大量 LLM 推理。让 23 个 bot **每天各跑一遍**既贵又冗余——
  而 market-context（regime）和 market-mainline（主线+基金池）本质是**客观市场判断**，与具体 bot 无关。
- 历史 PIT 数据是定死的 → 给定 as_of 日期，报告是**确定的** → **全历史只生成一次，所有 run / 所有 bot 复用**。
- bot 的**人设差异**不再体现在"重算 regime/主线"，而是下沉到"**读完同一份报告后怎么用**"
  （METHODOLOGY 据 risk_state + 主线确信度定自己的仓位/载体）。

## 2. 数据流

```
[pre-pass 离线驱动]  遍历回测日历，每月第一个交易日：
   PIT 钉到 as_of 日 → 顺序跑 3 个 reporter agent
     reporter-context  →(submit)→ market_context 报告
     reporter-mainline →(读 context, submit)→ market_mainline 报告
     reporter-rotation →(读 context+mainline, submit)→ mainline_rotation 报告
   ↓ 全部经 strategy-server.submit_market_report 落库
[fund.db] market_reports 表（scope=global）
   ↑ strategy-server.get_market_report（PIT：as_of<=世界当前日 的最近一期）
[bot 回测]  每日决策前 get_market_report(report_type=all) → 读三份 → 结合自身账户/人设操作
```

- **PIT 红线**：报告生成本身钉在 as_of 日（reporter 走 simworld-proxy 注入的 simulated_datetime）；
  读取端 `get_market_report` 由 strategy-server 用世界当前日 `getCurrentDate()` 做 `as_of_date<=` 截断。
  bot 只传 `report_type`，拿不到也改不了日期游标 → 未来函数不可能泄漏。
- **粒度**：架构按**日度**设计（cadence 可配）；回测先只在**每月第一个交易日**生成，月内各日 bot 读到同一份。
  rotation 的日度计数器（连续 X 日 top5 / 破 MA60…）由 agent 在生成日用 `sector_factor(start_date=回看窗)`
  拉日度序列**回算**，不依赖"每天真的跑一次"；路径依赖状态（核心晋升日/冷却期）靠读上一期报告续上。

## 3. 存储：fund.db `market_reports`

DDL 见 `scripts/sql/market_reports.sql`；strategy-server 启动时幂等 ensure。

| 列 | 含义 |
|---|---|
| `report_type` | `market_context` \| `market_mainline` \| `mainline_rotation` |
| `as_of_date` | YYYY-MM-DD，报告对应世界交易日 |
| `scope` | 恒 `global`（预留 per-bot/per-run 变体） |
| `content_md` | 研报正文（markdown） |
| `structured_json` | categorical 结构化字段（见 §5 契约） |
| `agent_run_id` | 生成该报告的 reporter run 标识（审计回放） |
| `generated_at` | 写入时间 |

`UNIQUE(report_type, as_of_date, scope)` → 同 (类型,日期) 重复提交覆盖（幂等续跑）。

## 4. 工具（strategy-server，已在每个 bot 的 MCP 列表）

- `get_market_report(bot_id, report_type)` — `report_type` 取三类之一或 `all`。返回 `as_of<=世界当前日`
  的最近一期；无则返回占位提示（非报错，bot 自行保守处理）。每份正文前有 `<!-- report_type=.. as_of=.. -->` 注释标注 vintage。
- `submit_market_report(bot_id, report_type, content_md, structured_json?)` — **仅 `reporter-` 开头的
  bot_id 可写**；`as_of_date` 由世界当前日决定（agent 无法指定）。

## 5. 报告契约（content_md 结构 + structured_json schema）

> reporter agent 产出端与 bot 消费端的共同合约。`content_md` 是给 bot/人读的研报；
> `structured_json` 是机器可读的 categorical 摘要（dashboard/research/下游可直接取）。
> 字段口径直接对齐三个 SKILL.md 的「输出范式」节。

### 5.1 market_context（行情/regime）

`structured_json`：
```json
{
  "regime": "复苏|宽松|通缩衰退|通胀|震荡",
  "regime_code": "recovery|easing|deflation|inflation|range",
  "confidence": "高|中|低",
  "risk_state": "risk_on_attack|risk_on_overweight|neutral|risk_off",
  "dominant_var": "一句话：当前哪个变量最能解释资产价格",
  "macro": { "growth": "...", "inflation": "...", "liquidity": "...", "risk_appetite": "..." },
  "price_check": { "stock": true, "bond": false, "gold": null },
  "counter_evidence": ["..."],
  "persistence": "第几期 / 是否连续两期确认",
  "stress": false,
  "risk_notes": ["..."],
  "one_liner": "定调 + 后市方向"
}
```
`content_md`：标题 + 上述各项展开的研报叙事（宏观四维判断、价格验证、反向证据、持续性、Stress 检查、风险提示）。

### 5.2 market_mainline（主线 + 基金池）

`structured_json`：
```json
{
  "mainline_theme": "9类之一 | 多类共振 | 无主线",
  "confidence": "强|中|弱|无",
  "mainline_sectors": [{ "code": "BKxxxxxx", "name": "...", "type": "industry|theme" }],
  "lifecycle": "初期/加速期 | 共识松动期",
  "quality": "情绪冷|情绪过热|中性",
  "triple_score": { "capital": 1, "prosperity": 1, "narrative": 0 },
  "gates": { "persistence": true, "diffusion": true, "crowding_pct": 78 },
  "second_opinion": "market_sentiment_20_90 = 0|0.5|1，与自判是否一致",
  "falsification": "满足什么就降档/推翻",
  "fund_pool": [
    { "code": "512480", "name": "...", "track_index": "...", "tier": "纯载体|代理|弱代理|兜底",
      "overlap": 0.62, "corr": 0.86, "beta": 1.1, "scale_yi": 80 }
  ],
  "bottom_line_product": { "code": "512480", "tier": "..." },
  "excluded": [{ "code": "...", "name": "...", "reason": "净值<半年|规模<2亿|相关<0.5" }]
}
```
`content_md`：主线识别流水线叙事（阶段0→5）+ §六输出范式 + §七基金池表。

### 5.3 mainline_rotation（主线轮动组合骨架）

`structured_json`：
```json
{
  "regime": "抱主线|无主线·宽基|防御·红利",
  "regime_days": 7,
  "portfolio": [
    { "role": "core|satellite", "sector": "BKxxxxxx 名", "fund_code": "512480", "fund_name": "...",
      "held_days": 62, "above_ma60": true, "crowding_pct": 78 }
  ],
  "today_action": "维持不动 | 新进<..> | 剔除<..>(原因+确认天数) | 留仓加仓<回调核心>",
  "counters": [{ "sector": "BKxxxxxx", "in_topk_days": 38, "out_topk_days": 0, "break_ma60_days": 0 }],
  "rebalance_falsification": "核心若连续3日破MA60或连续20日出top15则降级; regime 信号连续5日翻转则切打法"
}
```
`content_md`：五层框架判断叙事 + §换仓纪律检查 + §输出范式表。

> **rotation 是组合骨架、不是某个 bot 的实际持仓**：报告给"推荐的核心/卫星 + 载体基金 + 今日动作"，
> 每个 bot 据自己实际账户做 reconcile（买/卖到目标），总仓位由 METHODOLOGY 据 risk_state + regime 定。

## 6. 组件清单与状态

| # | 组件 | 位置 | 状态 |
|---|---|---|---|
| 1 | `market_reports` 表 + `fundDbFile()` | `scripts/sql/market_reports.sql` · `src/paths.ts` | ✅ |
| 2 | `get/submit_market_report` 工具 | `src/strategy-server/server.ts`（+ 测试） | ✅ |
| 3 | 报告契约 | 本文档 | ✅ |
| 4 | 3 个 reporter agent 工作区 | `bots/reporter-{context,mainline,rotation}/` | ✅ |
| 5 | pre-pass 月初日历驱动 | `src/market-reports/run-prepass.ts` + `config/world-market-reports.yaml` + `reporterMode`(config.ts/run.ts) + reporter 工作区 `FUND_UNIVERSE.md` | ✅ 代码完成/已验证；⏳ 实跑待确认 |
| 6 | bot 侧三个 skill 改薄 | `bots/bot101/skills/market-*`（消费端） | ✅ bot101 三个已改；其余 11 bot 的 market-context（不同变体）待你定是否一并铺开 |

## 7. 运行方式

```bash
cd world
npm run prepass                              # 用 config/world-market-reports.yaml 新建 run，全窗生成
npm run prepass -- --run-id <id> --resume    # 续跑（幂等：submit 覆盖同期；resume 从 cursor 接）
```

- 复用 `runWorld`：`reporter_mode: true` + 不设 `fund_mcp_cli` 把交易回测切成研报生成；`chat_step_mode: monthly` 每月首个交易日唤起；`concurrency: 1` + bots 顺序 = 同日 context→mainline→rotation 顺序执行。
- 与主 bot 回测物理解耦：**先跑 pre-pass 生成报告，再跑 bot 回测读取**。报告 scope=global、PIT 确定 → 全历史只生成一次、所有 run/bot 复用。

### 已知遗留 / 风险
- **18078 必须跑 simworld-mcp（62 工具）**，不能跑 ttjj_data_pit_mcp.py（19 工具，无 sector_*）；`./restart.sh` 会误切到 ttjj 致 sector 系 bot+reporter 全挂。详见 §1 发现 A。
- `test/pi-smoke.test.ts` / `test/resume.test.ts` 的 WorldConfig 字面量缺 `chatStepMode`（既有 tsc 报错，非本功能引入）。
- §七 选基金引擎 `scout/board_fund_match.py` 不在工作树 → 由 reporter LLM 用 `FUND_UNIVERSE.md`（1032 只全集，带主题/指数代码）+ idx_constituents/fund_nav 工具逐只算。
