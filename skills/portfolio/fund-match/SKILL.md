---
name: fund-match
description: **公募基金直投**选品（不是投顾）。基于最新市场状态和 bot 投资画像，从 fund.db 核心池 631 只基金中通过四层漏斗自主选品，写 fund_allocation_runs / fund_selection_runs（fund.db）。基金代码是 6 位数字；如果你想找投顾产品（5字母+1数字），请改用 /tougu-product-match。
---

## 范式参数(2026-04-29 三层框架)

执行前必须查询当日 paradigm。不同 paradigm 走不同选基逻辑:

| paradigm | 选基逻辑提示 |
|---|---|
| A | 跳过四层漏斗,直接从"大类代表性产品白名单"选 4-7 只(每类资产 1-2 只)。白名单由各 bot 在 `基金投资策略摘要.md` 自维护 |
| B1 | Layer 1 按 bot 自定义的 3-5 个目标行业分别筛选;Layer 2-4 在每个行业内挑业绩 + 经理 |
| B2 | Layer 1 直接锁定 capability_value 行业;Layer 2-4 在该行业内深选 + 配缓冲产品 |
| C | 完全走现有四层漏斗(L1 资产匹配 → L2 风格行业 → L3 业绩 → L4 个性终选) |

具体阈值、行业列表、缓冲产品类型等由 bot 自定;框架只规定 paradigm 必须先读。

## ⚠️ 写入约束（2026-04-29 起：MD-only，禁直写库）

> **本节优先级高于下方 Step 9 的旧"sqlite3 INSERT"段落。** 旧段落保留是为了说明字段含义，但**不再是 bot 的执行路径** — bot 只写 MD，由 `fund_md_to_db.py` 落库。

- bot 输出位置：`memory/portfolio/fund/个性化基金选择.md`（必须以 YAML frontmatter 起头）
- frontmatter schema：见 `workspace/skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md` "十二点五、统一 MD 格式" 第 3 项 `step: fund_selection`
- **禁止调用** `save_allocation_run` / `save_selection_run` / 任何 `save_*`/`apply_*`/`upsert_*` MCP 工具：bot 用的 fund-portfolio-mcp readonly 端不暴露这些工具，调了会报 `tool not found`。
- 落库由 `fund-daily-refresh.py` 跑完 bot CLI 后，调 admin 端 fund-portfolio-mcp 完成。

> ⚠️ **不要和投顾选品 `/tougu-product-match` 混淆。** 两条链路独立：
> | | **fund-match（本 skill）** | tougu-product-match |
> |---|---|---|
> | 业务 | 公募基金直投 | 投顾产品组合 |
> | 数据库 | fund.db | tougu.db |
> | 候选池 | `fund_info` 631 只 | `tougu_info` |
> | 写出表 | `fund_allocation_runs` / `fund_selection_runs` | `tougu_allocation_runs` / `portfolio_plans` |
> | 标的代码 | 6 位数字 | 5 字母+1 数字 |


# 基金选品 /fund-match

**触发词**: `/fund-match`, "你的基金组合是什么", "你买了哪些基金", "重新选基"
**执行频率**: 每次巡检时由 fund-review 调用，或手动触发
**输出位置（DB 是事实源，markdown 是人读视图，两者必须双写）**:
- 数据库 `/home/rooot/.openclaw/data/fund.db`:
  - `fund_allocation_runs` — 大类配置：`run_id / bot_id / trade_date / regime / market_summary_md / asset_target_json`
  - `fund_selection_runs` — 选品漏斗：`run_id / bot_id / trade_date / layer1_count / layer2_count / layer3_count / layer4_count / selected_funds_json / eliminated_json / selection_md`
- Markdown 归档: `memory/portfolio/fund/个性化基金选择.md`（人读，与 DB `selection_md` 保持一致）

**运行态要求**:
- 若任务上下文提供了 `run_id / trade_date`,必须沿用；否则自己生成（如 `manual-{date}-{bot_id}`）
- DB 写入用 sqlite3 直连即可（fund-portfolio-mcp 起来后也可走 `save_allocation_run` / `save_selection_run` 工具）
- markdown 和 DB 内容必须一致（先写 DB 再 dump 同样内容到 markdown）

---

## 定位

本 skill 是 **bot 自主选基能力**。与投顾选品（tougu-product-match）的核心区别:

| 维度 | 投顾选品 | 基金选品 |
|------|---------|---------|
| 候选池 | 几十个投顾产品,平台已筛过 | 核心池 631 只（已落 fund.db） |
| 决策权 | bot + 研究部确认 | **bot 完全自主** |
| 个性体现 | 主要靠权重和风险带 | **从选基开始就要体现个性**——基金经理偏好、风格选择、行业倾向 |
| 数据源 | tougu-portfolio-mcp | **fund.db**（Phase A 已落库，禁止再调 research-mcp） |
| 黄金配置 | 投顾产品中选 | **仅限黄金ETF场外联接基金白名单** |

**适用范围**: 所有参与基金直投的 bot 共享。不同 bot 之间只允许复用方法,不允许复用结论。

**边界说明**:
- 用户问"你自己的基金组合/你买了哪些基金"时触发本 skill
- 用户问"帮我选基金/我该买哪只"时**不应**触发
- 回答口径始终是第一人称买方视角

---

## 核心原则

1. **自上而下选基,业绩是最后一关**: 先定大类配置 → 按资产匹配 → 按风格行业筛 → 最后才看业绩
2. **穿透计算,不看表面分类**: 混合型基金必须穿透看底层股/债比例,按实际暴露归类
3. **黄金只买白名单**: 黄金配置仅限黄金ETF场外联接基金,不投贵金属主题基金
4. **个性在 Layer 2 和 Layer 4 体现**: 风格/行业选择和终选取舍是 bot 个性的核心表达
5. **数据缺失不编造**: 缺少关键字段标注"低置信度",不虚构业绩或风格
6. **每次重新选品**: 不复用旧结果,基于最新数据重新走漏斗
7. **T+1 成交,目标组合是估算**: 基金非盘中实时交易,T 日下单以 T+1 净值成交,目标组合中的份额/金额是基于当前净值的估算
8. **费率影响真实收益**: 申购费、赎回费、管理费都是真实成本,选基时必须考虑费率,同等业绩选费率更低的

---

## 数据采集

**所有数据从 fund.db 读，禁止调 research-mcp。** Phase A 已经把核心池 631 只基金的必要数据全部落库。

### fund.db 表结构（4 张已就绪）

| 表 | 用途 | 关键字段 |
|----|------|---------|
| `fund_info` | 基金主表（631 行） | `fund_code, fund_name, fund_company, fund_manager, fund_type, established_date, purchase_status, theme`（近一年主题：全市场/科技/医药/消费/金融/新能源/周期/基建地产 或多主题逗号串联，固收类为 NULL） |
| `fund_nav` | 最新净值（631 行） | `fund_code, nav_date, nav, acc_nav, daily_return_pct` |
| `fund_performance` | 多周期业绩（4704 行） | `fund_code, period`（1m/3m/6m/1y/2y/3y/5y/since_inception）`, return_pct, rank_pct, max_drawdown_pct, volatility_pct, sharpe_ratio, calmar_ratio` |
| `fund_style` | 股债现金占比（631 行） | `fund_code, as_of_date, equity_pct, bond_pct, cash_pct, other_pct`（来自最新季报；bond_pct 可能 > 100 是杠杆债基；equity_pct < 50 的指数型-股票多为 ETF 联接） |

### 数据约定

- 数据库已剔除「场内交易 / 暂停申购 / 封闭期」基金，只剩「开放申购 / 限大额」 — 选品时无需再筛 `purchase_status`
- 黄金白名单（002610/002611/000216/000217/000218/008701/008702）目前**不在 fund.db**，黄金类 bot 可直接套用白名单代码做配置（这是约定，无需查 DB）
- 货币基金不在核心池，现金类配置由 bot 在 markdown / DB 中标注「外部货基」即可

### 用 sqlite3 查 fund.db 即可（无外部 MCP 依赖）

```bash
sqlite3 -header /home/rooot/.openclaw/data/fund.db "SELECT ... FROM fund_info JOIN fund_style USING(fund_code) JOIN fund_performance ON ..."
```

---

## 输入约定

### 1. 市场状态 / 大类配置

优先级:

1. **allocation_runs**(推荐): 最近一次 `market-context` 输出
2. `memory/portfolio/市场环境判断.md`
3. 若缺失: 按 `regime=range`、`regime_code=neutral_range`、`timing_stance=hold` 处理,并标注

最少应拿到:

- `regime` / `regime_code` / `confidence` / `timing_stance`
- `asset_target_json`(若存在,用于约束大类配置方向)

### 2. 投资画像

按以下优先级读取:

1. `memory/portfolio/fund/基金投资策略摘要.md`(主入口,存在则必读)
2. `memory/portfolio/投资策略摘要.md`(投顾画像,提取能力圈和风险偏好)
3. `SOUL.md`、`IDENTITY.md`、`USER.md`
4. `memory/long_term/`
5. `memory/research/`
6. `memory/posts/`

### 3. 当前持仓(如有)

读取 `memory/portfolio/fund/当前基金持仓.md`,了解已有暴露,避免选品重复。

---

## 执行流程

### Step 1: 读取投资策略摘要

读取 `memory/portfolio/fund/基金投资策略摘要.md`,提取:

- 投资理念、选基标准(硬性门槛+软性偏好)
- 资产配置框架(三大类+现金的目标比例和弹性区间)
- 风格均衡要求
- 风险约束(单基金上限、单行业上限等)
- 黄金ETF白名单
- 能力圈分层(A/B/C档)

### Step 2: 读取市场状态,确定大类配置目标

从 `market-context` 获取:

- 当前市场环境(牛市/震荡/熊市/危机)
- `timing_stance`
- `asset_target_json`(若有)

结合策略摘要中的配置区间,确定本轮目标:

```
股票类: XX%  (区间 XX%-XX%)
债券类: XX%  (区间 XX%-XX%)
黄金类: XX%  (区间 XX%-XX%)
现金:   XX%  (区间 XX%-XX%)
```

### Step 3: 构建投资画像中间层

基于策略摘要 + 原始记忆,压缩成结构化画像:

- 风险承受力(结论 + 证据 + 置信度)
- 偏好基金类型(主动/被动/混合)
- 投资理念(长期持有 vs 波段)
- 能力圈赛道(A/B/C 档)
- 回避赛道
- 当前持仓/已知暴露
- 基金经理偏好(低换手/稳健/行业轮动)
- 回撤容忍度

### Step 4: 四层漏斗选基

#### Layer 1: 硬性门槛 + 资产配置匹配

**股票类和债券类 — 从全市场筛选:**

```
simple_fund_search(fund_type="股票型"/"混合型"/"指数型", limit=100)
simple_fund_search(fund_type="债券型", limit=100)
```

批量过基本门槛:

```
fund_basicinfo(fcodes="候选列表", fields="FCODE,SHORTNAME,FTYPE,DWJZ,FUNDSCALE,ESTABDATE,JJJL,SGZT,SHZT")
```

| 条件 | 阈值 |
|------|------|
| 基金规模 | ≥ 2亿 |
| 成立年限 | ≥ 2年 |
| 基金经理任职 | ≥ 1年 |
| 申赎状态 | 开放 |

穿透资产配置:

```
get_fund_comprehensive_analysis(fund_code="逐只")
→ 权益占比 / 固收占比 / 存款占比
→ 判断"偏股"还是"偏债"还是"均衡"
```

**黄金类 — 从固定白名单选择:**

黄金类只投黄金ETF场外联接基金(跟踪 AU9999),不投贵金属主题基金。直接从白名单筛选:

| 代码 | 名称 | 适合场景 |
|------|------|---------|
| 002610/002611 | 博时黄金ETF联接 A/C | 规模大,流动性好 |
| 000216/000217 | 华安黄金ETF联接 A/C | 规模大,费率低 |
| 000218 | 国泰黄金ETF联接A | 老牌产品 |
| 008701/008702 | 华夏黄金ETF联接 A/C | 注意限大额申购 |

长期持有选 A 类,定投选 C 类,费率相近时选规模更大的。

确认白名单基金状态:

```
fund_basicinfo(fcodes="002610,002611,000216,000217,000218,008701,008702", fields="FCODE,SHORTNAME,INDEXCODE,SGZT,FUNDSCALE")
```

**穿透匹配逻辑:**

一只偏股混合基金(权益占比 70%)占组合 20% → 贡献股票暴露 14%、债券暴露约 6%。所有基金穿透后加总应接近大类目标比例。

Layer 1 产出: **候选池(约 200-500 只)**,按大类分桶。

#### Layer 2: 风格标签 + 行业标签筛选

**这一层是 bot 个性的首次体现。**

风格维度 — `get_fund_style_analysis(fund_code)`:

| 风格 | 说明 | 适合的 bot 类型 |
|------|------|---------------|
| 大盘价值 | 低估值蓝筹,波动低 | 稳健型、"慢慢变富"型 |
| 大盘成长 | 龙头成长股,中等波动 | 均衡型 |
| 中小盘成长 | 高弹性,高波动 | 进取型、"追主线"型 |
| 均衡型 | 不偏科,分散 | 中性型 |

行业维度 — `get_fund_industry_holding(fund_code)` + `get_fund_sector(fund_code)`:

| bot 能力圈 | 行业筛选规则 |
|-----------|-------------|
| A档(深度跟踪) | 可选该行业的主题基金作为卫星仓 |
| B档(关注过) | 只通过宽基指数间接暴露,不选行业基金 |
| C档(证据不足) | 直接排除该行业的主题基金 |

> 风格分散、单风格集中度、行业主题占比、单基金权重上限等均由 bot 在自己的 `基金投资策略摘要.md` 里按人设自主定义,本 skill 不统一约束。

Layer 2 产出: **精选池(约 30-80 只)**,方向正确。

#### Layer 3: 业绩表现筛选

在方向正确的候选里**优中选优**。

`get_fund_performance(fund_code)`:

**硬约束（2026-05-11 起）**：
- 每只最终入选基金都**必须查看并写出** `1m / 3m / 6m / 1y / 3y` 五个周期的业绩数据，来源统一是 `fund_performance` 表。
- 这里的“写出”是指：在 `个性化基金选择.md` 的 frontmatter `funds[*].performance` 中带上这五个周期。
- 系统**不规定**这五个周期在你心中的权重怎么分配；短中长期哪个更重要，由 bot 按自己的人设和范式决定。
- 但**不得缺任何一个周期**。缺失会导致 `fund_md_to_db` 校验失败，整轮不入库。

| 指标 | 筛选标准 |
|------|---------|
| 近一年收益率 | 同类排名前 1/2 |
| 近两年收益率 | 同类排名前 1/2 |
| 近两年最大回撤 | 同类排名前 1/3 |
| 夏普比率(近两年) | ≥ 1.0 |
| 业绩稳定性 | 近四个季度中至少3个排名前 1/2 |

补充评估:

| 指标 | 工具 | 目的 |
|------|------|------|
| 基金经理历史 | `get_fund_manager_info` | 任职年限、管理规模、历史业绩 |
| 换手率 | `get_fund_turnover_rate` | 低换手 = 长期持有型经理 |
| 费率 | `fund_rate` | 同等业绩下选费率更低的 |
| 重仓股集中度 | `get_fund_top_stocks` | 前十大占比是否合理 |
| 超额收益 | `get_fund_index_return` | 相对基准的 alpha 稳定性 |

Layer 3 产出: **匹配池(约 10-20 只)**,方向对且业绩好。

#### Layer 4: 个性化终选(bot 人设驱动)

剩下的 10-20 只基金在资产类别、风格行业、业绩上都已合格。最终选出 5-8 只组成目标组合,这是 **bot 个性的终极表达**。

个性化维度从以下文件读取:

- `IDENTITY.md` → 人设定位
- `SOUL.md` → 核心价值观、投资底色
- `memory/portfolio/fund/基金投资策略摘要.md` → 风险偏好、能力圈、产品偏好

终选决策维度:

| 维度 | 如何体现个性 |
|------|------------|
| 基金经理偏好 | "慢慢变富"型选任职3年+、低换手;"追主线"型选行业轮动能力强的 |
| 集中度偏好 | 均衡型选持仓分散的;锐度型可接受集中持股 |
| 回撤容忍 | 稳健型同等收益选回撤更小的;进取型可接受更大回撤换更高收益 |
| 重叠度检查 | 检查候选基金之间重仓股重叠度,排除高度雷同的品种 |
| 组合完整度 | 确保最终组合在风格、行业、基金公司上足够分散 |

Layer 4 产出: **目标组合(5-8 只)**。

### Step 5: 费率评估与 A/C 类选择

在确定目标组合前,对每只候选基金做费率评估:

```
fund_rate(fund_code) → 申购费率、赎回费率阶梯、管理费、托管费、销售服务费
```

**A/C 类选择规则:**

| 持仓预期 | 选择 | 原因 |
|---------|------|------|
| 核心底仓(>1年) | A 类 | 申购费一次性 0.12%-0.15%,长期无额外成本 |
| 卫星仓位/可能调整(<1年) | C 类 | 免申购费,减少调仓摩擦 |
| 定投(分批买入) | C 类 | 每次免申购费,减少频繁小额的费率损耗 |

**费率纳入选基打分:**

- 同等业绩下选费率更低的(管理费+托管费 ≤ 同类中位数)
- 在最终组合中标注每只基金的 A/C 选择和费率

### Step 6: 市场状态映射为建仓闸门

根据 `timing_stance`,对新基金建仓做闸门判断:

| timing_stance | 建仓口径 | 执行含义 |
|------|------|------|
| `aggressively_add` | 立即建仓 | 按目标比例正常申购 |
| `add_on_pullback` | 分批建仓 | 高波动基金分两到三次申购 |
| `hold` | 谨慎观察 | 优势不显著则延后申购 |
| `defensive` | 防守准入 | 只允许债券/货币/低波基金立即申购 |
| `risk_off` | 原则上延后 | 原则上不新增高波动权益基金 |

补充规则:

- `timing_stance` 约束的是**新申购节奏**,不否定 bot 的长期偏好
- 强画像匹配但时机一般的基金,可进入目标组合,但执行建议写成"分批买"或"延后买"
- `confidence=low` 时,所有高波动基金最多"分批建仓"

### Step 7: 构建最终组合

- 核心底仓 2-3 只,卫星增强 1-3 只,防御缓冲 1-2 只
- 允许保留现金位(货币基金/活期理财)
- 不为分散买不懂的基金
- 不为追热点突破核心风险带

**硬约束校验：**

唯一持仓硬约束 —— **黄金类只买白名单内的黄金 ETF 场外联接基金**（详见硬约束章节）。

其余的单基金权重上限、行业暴露上限、同基金公司/经理集中度、行业主题占比等都由 bot 在自己的 `基金投资策略摘要.md` 里按人设自主定义；选品时 bot 自己对照检查，本 skill 不再设统一阈值。

对每个新入选基金给出执行标签: `立即买` / `分批买` / `延后买`

### Step 8: 文风改写层

文风放在决策之后。从 SOUL.md / IDENTITY.md / posts 提取表达风格。

**文风只影响怎么说,不影响选什么。**

### Step 9: 覆盖写入 DB + markdown

**先写 DB 两张表（事实源），再把同样内容 dump 到 markdown**。

#### 9.1 写 fund_allocation_runs（大类配置结果）

```bash
sqlite3 /home/rooot/.openclaw/data/fund.db "INSERT INTO fund_allocation_runs (run_id, bot_id, trade_date, regime, market_summary_md, asset_target_json) VALUES (?, ?, ?, ?, ?, ?);"
```

`asset_target_json` 必填示例：
```json
{"股票类": 0.40, "债券类": 0.30, "黄金类": 0.12, "现金": 0.18}
```
（百分比用小数表示，加总 = 1.0；可附带 `confidence`、`adjust_reason` 等字段）

#### 9.2 写 fund_selection_runs（选品结果）

```bash
sqlite3 /home/rooot/.openclaw/data/fund.db "INSERT INTO fund_selection_runs (run_id, bot_id, trade_date, layer1_count, layer2_count, layer3_count, layer4_count, selected_funds_json, eliminated_json, selection_md) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
```

`selected_funds_json` 是最终目标组合，每只基金一个对象，必含字段：
```json
[
  {
    "fund_code": "006729",
    "fund_name": "...",
    "asset_class": "股票类",       // 严格枚举见下
    "target_weight": 0.12,         // 小数
    "target_amount": 12000,        // 元（按总资金折算）
    "theme": "全市场",              // 来自 fund_info.theme，固收/现金可空
    "role": "核心底仓",             // 严格枚举见下
    "reason": "...",                // 选择理由（必填，体现 bot 人设）
    "performance": {
      "1m": {"return_pct": 3.2},
      "3m": {"return_pct": 8.7},
      "6m": {"return_pct": 15.4},
      "1y": {"return_pct": 22.1},
      "3y": {"return_pct": 41.6}
    }
  },
  ...
]
```

`performance` 字段是**硬约束**：
- 每只最终入选基金都必须带 `1m / 3m / 6m / 1y / 3y`
- 每个周期至少要有 `return_pct`
- 这些数据来自 `fund_performance` 表；可在正文里额外解释你更看重哪个周期，但 frontmatter 里五个周期必须齐

**枚举规范（严格，不允许自创）**：

| 字段 | 允许值 |
|------|-------|
| `asset_class` | `股票类` / `债券类` / `黄金类` / `现金` |
| `role` | `核心底仓` / `卫星增强` / `防御缓冲` / `对冲配置` / `流动性储备` |

> 这套与 [`tougu-product-match`](../tougu-product-match/SKILL.md) 选品的 role 命名保持一致，方便跨链路对比和聚合。

`selection_md` 存与 markdown 同样的全文内容（让 DB 自包含，方便后续查询）。

#### 9.3 dump markdown

把 `selection_md` 同样内容覆盖写到 `memory/portfolio/fund/个性化基金选择.md`，作为人读视图。

每次选品都覆盖写 markdown + 新增一条 DB 记录（DB 保留历史轨迹）。

---

## 输出格式

```markdown
# [bot 名字] 的基金选择

**选品日期：** YYYY-MM-DD
**数据来源：** fund.db（核心池 631 只 + 净值 + 业绩 + 配置比例）
**市场状态来源：** memory/portfolio/市场环境判断.md（每个 bot 自己一份）
**执行状态：** 成功 / 信息不足 / 不适用

---

## 输入状态

- **fund.db**：可读（4 张表行数）
- **人设文件**：已读取哪些文件
- **记忆覆盖**：long_term / research / posts 有无
- **市场状态**：已读取 / 缺失
- **当前基金持仓**：有 / 无

---

## 大类配置目标

| 大类 | 目标权重 | 弹性区间 | 本轮选定 |
|------|---------|---------|---------|
| 股票类 | XX% | XX%-XX% | XX% |
| 债券类 | XX% | XX%-XX% | XX% |
| 黄金类 | XX% | XX%-XX% | XX% |
| 现金 | XX% | XX%-XX% | XX% |

**市场环境：** regime / regime_code / timing_stance
**对本轮建仓的影响：** ...

---

## 投资画像中间层

- **风险承受力**：[结论] — [证据] — [置信度]
- **偏好基金类型**：...
- **投资理念**：...
- **能力圈赛道**：A档 / B档 / C档
- **回避赛道**：...
- **基金经理偏好**：...
- **回撤容忍度**：...

---

## 四层漏斗筛选过程

### Layer 1: 硬性门槛 + 资产配置匹配

- 初始候选：X 只
- 通过门槛：X 只
- 资产配置匹配后：X 只（股票类 X 只 / 债券类 X 只 / 黄金类 X 只）

### Layer 2: 风格 + 行业筛选

- bot 选择的风格方向：...
- bot 选择的行业方向：...
- 精选池：X 只

### Layer 3: 业绩表现

- 匹配池：X 只

### Layer 4: 个性化终选

终选依据摘要...

---

## 最终基金组合

| 基金名称 | 代码 | A/C | 大类 | 目标权重 | 角色 | 风格标签 | 申购费率 | 建仓建议 | 选择理由 |
|---------|------|-----|------|---------|------|---------|---------|---------|---------|

### 穿透后大类暴露

| 大类 | 目标 | 实际穿透 | 偏差 |
|------|------|---------|------|
| 股票类 | XX% | XX% | ... |
| 债券类 | XX% | XX% | ... |
| 黄金类 | XX% | XX% | ... |
| 现金 | XX% | XX% | ... |

### 我为什么这样配

用 bot 自己口吻解释。

### 新基金执行清单

| 基金名称 | 是否新纳入 | 执行动作 | 原因 |
|---------|-----------|---------|------|

其中 `执行动作` 只能写：
- `立即买` — T 日下单,T+1 净值成交
- `分批买` — 分 2-3 次申购,每次间隔 ≥ 3 个交易日
- `延后买` — 暂不下单,等待更好时机
- `不买` — 不进入执行

> **注意**: 所有"立即买"和"分批买"的实际成交价为 T+1 日净值,下单时无法确定。目标组合中的金额和份额均为基于当前净值的**估算值**。

---

## 我不会买的基金

| 基金名称 | 原因 | 证据强度 |
|---------|------|---------|

---

## 结论摘要

- **核心底仓**：[名称]
- **卫星增强**：[名称或"暂无"]
- **防御缓冲**：[名称或"暂无"]
- **本次判断最大的前提**：[说明]
- **本次判断最大的盲区**：[说明]
```

---

## 硬约束

1. **你是在替 bot 自己选,不是替用户选**: 第一人称买方视角
2. **每次都重新选品**: 不复用旧结果,基于最新数据重新走漏斗
3. **自上而下,业绩最后**: 先资产配置 → 风格行业 → 业绩,不能跳过前两层直接看业绩排名
4. **穿透计算**: 混合型基金必须穿透看底层股/债比例
5. **黄金只买白名单**: 仅限黄金ETF场外联接基金,不买贵金属主题基金
6. **个性在选择中体现**: Layer 2 和 Layer 4 必须体现 bot 的人设特征
7. **行为优先于口号**: 嘴上稳健但行为追高,按行为校准
8. **长期偏好优先于短期热点**: long_term 和 research 权重高于 posts
9. **证据不足时允许不知道**: 不编造人格、持仓或基金数据
10. **风格层与决策层分离**: 文风改写不能反推选择
11. **允许结果是"少买"或"不买"**: 没有合适基金时不强行构建
12. **所有关键判断留证据**: 至少写到评分依据
13. **市场状态只约束节奏,不直接决定偏好**: 择时不能覆盖 bot 的人格画像
14. **黄金白名单（唯一持仓硬约束）**: 黄金类只买黄金 ETF 场外联接白名单（002610/002611/000216/000217/000218/008701/008702），不买贵金属主题基金
15. **7 天持有底线（交易铁律）**: 申购后 7 天内绝不赎回（1.5% 惩罚费），crisis 例外
16. **费率必须评估**: 选基时用 `fund_rate()` 获取费率,核心底仓选 A 类,卫星仓选 C 类
17. **目标组合标注估算性质**: 份额和金额基于当前净值估算,实际以 T+1 成交净值为准
18. **单基金权重 / 行业集中度 / 风格分散等不再统一硬性规定**: 由 bot 在自己的 `基金投资策略摘要.md` 里按人设自主定义边界，本 skill 不强制统一阈值

_通用基金选品 skill,所有参与基金直投的 bot 共享。修改此文件会影响全部 bot 的选基逻辑。_
