# 板块（sector）

板块（行业/主题）数据。板块代码 `BKxxxxxx`，先用 `sector_search` 模糊检索；`sector_index_match` 做指数↔板块双向映射。因子分含 L1（动量/风险）与 L2（机会分，谨慎用）。

调用：`mcp__simworld_data__<工具名>(...)`；**`simulated_datetime` 由 proxy 自动注入，勿传**。精确 schema 见 `tools-catalog.json`。

## sector — 板块：检索/成分/行情/因子分/子因子明细/指数↔板块匹配

| 工具 | 说明 | 主要参数 |
|------|------|---------|
| `sector_constituents` | 板块成分股（PIT，15:00 收盘后可见，L1+软提示）：取板块 <= T 最新一期全部成分股（代码+名称）。 | **sec_codes** |
| `sector_factor` | 板块因子分（PIT，15:00 收盘后可见）：动量 + 风险分（L1）+ 机会分（⚠️ L2）。 | **sec_codes**, start_date, end_date |
| `sector_factor_detail` | 板块子因子明细 / 拥挤度细分（PIT，15:00 收盘后可见，纯 L1）：把风险分拆成两个构成因子。 | **sec_codes**, win, start_date, end_date |
| `sector_index_match` | 指数↔板块匹配（PIT，双向，L1）：板块映射到可交易指数/ETF，或指数拆到主题板块。 | sec_codes, index_codes, top_n |
| `sector_market` | 板块行情（PIT，15:00 收盘后可见）：日收益率 + 估值(PE/PB) + 主力净流入（全 L1）。 | **sec_codes**, start_date, end_date, value_type |
| `sector_search` | 板块检索（行业/主题，轻 PIT）：按名称模糊找板块代码 BKxxxxxx。 | keyword, sector_type |
