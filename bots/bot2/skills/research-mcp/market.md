# 市场择时 & 指数因子（market_timing / index_factor）

大盘择时与指数因子。`market_*` 指数用带后缀代码 `000300.SH`；`idx_*` 指数因子用东财内部 `securityvarietycode`（如 `1000157392`），可经 `sector_index_match` 找。`quant_factor` 是研究部离线挖掘+回测过的择时因子，作客观第二意见。

调用：`mcp__simworld_data__<工具名>(...)`；**`simulated_datetime` 由 proxy 自动注入，勿传**。精确 schema 见 `tools-catalog.json`。

## market_timing — 市场/择时：指数行情估值/股债性价比/市场温度/VIX/期权波动率/研究部择时因子

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `macro_50etf_vix` | 50ETF VIX（PIT，15:00 收盘可见约定）。 | start_date, end_date |
| `market_index_gzxjb` | A股指数股债性价比（PIT，15:00 收盘后可见）：股票相对债券的风险溢价，做仓位择时。 | **symbols**, calmodel, caltype, start_date, end_date |
| `market_index_quote` | 指数行情（PIT，15:00 收盘后可见）。当前只有 market='cn' 真接入，hk/us 上游未实现。 | **market**, **symbols**, start_date, end_date |
| `market_index_val` | A股指数估值（PIT，15:00 收盘后可见）：返回 PE_TTM + 历史百分位的单点快照。 | **symbols**, end_date |
| `market_temperature` | A股市场温度（PIT，15:00 收盘后可见）：市场情绪/风险综合温度计，可做仓位择时。 | start_date, end_date |
| `option_gvspread_signal` | 期权 GV-Spread 交易信号（PIT，15:00 收盘后可见，L1）：做波动率/拥挤度择时。 | type_codes, start_date, end_date |
| `option_vix` | 期权多品种波动率（PIT，15:00 收盘后可见，L1）：VIX / GVIX / GVSpread + 成交量金额。 | type_codes, start_date, end_date |
| `quant_factor` | 研究部离线挖掘 + 回测验证的择时因子（PIT 在线计算）。 | history_days |

## index_factor — 指数因子（择时）：成分权重 + 拥挤度/乖离/成交集中度及历史分位

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `idx_congestion_pctrank` | 指数复合拥挤度 + 历史分位（researchdata.dwa_idx_factor_ma5cjjzdpctrk JOIN dwa_idx_factor_ma5cjjzd）。 | **securityvarietycode**, **date**, win |
| `idx_constituents` | 指数成分权重（as-of-T，PIT）：返回 <= T 最新一期成分股 + 权重百分比。 | **index_code**, top_n |
| `idx_corrected_deviation` | 指数修正相对乖离度 + 历史分位（researchdata.dwa_idx_factor_ma200gllrk）。 | **securityvarietycode**, **date**, win |
| `idx_ma200_deviation` | 指数 200 日均线乖离率因子（researchdata.dwa_idx_factor_ma200gll.ma200gll）：原始 ma200gll 值。 | **securityvarietycode**, **date** |
| `idx_turnover_concentration` | 指数成交集中度因子（researchdata.dwa_idx_factor_cjjzd.cjjzd）：原始 cjjzd 值。 | **securityvarietycode**, **date** |
