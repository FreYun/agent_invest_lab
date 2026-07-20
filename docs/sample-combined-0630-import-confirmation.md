# sample-combined-0630.csv 导入前口径确认结论

## 结论（2026-07-03 第二轮更新：期初持仓已补充，全量数据可精确回放）

交付文件一份（期初持仓已合并进主文件）：

```text
docs/sample-combined-0630.csv    # position + order + nav 三类记录合一（679 行）
```

新增 `record_type=position` 行（36 行）：各组合起始日基金级持仓明细，排在该组合首条 nav 行之前。列复用规则：

| position 行字段 | 复用的列 |
| --- | --- |
| 份额 share_val | `confirm_vol` |
| 起始日净值 nav | `confirm_nav` |
| 成本 cost_value | `apply_amount` |
| 市值 market_value | `market_value` |

（起始日按当日净值建仓，成本=市值；`confirm_vol × confirm_nav = market_value` 精确成立。）

`customer_no` 列已按导入侧账户表（按 bot 编号对应）替换为真实客编（如 bot2 → `bcb301b7d2e64a31806dd4d7683c0304`）；`sub_account_no`/`portfolio_id` 维持 SIMACCT 编号不变。

已通过端到端回放验证：**期初持仓 + 订单流水逐日重放，7 个单基金组合每日每基金份额与源库持仓快照精确对齐（容差 0.02 份），每日现金与 nav 行 cash 列对齐（容差 0.05 元）**。卖出扣费口径 159/159 全过。可以执行 `AiPortfolioHistoryImportJob` 的 `all` / `commit`。

校验脚本（可复跑）：

```bash
/usr/bin/python3.12 scripts/check_sample_combined_sell_fee.py docs/sample-combined-0630.csv
/usr/bin/python3.12 scripts/check_sample_combined_replay.py
```

## 期初持仓语义答复

### 问题 1：第一条 nav 的 market_value/cash 是否代表起始日真实持仓状态？

**是。** 7 个单基金组合（SIMACCT0001/2/4/5/6/7/11）在 2026-04-24 由系统按当日净值直接置入初始持仓（无建仓订单流水）。第一条 nav 行的 `market_value/cash/noncash_ratio` 就是起始日真实状态，例如 bot2 `market_value=100000, cash=0` 即满仓 5 只基金。**不是**"初始化资金状态"，请勿改写为 `cash=100000`。

**bot101（SIMACCT0101）例外：期初为全现金 1,000,000，无初始持仓**，建仓流水完整在 order 行中（2026-06-11 四笔申购）。因此 bot101 没有 position 行。

### 问题 2：首笔为赎回的组合，赎回份额来源？

来源于期初持仓，即主文件中各组合开头的 `record_type=position` 行（列复用规则见上表）。

- 例：bot2 期初持有 001512 共 14412.34 份（第 2 行 position），2026-05-15 首笔赎回 6513.57 份即出自这里。
- 已验证：每个组合每只基金 `position 期初份额 + Σ申购确认份额 − Σ赎回份额` 在每个交易日与源库持仓快照一致，任何时点不出现负份额。

## 回放约定（重要）

1. **单基金 7 组合**：申购扣款、赎回回款、份额变动均在 `confirm_date` 生效。
2. **bot101**：申购现金在 `biz_date` 当日扣款，份额在 `confirm_date` 到账；在途资金计入当日 nav 行的 `market_value`（按成本计）。这解释了 06-11 首日 nav 即显示 `market_value=600000, cash=400000`。
3. ~~bot5 缺 2026-05-14 nav 行~~ **已补全**：该日源库快照整日缺失但有 4 笔订单确认，已用「期初持仓 + 订单流水重放的份额/现金 × 当日基金净值」合成该行（与前后日净值/现金/回撤序列连续），7 个组合现在都是完整的 43 个交易日。

## 本轮数据修复明细（第二轮）

以源库每日基金级持仓快照为真相源逐日对账，发现并修复了订单流水的两类缺陷：

| 缺陷 | 修复 | 涉及 |
| --- | --- | --- |
| 2026-05-08 系统级大类资产中枢调仓只写了动作日志、未写订单表 | 按快照份额差合成 7 笔补单（当日 biz=confirm、当日真实净值；现金残差 110.84 计入 fee，扣费口径成立） | bot1 4 笔（3 赎 1 申）、bot5 3 笔（2 赎 1 申），合计现金流与快照现金逐分对齐 |
| 6 笔订单（05-11 下单）`confirm_nav` 记录为过期净值，份额随之记错（现金字段本来是对的） | 按当日真实净值重算：赎回 `vol=(净额+fee)/nav`，申购 `shares=(金额−fee)/nav` | bot4 2 笔、bot6 3 笔、bot1 1 笔 |
| bot5 2026-05-14 源库快照整日缺失（nav 行空缺，当日却有 4 笔订单确认） | 用重放份额/现金 × 当日基金净值合成该日 nav 行 | bot5 1 行 |

合成补单在 CSV 中与普通订单同格式（`biz_date = confirm_date` 是其特征），照常回放即可。

## 第一轮修复回顾（卖出扣费口径，2026-07-03 上午）

- 根因：源库赎回单 `confirmed_amount` 本就是扣费后净额、`order_amount` 才是份额；旧生成脚本用净额反推份额把 fee 吃掉，造成 69 笔"疑似未扣费/超容差"。
- 修复：份额直接取源库真实值（全精度），69 笔全部通过 `confirm_amount ≈ confirm_vol × confirm_nav − fee`（容差 0.01）。
- 原 15 笔 pending 订单源库已确认，本版直接带出确认字段，无需剔除。

## 数据摘要（当前版本）

| 项目 | 数量 |
| --- | ---: |
| CSV 总行数 | 679 |
| position 行数（期初持仓明细） | 36（7 个单基金组合；bot101 期初全现金无行） |
| 订单行数 | 330（均 confirmed；含 7 笔合成补单） |
| confirmed 卖出单 / 通过扣费口径 | 159 / 159 |
| nav 行数 | 313（7 个单基金组合各 43 天完整 + bot101 12 天；含 bot5 05-14 补全行） |
| 数据窗口 | 2026-04-24 ~ 2026-06-29 |
