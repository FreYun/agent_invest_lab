# 基金（fund）

基金全维度数据。代码用 6 位数字数组 `fund_codes`（部分单基金工具用 `fund_code`）。

调用：`mcp__simworld_data__<工具名>(...)`；**`simulated_datetime` 由 proxy 自动注入，勿传**。精确 schema 见 `tools-catalog.json`。

## fund — 基金分析：基本信息/净值/业绩/持仓/经理/风格/费率/分红/筛选/申赎

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `fund_abnormal_movement` | 基金异动检测（PIT，21:00 入库约定）：返回单日涨跌幅超 4% 的事件。 | **fund_code**, start_date, direction |
| `fund_basic_info` | 基金基本信息（PIT）：基金公司、经理、类型、成立时间等。 | **fund_codes** |
| `fund_bonus` | 基金分红记录（PIT，FSRQ 除权日 <= T）。 | **fund_code**, date |
| `fund_index_return` | 基金指数超额收益（PIT，NAV 派生量血缘扩展，21:00 约定）。 | **fund_codes**, trade_date, period_codes |
| `fund_index_subscription_redemption` | 按指数汇总基金申赎（申请/赎回/净申赎），区分个人与机构客户。 | **index_code**, **calc_date**, cutoff_time |
| `fund_index_tracking` | 查某指数的跟踪基金（路由型 + 半 PIT，仅按 estabdate <= T 过滤）。 | **index_code** |
| `fund_industry_exposure` | 基金行业暴露（PIT，noticedate 闸门）。 | **fund_code**, source, industry_type, is_full |
| `fund_invest_position` | 基金详细持仓（PIT）：股票（A 股 + QDII）+ 债券（含债项评级）。 | **fund_code** |
| `fund_manager_profile` | 基金经理画像（PIT）：T 时刻在任经理 + 任职区间。 | **fund_codes** |
| `fund_nav` | 基金净值历史（PIT，21:00 入库可见约定）。 | **fund_codes**, start_date, end_date |
| `fund_performance` | 基金业绩（PIT）：近 N 周期收益率/最大回撤/夏普/卡玛/同类排名。 | **fund_codes** |
| `fund_rate` | 基金费率（PIT，半 PIT eutime 闸门）。 | **fund_code** |
| `fund_stock_holdings_screen` | 按股票反查持有该股的基金（PIT，noticedate 闸门）。 | **stock_codes**, fund_codes, match_all |
| `fund_style_analysis` | 基金风格分析（PIT）：大小盘 × 成长价值 × 行业配置。 | **fund_codes** |
| `fund_subscription_redemption_summary` | 基金申赎汇总。 | **calc_date**, cutoff_time |
| `fund_theme_screening` | 按主题筛选基金（PIT，双数据源）。 | **themes**, mode |
| `fund_top_holdings` | 基金前十大重仓股（PIT，按 noticedate 公告日闸门）。 | **fund_codes** |
| `fund_turnover_rate` | 基金换手率（PIT，noticedate 闸门）。 | **fund_code** |
