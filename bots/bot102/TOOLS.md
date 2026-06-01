# TOOLS.md - bot102 工具配置

---

## Bot 专属配置

- **account_id**: `bot102`
- **小红书 MCP**: 单进程多租户，所有 bot 共用 `:18060`，URL path 自动识别身份（已配置在 mcporter.json）

## 基金组合管理: fund-portfolio-mcp

当前配置加载的是 `fund-portfolio-mcp`，不是旧产品池工具。多指数权益组合执行时优先使用下面这些工具：

| 工具 | 功能 |
|------|------|
| `portfolio_get_buyable_funds` | 查询本轮 run 实际允许交易的基金代码池 |
| `get_fund_detail` | 查询单只基金主题、风格因子、规模、长期业绩、回撤、Sharpe 和同类排名 |
| `portfolio_get_my_history` | 查询当前账户、持仓、订单和历史动作 |
| `portfolio_get_my_trades` | 查询交易记录 |
| `portfolio_get_my_performance` | 查询账户绩效和回撤序列 |
| `portfolio_place_buy_order` | 下买入订单，必须写清 `reason` |
| `portfolio_place_sell_order` | 下卖出订单，必须写清 `reason` |

## 数据与研究

- `simworld-data`：市场状态、指数行情、股债性价比、估值、情绪、宏观、板块因子、资金流、研报搜索。
- `strategy-mcp`：只作为策略辅助和复核，不替代 `METHODOLOGY.md` 的主流程。

## 执行原则

- 每日先读 prompt 注入的可买池主题分布，再按方法论收敛候选。
- 不用 `get_fund_detail` 扫全池，只对最终 1-3 只候选拉全量画像。
- 每笔交易都必须留下可审计理由，理由要包含风险状态、主线判断、仓位结构变化。
