# 设计文档：ttjj-data-pit MCP（天天基金数据 · 时点版）

- 日期：2026-05-11
- 状态：已与用户确认设计，待写实现计划

## 1. 背景与目标

现有 `MCP/ttjj-data-mcp/server.py` 是天天基金数据 API（`http://ttjj-data-api.jijinmima.cn`）的薄代理，暴露 33 个 `@_mcp.tool()`。在用它做"基于历史时点的投资 agent 回测"时存在前视偏差（lookahead bias）：很多工具返回的是"当下最新"的数据或排名，agent 在模拟某个过去日期时不该知道这些。

目标是新做一个 MCP（不动原 server.py），满足两点：

1. **剔除没有日期参数、也无法判断数据是否"未来于目标时点"的函数。**
2. **数据调取按 `as_of_date`（目标时点）收口**：只允许返回 `as_of_date` 当天及之前的数据；哪怕上游数据库里有更新的数据，也不允许返回。

## 2. 形态与位置

- 新文件：`/home/rooot/agent_invest_lab/ttjj_data_pit_mcp.py`
- MCP 名称：`ttjj-data-pit`
- 监听端口：`18078`（`/home/rooot` 下当前未占用；`server.py` 用的是 18077）
- 配套文件（同目录）：`restart.sh`（仿 `MCP/ttjj-data-mcp/restart.sh`，端口换 18078，日志 `/tmp/ttjj-data-pit-mcp.log`）、`requirements.txt`（`mcp`、`requests`）
- 原 `MCP/ttjj-data-mcp/`（含 `server.py`、`restart.sh`）**完全不动**。
- 上游 API 地址、超时等沿用原值：`BASE_URL = "http://ttjj-data-api.jijinmima.cn"`，`TIMEOUT = 30`。

## 3. 工具去留（原 33 个 → 保留 19 / 剔除 14）

### 3.1 剔除（14 个）

| 工具 | 剔除理由 |
|---|---|
| `fund_select` | 按"近 1 年收益率"等指标筛选——筛选动作本身用了未来数据；且返回 `当前基金经理` |
| `fund_performance` | "近 N 年收益率/回撤/夏普"算到最新日期，无法锚定到 `as_of_date` |
| `fund_manager_profile` | 从业年限/管理规模/代表产品 = 当下快照 |
| `fund_style_analysis` | 风格标签/行业配置 = 当下快照 |
| `fund_rate` | 当前费率，无日期 |
| `fund_theme_screening` | 基于最新数据的主题筛选 |
| `fund_stock_holdings_screen` | 基于最新持仓的反查筛选 |
| `fund_index_tracking` | 基于最新成分的跟踪关系 |
| `fund_top_holdings` | 上游只返回最新一期"报告期"，无公告/披露日期字段 —— **本期暂不纳入，后续单独处理** |
| `fund_invest_position` | 同上（全部持仓，无披露日期） —— 本期暂不纳入 |
| `fund_turnover_rate` | 同上（仅最新一条 `report_dt`） —— 本期暂不纳入 |
| `fund_industry_exposure` | 同上（MAX(report_dt) 快照） —— 本期暂不纳入 |
| `entity_extract` | 纯 NLP 工具，无数据；按规则①（无 date 参数）剔除 |
| `health_check` | 纯工具；按规则①剔除 |

说明：上面那 4 个"报告期"类接口（`fund_top_holdings` / `fund_invest_position` / `fund_turnover_rate` / `fund_industry_exposure`）经查上游源码（`ttjj-api-master`）确认：响应里只有"报告期/报告日期"，**没有任何公告日期 / 披露日期 / 更新日期字段**，且上游只返回最新一期。所以无法在本期做出可靠的时点判断，**按用户要求本期不纳入新 MCP**，留待后续单独设计。

### 3.2 保留（19 个）

所有保留的工具都新增**必填**首参数 `as_of_date: str`，格式 `YYYY-MM-DD`。

**① 已有显式日期参数（15 个）** —— 原样保留入参，新增 `as_of_date`：
`fund_nav`、`fund_index_return`、`fund_bonus`、`fund_abnormal_movement`、`market_index_quote`、`stock_market`、`stock_capital_flow`、`stock_ownership`、`stock_financial_quality`、`stock_alpha`、`stock_events`、`macro_data`、`bond_yield_curve`、`commodity_market`、`research_view`

**② 无 date 参数、响应带静态日期，可判定（2 个）**：
- `fund_basic_info`：响应含 `成立时间`；按 `成立时间 > as_of_date` 整条剔除。另：响应里 `基金经理`（以及任何明显是"当下值"的字段，如 `最新定期报告时间`）置为 `null`，并在该 dict 同级加 `_pit_note: "字段已按时点屏蔽：基金经理为当前值，非 as_of_date 当时任职人"`。
- `stock_profile`：响应含 `上市日期`；按 `上市日期 > as_of_date` 整条剔除。（申万/中信行业分类变动慢，本期当作准静态，不屏蔽。）

**③ 特殊处理（2 个）**：
- `ttjj_research_search`：把"近 N 天"（`search_days`）重算为时间窗 `[as_of_date - search_days 天, as_of_date]` 后再调上游；返回结果再按发布日期 ≤ `as_of_date` 过滤。
- `market_realtime_quote`：保留 `as_of_date`；响应里的行情时间戳字段（如 `时间`/`更新时间`/`timestamp`）> `as_of_date` 则该条剔除。**已知局限**：`as_of_date` 早于今天时，实时行情几乎必然被全部过滤掉，相当于该工具仅对"今天"有效——会在 docstring 写明。

## 4. `as_of_date` 注入规则（调上游 API 之前）

对每个保留的工具，构造请求体时：

1. **`as_of_date` 自身校验**：解析失败（非 `YYYY-MM-DD`）→ 直接返回 `{"error": "bad_as_of_date", "message": "<原值>"}`，不调上游。
2. **有 `end_date` 入参的工具**（`fund_nav`、`market_index_quote`、`stock_market`、`stock_capital_flow`、`stock_alpha`、`stock_events`、`macro_data`、`commodity_market`、`research_view`）：
   `end_date_final = min(用户传的 end_date（若有，否则取 as_of_date）, as_of_date)`，静默裁剪后传给上游。
3. **有单点日期入参的工具**（`fund_index_return.trade_date`、`fund_bonus.date`、`stock_ownership.report_date`、`stock_financial_quality.trade_date`、`stock_alpha.trade_date`、`stock_market.trade_date`、`bond_yield_curve.date`）：
   - 用户传了且 > `as_of_date` → 返回 `{"error": "lookahead", "message": "<param>=<value> 晚于 as_of_date=<as_of_date>"}`，不调上游；
   - 用户没传 → 填 `as_of_date`。
4. **`start_date` 入参**（`fund_nav`、`fund_abnormal_movement`、`market_index_quote`、`stock_market`、`stock_capital_flow`、`stock_alpha`、`stock_events`、`macro_data`、`commodity_market`、`research_view`）：用户传了且 > `as_of_date` → 返回 `{"error": "lookahead", ...}`，否则原样传。
5. **`fund_abnormal_movement`** 只有 `start_date`、没有 `end_date` 入参，无法用上游参数收口右端 → 仅靠第 5 节响应过滤兜底。

> 注：上面"哪些工具有哪些日期入参"以 `MCP/ttjj-data-mcp/server.py` 现状为准（实现时逐个核对函数签名）。

## 5. 响应递归过滤（兜底，对所有保留工具生效）

上游返回 JSON 后，进 `_filter_response(obj, as_of_date)` 递归处理，再返回给调用方。

- **遍历到 list**：对其中每个元素，若元素是 dict 且含至少一个"日期型字段"且该字段值能解析成日期：
  - 该日期 > `as_of_date` → 把这个元素从 list 中剔除；
  - 否则保留，并对该元素继续递归。
  - 元素不是 dict（或没有日期型字段）→ 保留，继续递归。
- **遍历到 dict（非某个 list 的元素，或元素递归时）**：
  - 对每个值递归处理；
  - 此外，若 dict 里某个"日期型字段"的值能解析且 > `as_of_date`（典型如顶层 `data.最新净值日期` 之类的标量），把该字段置为 `null`，并在同级加一个 `_pit_truncated: true` 标记。
- **"日期型字段"判定**：字段名（区分大小写无关）正则匹配 `日期|时间|date|datetime`，覆盖中文如 `交易日期`、`净值日期`、`报告日期`、`报告期`、`分红日期`、`权益登记日`、`发放日`、`变动日期`、`发放年度`、`停牌日期`、`复牌日期`、`成立时间`、`上市日期`、`查询时间` 等。（`查询时间`/`query_time` 这类"接口本身的处理时刻"不应被过滤——见下面例外。）
- **例外字段**：`metadata.query_time` / `query_time` / `_as_of_date` 这类"接口元数据时间"不参与过滤（白名单，不当作数据日期）。
- **日期解析容错**：依次尝试 `%Y-%m-%d`、`%Y%m%d`、`%Y/%m/%d`、`%Y-%m-%d %H:%M:%S`、`%Y/%m/%d %H:%M:%S`、`%Y-%m`、`%Y`；都失败 → 认为该字段不是日期，不做任何处理。比较时统一按"日"粒度（带时分秒的截到日）。
- 处理完后在**最外层返回对象**加 `_as_of_date: "<传入的 as_of_date 原值>"`，方便调用方确认时点已生效。

## 6. 代码结构（单文件）

仿 `server.py` 的组织方式：

```
ttjj_data_pit_mcp.py
├── 常量：BASE_URL、TIMEOUT、_DATE_FIELD_RE、_DATE_FORMATS、_TIME_FIELD_WHITELIST
├── _get_session()                      # 同 server.py
├── _parse_date(value) -> date | None   # 容错解析
├── _is_date_field(key) -> bool
├── _filter_response(obj, as_of) -> obj # 第 5 节递归过滤
├── _check_as_of(as_of_date)            # 第 4.1 校验，返回 (ok: bool, err: dict|None, date_obj)
├── _clamp_end_date(user_end, as_of)
├── _reject_if_future(name, value, as_of) -> dict|None  # 单点/start_date 检查
├── _post(path, data, as_of_date)       # 调上游 + raise_for_status + success 检查 + _filter_response
├── _err(e)                             # 同 server.py
├── 19 个 @_mcp.tool()                   # 见第 3.2，每个首参 as_of_date
└── main()                              # argparse --port(默认18078) --host，run streamable-http
```

每个工具内部模式（以 `fund_nav` 为例）：

```python
@_mcp.tool()
def fund_nav(as_of_date: str, fund_codes: list[str],
             start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict[str, Any]:
    """基金净值历史（时点版：只返回 as_of_date 当天及之前）。"""
    ok, err, as_of = _check_as_of(as_of_date)
    if not ok:
        return err
    if (e := _reject_if_future("start_date", start_date, as_of)):
        return e
    try:
        d: dict = {"fund_codes": fund_codes}
        if start_date:
            d["start_date"] = start_date
        d["end_date"] = _clamp_end_date(end_date, as_of)   # = min(end_date or as_of, as_of)
        return _post("/api/fund/nav", d, as_of_date)
    except Exception as e:
        return _err(e)
```

`market_realtime_quote` / `ttjj_research_search` / `fund_basic_info` / `stock_profile` 在 `_post` 返回后再各自做一步定制处理（屏蔽字段 / 重算时间窗）。

## 7. 错误返回约定

沿用原 `{"error": "...", "message": "..."}` 结构，类型集合：

- `request_error` —— 网络/上游异常（同原 `_err`）
- `api_error` —— 上游 `success=false`（同原 `_post`）
- `bad_as_of_date` —— `as_of_date` 格式非法
- `lookahead` —— 用户传入的某个日期参数晚于 `as_of_date`

## 8. 已知局限（同时写进文件头注释）

1. `fund_top_holdings` / `fund_invest_position` / `fund_turnover_rate` / `fund_industry_exposure` 本期未纳入（上游无披露日期、只给最新一期），待后续单独处理。
2. `market_realtime_quote` 在 `as_of_date` 早于今天时几乎必然返回空（实时行情时间戳必然 > 过去的 as_of_date），属预期行为。
3. `fund_basic_info` 的 `基金经理` 等"当下值"字段被置 `null`；本 MCP 不提供"`as_of_date` 当时的真实任职经理"。
4. 响应过滤靠"字段名像日期"+"值能解析成日期"启发式；若上游某天改字段名或用了非常规日期格式，可能漏过滤——属可接受风险，必要时扩充 `_DATE_FORMATS` / 正则。

## 8.5 性能

新增开销相对原薄代理可忽略：

- `as_of_date` 校验 / `end_date` 裁剪 / 单点日期检查：几次日期解析，微秒级。
- `_filter_response` 是对响应 JSON 树的一次 O(N) 纯 Python 遍历；只对"字段名像日期"的值才尝试解析（其余标量看一眼 key 就跳过），即便几万节点的响应也是毫秒级，远小于上游一次 HTTP 往返。
- `_parse_date` 加两个快路径以防大响应里日期字段很多：① 值不以 4 位数字开头 → 直接判非日期；② 用单个正则一次性抓 `YYYY[-/]?MM[-/]?DD`，命中再 `date(...)`，未命中再退回 `%Y-%m`/`%Y` 等少数几个格式，不做 7 连 `strptime`。
- 把 `end_date` 收口到 `as_of_date` 通常让上游少返回数据，payload 更小，历史查询往往比原版还快。

## 9. 不做的事（YAGNI）

- 不做全局/启动时配置的截止日期（已定为每个工具的 `as_of_date` 入参）。
- 不做 B 组的"报告期 + 披露延迟"近似过滤（本期不纳入 B 组）。
- 不引入数据库、缓存、外部"报告期→披露日期"映射表。
- 不动原 `server.py`，不做向后兼容层。
- 不加 `health_check`（按规则①剔除）。

## 10. 验证方式

- 启动 `bash /home/rooot/agent_invest_lab/restart.sh`，确认 18078 起来、日志无异常。
- `as_of_date` 给一个历史日期（如 `2024-01-01`）调 `fund_nav(["110011"])`，确认返回的 `净值/交易日期` 没有任何 > 2024-01-01 的记录，且顶层有 `_as_of_date`。
- 给 `fund_nav` 传 `end_date="2025-06-01"`、`as_of_date="2024-01-01"`，确认被裁剪到 2024-01-01（不报错）。
- 给 `stock_ownership` 传 `report_date="2025-12-31"`、`as_of_date="2024-01-01"`，确认返回 `{"error": "lookahead", ...}`。
- `as_of_date="2024-13-40"` → 返回 `{"error": "bad_as_of_date", ...}`。
- `market_realtime_quote(as_of_date="2020-01-01", codes=[...])` → 返回（基本为空，不报错）。
- 确认剔除的 14 个工具在新 MCP 里不存在。
