# strategy.md - 投顾产品工具

## 工具列表

| 工具 | 功能 | 说明 |
|------|------|------|
| `get_strategy_info` | 查询投顾产品基本信息 | 返回策略ID、名称、管理人、申购限额、费率、基准、申购状态等 |
| `get_strategy_nav` | 查询投顾产品截至指定日期的最新净值 | 返回策略ID、名称、净值日期、净值 |
| `get_strategy_performance` | 查询投顾产品多区间绩效 | 返回近一个月、近三个月、近一年等区间收益/回撤/波动/夏普/卡玛 |
| `get_strategy_portfolio` | 查询投顾产品持仓基金明细 | 返回基金代码、名称、类型、持仓比例及主题标签 |
| `get_strategy_equity_analysis` | 查询投顾产品权益主题配置 | 返回各主题/行业在权益仓位中的占比 |

## 调用格式

```bash
npx mcporter call "research-mcp.get_strategy_info({\"strategy\":\"\"})"
npx mcporter call "research-mcp.get_strategy_nav({\"strategy\":\"\",\"as_of_date\":\"\"})"
npx mcporter call "research-mcp.get_strategy_performance({\"strategy\":\"\",\"as_of_date\":\"\"})"
npx mcporter call "research-mcp.get_strategy_portfolio({\"strategy\":\"\",\"as_of_date\":\"\"})"
npx mcporter call "research-mcp.get_strategy_equity_analysis({\"strategy\":\"\",\"as_of_date\":\"\"})"
```

## 参数说明

### `strategy`
- 可传投顾产品 ID 或名称
- 支持多个值，逗号分隔
- 为空时查询全部产品

### `as_of_date`
- 格式：`YYYY-MM-DD`
- 适用于净值、绩效、持仓、权益分析
- 为空时取最新数据

## 典型用途

- 看投顾产品基础信息：`get_strategy_info`
- 拉取每日最新净值：`get_strategy_nav`
- 看多区间绩效：`get_strategy_performance`
- 看底层基金持仓：`get_strategy_portfolio`
- 看权益风格暴露：`get_strategy_equity_analysis`
