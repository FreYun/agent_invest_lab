# 基金工具详细参数

## 基金信息查询

### `get_fund_comprehensive_analysis` — 一站式全面分析（首选）
包含基本信息 + 业绩 + 风格 + 重仓股 + 行业分布，一次搞定。
- `fund_code`: 基金代码，如 "000001"

### `fund_basicinfo` — 批量查净值/收益率
- `fcodes*`: 基金代码，逗号分隔，如 "000001,110011"
- `fields`: 输出字段，默认 "FCODE,SHORTNAME,DWJZ,SYL_1N"
  - 基本：FCODE, SHORTNAME, FTYPE, DWJZ(单位净值), LJJZ(累计净值)
  - 收益：SYL_Y(月), SYL_3Y(3月), SYL_6Y(6月), SYL_1N(1年), SYL_2N(2年), SYL_3N(3年), SYL_5N(5年), SYL_JN(今年)
  - 管理：JJGS(公司), JJJL(经理), ESTABDATE(成立日), RISKLEVEL(风险等级1-5)
  - 交易：SGZT(申购状态), SHZT(赎回状态), MINSG(最低申购额)

### `get_multiple_funds_nav` — 多基金净值对比
- `fund_codes`: 逗号分隔，如 "000001,110011"
- `start_date`, `end_date`: YYYYMMDD

### `get_fund_nav_and_return` — 单基金复权净值+日收益率
- `fund_code`, `start_date`, `end_date`

## 基金细项查询

每个工具参数都是 `fund_code`（6位数字）：

| 工具 | 返回内容 |
|------|---------|
| `get_fund_manager_info` | 基金经理姓名、履历 |
| `get_fund_performance` | 各周期收益率、排名百分比、最大回撤 |
| `get_fund_style_analysis` | 大盘/小盘、价值/成长标签 |
| `get_fund_top_stocks` | 重仓股代码、名称、占比、行业 |
| `get_fund_industry_holding` | 行业持仓占比 |
| `get_fund_ind_by_report` | 申万行业占比（按报告期） |
| `get_fund_sector` | 相关板块标签 |
| `get_fund_turnover_rate` | 最新报告期换手率 |
| `get_fund_campisi_indicator` | 债券持仓 Campisi 收益分解 |
| `get_fund_index_return` | 超额收益指标 |
| `fund_invest` | 持仓明细（重仓股+行业） |
| `fund_rate` | 费率、申赎状态、最低金额 |
| `fund_bonus` | 分红记录（可选 `date` 参数过滤） |

### `get_fund_abnormal_movement` — 净值异动检测
- `fund_code*`: 基金代码
- `start_date`: 可选，YYYYMMDD
- `movement_type`: 可选，筛选异动类型

## 基金筛选

### `simple_fund_search` — 简单条件筛选（首选）
- `fund_type`: "股票型" / "混合型" / "债券型" / "指数型" / "QDII"
- `min_return`: 最低收益率
- `max_drawdown`: 最大回撤限制
- `risk_level`: 风险等级
- `limit`: 返回数量

### `fund_theme_screening` — 按主题/板块筛
- `sec_name`: 主题名，如 "半导体"、"新能源"、"消费"
- `if_union`: 并集模式
- `if_intersection`: 交集模式

### `fund_stock_holdings` — 按持仓股票筛
- `stock_codes*`: 股票代码，如 "600519"
- `match_all`: true=所有股票都持仓，false=任一即可

### `fund_screening` — 多维度高级筛选
- `fund_codes`: 指定基金范围
- `filters_json`: JSON 格式筛选条件
- `limit`: 返回数量
- `only_init`: 是否仅初始化

### `filter_fund_by_turnover` — 按换手率筛选
- `bottom`: 换手率下限
- `up`: 换手率上限

### `performance_periods` — 获取可用业绩周期定义
无参数，返回周期代码与名称的映射。

> **不推荐使用**：`fund_info_basic`, `fund_info_detailed`, `get_fund_info` — 与上述工具功能重叠。
