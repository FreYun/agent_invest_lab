---
name: fund-review
description: **公募基金直投**巡检（不是投顾）。对 bot 在 fund.db / fund_bot_holdings 表里的公募基金持仓做巡检，用 fund.db 数据，写 fund.db 表。基金代码是 6 位数字（如 006729）；投顾产品代码是 5 字母+1 数字（如 O0QRRTT），如果你看到的是后者请改用 /tougu-portfolio-review。
---

## 范式参数(2026-04-29 三层框架)

执行前必须查询当日 paradigm。不同 paradigm 走不同巡检逻辑:

| paradigm | 巡检逻辑提示 |
|---|---|
| A | **不走单基金矩阵**:只看大类偏离度,触发条件是宏观信号变化或大类偏离 >5%。单基金不做止盈止损 |
| B1 | 单基金矩阵 + 行业相对强弱排序叠加:某行业排名跌出前列 → 减仓该行业 |
| B2 | 单基金矩阵 + 主战行业拥挤度/景气度叠加:行业拥挤过高 → 即使单基金状态 HOLD 也减仓到下限 |
| C | 完全走现有 Phase C 矩阵(产品 × regime → 矩阵 → 动能修饰 → 人设偏移) |

具体阈值、信号来源等由 bot 自定;框架只规定 paradigm 决定走哪条主分支。

## ⚠️ 写入约束（2026-04-29 起：MD-only，禁直写库）

> **本节优先级高于下方 Step 8/9 的"DB 写入"段落。** 旧段落保留是为了说明字段含义和 T+1 对账逻辑，但**不再是 bot 的执行路径** — bot 只写 MD，由 `fund_md_to_db.py` + admin MCP 完成事务写入。

bot 在巡检阶段需要写两份 MD（必须以 YAML frontmatter 起头）：

| 文件 | 作用 |
|---|---|
| `memory/portfolio/fund/当前基金持仓.md` | 巡检前/后持仓快照（review input + after 对账） |
| `memory/portfolio/fund/基金巡检记录.md` | 巡检结论 + 每只持仓的 action（含 HOLD） |

- **frontmatter schema 权威来源**：先调 `fund-portfolio-mcp.get_fund_md_schema()`（从落库校验常量实时读出，永远同步），或读 `workspace/skills/portfolio/_shared_docs/fund-investment/基金MD-frontmatter-schema.md`（人读版快照）。两者都列了每份 MD 的 `step` 固定值、所有必填字段、所有枚举白名单、数字口径。**严格照着写**——上几轮 8 个 bot 全军覆没就是因为：枚举字段写成句子（`layer2_pivot` 写「以 TMT 为核心 pivot…」而不是 `行业`）、`target_weight`/`weight` 用了百分数而不是 0~1 小数、漏 `step` 字段、选了非指数型基金等。
- **三个最容易错的口径**：① 大类比例 `today_target`/`central_baseline` 的 `*_pct` 是 0-100 百分数（和=100）；② 基金/持仓的 `target_weight`、`weight` 是 0~1 小数（占25% 写 0.25 不是 25）；③ `layer2_pivot`/`regime`/`timing_stance`/`asset_class`/`role`/`decision`/`action_type` 都是固定枚举不是自由文字（具体白名单见 get_fund_md_schema）。
- **职责边界**：bot 在 MD 里写的是“决策意图 + 人读解释”；系统真正落库时会按 DB 当前状态 + actions 重新计算 cash / holdings / weights / fee。`decision` / `action_type` / `amount` / `reason` / `fund_code` / `asset_class` / `role` / `target_weight` 等是事实输入必须对；`before_weight` / `after_weight` / `nav_used` / `shares` / `fee` / 持仓收益类数字是可选参考字段（系统会重算覆盖，可留空，留了要和同文件其他数字自洽，不要为凑字段硬编）。HOLD 行 `amount=0` + 理由即可。

### 写完 5 份 MD 后必做：调 `validate_fund_bot_md` 自检

bot 决策完、5 份 MD（投资框架/市场环境判断/个性化基金选择/当前基金持仓/基金巡检记录）都写好后，**必须**调一次：

```
fund-portfolio-mcp.validate_fund_bot_md(
  bot_id="bot1", run_id="<本轮 run_id>", trade_date="<YYYY-MM-DD>", paradigm_active="<A|B1|B2|C>")
```

返回 `{"success": bool, "blocking_issues": [...], "warnings": [...]}`。
- `success=false` → 看 `blocking_issues`，**回去修 MD**（对照 get_fund_md_schema 的 schema 改），改完重新调，**反复调直到 success=true 再结束**。
- `warnings`（以 `warn:` 开头）不阻断，系统会自动处理（比如自动补齐选基的多周期业绩），可以忽略。

### 系统落库由这些 MCP 小组件完成（bot 不用手动调，但要知道流程）

落库链路全部是 fund-portfolio-mcp 的写工具（每个一个独立组件，所有 DB 写入和数值计算都在 MCP 里，脚本里没有裸 SQL）。**bot 只负责"写 MD + 调 validate_fund_bot_md"**；下面这些由系统层 `fund_md_to_db.process_bot` / `fund-daily-refresh.py` 在 cron 里按顺序调用：

| 阶段 | MCP 工具 | 干什么 | 谁调 |
|------|---------|--------|------|
| 写 MD 前 | `get_fund_md_schema()` | 返回 5 份 MD 的完整 frontmatter schema（step/必填字段/枚举白名单/数字口径，从落库校验常量实时读出） | **bot 第一步就调，照着写** |
| Phase B-1（决策前） | `select_fund_paradigm(bot_id, trade_date, run_id)` | 读 `能力圈宣告.md` → 选定本日 paradigm（A/B1/B2/C/SKIP）→ 写 `fund_paradigm_runs` | cron（决策前已跑完，bot 拿到的 `paradigm_active` 就是它的产物）|
| 落库前自检 | `validate_fund_bot_md(bot_id, run_id, trade_date, paradigm_active)` | 校验 5 份 MD 的结构/一致性，返回 `{success, blocking_issues, warnings}` | **bot 写完 5 份 MD 后调，反复改到 success=true**（也是 cron 落库前的强校验）|
| 落库① | `save_allocation_run(run_id, bot_id, asset_target_json, trade_date, regime, market_summary_md, paradigm)` | 写市场环境判断 → `fund_allocation_runs` | cron（解析 `市场环境判断.md`）|
| 落库② | `save_selection_run(bot_id, trade_date, run_id, layer1~4_count, selected_funds_json, eliminated_json, selection_md)` | 写选基漏斗 → `fund_selection_runs` | cron（解析 `个性化基金选择.md`）|
| 落库③ | `apply_fund_review_and_rebalance(bot_id, decision, reason, actions_json, holdings_json, ..., paradigm)` | 写巡检结论 → `fund_bot_reviews`；把每条实质动作转成 **T 日 pending 在途单** → `fund_bot_orders`；更新持仓元数据。**不直接改 cash/持仓/收益** | cron（解析 `基金巡检记录.md` + `当前基金持仓.md`）|
| 落库失败回滚 | `rollback_fund_bot_run(bot_id, trade_date, run_id)` | 删本轮半成品（allocation_runs/selection_runs by run_id + 当日 reviews + 当日 pending orders）| cron（process_bot 中途失败时）|
| **T+1 收口** | `settle_pending_fund_orders(bot_id, as_of_date)` | 把 order_date 早于"下一交易日已出净值"的 pending 在途单按 **T+1 交易日的 `fund_nav`** 结算：申购费 = 金额×`fund_info.purchase_fee`（优惠1折）；赎回费 = 份额×T+1净值×`redeem_fee_json` 阶梯(持有天数)；管理费/托管费/销售服务费已含在公布净值里不二次计提。写 `fund_bot_actions`(action_date=T+1) + 更新 `fund_bot_holdings` + `fund_bot_accounts.cash`。**T+1 都是交易日不是自然日** | cron 下一轮 Phase A 拉到新净值后（Phase Settle），**不是 bot 的事** |
| 每日快照 | `record_fund_snapshot(bot_id, trade_date)` / `record_all_fund_snapshots(trade_date)` | 系统层**唯一**的账户业绩计算入口：持仓市值/份额/收益率/累计收益/最大回撤/大类权重 → `fund_bot_daily_snapshots` + `fund_bot_position_snapshots` + 更新 `fund_bot_holdings` 现态 | cron Phase D，**不是 bot 的事** |
| 费率维护 | `upsert_fund_fees(fees_json)` | 更新 `fund_info` 的申购费/管理费/托管费/销售服务费/赎回费阶梯 | 低频运维脚本 |

**关键时序**：bot 在 T 日决策 → MD 落库 → 实质买卖只挂成 T 日 pending 在途单（cash/持仓当天不变）→ T+1 交易日（下一轮 cron）`settle_pending_fund_orders` 按 T+1 净值 + 费率收口，持仓和现金才真正变 → 当天 `record_fund_snapshot` 重算业绩。所以 bot 写的"调仓后预估持仓/现金"只是参考，真实成交价是 T+1 净值，bot 看不到。

**bot 能调的 fund-portfolio-mcp 工具只有两个（都只读）**：`get_fund_md_schema`（拿 schema）和 `validate_fund_bot_md`（自检）。`save_*`/`apply_*`/`init_*`/`record_*`/`settle_*`/`rollback_*`/`upsert_*` 这些写工具一律不要调——它们由系统层在 cron 里调，参数（before 状态、actions 预演、费率）都由系统从 DB 算；bot 调了会得到 'tool not found'。bot 的活就是：调 get_fund_md_schema → 写 5 份 MD → 调 validate_fund_bot_md（改到 success=true）。

# 基金组合巡检 /fund-review

> ⚠️ **不要和投顾巡检 `/tougu-portfolio-review` 混淆。** 两条链路完全独立：
>
> | | **fund-review（本 skill）** | tougu-portfolio-review |
> |---|---|---|
> | 业务 | 公募基金直投 | 投顾产品组合 |
> | 数据库 | `/home/rooot/.openclaw/data/fund.db` | `/home/rooot/.openclaw/data/tougu.db` |
> | 持仓表 | `fund_bot_holdings` | `bot_holdings` |
> | 巡检表 | `fund_bot_reviews` / `fund_bot_actions` | `bot_reviews` / `bot_rebalance_actions` |
> | 标的代码 | 6 位数字（006729 / 002610） | 5 字母+1 数字（O0QRRTT / FYOR0DY） |
> | 数据源 | research-mcp + fund.db | research-mcp + tougu.db |
>
> **判断走哪条**：先 `SELECT fund_code FROM fund_bot_holdings WHERE bot_id='你的 bot_id' AND status='active'`。如果非空 → 走 fund-review；如果你的目标是「投顾产品」，切去 tougu-portfolio-review。两个 skill 不能交叉使用。

**触发词**: `/fund-review`, "检查你的**基金**要不要调", "更新你的**基金**持仓", "复核你的**基金**仓位"（注意"基金"和"投顾"是两个不同业务）

**定位**: 面向当前 bot 自己的**公募基金直投**持仓巡检 skill。模拟 bot 像真人基民一样，先用最新数据检查持仓异常，再对比目标组合决定要不要动、动多少。

**与投顾巡检的核心区别**:

| 维度 | 投顾巡检 | 基金巡检 |
|------|---------|---------|
| 数据源 | tougu-portfolio-mcp | **fund.db**（Phase A 已落库） |
| 异常检测 | 投顾产品少,无需 | **基金经理变更、规模骤降、风格漂移、净值异动** |
| 成交净值 | T 日净值即时成交 | **T+1 净值成交,下单时成交价未知** |
| 交易费率 | 投顾产品内含 | **申购费(0.12%-0.15%) + 赎回费(按持有天数阶梯)** |
| 7天惩罚费 | 无 | **持有<7天赎回费 1.5%,绝对禁止** |
| 订单确认 | 即时 | **T+1 确认,QDII T+2~T+3,需对账步骤** |
| 事实源 | MCP 数据库 | **fund.db**（DB 是事实源，markdown 是人读视图） |
| 最小权重变更 | 5% | **3%** |
| 操作顺序 | 先卖后买 | **先赎回 → 等资金到账(T+1~T+3) → 再申购** |

**运行态要求**:
- 若任务上下文给了 `run_id / trade_date`,必须沿用；否则自己生成（如 `review-{date}-{bot_id}`）
- DB 写入用 sqlite3 直连即可

---

## 核心数据源 / 输出位置

### 读（DB 事实源）

| 数据 | 表 | 用途 |
|------|----|----|
| 基金主表 / 净值 / 业绩 / 配置比例 | `fund_info` / `fund_nav` / `fund_performance` / `fund_style` | Phase A 已落库 |
| 当前账户 | `fund_bot_accounts` | 现金、初始资金 |
| 当前持仓 | `fund_bot_holdings WHERE status='active'` | 已确认持仓 |
| 在途订单 | `fund_bot_orders WHERE status='pending'` | T+1 待确认 |
| 目标组合 | `fund_selection_runs`（最新一行）+ `fund_allocation_runs`（最新一行） | 由 fund-match 写 |
| 市场状态 | `memory/portfolio/市场环境判断.md`（每个 bot 自己一份） | 由 market-context 写 |

### 写（DB 双写 + markdown 人读视图）

| 数据 | 表 / 文件 | 时机 |
|------|----------|------|
| 巡检结论 | `fund_bot_reviews`（含 `review_md` 字段） | Step 7 完成后 |
| 调仓动作 | `fund_bot_actions`（每只基金一行，链接 review_id） | Step 7 完成后 |
| 新订单 | `fund_bot_orders`（status=pending） | Step 7 决定调仓时插入 |
| 持仓更新 | `fund_bot_holdings` | Step 9 T+1 对账确认后写 |
| 账户现金 | `fund_bot_accounts.cash` | Step 9 对账后扣减/补足 |
| markdown 归档 | `memory/portfolio/fund/基金巡检记录.md` / `基金调仓日志.md` / `当前基金持仓.md` | Step 8（与 DB 同步） |

### 投资画像（read only）

`memory/portfolio/fund/基金投资策略摘要.md` — 包含 bot 自己定义的单基金权重上限、行业暴露、回撤红线、止损止盈线等（由 bot 按人设自定，不在 skill 层强制）

---

## Enum 规范（必须严格使用，与投顾流程一致）

写库时下列字段**只允许使用以下取值**，不许自创、不许译成英文/拼音、不许加 `_up` 之类后缀。和投顾流程 [`tougu-portfolio-review`](../tougu-portfolio-review/SKILL.md) 同源，参考权威 [`择时决策框架.md`](../_shared_docs/tougu/择时决策框架.md)。

### `fund_bot_reviews.decision`（review 级，全局结论）

| 值 | 含义 |
|----|------|
| `KEEP` | 维持现有持仓，无动作 |
| `REBALANCE` | 调权重，不换基金 |
| `SWITCH` | 替换基金（赎一只买一只） |

### `fund_bot_actions.timing_state`（持仓盈亏五档）

判定基于该基金的**累计收益率**（建仓以来）：

| 值 | 判定 |
|----|------|
| `大幅盈利` | 累计收益 ≥ bot 自定义止盈阈值 |
| `小幅盈利` | 0% < 累计收益 < 止盈阈值 |
| `微亏` | 止损阈值 < 累计收益 ≤ 0%（**刚建仓累计收益恰好 0%，归此档**） |
| `大幅亏损` | 累计收益 ≤ 止损阈值 |
| `高点回撤` | 累计仍为正，但区间最大回撤 ≥ bot 自定义加仓阈值 |

### `fund_bot_actions.momentum_state`（近期动能五档）

基于 `fund_performance` 表近一月 / 近三月数据：

| 值 | 判定 |
|----|------|
| `加速向上` | 近一月 ≥ +5% 且 近一月 ≥ 近三月 × 0.45 |
| `稳步向上` | 近一月 +2%~+5%，或 近一月 > 0 且 近一月最大回撤 ≤ -3% |
| `停滞` | -2% ≤ 近一月 ≤ +2%（**刚建仓无近一月数据，归此档**） |
| `减速但未转` | 近一月 < 0 且 近三月 > 0 |
| `已转弱` | 近一月 ≤ -3% 或 近一月最大回撤 ≤ -8% |

### `fund_bot_actions.matrix_suggestion`（决策矩阵原始建议）

矩阵交叉格输出，**不是 KEEP/REBALANCE**，是单基金动作类型：

| 值 | 含义 |
|----|------|
| `HOLD` | 继续持有 |
| `ADD` | 加仓 |
| `REDUCE` | 减仓（部分赎回） |
| `TAKE_PROFIT` | 止盈（按 1/3 减仓） |
| `STOP_LOSS` | 止损（清仓或减半） |

### `fund_bot_actions.final_decision`（叠加动能修饰 + 人设偏移后的最终动作）

**取值集合与 `matrix_suggestion` 相同**：`HOLD / ADD / REDUCE / TAKE_PROFIT / STOP_LOSS`

### `fund_bot_actions.action_type`（写库的动作类别，最终落地）

通常等于 `final_decision`：`HOLD / ADD / REDUCE / TAKE_PROFIT / STOP_LOSS`。`HOLD` 也必须写一行（至少 fund_code + action_type + amount=0 + reason），保留完整审计轨迹。`before_weight` / `after_weight` 若写，属于参考字段，不是系统事实源。

### `fund_bot_actions.trigger`（触发通道）

| 值 | 含义 |
|----|------|
| `timing_matrix` | 决策矩阵触发的常规巡检动作 |
| `anomaly` | 异常检测触发（经理变更/规模骤降/风格漂移/净值异动） |
| `rebalance` | 大类配置偏差触发的再平衡 |
| `cooldown` | 冷静期内被拦截 |
| `momentum_reversal` | 动能反转修饰触发 |

### `fund_bot_orders.order_type`

`BUY` / `REDEEM`

### `fund_bot_orders.status`

`pending` / `confirmed` / `cancelled`

> **写库前自检**：把每条 action / order 的枚举字段过一遍上述清单。如果碰到「累计收益率刚好 0%」「刚建仓无动能数据」这种边界情况，按上面括号里的默认值填，不要写 `neutral` / `just_entry` / `strong_up` 等自创值。

---

## 定时主流程

```
Step 0: 读取最新市场状态(market-context / allocation_runs)
    ↓
Step 0.5: 检查在途订单,若有 pending 订单且 T+1 净值已出 → 先执行对账确认(Step 9)
    ↓
Step 1: 基金异常检测(基金直投特有)
    ↓
Step 2: 获取最新数据,重新生成目标组合(调用 fund-match)
    ↓
Step 3: 读取当前持仓(区分已确认持仓 vs 在途订单)
    ↓
Step 4: 评估已有持仓的择时状态
    ↓
Step 5: 对比目标组合 vs 当前持仓
    ↓
Step 6: 套用真实世界约束
    ↓
Step 7: 形成调仓动作(先赎回后申购,含费率计算,T日下单/T+1成交)
    ↓
Step 8: 写回 markdown 归档(人读视图；数值可为参考估算,最终事实以系统落库和 Phase D 快照为准)
    ↓
Step 9: T+1 对账确认(净值公布后,用实际值更新持仓)
```

---

## 输入优先级

### 1. 当前持仓

```bash
sqlite3 /home/rooot/.openclaw/data/fund.db "SELECT h.*, n.nav AS today_nav FROM fund_bot_holdings h LEFT JOIN fund_nav n USING(fund_code) WHERE h.bot_id='{bot_id}' AND h.status='active';"
```

无持仓时（`fund_bot_accounts` 无该 bot），先跑 fund-match 生成目标组合，再用 `scripts/fund-bootstrap.py --bot {bot_id}` 初始化建仓。

### 2. 目标组合

**每次巡检都重新生成**，不复用旧的目标组合:

1. 调用 `fund-match` 重新选基（写新行到 `fund_selection_runs` + `fund_allocation_runs`）
2. 巡检中用最新一行作为「目标组合」
3. markdown 同步覆盖 `memory/portfolio/fund/个性化基金选择.md`

### 3. 投资画像

必须先读 `memory/portfolio/fund/基金投资策略摘要.md`,不足时才回看 SOUL.md / IDENTITY.md。

### 4. 市场状态

优先级:

1. **allocation_runs**: 最近一次 `market-context` 输出
2. `memory/portfolio/市场环境判断.md`
3. 若缺失: 按 `regime=range`、`timing_stance=hold` 处理,并标注

---

## 真实世界约束

| 约束 | 值 | 说明 |
|------|-----|------|
| 冷静期 | 7 天 | 距上次调仓不足 7 天,默认不做大动作 |
| **7天持有底线** | **绝对禁止** | **持有<7天赎回=1.5%惩罚费,任何情况下不可触犯** |
| 最小变更阈值 | 3% | 单基金权重变化 < 3% 默认忽略 |
| 最大换手 | 25% | 单次巡检最多调整总仓位的 25% |
| 核心基金替换 | 1 只 | 单次最多替换 1 只核心基金 |
| 操作顺序 | 先赎回后申购 | **先释放资金,等到账后再申购**(T+1~T+3 延迟) |
| QDII 特殊处理 | 赎回到账 T+5~T+10 | 调仓计划需考虑资金占用 |
| 证据不足 | 偏向不动 | 新方案优势不明显时继续持有 |
| 风险不升档 | 无强证据不升 | 不因短期收益高而上调风险带 |
| 现金是主动仓位 | — | 保留现金是风险管理,不是没想好 |

---

## 执行流程

### Step 1: 基金异常检测(基金直投特有)

投顾产品不用担心的事,基金直投必须自己盯。对每只持仓基金逐一检测:

| 异常 | 检测方式 | 应对 |
|------|---------|------|
| 基金经理变更 | `get_fund_manager_info(fund_code)` 对比上次记录 | 标记关注,观察 1 个月,严重时减仓 |
| 规模骤降(>30%) | `fund_basicinfo(fcodes, fields="FUNDSCALE")` 对比上次 | 立即评估是否赎回 |
| 风格漂移 | `get_fund_style_analysis(fund_code)` 对比上次 | 评估是否偏离选基理由 |
| 净值异动 | `get_fund_abnormal_movement(fund_code)` | 查原因,判断是否调仓 |
| 暂停赎回/限购 | `fund_basicinfo(fcodes, fields="SGZT,SHZT")` | 暂停赎回标记风险,限购影响加仓 |

**异常检测结论**: 每只基金标记为 `正常` / `关注` / `警告` / `危险`。`危险` 级别直接进入减仓/赎回候选。

### Step 2: 重新生成目标组合

调用 `fund-match` 方法,基于最新数据 + 投资策略摘要,生成新的目标组合。

**覆盖写入** `memory/portfolio/fund/个性化基金选择.md`。

### Step 3: 读取当前持仓

从 `memory/portfolio/fund/当前基金持仓.md` 读取:

- 账户概况(总资产、持仓市值、现金)
- 每只基金的持仓明细(份额、净值、市值、权重、角色、收益率)
- 大类配置实际分布(穿透后)
- 上次调仓日期(判断冷静期)

同时刷新持仓基金的最新净值:

```
fund_basicinfo(fcodes="持仓基金代码列表", fields="FCODE,SHORTNAME,DWJZ,JZRQ")
```

### Step 4: 评估已有持仓的择时状态

对每只当前持仓依次判断:

- 是否触发**单基金止损 / 止盈**（具体阈值由 bot 在自己的 `基金投资策略摘要.md` 里定）
- 是否触发**组合整体止损 / 回撤红线**（具体阈值同样由 bot 自定）
- 在当前 `regime / timing_stance` 下,是继续拿、加一点、减一点
- 该基金当前问题是"赛道逻辑变了"还是"只是入场时机一般"

> 此前文档示例的 -15% 单基止损 / +30% 止盈 / -10% 组合止损 / -8% 回撤红线仅供 bot 起草自己策略摘要时参考，**不再作为 skill 层的统一阈值**。每个 bot 应在策略摘要中按人设写明自己的止损止盈触发线。

每个持仓形成中间动作标签:

| 标签 | 含义 |
|------|------|
| `HOLD` | 继续持有 |
| `ADD` | 加仓 |
| `REDUCE` | 减仓 |
| `TAKE_PROFIT` | 止盈(赎回部分) |
| `STOP_LOSS` | 止损(赎回部分或全部) |

约束:

- **7天持有底线**: 持有不足 7 天的基金**绝对不可赎回**,即使触发止损条件也必须等满 7 天
- 不允许因净值上涨导致权重自然增大就误判需要卖出
- `timing_stance=defensive/risk_off` 时可降低加仓倾向,但不能无依据一刀切清空

### Step 5: 对比目标组合 vs 当前持仓

逐项比较:

- 基金是否一致
- 权重差异是否超过 3% 阈值
- 大类配置偏差是否超过 5%
- 当前持仓是否偏离 bot 风险带
- 新目标是否真的显著更优
- 新入选基金的建仓建议(`立即买`/`分批买`/`延后买`)

结论三选一: `KEEP` / `REBALANCE` / `SWITCH`

### Step 5.5: 区分已有持仓动作和新基金准入

先处理已有仓位,再处理新基金:

1. **已有持仓** — 根据 Step 4 的中间动作标签 + Step 1 的异常检测结果,决定维持、加仓、减仓、止盈或止损

2. **新基金** — 读取 `个性化基金选择.md` 中的建仓建议:
   - `立即买`: 允许纳入本轮申购
   - `分批买`: 本轮只买目标仓位的一部分
   - `延后买`: 先不纳入实际申购动作
   - `不买`: 不进入动作列表

如果目标组合里有新基金,但建仓建议为 `延后买`,最终结论仍可能是 `KEEP` 或仅小幅 `REBALANCE`。

### Step 6: 套用决策闸门

SWITCH 条件(全部满足):

- 新基金有明确优势
- 风险匹配不下降
- 不违反冷静期或换手上限
- 新基金在 bot 可接受能力圈
- 新基金申赎状态开放
- 新基金建仓建议不是 `延后买`
- **被替换的基金持有满 7 天**
- **摩擦成本核算通过**(见下)

**摩擦成本核算(换基必算)**:

换基金 A → B 时,必须计算总交易成本并与预期收益对比:

```
总摩擦成本 = 赎回A的赎回费 + 申购B的申购费
           = 赎回金额 × 赎回费率(按持有天数) + 申购金额 × 申购费率/(1+申购费率)

示例: 赎回持有 45 天的 A 基金 ¥20,000,申购 B 基金 ¥20,000
  赎回费 = 20000 × 0.25% = ¥50
  申购费 = 20000 × 0.15%/1.0015 ≈ ¥30
  总摩擦成本 = ¥80(占本金 0.40%)
```

**决策规则**: 如果新基金 B 相比旧基金 A 的预期年化超额 < 总摩擦成本 × 2,则不值得换。轻微更优 → 保持不动或仅小幅再平衡。

### Step 7: 形成调仓动作(先赎回后申购)

**固定顺序**: 先执行赎回释放资金 → 等资金到账 → 再执行申购。

#### 7.1 核心理解: 下单 vs 成交

bot 在 T 日做决策、下单,但**成交净值是 T+1 的**,下单时未知。因此:

- **下单时**: 用 T 日净值做**估算**,记录为 `decision_nav`(决策参考净值)
- **确认后**: T+1 净值公布后,用实际成交净值 `confirmed_nav` 重算份额和金额,更新持仓

所有调仓记录必须区分这两个阶段:

```
下单阶段(T日): decision_nav = T日净值, estimated_shares = 估算份额
确认阶段(T+1): confirmed_nav = T+1净值, confirmed_shares = 实际份额
```

#### 7.2 申购金额计算(含费率)

```
申购金额 = bot 决定投入的总金额(含手续费)
申购费率 = fund_rate() 返回的申购费率(平台折扣后)
净申购金额 = 申购金额 / (1 + 申购费率)
手续费 = 申购金额 - 净申购金额

# T日下单时估算:
estimated_shares = 净申购金额 / decision_nav(T日净值)

# T+1确认时实际:
confirmed_shares = 净申购金额 / confirmed_nav(T+1净值)
```

**示例**: 申购 ¥10,000,申购费率 0.15%(1折后),T 日净值 1.5000
- 净申购金额 = 10000 / 1.0015 = ¥9985.01
- 手续费 = ¥14.99
- T 日估算份额 = 9985.01 / 1.5000 = 6656.67 份
- 若 T+1 净值为 1.5200 → 实际份额 = 9985.01 / 1.5200 = 6569.09 份

#### 7.3 赎回到账计算(含费率)

```
赎回份额 = bot 决定赎回的份额
赎回费率 = 按持有天数查阶梯费率

# T日下单时估算:
estimated_amount = 赎回份额 × decision_nav(T日净值) × (1 - 赎回费率)

# T+1确认时实际:
confirmed_amount = 赎回份额 × confirmed_nav(T+1净值) × (1 - 赎回费率)
手续费 = 赎回份额 × confirmed_nav × 赎回费率
```

**示例**: 赎回 5000 份,持有 45 天(赎回费率 0.25%),T 日净值 1.5000
- T 日估算到账 = 5000 × 1.5000 × (1 - 0.0025) = ¥7481.25
- 若 T+1 净值为 1.4800 → 实际到账 = 5000 × 1.4800 × 0.9975 = ¥7381.50

#### 7.4 每笔动作记录字段

| 字段 | 说明 |
|------|------|
| `action` | REDEEM / SUBSCRIBE / CONVERT |
| `fund_code` / `fund_name` | 基金标识 |
| `order_date` | 下单日(T 日) |
| `order_amount` | 申购金额(含费) / 赎回份额 |
| `decision_nav` | T 日净值(决策参考,**非成交价**) |
| `fee_rate` | 适用费率 |
| `estimated_fee` | 估算手续费 |
| `estimated_shares` | 申购估算份额 / 赎回实际份额 |
| `estimated_net_amount` | 申购净金额 / 赎回估算到账金额 |
| `expected_confirm_date` | 预计确认日(普通 T+1,QDII T+2~T+3) |
| `expected_arrival_date` | 赎回资金预计到账日 |
| `status` | `pending`(已下单待确认) / `confirmed`(T+1 已确认) |
| `confirmed_nav` | T+1 实际成交净值(确认后填入) |
| `confirmed_shares` | 实际确认份额(确认后填入) |
| `confirmed_amount` | 赎回实际到账金额(确认后填入) |
| `reason` | **必填**,该笔操作的具体理由 |

#### 7.5 申购前校验

- 可用现金 ≥ 申购金额(**已到账的**,不含在途赎回资金)
- 申购金额 ≥ min_deposit(若有)
- 基金未限购或申购金额 ≤ 限额
- 基金申购状态 ≠ 暂停申购
- **考虑费率后的实际投入**: 净申购金额 = 申购金额 / (1 + 申购费率),确保投入金额符合预期

#### 7.6 赎回前校验

- **持有天数 ≥ 7 天**(绝对铁律,1.5% 惩罚费)
- 赎回份额 ≤ 持有份额
- 基金赎回状态 ≠ 暂停赎回
- **确认赎回费率**: 按实际持有天数查阶梯,费率过高时重新考虑是否值得赎回

#### 7.7 动作映射

| 中间动作 | 调仓动作 |
|---------|---------|
| `HOLD` | 无动作 |
| `ADD` | SUBSCRIBE |
| `REDUCE` | REDEEM(部分) |
| `TAKE_PROFIT` | REDEEM(部分:赎回 1/3) |
| `STOP_LOSS` | REDEEM(部分:赎回 1/2,或全部赎回视严重程度) |

#### 7.8 费率对调仓决策的影响

费率不只是成本,还会反过来影响决策:

| 场景 | 费率考量 |
|------|---------|
| 持有 8 天想赎回 | 赎回费 0.50%,成本偏高,非必要可再等几周(30天后降至 0.25%) |
| 持有 25 天想赎回 | 赎回费 0.50%,若非止损/止盈触发,考虑等到 30 天 |
| 想换基金 A → B | 赎回 A 的费 + 申购 B 的费 = 总摩擦成本,B 的预期超额是否覆盖这个成本? |
| A/C 类选择 | 短期调仓频率高的仓位用 C 类(免申购费),长期底仓用 A 类 |

### Step 8: 写回 DB + markdown（下单阶段）

**DB 是事实源，markdown 是人读视图。先写 DB 三张表，再 dump 同样内容到 markdown。**

#### 8.0 写 fund_bot_reviews（巡检结论）

```sql
INSERT INTO fund_bot_reviews
(bot_id, review_date, regime, decision, action_count, reason, review_md,
 cooldown_end, cash_before, cash_after, portfolio_value_before, portfolio_value_after,
 turnover_amount, turnover_ratio)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
```
- `decision` ∈ `KEEP / REBALANCE / SWITCH / STOP_LOSS_TRIGGERED`
- `review_md` 存与 `基金巡检记录.md` 同样的人读全文（DB 自包含）
- `cash_before/after` / `portfolio_value_before/after` 用 T 日净值估算
- `turnover_ratio = turnover_amount / portfolio_value_before`

拿到 `review_id`（last insert rowid）供后面两张表外键引用。

#### 8.1 写 fund_bot_actions（每只基金一行）

```sql
INSERT INTO fund_bot_actions
(review_id, bot_id, fund_code, action_type, trigger, timing_state, momentum_state,
 matrix_suggestion, final_decision, before_weight, after_weight, nav_used, amount, shares, fee, reason, action_date)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
```
- `action_type` ∈ `HOLD / ADD / REDUCE / TAKE_PROFIT / STOP_LOSS / SWITCH_OUT / SWITCH_IN`
- `trigger` ∈ `timing_matrix / anomaly / rebalance / momentum_reversal / cooldown`
- `timing_state` ∈ `大幅盈利 / 小幅盈利 / 微亏 / 大幅亏损 / 高点回撤`
- `momentum_state` ∈ `加速向上 / 稳步向上 / 停滞 / 减速但未转 / 已转弱`
- `matrix_suggestion` 是矩阵给的原始建议（HOLD/REDUCE/...），`final_decision` 是叠加修饰和人设偏移后的最终动作
- HOLD 动作也写一行（before_weight = after_weight，amount = 0），保留完整审计轨迹

#### 8.2 写 fund_bot_orders（仅对实际下单的动作）

```sql
INSERT INTO fund_bot_orders
(review_id, bot_id, fund_code, fund_name, order_type, order_date, confirm_date,
 order_amount, reference_nav, action_reason, status)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending');
```
- `order_type` ∈ `BUY / REDEEM`
- `confirm_date` = T+1 交易日（用于 Step 9 对账定位）
- `confirmed_*` 字段在下单阶段留空，Step 9 对账后回填
- 申购：`order_amount` 是金额；赎回：`order_amount` 仍是金额（用 reference_nav × shares 折算），方便统一计算换手率

#### 8.3 dump markdown（人读视图）

如果你是 bot，本阶段只负责写 markdown 视图；真正 DB 写入由 cron 后续调用 `fund_md_to_db.py` + admin MCP 完成。
系统落库后会再把 `review_md` / 持仓结果同步回 markdown，所以 markdown 更适合作为人读视图，而不是事实源。
调仓日志（每笔订单）单独 dump 到 `基金调仓日志.md`（追加，不覆盖）。
持仓快照（含在途订单）覆盖到 `当前基金持仓.md`。

---

#### 8a. 当前基金持仓 `memory/portfolio/fund/当前基金持仓.md`

根据调仓动作,更新:

- 账户概况(总资产、现金、持仓市值)
- 持仓明细(份额、净值、市值、权重)
- 大类配置分布(穿透后)
- **在途订单**(申购待确认 / 赎回待到账,含估算金额和预计确认日)
- **冻结现金**(已提交申购但未确认的金额,不可再次使用)

说明：这里的总资产、份额、净值、市值、权重如果由 bot 写入，只是“本轮决策时的人读快照”。系统落库时会以 DB 当前状态和 actions 重新计算；不要把这里当成最终真值回填来源。

```markdown
## 在途订单

| 下单日 | 动作 | 基金名称 | 代码 | 下单金额/份额 | 决策参考净值 | 估算份额/到账 | 预计确认 | 状态 |
|--------|------|---------|------|-------------|------------|-------------|---------|------|
| 04-23 | 申购 | xxx | 110011 | ¥10,000 | 1.5000 | ~6657份 | 04-24 | pending |
| 04-23 | 赎回 | yyy | 002611 | 5000份 | 1.2300 | ~¥6131 | 04-24 | pending |
```

#### 8b. 巡检记录 `memory/portfolio/fund/基金巡检记录.md`

```markdown
## YYYY-MM-DD

- **巡检类型**：常规巡检 / 初始化
- **结论**：KEEP / REBALANCE / SWITCH
- **本轮是否动作**：是 / 否
- **异常检测**：正常 / [列出异常基金和级别]
- **动作摘要**：
- **调仓金额**：¥XXX（换手率 X%）— 基于 T 日净值估算
- **预估手续费**：申购费 ¥XX + 赎回费 ¥XX
- **市场状态**：regime / regime_code / timing_stance
- **已有持仓动作标签**：逐只列出
- **新基金准入结论**：立即买 / 分批买 / 延后买 / 无新增
- **在途订单**：申购 ¥XXX 待确认 / 赎回 ¥XXX 预计 MM-DD 到账
- **触发原因**：
- **下次观察点**：
```

说明：这一段里涉及的调仓金额、手续费、权重等都属于人读摘要。真正落入 `fund_bot_reviews` / `fund_bot_actions` / `fund_bot_holdings` 的数值，由系统按 DB + actions 推演生成。

#### 8c. 调仓日志 `memory/portfolio/fund/基金调仓日志.md`

```markdown
## YYYY-MM-DD（下单）

| 动作 | 基金名称 | 代码 | 下单金额/份额 | 决策净值(T日) | 费率 | 估算手续费 | 估算份额/到账 | 预计确认 | 状态 | 原因 |
|------|---------|------|-------------|-------------|------|-----------|-------------|---------|------|------|
| 申购 | xxx | 110011 | ¥10,000 | 1.5000 | 0.15% | ¥14.99 | ~6657份 | 04-24 | pending | ... |
| 赎回 | yyy | 002611 | 5000份 | 1.2300 | 0.25% | ¥15.38 | ~¥6131 | 04-24 | pending | ... |
```

### Step 9: T+1 对账确认（DB 写入）

**T+1 日净值公布后,必须回来对账。** 这是基金交易区别于投顾的关键步骤。

#### 9.1 触发条件

```sql
SELECT * FROM fund_bot_orders
WHERE bot_id=? AND status='pending' AND confirm_date <= date('now');
```
当上面查询返回非空，且 fund_nav 中对应日期的净值已落库（Phase A 已跑），执行对账。

#### 9.2 对账流程（事务内一次完成）

```
1. 取每笔 pending 订单的 T+1 净值：
   SELECT nav FROM fund_nav WHERE fund_code=? AND nav_date=?

2. 申购单（order_type='BUY'）：
   - confirmed_shares = order_amount / confirm_nav
   - confirmed_amount = order_amount
   - 更新或插入 fund_bot_holdings 行（如已有则累计 shares + amount_invested + 重算 entry_nav 加权平均）
   - 扣减 fund_bot_accounts.cash

3. 赎回单（order_type='REDEEM'）：
   - confirmed_amount = order_amount × (confirm_nav / reference_nav) × (1 - 赎回费率)
   - 持有天数判定赎回费率（< 7 天禁止 / 7-30 天 0.5% / 30-365 天 0.25% / > 365 天 0.0%）
   - 减少或移除 fund_bot_holdings 行（部分减仓更新 shares + amount_invested；全部赎回 status='exited' + exit_date）
   - 增加 fund_bot_accounts.cash

4. UPDATE fund_bot_orders SET status='confirmed', confirm_nav=?, confirmed_shares=?, confirmed_amount=?, fee=?

5. dump markdown 视图（更新 当前基金持仓.md，追加 基金调仓日志.md 的 T+1 确认行）
```

#### 9.3 对账后更新调仓日志

```markdown
## YYYY-MM-DD（T+1 确认）

| 动作 | 基金名称 | 代码 | 成交净值(T+1) | 实际份额/到账 | 实际手续费 | 与估算偏差 |
|------|---------|------|-------------|-------------|-----------|-----------|
| 申购确认 | xxx | 110011 | 1.5200 | 6569.09份 | ¥14.99 | 份额少 1.3% |
| 赎回确认 | yyy | 002611 | 1.2100 | ¥6031.24 | ¥15.19 | 到账少 1.6% |
```

#### 9.4 持仓状态机

```
下单 → pending(在途) → confirmed(已确认) → 进入正式持仓

申购: 现金冻结 → pending → 份额到账,进入持仓
赎回: 份额冻结 → pending → 资金到账,进入可用现金
```

**在途期间的持仓计算**:
- 申购 pending 的金额: 从可用现金扣除,但尚未计入持仓市值
- 赎回 pending 的份额: 仍计入持仓(按最新净值估值),但不可再次赎回
- 总资产 = 已确认持仓市值 + 在途申购金额 + 在途赎回估值 + 可用现金

---

## 基金交易结算机制(核心)

### T+1 净值成交规则

基金交易**不是盘中实时成交**,与股票/投顾产品有本质区别:

```
T 日(决策日)                    T+1 日(成交日)
┌─────────────────────┐        ┌─────────────────────┐
│ bot 看到 T 日净值     │        │ 基金公司公布 T+1 净值 │
│ 基于 T 日数据做分析   │        │ 按 T+1 净值确认成交   │
│ 在 T 日 15:00 前下单  │  ───→  │ 份额 = 金额 / T+1 净值│
│ （15:00后算下一个交易日）│       │ 实际成交价≠决策时净值  │
└─────────────────────┘        └─────────────────────┘
```

**关键理解**: bot 在 T 日基于 T 日净值做出"加仓 A 基金 ¥10000"的决策,但这笔申购实际以 **T+1 日收盘后公布的净值** 成交。T+1 净值在下单时是**未知的**。

这意味着:

1. **决策净值 ≠ 成交净值**: 所有份额/金额计算在下单时只是**估算**,T+1 净值公布后才是实际值
2. **下单时间有截止点**: 15:00 前提交的申购/赎回按当日受理,15:00 后按下一个交易日受理
3. **不能精确控制成交价**: 与股票限价单不同,基金只能控制金额,不能控制净值
4. **需要 T+1 对账确认**: 成交后必须用实际净值重新计算份额和持仓

### 费率结构与计算

#### 申购费

```
净申购金额 = 申购金额 / (1 + 申购费率)
确认份额   = 净申购金额 / T+1 成交净值
申购手续费 = 申购金额 - 净申购金额
```

| 基金类型 | A 类申购费(原始) | 平台折扣后 | C 类申购费 |
|---------|----------------|-----------|-----------|
| 股票型/混合型 | 1.2%-1.5% | **0.12%-0.15%**(1折) | 0(免申购费) |
| 债券型 | 0.6%-0.8% | 0.06%-0.08% | 0 |
| 指数型 | 0.1%-1.2% | 0.01%-0.12% | 0 |
| 黄金ETF联接 | 见白名单 | — | 0 |
| 货币基金 | 0 | 0 | — |

> 主流互联网平台(天天基金/蚂蚁等)普遍申购费 1 折。选基时用 `fund_rate(fund_code)` 获取准确费率。

#### 赎回费(按持有天数阶梯)

```
赎回到账金额 = 赎回份额 × T+1 成交净值 × (1 - 赎回费率)
赎回手续费   = 赎回份额 × T+1 成交净值 × 赎回费率
```

| 持有天数 | 赎回费率 | 备注 |
|---------|---------|------|
| **< 7 天** | **1.50%** | **绝对禁止,任何情况不可触犯** |
| 7-30 天 | 0.50% | 短期赎回成本高,非必要不动 |
| 30-365 天 | 0.25% | 正常持有期 |
| 365 天-2 年 | 0.125% | — |
| ≥ 2 年 | 0 | 长期持有免赎回费 |

> 以上为常见阶梯,不同基金略有差异。实际费率以 `fund_rate(fund_code)` 为准。C 类基金通常 30 天后免赎回费,但收取年化 0.2%-0.4% 的销售服务费。

#### 持有成本(年化,每日从净值中扣除)

| 费种 | 典型范围 | 说明 |
|------|---------|------|
| 管理费 | 0.5%-1.5% | 主动型偏高,指数型偏低 |
| 托管费 | 0.1%-0.25% | — |
| 销售服务费 | 0-0.4% | 仅 C 类收取 |

持有成本不需要单独计算,已反映在每日净值中。但选基时**同等业绩选费率更低的**。

#### 费率对选基的影响

- **长期持有(>1年)选 A 类**: 申购费一次性 0.12%-0.15%,之后零费用
- **短期持有/定投选 C 类**: 免申购费,但每年 0.2%-0.4% 销售服务费
- **临界点约 1-2 年**: 持有超过此期限 A 类总成本更低

### 时效汇总

| 维度 | 普通基金 | QDII |
|------|---------|------|
| 申购受理截止 | T 日 15:00 | T 日 15:00 |
| 申购成交净值 | **T+1 日净值** | T+2~T+3 日净值 |
| 申购份额确认 | T+1 | T+2~T+3 |
| 赎回成交净值 | **T+1 日净值** | T+2~T+3 日净值 |
| 赎回资金到账 | T+1~T+3 | T+5~T+10 |
| 7天惩罚性赎回费 | 1.5%,**绝对禁止** | 同 |
| 分红方式 | 默认红利再投 | 同 |

**关键**: 赎回资金到账前不能计入可用现金。调仓计划中涉及"先赎后买"的,必须考虑资金到账延迟。

---

## 数据采集工具

### 日常巡检(每个交易日)

| 步骤 | 工具 | 用途 |
|------|------|------|
| 1 | `fund_basicinfo(fcodes="持仓列表", fields="FCODE,SHORTNAME,DWJZ,JZRQ,FUNDSCALE,SGZT,SHZT")` | 批量刷新净值、规模、申赎状态 |
| 2 | `get_fund_performance(fund_code)` | 检查业绩排名变化 |
| 3 | `get_fund_abnormal_movement(fund_code)` | 检测净值异动 |

### 月度检查

| 工具 | 用途 |
|------|------|
| `get_fund_style_analysis(fund_code)` | 检测风格漂移 |
| `get_fund_manager_info(fund_code)` | 检测经理变更 |
| `get_fund_industry_holding(fund_code)` | 检查行业暴露变化 |

### 工具注意事项

| 坑 | 说明 |
|---|------|
| `get_fund_industry_holding` 返回值 | 返回**基点**(3907.52 = 39.08%),需 ÷100 |
| `fund_invest` 参数名 | 是 `fcode` 不是 `fund_code` |
| `get_fund_comprehensive_analysis` | **首选工具**,一次返回全面数据 |
| 日期格式 | 必须 `YYYYMMDD`,不是 `YYYY-MM-DD` |
| 基金代码 | 6 位字符串,多只用逗号分隔无空格 |

---

## 硬约束

1. **流程顺序固定**: 在途对账 → 异常检测 → 目标组合(重新生成) → 当前持仓 → review → 写回
2. **T+1 成交,决策净值 ≠ 成交净值**: 下单时用 T 日净值估算,T+1 净值公布后必须对账确认,更新实际份额/金额
3. **在途订单优先处理**: 巡检开始前先检查是否有 pending 订单需要确认
4. **7天持有底线**: 持有<7天的基金绝对不可赎回,**任何情况不例外**
5. **先赎回后申购**: 赎回释放资金**到账后**才能申购新基金,不能用在途资金
6. **费率计入决策**: 换基时赎回费+申购费=总摩擦成本,新基金预期超额必须覆盖此成本
7. **持有天数影响赎回费**: 7-30 天 0.50%,30-365 天 0.25%,非必要避免在高费率期赎回
8. **每次重新选基**: 不复用旧的目标组合,每次巡检都覆盖写入
9. **不频繁折腾**: 轻微更优不等于立刻切换,更要考虑交易摩擦成本
10. **风险纪律优先**: 不为更高收益突破风险带
11. **证据不足时宁可不动**: 偏向保守
12. **写回必须可追踪**: 每次巡检留痕,动作留金额明细,下单和确认分开记录
13. **先处理老仓,再处理新仓**: 已有持仓动作和新基金准入不能混成一句话
14. **异常检测不可跳过**: 每次巡检必须先做异常检测
15. **QDII 考虑资金占用**: 赎回到账 T+5~T+10,调仓计划必须考虑
16. **黄金白名单（唯一持仓硬约束）**: 黄金类只买黄金 ETF 场外联接白名单（002610/002611/000216/000217/000218/008701/008702），新买入或换基替换都必须遵守
17. **单基金权重 / 行业集中度 / 最大回撤红线不再统一硬性规定**: 由 bot 在自己的 `基金投资策略摘要.md` 里按人设自主定义；巡检时 bot 用自己的阈值判断是否触发审查/止损

_通用基金组合巡检 skill,所有参与基金直投的 bot 共享。修改此文件会影响全部 bot。_
