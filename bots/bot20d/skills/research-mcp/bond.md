# 债券（bond）

国债收益率曲线 + 可转债。`bond_yield_curve` 的 `curve_type` 支持 cn/us/credit，`maturities` 必填。

调用：`mcp__simworld_data__<工具名>(...)`；**`simulated_datetime` 由 proxy 自动注入，勿传**。精确 schema 见 `tools-catalog.json`。

## bond — 债券：国债收益率曲线 + 可转债分析/配置利差/转股溢价

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `bond_yield_curve` | 国债收益率曲线（PIT，走 researchdata.dwd_bd_yield_curve_standard）。 | **maturities**, curve_type, start_date, end_date |
| `convertible_bond_analysis` | 可转债分析（PIT，双模式，L1）。底表 researchdata.dwd_bd_trd_convert ⟕ dim_bd_info。 | bond_codes, start_date, end_date, order_by, order_dir, top_n |
| `convertible_bond_market_spread` | 转债配置利差（PIT，L1）。底表 researchdata.dwa_bd_cvt_mkt_yield_spread。 | start_date, end_date |
| `convertible_bond_premium_estimate` | 转债百元券转股溢价率估计（PIT，L1）。底表 researchdata.dwa_bd_cvt_premium_rates。 | start_date, end_date |
