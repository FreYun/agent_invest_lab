# 真实用户曲线叠加到回测看板 — 设计文档

日期：2026-05-25
状态：待评审

## 1. 背景与目标

回测看板（`world/src/backtest-dashboard`，`http://localhost:18888/backtest-dashboard/`）
当前每个 bot 的详情图上有两条曲线：bot 组合净值、基准基金净值（买入持有）。

目标：把**同一只基金上的真实用户**的收益走势叠加到同一张图上，直观比较
「agent 和真实用户在同一标的上孰强孰弱」。

数据来源：`Result_7.csv`（38439 行，421 只基金，3068 个客户，3788 个用户周期），
由 `user_cycle_return_top10_per_fund.sql` 从仓库取出（每只基金最多 ~10 个用户）。

## 2. 已确认的关键决策

| 决策点 | 选择 |
|--------|------|
| 比较载体 | 叠加到现有 per-bot 详情图（已有 bot + 基准两条曲线） |
| 用户匹配 | **精确同基金**：bot 持有/交易过的 fund_code，取该 code 的真实用户 |
| 展示上限 | 每个 bot 最多 **10 个用户**，在 bot 的多只基金间平均分配（1 只→10，2 只→5+5，余数给候选多的池子） |
| 用户呈现 | **重建的资金加权（money-weighted）收益曲线**，细线 |
| 用户选取 | **代表性分布**：按 `clear_return2` 取最差 / 最好 / 中位 + p25/p75 填充 |
| 数据落位 | 原始 CSV 放 `data/user/`，落库到 `fund.db` 新表 |
| 终点处理 | **终点锁定到 `ClearReturn2`**（整条曲线统一缩放，幅度用权威值） |

## 3. 数据模型

CSV 列：`#,FundCode,Customerno,id,StartDate,EndDate,ClearReturn2,BigLossRate,BigProfitRate,
C_BUSINTYPE,C_BUSINNAME,C_CFMAMOUNT,C_CFMVOL,C_TRANSACTIONDATE`

- 周期级字段（同一 `id` 内恒定）：FundCode, Customerno, id, StartDate, EndDate,
  ClearReturn2, BigLossRate, BigProfitRate
- 交易级字段：C_BUSINTYPE, C_BUSINNAME, C_CFMAMOUNT(金额), C_CFMVOL(份额), C_TRANSACTIONDATE
- `id` 本身已含「客户号+基金代码+序号」，全局唯一，用作 `cycle_id`。

`fund.db` 新增 3 张表：

```sql
-- 用户周期（每个用户在一只基金上的一段持有）
CREATE TABLE real_user_cycles (
  cycle_id        TEXT PRIMARY KEY,   -- = CSV.id
  fund_code       TEXT NOT NULL,
  customerno      TEXT,
  start_date      TEXT NOT NULL,
  end_date        TEXT NOT NULL,
  clear_return2   REAL,               -- 权威最终收益
  big_loss_rate   REAL,
  big_profit_rate REAL
);
CREATE INDEX idx_ruc_fund ON real_user_cycles(fund_code);

-- 交易明细
CREATE TABLE real_user_txns (
  cycle_id    TEXT NOT NULL,
  fund_code   TEXT NOT NULL,
  busin_type  TEXT,
  busin_name  TEXT NOT NULL,
  amount      REAL,                   -- C_CFMAMOUNT 现金
  vol         REAL,                   -- C_CFMVOL 份额（恒正，方向由 busin_name 决定）
  txn_date    TEXT NOT NULL
);
CREATE INDEX idx_rut_cycle ON real_user_txns(cycle_id);

-- 预计算的净值路径（终点已锁定到 clear_return2）
CREATE TABLE real_user_curves (
  cycle_id    TEXT NOT NULL,
  trade_date  TEXT NOT NULL,
  net_value   REAL NOT NULL,          -- = 1 + roi(t)
  PRIMARY KEY (cycle_id, trade_date)
);
```

## 4. 数据落库（`scripts/ingest_user_data.py`，Python，带测试）

行为：
1. 读取 `data/user/Result_7.csv`。
2. 重建 `real_user_cycles`、`real_user_txns`。
3. 计算并写入 `real_user_curves`（见 §5）。
4. 幂等：每次运行先 `DROP` 再重建这 3 张表（不动 `fund.db` 其它表）。

把曲线重建放在 Python 落库阶段（而非 TS 服务端按需算）的理由：
- 数学逻辑复杂，Python 便于单测；TS 服务端保持只做查询，薄而稳。
- 这些历史窗口的 NAV 已稳定，预计算不会过期。
- 量级可控：3788 周期 × ~300 天 ≈ 110 万行，sqlite 无压力。

交易类型 → 份额方向映射：

| busin_name | 处理 |
|------------|------|
| 申购确认 / 定时定额投资确认 | 买入：shares += vol，invested += amount |
| 赎回确认 / 强行赎回 | 卖出：shares −= vol，proceeds += amount |
| 转入投资账户 / 转托管入确认 / 份额转卡转入 | 仅份额 +vol，无外部现金流 |
| 转出投资账户 / 份额转卡转出 | 仅份额 −vol，无外部现金流 |
| 设置分红方式确认 | 跳过（amount/vol = 0） |
| 转换确认 / 转托管确认 | 方向不明、量少，v1 跳过（记录到日志） |

## 5. 曲线重建方法（资金加权）

对每个 `cycle_id`，沿其基金 `fund_nav` 在 `[start_date, end_date]` 的每个交易日 t：

```
shares(t)   = 截至 t 的累计带符号份额
mv(t)       = shares(t) × nav(t)
invested(t) = 截至 t 的累计买入现金
proceeds(t) = 截至 t 的累计卖出现金
roi(t)      = (mv(t) + proceeds(t) − invested(t)) / invested(t)      # invested(t)<=0 时记 0
net_value(t)= 1 + roi(t)
```

**为什么用资金加权而不是时间加权**：单基金持有者的时间加权收益（TWR）等于该基金
NAV 的涨跌路径，会让同一只基金的所有用户曲线重合，抹掉择时差异。资金加权能体现
用户实际的买卖择时（定投会拉低/抬高成本基准），这正是「孰强孰弱」要看的东西。

**终点锁定**（已确认）：设重建终点 `roi_end = roi(end_date)`，权威值 `c = clear_return2`。
统一缩放因子 `f = c / roi_end`，令 `roi_adj(t) = roi(t) × f`，`net_value(t) = 1 + roi_adj(t)`。
保形、终点精确落在 `1 + c`。

守护（避免缩放爆炸/翻号）：当 `|roi_end| < ε`（如 1e-4）或 `sign(roi_end) ≠ sign(c)` 时，
不做乘性缩放，改用加性平移 `roi_adj(t) = roi(t) + (c − roi_end)`，并在日志标记该 cycle。

## 6. 用户选取（`server.ts`，serve 时按 bot 计算）

1. 取 bot 的基金集合 = position/action 里出现的 `fund_code`。
2. 仅保留在 `real_user_cycles` 里有用户的基金，作为候选池。
3. 配额：总上限 10，在候选池之间平均分（`floor(10/n)`，余数给候选数更多的池子）。
4. 池内**代表性分布**：按 `clear_return2` 升序，依次取
   最差、最好、中位，再用 p25、p75 …… 按需补足到该池配额，去重。

## 7. 服务端与前端改动

**`world/src/backtest-dashboard/server.ts`**
- `BotDataset` 增加：
  ```ts
  realUsers: {
    fundCode: string
    fundName: string
    cycleId: string
    clearReturn2: number
    bigLossRate: number | null
    bigProfitRate: number | null
    txnCount: number
    series: { trade_date: string; net_value: number }[]
  }[]
  ```
- 在 `loadBotForRun` 里：选用户（§6）→ 从 `real_user_curves` 取曲线 → 填充 `realUsers`。
- 仅在 per-bot 详情接口（`/api/backtest/bot`）返回；列表轮询接口
  `/api/backtest/data` 不带（与 `holdingsByDate` 一样剥离，控制体积）。

**`world/src/backtest-dashboard/index.html`**
- 每个用户一条**细、半透明**曲线（区别于 bot 粗线、基准虚线），按基金分组配色。
- 图例分组按基金；hover 显示：基金 / 持有窗口 / `clear_return2` / 交易笔数。
- 同一日历 x 轴（用户窗口落在 bot 窗口内，§ 已验证 98.6% EndDate ≤ bot 末日）。

## 8. 测试

- Python 单测 `scripts/test_ingest_user_data.py`：
  - 份额方向映射（各 busin_name）。
  - 重建场景：纯定投、定投+部分赎回、全额赎回、含转入转出。
  - 终点锁定：乘性缩放后 `net_value(end) == 1 + clear_return2`；守护分支（roi_end≈0 / 异号）走加性平移。
  - 选取：单基金取 10、双基金 5+5、代表性分布覆盖最差/最好/中位。
- 既有 `world/test/backtest-dashboard.test.ts` 增补：`realUsers` 字段结构与配额上限。

## 9. 涉及文件清单

- 新增 `data/user/Result_7.csv`（原始数据，从仓库根目录移入）
- 新增 `scripts/ingest_user_data.py`、`scripts/test_ingest_user_data.py`
- 改 `world/src/backtest-dashboard/server.ts`（类型 + 选取 + 查询）
- 改 `world/src/backtest-dashboard/index.html`（渲染）
- 改 `world/test/backtest-dashboard.test.ts`（断言）

## 10. 不在本次范围

- 按「指数」聚合匹配（已选精确同基金；指数分类器留待将来）。
- 现金分红 / 申赎手续费的精确建模（用终点锁定吸收这部分偏差）。
- 转换确认 / 转托管确认 的精确方向还原。
- 真实用户与 bot 的统计显著性检验、胜率汇总面板。
