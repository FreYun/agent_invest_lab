# 研究（research）

新闻/研报/观点/实体抽取。`research_search` 的 `search_type` 区分 news/research；`research_view` 的 `view_type` 走不同上游；`entity_extract` 从自然语言抽取基金/经理/公司/指数/主题（无 PIT）。

调用：`mcp__simworld_data__<工具名>(...)`；**`simulated_datetime` 由 proxy 自动注入，勿传**。精确 schema 见 `tools-catalog.json`。

## research — 研究：新闻/研报搜索 + 研究观点 + 金融实体抽取

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `entity_extract` | 从自然语言中抽取金融实体（基金/经理/公司/指数/主题）。 | **text** |
| `research_search` | 新闻 / 研报搜索（半 PIT，KB API + SHOWTIME/INFOCODE 兜底过滤）。 | **query**, search_type, top_k, start_date, end_date |
| `research_view` | 研究观点（PIT）。三种 view_type 走不同上游链路。 | **view_type**, labels, sec_codes, start_date, end_date, direction, latest_only, weeks, fund_code, days |
