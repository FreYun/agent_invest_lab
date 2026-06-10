---
name: market-mainline
description: 市场主线识别（消费端）。**不再自己识别主线/筛基金池**——系统侧 reporter 已按方法论预生成全局共享的 market_mainline 研报（主线归属+确信度+主线板块 BKxxxxxx+可投基金池，§七成分重叠×净值相关双测度已算好）。本 skill = 调 get_market_report 取回当期研报，按其主线板块/基金池结合自身人设精选载体、定仓。与 market-context（risk_state）、mainline-rotation（组合）配套。
---

# 市场主线识别 /market-mainline（消费端）

**触发词**: `/market-mainline`、"今天的主线是什么"、"识别市场主线"、"哪个板块是主线"
**执行时机**: 每日 risk_state（market-context）之后、仓位/选品决策之前。

> **本 skill 已从「自己算」改为「读报告」。** 主线识别（动量+资金+景气/叙事三证据 × 持续/扩散/拥挤三闸门）与 §七「主线板块→可投基金池」（成分重叠×净值相关双测度）几乎纯客观、对所有 bot 一致，已由系统侧 `reporter-mainline` 在每月初按完整方法论预生成。你**不要再自己跑那套 sector 分析与逐只基金匹配**——直接读当期报告，把精力放在「在给定基金池里精选最终载体、据主线确信度定仓位」。

## 怎么用

1. **取报告**：调 `strategy-mcp` 的 `get_market_report(bot_id="<你的id>", report_type="market_mainline")`（或 `all`）。PIT：当前世界日及之前最近一期。
2. **读关键字段**：
   - `mainline_theme` / `confidence`：主线归属（9类之一/多类共振/无主线）+ 确信度（强/中/弱/无）。
   - `mainline_sectors`：主线板块 BKxxxxxx + 名称。
   - `fund_pool`：**已筛好的可投基金池**——每只带 `tier`（纯载体/代理/弱代理/兜底）、`overlap`(成分重叠)、`corr`(净值相关)、`beta`、`scale`。`bottom_line_product`：每条主线的底线产品。
   - `lifecycle`(时序) / `gates`(三闸门含拥挤分位) / `falsification`(证伪条件)。
3. **据人设精选 + 定仓**（你的活）：
   - 在 `fund_pool` 内精选最终 1–3 只载体（交 `fund-screening`：跟踪质量40%/费率35%/规模25%；优先场内 ETF 控换手）。
   - 仓位由你的 METHODOLOGY 据主线 `confidence`（强/中/弱）+ market-context 的 `risk_state` 决定。本 skill 不直接给配比。
   - `无主线` 时方向=全市场，按报告兜底的宽基/红利处理，别硬找主线。

## 报告缺失时（兜底）
`get_market_report` 返回"暂无报告" → 按 `无主线/全市场` 保守处理，方向落到宽基/红利，并在巡检里记一笔。

## 边界
- 上游：`market-context` 的 risk_state。下游：`fund-screening` 精排、`METHODOLOGY` 定仓、`mainline-rotation` 组合。
- 完整识别+匹配方法论（阶段0→5、§七双测度、硬门槛）现由系统侧 `reporter-mainline` 持有并执行，见 `world/docs/market-reports-design.md`。候选基金宇宙见 reporter 的 `FUND_UNIVERSE.md`。
