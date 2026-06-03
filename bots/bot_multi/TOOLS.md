# TOOLS.md - bot_multi 工具配置

## Bot 专属配置

- **account_id**: `bot_multi`
- **小红书 MCP**: 单进程多租户，所有 bot 共用 `:18060`，URL path 自动识别身份（已配置在 `mcporter.json`）。

## 基金组合管理：fund-portfolio-mcp

| 工具 | 使用场景 |
|---|---|
| `portfolio_get_buyable_funds` | 每轮先确认实际可买基金池，按 A股/债券/黄金/货币分类 |
| `get_fund_detail` | 只在最终 1-3 只候选阶段深查基金主题、风格、长期业绩、回撤、Sharpe、排名 |
| `portfolio_get_my_history` | 查询账户、持仓、订单和历史动作 |
| `portfolio_get_my_trades` | 查询交易记录，检查短持和重复交易 |
| `portfolio_get_my_performance` | 查询账户绩效和回撤序列，供回撤闸门使用 |
| `portfolio_place_buy_order` | 下买入订单，`reason` 必须写明状态、主线、权重变化、风险闸门 |
| `portfolio_place_sell_order` | 下卖出订单，`reason` 必须写明触发闸门、降仓对象和恢复条件 |

## 数据与研究：simworld-data

| 资产或模块 | 首选数据 |
|---|---|
| A股 | `market_index_quote`, `market_index_val`, `market_index_gzxjb`, `market_temperature`, `option_vix`, `stock_market`, `stock_capital_flow` |
| 债券 | `bond_yield_curve`, `bond_yield_curve(curve_type="cn")`, `bond_yield_curve(curve_type="credit")`, `bond_yield_curve(credit−cn 自算利差)` |
| 黄金 | `commodity_market(symbols=["黄金9999"或"AU9999"])`, `commodity_market(market_type="futures")`, `research_search(search_type="news")` |
| 现金 | 可用现金、货币基金收益、短端利率、其他资产信号强弱 |
| 宏观 | `macro_data`, `macro_data(region="cn")`, `macro_nowcasting_snapshot`, `macro_data(region="us")` |
| 研报与事件 | `research_search`, `research_view`, `research_search(search_type="news")` |

## 工具使用顺序

1. 先读 daily prompt 已注入的数据：账户、持仓、绩效、主要指数、可买池摘要。
2. 关键字段缺失时补调 `fund-portfolio-mcp`。
3. 四类资产证据不足时补调 `simworld-data`。
4. 只在候选基金已收敛后调用 `get_fund_detail`。
5. 下单前重新核对持仓、现金、成本和风险闸门。

## 禁止事项

- 不得用 `fund_nav` 或基金详情工具扫描全池。
- 不得为了相对强弱比较批量深查几十只基金。
- 不得跳过债券或黄金数据，直接用 A股结论推导组合权重。
- 不得把浏览器、新闻或研报当唯一承重证据；价格验证和账户风险仍要检查。
- 工具返回超时或为空时，不写 retry 循环反复调用；标记缺失，降低置信度，必要时不交易。

## 下单理由模板

```text
状态=<Normal/Warning/Stress/Recovery>；
主线=<复苏/宽松/通缩衰退/通胀再通胀/无主线>，置信度=<高/中/低>；
证据=<A股/债券/黄金/现金各一句>；
风险闸门=<pass/warning/fail 摘要>；
目标权重变化=<从 x 到 y>；
成本检查=<可接受/不接受>；
恢复或证伪条件=<条件>。
```
 