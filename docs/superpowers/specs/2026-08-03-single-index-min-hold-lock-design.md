# 单指数 bot 最短持有硬锁 + 双轨解锁 设计

日期:2026-08-03
分支:feat/world-system
范围:仅单指数 bot(`botKindOf(botId) === 'single-fund'`,含 bot1~20 及深研 d 系列 bot5d/10d/16d/18d/20d;排除 bot105d 等 `bot1XX` 多指数 bot)

## 1. 背景与问题

现有「深研持仓承诺闸门」(commit `02e44e5`)只覆盖**深研日建的仓**:
`run.ts:1724` 处只有 `status.deepResearchFired && status.deepResearchBuys?.length` 才生成承诺,`min_holding_days = max(7, 赎回窗口)`(`run.ts:1656`),proxy `checkSellCommitment`(`run.ts:691`)在承诺期内、非深研日拒绝卖单。

两个缺口:

1. **普通日建仓不受保护**。非深研日买入不生成承诺,第二天因 belief/言行一致规则日间反复(`p_up=0.55` 中性偏多 ↔ 40% 底仓)想平就能平,照样交短期赎回费、翻烙饼。
2. **建仓前 agent 不知道会被锁**。承诺是买入**之后**由 world 侧解析 tool_trace 生成的。现有 `holdCommitmentBlock`(`message.ts:961`)虽每天对单指数渲染,但:讲的是「赎回费免赎档」而非硬锁口径 `max(7,窗口)`;零赎费基金直接空串(`message.ts:972`)却仍被锁 7 天——静默缺口。agent 若事先知道「买下去至少锁 7 天、窗内普通日跑不掉」,很可能就不会轻仓试。

目标:对单指数 bot,**任何买入都至少持有 `max(7, 赎回窗口)` 自然日**,硬锁(proxy 拒卖),并在**建仓前**把这条成本告知 agent,让它把锁定纳入建仓决策。

## 2. 需求决议(已与用户确认)

- **范围**:仅 `single-fund` bot。多指数 bot(bot105d 等 `bot1XX`)完全不动。`botKindOf` 已天然分对,无需改分类。
- **强度**:硬锁 + 风控例外。proxy 直接拒卖;唯一提前放行 = 系统硬风控(急跌 `target-move` / 账户回撤越线 `account-drawdown` / capitulation 强制深研)。
- **解锁语义:双轨**
  - 普通买入锁(`min_hold`):窗内只认「硬风控」+「到期」;主动深研**不**解锁。
  - 深研买入锁(`deep_research`):保留现有「新深研证伪即可平仓」;即窗内认「任何 `start_research`」+「硬风控」+「到期」。
- **告知时机**:因所有买入都上锁,告知改为**每个决策日**都出现(推翻早先「仅深研日」的答案——该答案基于「只有深研买入才锁」的旧前提,前提已变)。

## 3. 详细设计

### 3.1 承诺生成扩到所有买入(run.ts)

- 把 `feeWindowByFund` 的计算从 `if (status.deepResearchFired)` 块(`run.ts:1655`)提出来,对所有单指数 bot 的买入公用。
- 对每个 status:
  - `status.deepResearchFired` 为真 → 该日所有 `successfulBuys` 生成 `kind:'deep_research'` 承诺(原路径,thesis 照记)。
  - 否则(普通日)→ `successfulBuys` 生成 `kind:'min_hold'` 承诺(新路径),`thesis` 用一句固定文案(如「普通建仓最短持有承诺:持有至 X 日或硬风控放行」),`min_holding_days = max(7, feeWindow)`。
- 仅当 `botKindOf(status.bot) === 'single-fund'` 时生成 `min_hold` 承诺(`deep_research` 承诺维持现状,不额外限范围——多指数 bot 当前不启用该 gate 的写入路径,保持不变)。

### 3.2 kind 字段与 upsert 升级(deep-research-commitment.ts)

- `DeepResearchCommitment` 增 `kind: 'deep_research' | 'min_hold'`。
- `upsertDeepResearchCommitments` 增参 `kind`;合并规则:
  - `commit_until` 取更晚者(沿用现有 `run.ts:108` 逻辑)。
  - `kind` **只升不降**:已有活跃 `deep_research` 承诺时,`min_hold` 加仓不把它降级为 `min_hold`(深研标的的重研平仓通道保留);`min_hold` 承诺遇 `deep_research` 买入则升级为 `deep_research`。
- 旧承诺(无 `kind` 字段)读取时默认 `deep_research`,保证现有在跑 run 的重研解锁行为不回归。

### 3.3 双轨解锁(run.ts + fund-portfolio-proxy/server.ts)

- 每 bot 每天两个标志(替换现单一 `unlockedByBot`):
  - `unlockedByResearch[bot]`:当天 `start_research` notification 触发过(沿用 `run.ts:620` 钩子,仅改名)。
  - `unlockedByForcedRiskControl[bot]`:chat 前按 `drState.reasons.some(r => r === 'target-move' || r === 'account-drawdown')` 置位。
  - 两标志均每日 chat 前重置为 false(沿用 `run.ts:1469` 重置点)。
- `checkSellCommitment` 判定:
  ```
  const c = commitmentsByBot[bot]?.[fund]
  if (!isCommitmentActive(c, tradeDate)) return { allowed: true }          // 到期
  if (unlockedByForcedRiskControl[bot]) return { allowed: true }           // 硬风控:两种锁都放
  if (c.kind === 'deep_research' && unlockedByResearch[bot]) return { allowed: true } // 深研锁额外认重研
  return { allowed: false, message: <按 kind 措辞> }
  ```
- 硬风控强制深研时 `start_research` 也会触发 → 两标志同时为真,天然覆盖。主动深研(授权日自发,无硬风控)只置 `unlockedByResearch` → 只解 `deep_research` 锁,不解 `min_hold` 锁。

### 3.4 拒单消息按 kind 措辞(fund-portfolio-proxy/server.ts)

- `min_hold`:「基金 X 在最短持有承诺期(committed_on→commit_until),窗内普通日禁止卖出,仅系统硬风控(急跌/账户回撤越线)可提前放行;或等承诺到期。」
- `deep_research`:沿用现有措辞(重研可证伪)。

### 3.5 建仓前告知块升级(message.ts:holdCommitmentBlock)

- 保持「仅 `single-fund` 渲染」(`message.ts:962`)与「每天渲染」。
- 口径从「赎回费免赎档窗口」换成**硬锁口径 `max(7, 赎回窗口)`**,逐只列锁定天数。
- 拆掉 `message.ts:972` 的零赎费空串早退:**零赎费基金也列**(锁 7 天,无赎回费但仍不可卖)。
- 文案:「【建仓 = 最短持有承诺】今日买入/加仓 → 锁至第 N 自然日;窗内普通日交易代理直接拒卖(`blocked_by`),只有系统硬风控能提前放行。买之前想清楚能不能拿住——拿不住就别在今天建。」
- 深研可触发日(`deepResearchForced || deepResearchAuthorized`)**额外**补一句:「深研买入可在下个深研日重研明确证伪后平仓(普通买入不享此通道)。」需把 `deepResearchForced/Authorized` 传入(已在 ctx,顺手)。
- 保留现有「窗内持仓 · 今日若卖出的确切成本 + 思考闸」(`inWindowGateLines`),它讲的是赎回费,与硬锁并存不冲突。

## 4. 状态 / 文件 / 测试

**状态**:`state.ts` 的 `DeepResearchCommitment` 加 `kind`(可选,向后兼容,读时缺省 `deep_research`)。

**改动文件**:
- `world/src/deep-research-commitment.ts`:kind 字段 + upsert 升级(只升不降)+ 渲染兼容。
- `world/src/run.ts`:全买入建承诺、feeWindow 提取公用、双标志、proxy 分流回调。
- `world/src/message.ts`:`holdCommitmentBlock` 口径升级、零赎费覆盖、深研日补句。
- `world/src/fund-portfolio-proxy/server.ts`:拒单消息按 kind。
- `world/src/state.ts`:类型。

**测试**(node --test):
- `deep-research-commitment.test.ts`:kind 只升不降;旧承诺缺省 deep_research;min_hold upsert。
- `run.test.ts`:普通买入建 min_hold 锁;主动深研不解 min_hold、解 deep_research;硬风控解两种锁;零赎费基金也建锁。
- `message.test.ts`:holdCommitmentBlock 显示 max(7,窗口)、零赎费基金也列、深研日补句、非单指数返回空串。

## 5. 非目标(YAGNI)

- 不改多指数 bot 行为。
- 不改深研调度(forced/authorized/gap)逻辑本身,只读它的 reasons 判硬风控。
- 不改 soft「思考闸」审计逻辑(赎回费维度并存)。
- 不引入可配置的最短持有天数开关——固定 `max(7, 赎回窗口)`。

## 6. 风险

- **黑天鹅陷仓**:建仓第 2 天崩盘,靠硬风控(`target-move`/`account-drawdown` 强制深研)解锁。依赖崩盘/回撤触发器已启用(`config.crashTriggerEnabled` / `accountDrawdownTriggerEnabled`);若某 run 未启用触发器,则 min_hold 锁只能等到期——需在 run 配置层确认触发器开启。
- **向后兼容**:在跑 run 的 `state.json` 已有无 kind 的承诺;读取缺省 `deep_research` 保证不回归。
