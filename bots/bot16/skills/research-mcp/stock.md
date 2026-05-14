# 个股数据工具详细参数

所有工具的 `stock_code` 参数为 6 位纯数字，如 `600519`(茅台)、`000001`(平安银行)。

## 基本面

### `get_stock_info` — 股票基本信息
- `stock_code`: 如 "600519"
- 返回：证券简称、交易市场、上市日期、行业分类等

### `get_stock_industry` — 申万行业分类
- `stock_code`
- 返回：一级/二级行业代码和名称

## 行情与走势

### `get_stock_daily_quote` — 日 K 线
- `stock_code`, `start_date`, `end_date`(YYYYMMDD)
- 返回：开盘、最高、最低、收盘、成交量、成交额

## 估值

### `get_stock_valuation` — 估值因子
- `stock_code`, `trade_date`(可选，YYYYMMDD)
- 返回：PE_TTM, PE_LYR, PB_LYR 等

### `get_stock_market_value` — 市值
- `stock_code`, `trade_date`(可选)
- 返回：总市值、A 股市值（亿元）

## 资金面

### `get_stock_fund_flow` — 资金流向
- `stock_code`, `start_date`, `end_date`
- 返回：净流入、超大单/大单/中单/小单金额（亿元）

### `get_stock_northbound_holding` — 北向资金持股
- `stock_code`, `start_date`, `end_date`
- 返回：北向资金持股数量和变化

## 股东与股本

### `get_stock_shareholder` — 流通股东
- `stock_code`, `report_date`(可选)
- 返回：前十大流通股东名称、类型、持股数、持股比例

### `get_stock_share_structure` — 股本结构
- `stock_code`
- 返回：总股本、流通股本、变动事件历史

## 其他

### `get_stock_suspend_info` — 停复牌信息
- `stock_code`
- 返回：停牌日期、复牌日期、停牌原因、停牌时长
