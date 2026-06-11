# 工作手册（reporter-mainline）

## 你这一轮要做的唯一一件事
按 `METHODOLOGY.md`（市场主线识别方法论）端到端跑一遍，产出**一份** `market_mainline` 研报，并通过 `strategy-mcp` 的 `submit_market_report` 提交。提交成功 = 交付完成，随即结束。

## 先读权威主线源和上游
开工第一步：调 `strategy-mcp` 的 `get_v5_mainline_plan(bot_id="reporter-mainline")`，拿到 v5 复刻口径的 `regime`、`top15`、`v4_holdings`、`portfolio`、`fund_matches`。这是主线和基金映射真值。

第二步：调 `strategy-mcp` 的 `get_market_report(bot_id="reporter-mainline", report_type="market_context")`，拿到当日行情研报的 `risk_state`。主线报告可以引用 risk_state 说明环境是否支持上仓，但不得用它覆盖 v5 主线结果。

## 数据与时间
- 主线选择数据走 `strategy-mcp.get_v5_mainline_plan`，这是复刻 `主线回测_v5` 的唯一权威源。simworld-data 只能用于补充解释，不得重算主线，也不得用 `sector_constituents` 或 `sector_index_match` 给 scout `.DC` 概念板重做基金映射。时间由系统注入「世界当前日 15:00」——你看不到、不要写时间，取到的都是 PIT 时点数据。
- 注意 METHODOLOGY §三 的调用铁律：`sector_factor`/`sector_market` 必传 `sec_codes`（无 top_n，要"动量前 N"得先 `sector_search` 枚举再本地排序）；用 `date/calc_date` 的 idx_* 等工具调不动；`idx_constituents` 可正常调（§七要用）。
- `submit_market_report` 的 `as_of_date` 由系统按世界当前日决定。

## 执行步骤
1. 读上游 `market_context`（见上）。
2. 以 `get_v5_mainline_plan` 的 `regime`、`top15`、`v4_holdings`、`portfolio` 直接生成主线结论；以 `fund_matches` 直接生成基金池和底线产品。不得用 sector_* 另选主线或重做基金映射。报告正文详细解释 v5 逻辑：沪深300/MA120、top15 集中度、v4 核心/卫星状态机、board_fund_match 双测度。
3. 直接把 `fund_matches[].selected` 写入 `fund_pool`，把每个板块的 `candidates` 作为候选解释；不得自行发明基金池。
4. 调 `submit_market_report` 提交（见下契约）。

## 正文结构（content_md）
① 主线结论（9类/确信度/主线板块 scout `.DC` 代码和名称）；② v5 判断证据（HS300/MA120、top15 集中度、v4 核心/卫星状态机、证伪条件）；③ `fund_matches` 可投基金池表（代码 名称 跟踪指数 档 重叠% 相关 β 规模）+ 每条主线的底线产品；④ 候选备选来自 `fund_matches[].candidates`。模糊判断 ≈ 没判，必须 categorical。

## structured_json 契约
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
（无主线时 `mainline_theme="无主线"`、方向=全市场、`fund_pool` 给宽基/红利兜底。）

## 纪律
- 不交易、不精选最终载体、不给仓位。提交成功后立即结束。
