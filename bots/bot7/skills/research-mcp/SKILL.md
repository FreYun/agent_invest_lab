---
name: research-mcp
description: 金融研究数据 MCP — 92 个工具，分 10 个类别。按需 Read 子模块获取工具详情。
---

# Research MCP（金融研究数据）

直连 MCP，92 个原子工具，分 10 个类别（工具箱）。调用方式：`npx mcporter call "research-mcp.tool_name(...)"`

## 10 个工具箱

| 类别 | 工具数 | 说明 | 详见 |
|------|--------|------|------|
| market_ashares | 8 | A股行情、指数、估值、恐慌指数 | `market.md` |
| market_hk | 6 | 港股行情、恒生指数、南向资金 | `market.md` |
| market_us | 5 | 美股指数（道琼斯/标普/纳指） | `market.md` |
| stock | 29 | 个股基本面、行情、估值、资金、因子 | `stock.md` |
| fund | 22 | 基金分析、净值、经理、风格、持仓 | `fund.md` |
| fund_screen | 7 | 基金筛选（类型/主题/持仓/换手率） | `fund.md` |
| strategy | 5 | 投顾产品信息、净值、绩效、持仓、权益分析 | `strategy.md` |
| bond | 5 | 国债/信用债收益率、利差 | `bond.md` |
| macro | 3 | 中国/美国宏观经济数据 | `macro.md` |
| commodity | 2 | 商品现货与期货行情 | `macro.md` |
| news_report | 5 | 新闻/研报搜索、实体识别 | `news.md` |

## 格式铁律

- **日期**：`YYYYMMDD`（如 `20260317`），不是 `YYYY-MM-DD`
- **A股指数**：带后缀 `000300.SH`, `399006.SZ`
- **港股指数**：`HSI.HI`, `HSTECH.HI`, `HSCEI.HI`
- **美股指数**：`DJIA.GI`, `SPX.GI`, `NDX.GI`
- **股票/基金代码**：6 位纯数字 `600519`, `000001`
- **中国宏观 category**：小写 `gdp,cpi,ppi,m2`
- **债券期限**：国债大写 `10Y`，信用债小写 `3y`

## 意图路由（按需求找模块，Read 对应文件获取工具详情）

| 我想… | 推荐工具 | 详见 |
|--------|---------|------|
| **看 A 股/港股/美股大盘** | `market_overview` / `get_hshares_market_overview` | `market.md` |
| **查指数行情、估值、成交额** | `get_ashares_index_quote` / `get_ashares_index_val` | `market.md` |
| **查恐慌指数、南向资金** | `get_ashares_gvix` / `get_southbound_hkd_turnover` | `market.md` |
| **全面分析一只基金** | `get_fund_comprehensive_analysis` — 一站式 | `fund.md` |
| **批量查基金净值收益率** | `fund_basicinfo` | `fund.md` |
| **比较多只基金走势** | `get_multiple_funds_nav` | `fund.md` |
| **按条件筛基金** | `simple_fund_search` / `fund_theme_screening` | `fund.md` |
| **查投顾产品信息/净值/绩效** | `get_strategy_info` / `get_strategy_nav` / `get_strategy_performance` | `strategy.md` |
| **查投顾产品持仓/权益主题** | `get_strategy_portfolio` / `get_strategy_equity_analysis` | `strategy.md` |
| **找重仓某股票的基金** | `fund_stock_holdings` | `fund.md` |
| **查个股基本面/K线/估值** | `get_stock_info` / `get_stock_daily_quote` / `get_stock_valuation` | `stock.md` |
| **查个股资金流向/北向持股** | `get_stock_fund_flow` / `get_stock_northbound_holding` | `stock.md` |
| **查国债/信用债收益率** | `get_cn_bond_yield` / `get_credit_bond_yield` | `bond.md` |
| **查中美利差/信用利差** | `get_bond_yield_spread` | `bond.md` |
| **查中国 GDP/CPI/M2 等** | `get_cn_macro_data` | `macro.md` |
| **查美国宏观数据** | `us_macro_simple` | `macro.md` |
| **查黄金等商品行情** | `commodity_data` | `macro.md` |
| **搜新闻** | `news_search` | `news.md` |
| **搜研报** | `research_search` | `news.md` |
| **从文本提取股票/基金名** | `query_segment` | `news.md` |

## 常用工作流

### 市场日报
```
market_overview()
get_hshares_market_overview()
get_usstock_index_quote(symbol="DJIA.GI,SPX.GI,NDX.GI", start_date="20250101", end_date="20250317")
get_ashares_gvix(start_date="20250101", end_date="20250317")
news_search(query="A股 市场 热点", top_k=5, search_day_ago=3)
```

### 基金分析
```
get_fund_comprehensive_analysis(fund_code="110011")
# 如需净值走势：
get_fund_nav_and_return(fund_code="110011", start_date="20240101", end_date="20250317")
# 如需异动检测：
get_fund_abnormal_movement(fund_code="110011", start_date="20240101")
# 如需对比多只基金：
get_multiple_funds_nav(fund_codes="110011,000001", start_date="20240101", end_date="20250317")
```

### 投顾产品
```
get_strategy_info(strategy="")
get_strategy_nav(strategy="", as_of_date="")
get_strategy_performance(strategy="", as_of_date="")
get_strategy_portfolio(strategy="", as_of_date="")
get_strategy_equity_analysis(strategy="", as_of_date="")
```

### 个股研究
```
get_stock_info(stock_code="600519")
get_stock_daily_quote(stock_code="600519", start_date="20250101", end_date="20250317")
get_stock_valuation(stock_code="600519", start_date="20250101", end_date="20250317")
get_stock_fund_flow(stock_code="600519", start_date="20250101", end_date="20250317")
get_stock_northbound_holding(stock_code="600519", start_date="20250101", end_date="20250317")
fund_stock_holdings(stock_codes="600519", match_all=false)
```

### 宏观环境
`get_cn_macro_data`(cpi,ppi,m2,pmi) → `us_macro_simple` → `get_cn_bond_yield`(10Y) → `get_bond_yield_spread`(cn_vs_us) → `commodity_data`(AU9999)
