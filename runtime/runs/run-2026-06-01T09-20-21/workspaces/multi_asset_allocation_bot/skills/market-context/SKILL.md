---
name: market-context
description: 每日市场环境判断（每个 bot 一份，按自己的人设视角观察）。不预测涨跌,只回答"当前市场处于什么状态"。输出 regime(牛/熊/震荡/危机) + 四类资产方向 + 关键信号摘要,供基金/投顾两条巡检链路读取；执行时必须严格停留在当前链路，不得混用 fund 与 tougu 的工具、表、路径。
---

## 范式参数(2026-04-29 三层框架)

执行前必须查询当日 paradigm:

```sql
SELECT paradigm_active, capability_field, capability_value
FROM fund_paradigm_runs
WHERE bot_id = ? AND trade_date = ?;
```

| paradigm | 输出重点 |
|---|---|
| A 大类资产轮动 | 完整宏观判断 + 大类目标比例(equity/bond/gold/cash) |
| B1 多行业轮动 | 简化宏观 + 各行业相对强弱排序;asset_target 可模糊 |
| B2 单行业深耕 | 简化宏观 + capability_value 指定行业的景气/拥挤度;asset_target 可模糊 |
| C 个基 alpha | 完整宏观判断(必须给 regime,供 Phase C 矩阵的列轴用);asset_target 不强求 |
| SKIP | 该 bot 当日不参与基金链路,跳过 |

具体的"宏观要拆多细"、"行业排名怎么算"、"拥挤度看什么指标"等细节,由本 skill 内部的判断逻辑 + bot 的 `基金投资策略摘要.md` 共同决定;框架只规定输出重点的边界。

# 市场环境判断 /market-context

**触发词**: `/market-context`, "今天市场怎么样", "判断一下行情", "当前市场环境"
**执行频率**: 每日一次,作为组合巡检 Phase B0(市场状态输入)的第一步,在数据刷新(Phase A)之后、选品/巡检之前
**输出位置**:
- 投顾链路: `memory/portfolio/市场环境判断.md`
- 基金链路: `memory/portfolio/fund/市场环境判断.md`

> ⚠️ 这是 bot 巡检会话内的第一步,不是上游独立流程。即使 `市场环境判断.md` 已存在,也必须按本 skill 的 frontmatter schema 整段覆盖以对齐本轮 trade_date/run_id;不要因为"上次已经写过"或"看起来是上游产物"就跳过。

## 定位

**不是预测工具,是状态判断工具。每个 bot 一份,体现各自人设的市场观点。**

不告诉你"明天会涨还是跌",而是告诉你"在我看来现在是晴天/暴雨/阴天",你自己决定带不带伞。

每个 bot 在自己工作空间下维护一份 `memory/portfolio/市场环境判断.md`,反映该 bot 的市场观点。同样的数据,不同 bot 因人设(风险偏好、能力圈、对宏观信号的敏感度)不同,可能给出不同的 regime 判断 —— 这是设计意图,不是 bug。

投顾巡检和基金巡检时,bot 读的是**自己那份**,结合自己持仓的收益/回撤,在"决策矩阵"里查出应该 HOLD / ADD / REDUCE / STOP LOSS。

## 链路边界（必须遵守）

- 本 bot 当前只走**基金链路**: 只使用 `fund.db` / `memory/portfolio/fund/*` / `fund-portfolio-mcp` / `fund_md_to_db.py`
- 如果当前会话传入 `run_id/trade_date/paradigm_active`，必须保持在基金链路内，不要切到其它产品池或其它数据库路径
- 市场判断只为基金组合巡检服务，不写入非基金链路路径

## 输出格式

写入 `memory/portfolio/市场环境判断.md`,格式如下:

```markdown
# 市场环境判断 · YYYY-MM-DD

## Layer 1: 市场大势

| 指标 | 值 | 说明 |
|------|-----|------|
| **regime** | bull / range / bear / crisis | 当前市场阶段 |
| **trend_strength** | strong / moderate / weak | 趋势强度 |
| **risk_level** | low / medium / high / extreme | 风险水平 |

## Layer 2: 各类资产方向

| 资产 | 方向 | 动量 | 依据 |
|------|------|------|------|
| 股票(A股) | up / flat / down | strengthening / stable / weakening | (简述) |
| 债券 | up / flat / down | ... | ... |
| 黄金 | up / flat / down | ... | ... |
| 现金 | — | attractiveness: low / medium / high | (货基收益率环境) |

## Layer 3: 六维证据（必填）

> 以下六个 H3 章节标题**必须一字不差**地出现在 market_summary_md 中。每个章节至少给出 1-2 条 A 股数据/事实，不得留空、不得合并、不得改名。
>
> 具体回看多长(5 日 / 20 日 / 60 日 / 120 日 / 250 日 / 多年百分位…)由 bot 根据自己的人设和今日市场情境自行判断,skill 不规定。下方的均线长度、百分位窗口只是示例,bot 可以采用更短或更长的口径。

### 趋势结构

- 上证 / 沪深300 / 创业板 收盘价相对均线的位置(均线长度自选,如 20/60/120/250 日)
- 近 N 日方向（连阳/连阴/震荡）

### 市场宽度

- 上涨家数 vs 下跌家数（来自 `get_stock_fund_flow` 或行情统计）
- 涨停/跌停家数；强势股占比；行业分化度
- 没数据时也要写"今日市场宽度数据缺失，按 X 估计"

### 量能

- 沪深两市总成交额（亿元）；与 bot 自选的均量窗口对比(常见 20 / 60 / 250 日)
- 量能档位归类：萎缩 / 一般 / 放量 / 巨量

### 情绪

- GVIX 数值与历史区间对比(对比窗口由 bot 自选)
- 北向资金净流入/流出
- 融资融券余额变化（如有）

### 估值

- 沪深300 / 中证500 / 创业板 PE/PB 百分位(`get_ashares_index_val` 默认窗口由工具返回,如需 1 年 / 3 年 / 多年价格百分位,bot 可从 K 线序列自算)
- 股债性价比（10Y 国债 vs 沪深300 股息率）

### 宏观

- 10Y 国债利率方向；信用利差变化
- 月度宏观数据（PMI / CPI / 社融）若当周有发布
- 外围环境：美股 / 美元指数 / 大宗商品要点

## Layer 4: 关键信号

**一句话概要**: (用一句话描述今日市场状态,供 bot 在调仓叙事中引用)

**利多信号**: (列举)
**利空信号**: (列举)
**风险提示**: (列举)

## Layer 5: 择时口径

- **timing_stance**: aggressively_add / add_on_pullback / hold / defensive / risk_off
- **执行含义**: (一句话解释当前更适合立即加仓 / 回调分批 / 继续观察 / 偏防守 / 降风险)
- **注意**: 这里只给市场口径,**不输出任何具体大类资产比例、中枢比例、目标仓位或配比区间**
```

## ⚠️ 写入约束（MD 是唯一真相源）

**bot 不再调用任何 MCP 写工具**（`save_allocation_run` / `save_*` 工具已被服务端 readonly 模式屏蔽）。

bot 只负责把今日市场判断写入**当前链路对应的目标路径**，**文件必须以 YAML frontmatter 起头**。
- 投顾 cron: `tougu_md_to_db.process_bot()` 解析 `memory/portfolio/市场环境判断.md`
- 基金 cron: `fund_md_to_db.process_bot()` 解析 `memory/portfolio/fund/市场环境判断.md`

### Frontmatter 强制 schema

文件结构：

```markdown
---
step: market_context
trade_date: YYYY-MM-DD                      # 必须 = cron 传入的 trade_date
run_id: cron-YYYY-MM-DD-xxxxxxxx            # 必须 = cron 传入的 run_id
regime: range                                # bull / range / bear / crisis
regime_code: range_up                        # 自由文本，可选细分
confidence: 0.7                              # 0~1，选填
timing_stance: add_on_pullback               # aggressively_add/add_on_pullback/hold/defensive/risk_off
summary: 一句话市场口径
central_baseline:                            # 使用当前链路约定的中枢/基线来源，不要跨链路取值
  equity_pct: 75
  bond_pct: 5
  gold_pct: 0
  cash_pct: 20
  set_date: 2026-04-23
  tolerance_pct: 10
today_target:                                # 4 比例和必须 = 100，与 central 偏差 ≤ tolerance_pct
  equity_pct: 70
  bond_pct: 5
  gold_pct: 0
  cash_pct: 25
  rationale: regime=range, timing_stance=hold；按中枢偏防守 5%，提升现金留作回调子弹
---

# 市场环境判断 · YYYY-MM-DD

## Layer 1: 市场大势
（按上方模板继续写完 Layer 1~5）
```

系统会把 frontmatter 转成当前链路的配置结果 JSON，把 `---` 之后的全部正文写入当前链路的市场判断正文列：
- 投顾链路: `allocation_runs.asset_target_json` + `allocation_runs.market_summary_md`
- 基金链路: `fund_allocation_runs.asset_target_json` + `fund_allocation_runs.market_summary_md`

### 正文（market_summary_md）的硬约束

系统层校验器会检查 frontmatter 之后的 markdown 正文，必须满足：

1. **包含 Layer 3 的 6 个 H3 标题**：`### 趋势结构 / ### 市场宽度 / ### 量能 / ### 情绪 / ### 估值 / ### 宏观`，**一字不差，不得简写、合并、改名**。每个章节至少 1 条 A 股数据/事实；缺数据写"今日数据缺失，按 X 处理"。
2. **至少 4 个 A 股证据词**：上证 / 沪深300 / 中证500 / 中证1000 / 创业板 / 成交额 / 量能 / 市场宽度 / 上涨家数 / GVIX / 估值 / PE / 股债 / 国债 / 信用利差 / PMI / 社融。
3. **不得出现固定大类配比语言**：`大类资产配置建议 / 大类约束`（这些应该只在 frontmatter 的 `today_target` 里出现，不要写在叙述正文里）。
4. **不得用本 bot 持仓收益替代市场证据**：`持仓净值 / 三只产品均盈利 / 冷静期` 这类词如果出现，A 股证据词必须 ≥ 6 个，否则判"overuses holding/cooldown evidence"。

### today_target 推导规则（与 timing_stance 联动）

| timing_stance | 偏移方向 | 大类调整 |
|---|---|---|
| `aggressively_add` | 进攻 | 现金降 5-10%，降低部分给权益 |
| `add_on_pullback` | 偏多 | 现金降 0-5%（保留部分子弹） |
| `hold` | 中性 | 严格按中枢，或 ±2% 微调 |
| `defensive` | 偏守 | 现金升 5-10%，权益降低对应比例 |
| `risk_off` | 防御 | 现金升 10%+，权益降至最低 |

**铁律**：`today_target` 4 大类合计 = 100%；每一项与 `central_baseline` 偏差 ≤ `tolerance_pct`%（默认 ±10%）；中枢本身不在 Phase B0 改写——只能由研究部走管理通道更新。

## 数据采集

使用 research-mcp 的以下工具采集数据,**所有工具调用完成后再做 regime 判定 + 写 Layer 3**。

### 起始日(建议拉满 5 年,bot 也可视需要自取)

调用 MCP 工具时**建议默认按 5 年回看**(`start_date = trade_date - 1900 自然日`),这样无论 bot 想看短期、中期还是长期窗口,数据都已经在手里。

```
end_date   = trade_date(YYYYMMDD)
start_date = trade_date - 1900 自然日(≈5 年,YYYYMMDD)
```

bot 拿到 K 线序列后,**自行决定**要算 5 日 / 20 日 / 60 日 / 120 日 / 250 日 MA、近 1 月 / 1 年 / 5 年百分位等等;skill 不规定看哪个窗口、不规定怎么权衡。如果某天 bot 觉得只看短窗就够、不需要长窗,完全 OK,直接传短一点的 `start_date` 即可。

### 必查(每日)

支持 `start_date/end_date` 的工具**最好都显式传入**——不传 → MCP 默认窗口太短(常见近 1-2 月),想看长周期时数据不在手。

| 步骤 | 工具 | 查什么 | 拿到序列后 bot 要算什么 |
|------|------|--------|---------|
| 1 | `get_ashares_index_quote(symbol="000001.SH,000300.SH,399006.SZ", start_date=START, end_date=END)` | 上证 / 沪深300 / 创业板指数历史 K 线 | 5/20/60/120/250 日 MA;近 5/20 日方向;沪指当前价 vs 多年中枢 |
| 2 | `get_ashares_index_val(symbol="000300.SH")` | 沪深 300 PE/PB 百分位(工具默认窗口) | 估值水位;若工具默认 5 年,直接当 5 年百分位;1 年/3 年百分位见说明 |
| 3 | `get_ashares_gvix(start_date=START, end_date=END)` | A 股隐含波动率历史 | 当前值 + 与近 1 月、近 1 年、近 5 年均值/极值对比 |
| 4 | `get_ashares_turnover(start_date=START, end_date=END)` | 沪深两市总成交额历史 | 近 5 日 / 20 日 / 60 日 / 250 日均量;当前 vs 多年量能区间位置 |
| 5 | `get_stock_northbound_holding(start_date=START, end_date=END)` | 北向资金持仓变化历史 | 近 20 日 / 60 日 / 250 日累计净流入 |
| 6 | `get_cn_bond_yield(start_date=START, end_date=END)` | 10Y 国债利率历史 | 当前值 + 短/中/长期方向 |
| 7 | `get_bond_yield_spread(start_date=START, end_date=END)` | 信用利差历史 | 当前 vs 20 日 / 60 日 / 多年均值 |

### 选查(有条件时)

| 步骤 | 工具 | 何时查 | 判断什么 |
|------|------|--------|---------|
| 8 | `get_usstock_index_quote(symbol="DJIA.GI,SPX.GI,NDX.GI", start_date=START, end_date=END)` | 美股有大波动时 | 海外映射 + 短中长周期位置 |
| 9 | `get_stock_fund_flow` | 成交额异常时 | 主力资金方向(短期信号) |
| 10 | `get_cn_macro_data` | 月初 PMI/CPI 发布时 | 宏观周期拐点 |
| 11 | `get_southbound_hkd_turnover(start_date=START, end_date=END)` | 港股联动显著时 | 南向资金短中长热度 |

**说明**:
- 步骤 2 `get_ashares_index_val` 不支持 `start_date/end_date`,百分位窗口由后端默认(通常 5 年)。如需 1 年 / 3 年百分位,bot 可从步骤 1 拿到的 K 线序列里自算价格分位作为近似
- 所有 K 线类工具拿到序列后,bot 自行决定算什么(MA / 均量 / 累计 / 历史分位 / 多年中枢距离…),skill 不规定
- 数据缺失(工具超时/返回为空)→ 对应维度叙事写"缺失,按 X 处理",`regime` 降级 `range`

## Regime 判定规则

### bull(牛市)

满足以下条件中的 **3 条以上**:
- 沪指收盘价 > 60 日均线,且 20 日均线 > 60 日均线
- 近 5 个交易日平均成交额 > 8000 亿
- 北向资金近 20 日累计净流入 > 0
- GVIX(隐含波动率) < 20
- 沪深 300 PE 百分位 < 80%(未到极端泡沫)

### range(震荡)

不满足 bull / bear / crisis 任何一个的条件,或:
- 沪指在 60 日均线附近 ±3% 范围内
- 成交额在 6000~9000 亿之间
- 无明确趋势方向(20 日均线走平)

### bear(熊市)

满足以下条件中的 **3 条以上**:
- 沪指收盘价 < 60 日均线,且 20 日均线 < 60 日均线
- 近 5 个交易日平均成交额 < 6000 亿
- 北向资金近 20 日累计净流出
- 信用利差走阔(高于 20 日均值)

### crisis(危机)

满足以下条件中的 **2 条以上**:
- 沪指单周跌幅 > 8%
- GVIX > 30
- 信用利差单周走阔幅度 > 20bp
- 出现连续 3 日以上千股跌停

> crisis 是极端状态,触发后投顾巡检进入"安全模式"——以保命为主,暂停所有加仓操作。

## 各类资产方向判定

### 股票(A 股)

| 方向 | 条件 |
|------|------|
| **up** | 沪指/创业板指 > 20 日均线,且最近 5 日有 3 日收阳 |
| **flat** | 指数在 20 日均线附近震荡,无明确方向 |
| **down** | 指数 < 20 日均线,且最近 5 日有 3 日收阴 |

动量(momentum):
- **strengthening**: 方向和成交额同步放大
- **stable**: 方向延续但成交额稳定
- **weakening**: 方向还在但成交额萎缩(可能变盘)

### 债券

| 方向 | 条件 |
|------|------|
| **up**(债牛) | 10Y 国债利率下行趋势(利率跌 = 债价涨) |
| **flat** | 利率窄幅波动 |
| **down**(债熊) | 10Y 国债利率上行趋势 |

### 黄金

| 方向 | 条件 |
|------|------|
| **up** | 黄金 ETF(518880) 价格 > 20 日均线,或国际金价创近期新高 |
| **flat** | 窄幅震荡 |
| **down** | 价格 < 20 日均线,且连续回调 |

### 现金吸引力

| 水平 | 条件 |
|------|------|
| **high** | 货基 7 日年化 > 2%,且其他资产方向 down/flat — 现金是避风港 |
| **medium** | 货基收益一般,或其他资产方向 mixed |
| **low** | 货基收益低,且股/金等资产方向 up — 持现金是浪费 |

## 基金链路适配（2026-04-29 起）

当本 skill 在**基金链路**中被调用（cron prompt 会显式标注 `paradigm_active`）：

- **输出位置**：写到 `memory/portfolio/fund/市场环境判断.md`（注意 `fund/` 子目录），不是上面的 `memory/portfolio/市场环境判断.md`
- **frontmatter 要加 `paradigm_active` 字段**（值取自 `fund_paradigm_runs`）；其他字段沿用上方 schema
- **`focus_industries` 字段**：B1/B2 范式必填（B2 填单一行业、B1 填关注的多个行业）；A/C 留空数组
- **完整 5 份 MD 的 schema 与校验红线**见 `workspace/skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md` "十二点五、统一 MD 格式与 MD→DB 落库"
- **基金链路的写库由 `fund_md_to_db.py` 完成**，bot 只写 MD，不调任何 `save_*`/`apply_*` 工具

## 与投顾巡检的对接

本 skill 的输出会被以下环节读取:

1. **Phase B0(市场状态输入)**: 读 regime + 各资产方向 + timing_stance,作为后续 bot 自己决定仓位和节奏的市场背景
2. **Phase C(巡检决策)**: 读 regime,在"决策矩阵"中查对应列——同一个产品表现,在 bull 和 bear 下做不同决策
3. **bot 调仓叙事**: 引用 Layer 3 的"一句话概要"在调仓理由中引用,让叙事有市场背景

## 注意事项

- 这个 skill **不做投资建议,不预测方向**,只做状态分类
- 如果数据不全(某个工具调用失败),regime 降级为 `range`(最保守的默认值)
- crisis 判定要慎重——一旦输出 crisis,所有 bot 会进入安全模式暂停加仓
- 输出的 regime 是**当前 bot 自己的市场观点**,不与其他 bot 同步。同一交易日不同 bot 给出不同 regime 是正常的——每只 bot 的人设(风险偏好、能力圈、对宏观信号的敏感度)决定了 ta 怎么看市场。例如保守型 bot 看到指数连续 2 日回调可能就判 range,进取型 bot 同样数据下可能仍判 bull(认为只是技术性回调)。人设差异不仅体现在"面对同一个 regime 做不同决策",更前置地体现在"对市场状态的判断本身"。
- **不要输出任何大类资产比例**。权益/债券/黄金/现金配比由每个 bot 在后续选品环节结合自己的画像、风险带和当前持仓独立决定
