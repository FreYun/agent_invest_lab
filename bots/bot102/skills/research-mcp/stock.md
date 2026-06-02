# 个股（stock）

个股基本面/行情/资金/因子。代码用 6 位数字 `stock_codes` 数组。`stock_factor` 支持横截面选股 + 时序两种模式。

调用：`mcp__simworld_data__<工具名>(...)`；**`simulated_datetime` 由 proxy 自动注入，勿传**。精确 schema 见 `tools-catalog.json`。

## stock — 个股：画像/行情/资金/股权/财务质量/Alpha因子/事件/技术因子

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `stock_alpha` | Alpha 因子（PIT，15:00 约定）：一致预期 + Barra 暴露/归因。 | **stock_codes**, trade_date, start_date, end_date |
| `stock_capital_flow` | 股票资金流向（PIT，15:00 约定）：资金流 + 北向持股 + 超大单。 | **stock_codes**, start_date, end_date |
| `stock_events` | 股票事件（PIT）：停复牌（suspendtime）+ SUE（15:00 约定）。 | **stock_codes**, start_date, end_date |
| `stock_factor` | 个股技术/模型因子（PIT as-of，双模式：横截面选股 + 时间序列）。 | **factor_category**, stock_code, start_date, universe, order_by, top_n, offset |
| `stock_financial_quality` | 财务质量（PIT，15:00 约定）：盈利能力/收益质量/营运/资本结构 等 8 张表。 | **stock_codes**, trade_date, d_type |
| `stock_market` | 股票行情（PIT，15:00 收盘约定）：行情/市值/估值/股息率。 | **stock_codes**, start_date, end_date, trade_date |
| `stock_ownership` | 股权结构（PIT，noticedate 公告日闸门）：股东 + 股本结构 + 股权分配。 | **stock_codes**, report_date |
| `stock_profile` | 股票画像（PIT）：基础信息、申万行业、中信行业（按 T 时分类）。 | **stock_codes** |
