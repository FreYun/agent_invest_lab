# 基金 5 份 MD frontmatter 完整 schema

> **权威来源 = MCP 工具 `fund-portfolio-mcp.get_fund_md_schema()`**（从 `fund_md_to_db.py` 的校验常量实时读出，
> 永远跟落库前的强校验同步）。bot 在写 5 份基金 MD **之前先调它**，照返回的 schema 写就不会被校验拒。
> 本文是人读版说明 + 快照（枚举值偶尔会变，以 MCP 返回的为准）。

## bot 的执行口径（一句话）

bot 只写 5 份 MD（YAML frontmatter + 正文）；落库（save_allocation_run / save_selection_run /
apply_fund_review_and_rebalance）由 cron 的 `fund_md_to_db.py` 调 admin MCP 完成；T+1 在途单收口
（settle_pending_fund_orders）和收益快照（record_fund_snapshot）也是 cron 干的。bot 能调的
fund-portfolio-mcp 工具只有两个只读的：`get_fund_md_schema`（拿 schema）和 `validate_fund_bot_md`（自检）。

## 数字口径（最常翻车）

| 字段 | 口径 | 例子 |
|------|------|------|
| `today_target` / `central_baseline` 里的 `equity_pct`/`bond_pct`/`gold_pct`/`cash_pct` | **0-100 百分数**，四个加起来 = 100 | `equity_pct: 75, bond_pct: 5, gold_pct: 0, cash_pct: 20` |
| 个性化基金选择.md `funds[*].target_weight` | **0~1 小数**，所有 funds 之和 ≤ 1.0（且 ≥ 0.5） | 占 25% 写 `0.25`，**不是 `25`** |
| 当前基金持仓.md `holdings[*].weight`（或 `actual_weight`） | **0~1 小数**，≈ `market_value / total_value`（偏差 >0.02 被拒） | 占 26.74% 写 `0.2674`，**不是 `26.74`** |

## 枚举字段（不是自由文字！）

| 字段 | 在哪份 MD | 白名单 |
|------|-----------|--------|
| `paradigm_active` | 全部 5 份 | A / B1 / B2 / C（= 本轮运行参数，不能自己改） |
| `layer2_pivot` | 投资框架.md | 大类资产 / 行业 / 单一行业 / 单基金（按 paradigm：A→大类资产，B1→行业，B2→单一行业，C→单基金） |
| `regime` | 市场环境判断.md | bull / range / bear / crisis（不许 `bull_trend`、`range_down`、`bull_pullback` 这种变体） |
| `timing_stance` | 市场环境判断.md | aggressively_add / add_on_pullback / hold / defensive / risk_off |
| `asset_class` | 个性化基金选择.md `funds[*]` | 股票类 / 债券类 / 黄金类 / 现金 |
| `role` | 个性化基金选择.md `funds[*]` | 核心底仓 / 卫星增强 / 对冲配置 / 防御缓冲 / 流动性储备（不是「核心持仓-半导体赛道」这种） |
| `decision` | 基金巡检记录.md | KEEP / REBALANCE / SWITCH |
| `action_type`（及 `matrix_suggestion`/`final_decision`） | 基金巡检记录.md `actions[*]` | HOLD / ADD / REDUCE / TAKE_PROFIT / STOP_LOSS（禁止 INIT/SELL/BUY/CASH） |

## 每份 MD 的字段清单

每份的 frontmatter 都必须有公共四字段：`step`（固定值，见下）、`run_id`、`trade_date`、`paradigm_active`（= 本轮运行参数）；frontmatter 后的正文 markdown 必须非空。

### ① 投资框架.md — `step: investment_framework`
必填：`layer2_pivot`（枚举，见上）+ 4 个非空文字简述 `layer1_capability_brief` / `layer2_structure_brief` / `layer3_signal_source` / `layer3_discipline_brief`（各一两句话即可）。

### ② 市场环境判断.md — `step: market_context`
必填：`regime`（枚举）、`timing_stance`（枚举）、`today_target`（dict，4 个 *_pct 键，0-100，和=100）、`central_baseline`（dict，同样 4 个 *_pct 键）；**B1/B2 范式必填 `focus_industries`（非空 list）**，A/C 可不写。可选：`regime_code`/`confidence`/`summary`。

### ③ 个性化基金选择.md — `step: fund_selection`
必填：`funnel`（dict，含 `layer1_count`/`layer2_count`/`layer3_count`/`layer4_count` 整数）、`funds`（非空 list）。每个 `funds[*]`：
- `fund_code`（字符串，**必须是核心池里 `fund_info.fund_type` 以「指数型」开头的基金**——主动型/混合型/股票型都会被拒）
- `fund_name`（字符串）
- `asset_class`（枚举：股票类/债券类/黄金类/现金）
- `role`（枚举：核心底仓/卫星增强/对冲配置/防御缓冲/流动性储备）
- `thesis`（字符串，选中逻辑；会被当作该基金持仓的 thesis）
- `performance`（dict，含 `1m`/`3m`/`6m`/`1y`/`3y` 五个周期，每个是 `{return_pct: 数字}`；系统会从 fund_info 补但最好自己填）
- `target_weight`（**0~1 小数**）

约束：所有 funds 的 `target_weight` 之和 ≤ 1.0 且 ≥ 0.5；股票类基金的 `target_weight` 之和×100 ≈ `today_target.equity_pct`（债券类↔bond_pct、黄金类↔gold_pct 同理；隐含现金 = 100−Σ target_weight×100 ≈ cash_pct；偏差 >10pp 被拒）。可选：`eliminated`（list）。

### ④ 当前基金持仓.md — `step: fund_holdings_snapshot`
必填：`account`（dict，含 `cash` 和 `total_value` 数字）、`holdings`（list）。每个 `holdings[*]`：`fund_code`（不能重复）、`market_value`（数字）、`weight` 或 `actual_weight`（**0~1 小数**，≈ `market_value/total_value`）。
约束：`account.total_value == account.cash + Σ holdings[*].market_value`（±1.0）。

### ⑤ 基金巡检记录.md — `step: fund_review`
必填：`decision`（枚举：KEEP/REBALANCE/SWITCH）、`actions`（list）。每个 `actions[*]`：`fund_code`、`action_type`（枚举）、`amount`（数字：ADD=要花的现金；REDUCE/TAKE_PROFIT/STOP_LOSS=要赎回的¥金额；HOLD=0）、`reason`（字符串）。
约束：`actions` 必须覆盖 DB 里**所有** active 持仓（不动的也写一条 HOLD）；ADD 的 `fund_code` 必须是指数型基金；非 ADD 的 `fund_code` 必须是当前持有的基金；`decision=KEEP` 时不允许出现非 HOLD 的非零 amount 动作，且 `turnover_amount`/`turnover_ratio` 必须为 0。
**可执行性（落库前 `validate_fund_bot_md` 会回放 actions 强校验）**：所有 `ADD` 的 `amount` 之和 ≤ 当前可用现金（= ④ `当前基金持仓.md` 的 `account.cash`）；`REDUCE`/`TAKE_PROFIT`/`STOP_LOSS` 的 `amount` ≤ 该基金当前持仓市值。透支/超卖整轮被拒——宁可少加仓也不要透支。先定 ② 的 `today_target` → 反推 ③ 的 `target_weight` → 据此给 ⑤ 的 `amount`。
可选/系统重算：`account_after`（含 `cash`/`portfolio_value`，应与持仓快照一致 ±0.2）、`cooldown_days`（默认7）、`review_md_section`、`turnover_amount`/`turnover_ratio`、每条 action 的 `before_weight`/`after_weight`/`nav_used`/`shares`/`fee`/`timing_state`/`momentum_state`/`matrix_suggestion`/`final_decision`/`trigger`（可留空，系统会重算覆盖）。

## 写完之后

调 `fund-portfolio-mcp.validate_fund_bot_md(bot_id, run_id, trade_date, paradigm_active)` 自检 → 返回
`{success, blocking_issues, warnings}`。`success=false` 就按 `blocking_issues` 改 MD，**反复改到 success=true 再结束**；
`warn:` 开头的不阻断（系统会自动处理，比如自动补齐 performance 周期）。
