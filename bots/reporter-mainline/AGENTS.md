# 工作手册（reporter-mainline）

## 你这一轮要做的唯一一件事
按 `METHODOLOGY.md`（市场主线识别方法论）端到端跑一遍，产出**一份** `market_mainline` 研报，并通过 `strategy-mcp` 的 `submit_market_report` 提交。提交成功 = 交付完成，随即结束。

## 先读上游
开工第一步：调 `strategy-mcp` 的 `get_market_report(bot_id="reporter-mainline", report_type="market_context")`，拿到**当日**行情研报的 `risk_state`（敢不敢上仓的总环境）。主线识别要在这个 regime 背景下做（如 Stress / risk_off 时主线即便存在也要在报告里点明"环境不支持上仓"）。

## 数据与时间
- 行情/板块/指数/基金数据走 `simworld-data`。时间由系统注入「世界当前日 15:00」——你看不到、不要写时间，取到的都是 PIT 时点数据。
- 注意 METHODOLOGY §三 的调用铁律：`sector_factor`/`sector_market` 必传 `sec_codes`（无 top_n，要"动量前 N"得先 `sector_search` 枚举再本地排序）；用 `date/calc_date` 的 idx_* 等工具调不动；`idx_constituents` 可正常调（§七要用）。
- `submit_market_report` 的 `as_of_date` 由系统按世界当前日决定。

## 执行步骤
1. 读上游 `market_context`（见上）。
2. 按 METHODOLOGY 阶段 0→5 选主线板块：环境闸门 → 候选定位（行业+主题双轨）→ 三重独立确认 → 三道质量闸门 → 时序定位 → 综合定档 + 客观第二意见。归到 9 类之一/多类共振/无主线。
3. 按 METHODOLOGY §七 双测度（成分重叠 × 净值相关）从可买池筛**可投基金池**，分档（纯载体/代理/弱代理/兜底），每条主线至少给 1 只。
4. 调 `submit_market_report` 提交（见下契约）。

## 正文结构（content_md）
① 主线结论（9类/确信度/主线板块 BKxxxxxx 名）；② 阶段 0→5 的判断与证据（三重得分、三道闸门含拥挤分位、时序、第二意见、证伪条件）；③ §七可投基金池表（代码 名称 跟踪指数 档 重叠% 相关 β 规模）+ 每条主线的底线产品；④ 剔除清单与原因。模糊判断 ≈ 没判，必须 categorical。

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
