---
name: research-mcp
description: ttjj（天天基金）研究数据 — 运行时经 simworld-data 提供 58 个时点(PIT)工具，覆盖基金/个股/板块/指数因子/市场择时/债券/宏观/商品/研究。按需 Read 子模块取工具详情。
---

# ttjj 研究数据（天天基金 · simworld-data）

bot 在 world 里实际能调的研究数据工具由 **`simworld-data`** 这个 MCP 提供，上游是天天基金（ttjj，`research.tiantianfunds.com.cn`），共 **58 个时点工具**，分 10 类。

## 调用方式（铁律）

- 工具以 **`mcp__simworld_data__<工具名>`** 形式暴露，例如 `mcp__simworld_data__fund_nav(...)`、`mcp__simworld_data__market_index_quote(...)`。
- **时点参数 `simulated_datetime` 不要传** —— world 的 simworld-proxy 会自动按"当前世界日期 15:00:00"注入，你看到的 schema 里也没有它。传了反而可能被拒。
- 其余参数（`start_date`/`end_date`/`trade_date`/`date`/代码列表等）照常传。
- 所有工具都是 **PIT（Point-In-Time）**：只返回世界当前日期当天及之前的数据，未来数据自动截断。

## 格式约定

- **日期**：`YYYY-MM-DD`（如 `2026-05-22`）。
- **基金/股票代码**：6 位纯数字 `600519`、`000001`（多数工具收 `*_codes` 数组）。
- **指数行情/估值**（`market_*`）：带后缀 `000300.SH`、`399006.SZ`。
- **指数因子**（`idx_*`）：用东财内部 **`securityvarietycode`**（如 `1000157392`），不是 `000300.SH`；可先用 `sector_search` / `sector_index_match` 找代码。
- **板块**（`sector_*`）：板块代码 `BKxxxxxx`，可用 `sector_search` 模糊检索。
- **精确入参/出参以 `tools-catalog.json` 为准**（已剥掉自动注入的 simulated_datetime）。

## 10 个类别（按需 Read 对应子文档）

| 类别 | 工具数 | 说明 | 详见 |
|------|--------|------|------|
| fund | 18 | 基金信息/净值/业绩/持仓/经理/风格/费率/分红/筛选/申赎 | `fund.md` |
| stock | 8 | 个股画像/行情/资金/股权/财务质量/Alpha/事件/技术因子 | `stock.md` |
| market_timing | 8 | 指数行情估值/股债性价比/市场温度/VIX/期权波动率/择时因子 | `market.md` |
| index_factor | 5 | 指数成分权重 + 拥挤度/乖离/成交集中度及历史分位 | `market.md` |
| sector | 6 | 板块检索/成分/行情/因子分/子因子/指数↔板块匹配 | `sector.md` |
| bond | 4 | 国债收益率曲线 + 可转债分析/利差/溢价 | `bond.md` |
| macro | 4 | 宏观数据 + EDB 指标检索/取值 + Nowcasting | `macro.md` |
| commodity | 1 | 商品现货/期货行情 | `macro.md` |
| research | 3 | 新闻/研报搜索 + 研究观点 + 金融实体抽取 | `research.md` |
| system | 1 | `health_check` 健康检查 | — |

## 意图路由（按需求找工具，Read 对应子文档取详情）

| 我想… | 推荐工具 | 详见 |
|--------|---------|------|
| **看某基金净值/业绩** | `fund_nav` / `fund_performance` | `fund.md` |
| **看基金持仓/重仓股/风格** | `fund_invest_position` / `fund_top_holdings` / `fund_style_analysis` | `fund.md` |
| **按主题/持仓股筛基金** | `fund_theme_screening` / `fund_stock_holdings_screen` | `fund.md` |
| **某指数的跟踪基金** | `fund_index_tracking` | `fund.md` |
| **看个股行情/估值/资金/财务** | `stock_market` / `stock_capital_flow` / `stock_financial_quality` | `stock.md` |
| **个股一致预期/Barra/选股因子** | `stock_alpha` / `stock_factor` | `stock.md` |
| **看大盘指数行情/估值** | `market_index_quote` / `market_index_val` | `market.md` |
| **做仓位择时（股债性价比/温度/VIX/因子）** | `market_index_gzxjb` / `market_temperature` / `option_vix` / `quant_factor` | `market.md` |
| **指数拥挤度/均线乖离分位** | `idx_congestion_pctrank` / `idx_ma200_deviation` / `idx_corrected_deviation` | `market.md` |
| **看板块行情/因子/成分** | `sector_market` / `sector_factor` / `sector_constituents` | `sector.md` |
| **指数↔板块/ETF 映射** | `sector_index_match` | `sector.md` |
| **国债收益率/可转债** | `bond_yield_curve` / `convertible_bond_analysis` | `bond.md` |
| **宏观数据/EDB 指标** | `macro_data` / `macro_indicator_search` / `macro_indicator_value` | `macro.md` |
| **商品/期货行情** | `commodity_market` | `macro.md` |
| **搜新闻/研报、抽实体** | `research_search` / `research_view` / `entity_extract` | `research.md` |

> 注：投顾产品（净值/持仓/绩效）不在本技能里，走 `strategy-mcp`。本技能只提供研究数据。
