# 宏观经济与商品工具详细参数

## 中国宏观

### `get_cn_macro_data` — 中国宏观经济数据
- `category`: **小写**，多个用逗号分隔
- `start_date`, `end_date`: YYYYMMDD，可选

可用 category（全部小写）：

| category | 含义 | 频率 |
|----------|------|------|
| `gdp` | GDP 国内生产总值 | 季频 |
| `cpi` | CPI 消费者价格指数 | 月频 |
| `ppi` | PPI 生产者价格指数 | 月频 |
| `m1` | M1 货币供应量 | 月频 |
| `m2` | M2 货币供应量 | 月频 |
| `pmi` | PMI 采购经理指数 | 月频 |
| `export` | 出口数据 | 月频 |
| `import` | 进口数据 | 月频 |
| `usdcny` | 美元兑人民币汇率 | 日频 |
| `fai` | 城镇固定资产投资 | 月频 |
| `iva` | 工业增加值 | 月频 |
| `retail` | 社会消费品零售总额 | 月频 |
| `afre` | 社会融资增量 | 月频 |

示例：`category="cpi,ppi,m2"` 一次查多个指标。

## 美国宏观

### `us_macro_simple` — 近 6 月全量数据（首选）
无参数，返回所有美国宏观指标近6个月数据。快速概览用这个。

### `us_macro_data` — 按类别查询
- `category`: 指标类别
- `start_date`, `end_date`: YYYYMMDD

## 商品

### `commodity_data` — 商品历史行情（首选）
- `symbol`: 商品名或代码，逗号分隔
  - 支持中文："黄金9999", "白银9999"
  - 支持代码："AU9999", "AG9999"
- `start_date`, `end_date`: YYYYMMDD 或 YYYY-MM-DD（此接口两种都支持）

### `futures_data` — 期货行情
- `symbol`: 期货代码，如 "AU.SHF,CU.SHF"
- `start_date`, `end_date`: YYYYMMDD

> ⚠️ `futures_data` 数据不完整，优先用 `commodity_data`。
