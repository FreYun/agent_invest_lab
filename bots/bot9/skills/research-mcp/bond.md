# 债券工具详细参数

## 国债收益率

### `get_cn_bond_yield` — 中国国债收益率
- `maturity*`: 期限，逗号分隔。如 "10Y,5Y,2Y,1Y"（大写）
- `start_date`, `end_date`: YYYYMMDD，默认近1月
- 返回：每日收益率数据

### `get_us_bond_yield` — 美国国债收益率
- `maturity*`: 如 "10Y,2Y"（大写）
- `start_date`, `end_date`: YYYYMMDD

## 信用债

### `get_credit_bond_yield` — 中债信用债收益率
- `maturity*`: 期限用**小写**，如 "3y,5y,10y"
  - 可选：1y, 2y, 3y, 4y, 5y, 6y, 7y, 8y, 9y, 10y, 15y, 20y, 30y
- `start_date`, `end_date`: YYYYMMDD

## 利差

### `get_bond_yield_spread` — 利差查询
- `spread_type`: **必须**是以下两个值之一：
  - `"cn_vs_us"` — 中美利差（中国国债 - 美国国债），maturity 用大写如 "10Y"
  - `"credit_vs_cn"` — 信用利差（信用债 - 中国国债），maturity 用小写如 "5y"
- `maturity`: 见上
- `start_date`, `end_date`: YYYYMMDD
- `include_raw_data`: 默认 true，是否包含原始收益率

### `get_bond_yield_comparison` — 债券收益率对比
- `bond_type`: 如 "国债"
- `maturity`: 如 "10Y"
- 返回：对比数据

## 期限格式速记

| 场景 | 格式 | 示例 |
|------|------|------|
| 国债（中/美） | 大写 | 10Y, 5Y, 2Y, 1Y |
| 信用债 | 小写 | 10y, 5y, 3y, 1y |
| 中美利差 maturity | 大写 | 10Y, 5Y, 3M |
| 信用利差 maturity | 小写 | 10y, 5y, 3y |
