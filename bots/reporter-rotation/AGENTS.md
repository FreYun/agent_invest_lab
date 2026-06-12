# 工作手册（reporter-rotation）

## 你这一轮要做的唯一一件事
按 `METHODOLOGY.md`（主线轮动配置方法论）跑一遍，产出**一份** `mainline_rotation` 组合研报，并通过 `strategy-mcp` 的 `submit_market_report` 提交。提交成功 = 交付完成，随即结束。

## 先读上游 + 读自己上一期（计数器续期的关键）
开工前两步：
1. `get_market_report(bot_id="reporter-rotation", report_type="market_context")` → 拿 `risk_state`（决定能不能抱主线）。
2. `get_market_report(bot_id="reporter-rotation", report_type="market_mainline")` → 拿当日主线板块 + 可投基金池（你**不重复识别主线**，直接消费）。
3. `get_market_report(bot_id="reporter-rotation", report_type="mainline_rotation")` → 此刻你**今天还没提交**，所以拿到的是**上一期**的组合与计数器（核心/卫星集合、各自 in_topK/out_topK/破MA60 天数、入选日）。**以此为基准推进计数器**，不要从零起算。

## 计数器怎么续（重要）
- 报告是月度生成（约隔 20 交易日），但"连续 X 日 top5 / 破 MA60 / 出 top15"这类计数器要按**日度**口径算：用 `sector_factor(sec_codes=[...], start_date=回看窗)` 拉**日度动量序列**、`market_index_quote`/板块行情拉 MA60，在本地回算"截至今日连续满足了几日"，叠加上一期报告里的累计值与入选日。
- 路径依赖状态（某板块何时晋升核心、是否在冷却期）从上一期报告续上。两期间隔 ≈ 20 交易日，最小持有期(15日)/冷却期(15日)通常已自然满足。

## 数据与时间
- 走 `simworld-data`；时间由系统注入「世界当前日 15:00」，你看不到、不要写时间，取到的都是 PIT 数据。
- 注意 METHODOLOGY 与 market-mainline §三 的调用铁律（sector_factor 必传 sec_codes、idx_* 日期类工具调不动、idx_constituents 可调）。
- `submit_market_report` 的 `as_of_date` 由系统按世界当前日决定。

## 执行步骤
1. 读上游 context + mainline + 自己上一期 rotation（见上）。
2. 第0层 regime 开关：沪深300 距 MA120 偏离 + 主线集中度 → 抱主线 / 无主线·宽基 / 防御·红利（切换要连续 ≥5 日确认 + 年线 ±3% 迟滞带，据上一期 regime_days 续期）。
3. 抱主线态：按第1-3层定核心（连续≥40日 top5 且站 MA60）+ 卫星（连续≥5日 top3，优先扩散段）；每个持仓板块交 market-mainline §七双测度 + fund-screening 取 1 只载体，等权。
4. 应用换仓纪律：默认 hold；只有确认型触发才调仓；遵守最小持有期/冷却期/小变动不调。
5. 调 `submit_market_report` 提交（见下契约）。

## 正文结构（content_md）
① regime（含连续维持天数）；② 组合（等权，核心/卫星：板块→基金代码名称、已持天数、站MA60、拥挤分位）；③ 今日动作（绝大多数=维持不动；若调仓写明触发与确认天数）；④ 计数器（各候选/核心 距触发阈值还差几日）；⑤ 再平衡/证伪条件。

## structured_json 契约
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

## 纪律
- 不交易、不给某个 bot 的具体下单——只给"推荐组合骨架 + 今日动作"。稳为先，拿不准就维持原组合。提交成功后立即结束。
