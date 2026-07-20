# 宏观 & 商品（macro / commodity）

宏观经济与商品。`macro_data` 按 `region`(cn/us)+`categories` 取；EDB 48 万指标先 `macro_indicator_search` 找 id 再 `macro_indicator_value` 取值。`commodity_market` 的 `market_type` 支持 spot/futures。

调用：`mcp__simworld_data__<工具名>(...)`；**`simulated_datetime` 由 proxy 自动注入，勿传**。精确 schema 见 `tools-catalog.json`。

## macro — 宏观：宏观数据 + EDB指标检索/取值 + Nowcasting快照

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `macro_data` | 宏观数据（PIT，COALESCE 派生兜底）。 | **region**, categories, start_date, end_date |
| `macro_indicator_search` | EDB 指标检索（48 万指标，name/ename LIKE + 国家/大类/重要度过滤，无 PIT）。 | **keyword**, country, majorindtype, important, limit |
| `macro_indicator_value` | EDB 指标取值（PIT，COALESCE(publish_date, indicator_dt+30d)<=T 闸门）。 | **indicator_ids**, start_date, end_date |
| `macro_nowcasting_snapshot` | 信澳宏观 Nowcasting 快照。 | — |

## commodity — 商品：现货/期货行情

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `commodity_market` | 商品行情（PIT，16:10 约定，走 researchdata.dwd_commodity_trd_daily）。 | market_type, symbols, start_date, end_date |
