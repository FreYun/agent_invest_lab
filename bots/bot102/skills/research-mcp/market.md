# 市场指数工具详细参数

## A 股

### `market_overview` — A 股市场总览（首选）
无参数，返回各主要指数行情 + 成交额。快速了解大盘用这个。

### `get_ashares_index_quote` — A 股指数行情
- `symbol`: 指数代码，逗号分隔。如 "000001.SH,000300.SH,399006.SZ"
- `start_date`, `end_date`: YYYYMMDD
- 返回：收盘价、日/周/月/季/年涨跌幅

### `get_ashares_index_val` — A 股指数估值
- `symbol`: 如 "000300.SH"
- 返回：PE_TTM + 历史百分位

### `get_ashares_turnover` — A 股总成交额
- `start_date`, `end_date`: YYYYMMDD

### `get_ashares_gvix` — A 股恐慌指数
- `start_date`, `end_date`: YYYYMMDD

### `index_quote_simple` — 快速查单个指数
- `symbol*`: 如 "000300.SH"
- 返回最近一个月行情

**常用 A 股指数代码**：
`000001.SH`(上证综指), `000300.SH`(沪深300), `000905.SH`(中证500), `000852.SH`(中证1000), `399001.SZ`(深证成指), `399006.SZ`(创业板指), `399673.SZ`(创业板50)

---

## 港股

### `get_hshares_market_overview` — 港股市场总览（首选）
无参数，返回恒生系列指数行情。

### `get_hsi_quote` — 恒生指数行情
- `start_date`, `end_date`: YYYYMMDD

### `get_hstech_quote` — 恒生科技指数行情
- `start_date`, `end_date`: YYYYMMDD

### `get_hshares_index_quote` — H 股指数通用查询
- `symbol`: 如 "HSI.HI,HSTECH.HI,HSCEI.HI" 或中文 "恒生指数,恒生科技"
- `start_date`, `end_date`: YYYYMMDD

### `get_hshares_turnover` — H 股总成交额
- `start_date`, `end_date`: YYYYMMDD

### `get_southbound_hkd_turnover` — 港股通南向资金
- `start_date`, `end_date`: YYYYMMDD
- 返回：买入/卖出金额（百万港币）

**港股指数代码**：`HSI.HI`(恒生), `HSTECH.HI`(恒生科技), `HSCEI.HI`(恒生国企)

---

## 美股

### `get_usstock_index_quote` — 美股指数通用查询（首选）
- `symbol`: 如 "DJIA.GI,SPX.GI,NDX.GI"，逗号分隔
- `start_date`, `end_date`: YYYYMMDD

便捷版（无需指定 symbol）：
- `get_dowjones_quote` — 道琼斯
- `get_nasdaq100_quote` — 纳指100
- `get_sp500_quote` — 标普500

### `get_usstock_index_list` — 查可用美股指数列表
无参数。

**美股指数代码**：`DJIA.GI`(道琼斯), `SPX.GI`(标普500), `NDX.GI`(纳指100)
