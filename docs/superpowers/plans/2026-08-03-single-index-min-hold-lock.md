# 单指数 bot 最短持有硬锁 + 双轨解锁 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让单指数 bot 的每一笔买入都至少持有 `max(7, 赎回窗口)` 自然日,proxy 硬拦窗内卖单;普通买入锁只认硬风控 + 到期,深研买入锁额外认重研;并在建仓前每日把锁定成本告知 agent。

**Architecture:** 复用现有「深研持仓承诺闸门」(commit `02e44e5`)。给承诺加 `kind`(`deep_research` | `min_hold`)区分两轨;承诺生成从「仅深研日买入」扩到「所有单指数买入」;卖出闸门按 kind 双轨判定(硬风控解两轨、重研只解深研轨);建仓前告知块 `holdCommitmentBlock` 升级为硬锁口径并覆盖零赎费基金。核心逻辑抽成纯函数单测,再在 `runLoop` 里接线。

**Tech Stack:** TypeScript,Node >= 22.6(`--experimental-strip-types` 直跑 .ts),`node --test` 内置测试框架,`node:assert/strict`。

## Global Constraints

- 全程中文注释/文案(代码标识符除外)。
- 范围**仅** `botKindOf(botId) === 'single-fund'`;多指数 bot(`bot1XX`,含 bot105d)行为不变——`min_hold` 承诺只对单指数生成,`deep_research` 承诺路径对所有 bot 保持现状。
- 最短持有天数固定 `max(7, 赎回费窗口)`,不引入可配置开关。
- 向后兼容:在跑 run 的 `state.json` 已有无 `kind` 的承诺,读取时缺省视作 `deep_research`,不得改变现有 run 的重研解锁行为。
- 单文件跑测:`cd world && node --experimental-strip-types --test test/<file>.test.ts`;全量:`cd world && npm test`。
- 类型检查:`cd world && npm run check`。
- 每个 Task 结束提交一次;提交信息中文,结尾带 `Co-Authored-By: claude-opus-4-8 <noreply@anthropic.com>`。

## 文件结构

- `world/src/deep-research-commitment.ts` — 承诺数据结构与纯逻辑:加 `kind` 字段、`upsert` 支持 kind(只升不降)、`decideSellCommitment` 双轨判定、`minHoldingDaysFor` / `feeWindowByFundFrom` 辅助。**新逻辑集中在这里,便于单测。**
- `world/src/run.ts` — 接线:`planCommitmentUpsert`(定哪些买入建承诺、什么 kind)、双标志 `unlockedByResearch` / `unlockedByForcedRiskControl`、proxy 回调改用 `decideSellCommitment`、`runLoop` 内按 status 建承诺。
- `world/src/message.ts` — `holdCommitmentBlock` 升级:硬锁口径 + 零赎费覆盖 + 深研日补句 + 新增 `deepResearchEligible` 参数。
- `world/src/fund-portfolio-proxy/server.ts` — 不改逻辑(`SellCommitmentGateDecision` 已导出,`blocked_by` 措辞沿用);仅确认 `decideSellCommitment` 返回的 message 被透传。
- `world/src/state.ts` — 无需改(`DeepResearchCommitment` 类型定义在 `deep-research-commitment.ts`,state.ts 仅 import)。

---

## Task 1: 承诺加 kind 字段 + upsert 只升不降

给 `DeepResearchCommitment` 加 `kind`,让 `upsertDeepResearchCommitments` 接受 kind 参数,并实现「活跃 deep_research 承诺不被 min_hold 加仓降级、min_hold 遇 deep_research 升级」。

**Files:**
- Modify: `world/src/deep-research-commitment.ts`(interface `DeepResearchCommitment` 第 1-9 行;`upsertDeepResearchCommitments` 第 91-116 行)
- Test: `world/test/deep-research-commitment.test.ts`

**Interfaces:**
- Produces:
  - `DeepResearchCommitment.kind?: 'deep_research' | 'min_hold'`(可选,兼容旧数据;读取缺省 `'deep_research'`)
  - `upsertDeepResearchCommitments(current, botId, tradeDate, buys, kind: 'deep_research' | 'min_hold'): DeepResearchCommitmentsByBot`(新增末位参数 `kind`)

- [ ] **Step 1: 写失败测试**

在 `world/test/deep-research-commitment.test.ts` 末尾追加:

```typescript
test('kind 只升不降：min_hold 加仓不降级活跃 deep_research；min_hold 遇 deep_research 升级', () => {
  // deep_research 建仓 → 之后 min_hold 加仓：kind 保持 deep_research，commit_until 取更晚
  let s = upsertDeepResearchCommitments({}, 'bot16d', '2025-10-21', [{ fundCode: '008591', reason: '深研', minHoldingDays: 7 }], 'deep_research')
  assert.equal(s.bot16d['008591'].kind, 'deep_research')
  assert.equal(s.bot16d['008591'].commit_until, '2025-10-28')
  s = upsertDeepResearchCommitments(s, 'bot16d', '2025-10-22', [{ fundCode: '008591', reason: '普通加仓', minHoldingDays: 7 }], 'min_hold')
  assert.equal(s.bot16d['008591'].kind, 'deep_research', '活跃 deep_research 不被降级')
  assert.equal(s.bot16d['008591'].commit_until, '2025-10-29', 'commit_until 延长到 min_hold 到期')

  // min_hold 建仓 → deep_research 加仓：升级为 deep_research
  let t = upsertDeepResearchCommitments({}, 'bot5d', '2025-10-21', [{ fundCode: '510300', reason: '普通', minHoldingDays: 7 }], 'min_hold')
  assert.equal(t.bot5d['510300'].kind, 'min_hold')
  t = upsertDeepResearchCommitments(t, 'bot5d', '2025-10-22', [{ fundCode: '510300', reason: '深研', minHoldingDays: 7 }], 'deep_research')
  assert.equal(t.bot5d['510300'].kind, 'deep_research', 'min_hold 被深研买入升级')
})
```

同时修正同文件已有测试 `深研成功买入形成承诺...` 第 26、28 行的 `upsertDeepResearchCommitments(...)` 调用,给它们补第 5 个实参 `'deep_research'`:

```typescript
  let state = upsertDeepResearchCommitments({}, 'bot16d', '2025-10-21', actions.buys.map(b => ({ ...b, minHoldingDays: 7 })), 'deep_research')
  // ...
  state = upsertDeepResearchCommitments(state, 'bot16d', '2025-10-22', [{ ...actions.buys[0], minHoldingDays: 3 }], 'deep_research')
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/deep-research-commitment.test.ts`
Expected: FAIL — `upsertDeepResearchCommitments` 第 5 参未定义 / `kind` 属性 undefined。

- [ ] **Step 3: 改 interface**

在 `world/src/deep-research-commitment.ts` 第 1-9 行的 `DeepResearchCommitment` 里,`thesis` 之后加一行:

```typescript
export interface DeepResearchCommitment {
  bot_id: string
  fund_code: string
  committed_on: string
  commit_until: string
  min_holding_days: number
  thesis: string
  /** 承诺类型：deep_research=深研日建仓（重研可证伪平仓）；min_hold=普通建仓（仅硬风控/到期解锁）。
   *  可选：老 run 的 state.json 无此字段，读取时缺省视作 deep_research。 */
  kind?: 'deep_research' | 'min_hold'
  source_amount?: number
}
```

- [ ] **Step 4: 改 upsert 支持 kind 只升不降**

把 `world/src/deep-research-commitment.ts` 第 91-116 行的 `upsertDeepResearchCommitments` 整体替换为:

```typescript
export function upsertDeepResearchCommitments(
  current: DeepResearchCommitmentsByBot | undefined,
  botId: string,
  tradeDate: string,
  buys: Array<SuccessfulFundAction & { minHoldingDays: number }>,
  kind: 'deep_research' | 'min_hold',
): DeepResearchCommitmentsByBot {
  const next: DeepResearchCommitmentsByBot = structuredClone(current ?? {})
  const byFund = { ...(next[botId] ?? {}) }
  for (const buy of buys) {
    const minHoldingDays = Math.max(1, Math.floor(buy.minHoldingDays))
    const existing = byFund[buy.fundCode]
    const candidateUntil = addCalendarDays(tradeDate, minHoldingDays)
    // 同一基金已有尚未到期的承诺时，加仓只能延长、不能缩短承诺。
    const commitUntil = existing && existing.commit_until > candidateUntil ? existing.commit_until : candidateUntil
    // kind 只升不降：已有活跃 deep_research 承诺时，min_hold 加仓不把它降级为 min_hold
    // （深研标的的重研平仓通道要保留）；min_hold 承诺遇 deep_research 买入则升级。
    const existingActiveDeep = Boolean(existing && (existing.kind ?? 'deep_research') === 'deep_research' && isCommitmentActive(existing, tradeDate))
    const resolvedKind: 'deep_research' | 'min_hold' = kind === 'deep_research' || existingActiveDeep ? 'deep_research' : 'min_hold'
    byFund[buy.fundCode] = {
      bot_id: botId,
      fund_code: buy.fundCode,
      committed_on: tradeDate,
      commit_until: commitUntil,
      min_holding_days: minHoldingDays,
      kind: resolvedKind,
      thesis: buy.reason || existing?.thesis || (resolvedKind === 'deep_research'
        ? '深研日建仓；持有至承诺到期或下一次深研明确证伪。'
        : '普通建仓最短持有承诺；持有至承诺到期或系统硬风控放行。'),
      ...(buy.amount !== undefined ? { source_amount: buy.amount } : {}),
    }
  }
  if (Object.keys(byFund).length) next[botId] = byFund
  return next
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/deep-research-commitment.test.ts`
Expected: PASS(含新用例与已修正的旧用例)。

- [ ] **Step 6: 提交**

```bash
cd /home/rooot/agent_invest_lab && git add world/src/deep-research-commitment.ts world/test/deep-research-commitment.test.ts && git commit -m "$(cat <<'EOF'
feat(world): 承诺加 kind 字段,upsert 支持双轨且只升不降

Co-Authored-By: claude-opus-4-8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 双轨卖出判定 decideSellCommitment(纯函数)

把卖出闸门决策抽成纯函数,实现双轨:硬风控解两轨、重研只解深研轨、min_hold 只认硬风控 + 到期。

**Files:**
- Modify: `world/src/deep-research-commitment.ts`(新增导出函数;文件已有 `isCommitmentActive`)
- Test: `world/test/deep-research-commitment.test.ts`

**Interfaces:**
- Consumes: `DeepResearchCommitment`(Task 1 的 `kind`);`isCommitmentActive`(已有)
- Produces:
  - `interface SellCommitmentDecision { allowed: boolean; message?: string }`
  - `decideSellCommitment(input: { commitment: DeepResearchCommitment | undefined; tradeDate: string; unlockedByResearch: boolean; unlockedByForcedRiskControl: boolean }): SellCommitmentDecision`

- [ ] **Step 1: 写失败测试**

在 `world/test/deep-research-commitment.test.ts` 顶部 import 补上 `decideSellCommitment`:

```typescript
import {
  activeCommitmentsForBot,
  decideSellCommitment,
  extractSuccessfulFundActions,
  renderDeepResearchCommitmentBlock,
  upsertDeepResearchCommitments,
} from '../src/deep-research-commitment.ts'
```

末尾追加:

```typescript
test('decideSellCommitment 双轨：min_hold 只认硬风控/到期；deep_research 额外认重研', () => {
  const mk = (kind: 'deep_research' | 'min_hold') => ({
    bot_id: 'bot5d', fund_code: '510300', committed_on: '2025-10-21',
    commit_until: '2025-10-28', min_holding_days: 7, kind,
    thesis: 't',
  })
  const day = '2025-10-23' // 窗内

  // 到期：无条件放行
  assert.equal(decideSellCommitment({ commitment: mk('min_hold'), tradeDate: '2025-10-28', unlockedByResearch: false, unlockedByForcedRiskControl: false }).allowed, true)

  // min_hold 窗内、无解锁 → 拒
  const blocked = decideSellCommitment({ commitment: mk('min_hold'), tradeDate: day, unlockedByResearch: false, unlockedByForcedRiskControl: false })
  assert.equal(blocked.allowed, false)
  assert.match(blocked.message!, /最短持有承诺/)

  // min_hold 窗内、主动深研(unlockedByResearch) → 仍拒(不解普通锁)
  assert.equal(decideSellCommitment({ commitment: mk('min_hold'), tradeDate: day, unlockedByResearch: true, unlockedByForcedRiskControl: false }).allowed, false)

  // min_hold 窗内、硬风控 → 放行
  assert.equal(decideSellCommitment({ commitment: mk('min_hold'), tradeDate: day, unlockedByResearch: false, unlockedByForcedRiskControl: true }).allowed, true)

  // deep_research 窗内、主动深研 → 放行(重研可证伪)
  assert.equal(decideSellCommitment({ commitment: mk('deep_research'), tradeDate: day, unlockedByResearch: true, unlockedByForcedRiskControl: false }).allowed, true)

  // deep_research 窗内、无解锁 → 拒
  const dr = decideSellCommitment({ commitment: mk('deep_research'), tradeDate: day, unlockedByResearch: false, unlockedByForcedRiskControl: false })
  assert.equal(dr.allowed, false)
  assert.match(dr.message!, /深研持仓承诺期/)

  // 旧承诺无 kind → 缺省 deep_research：主动深研可解锁
  const legacy = { bot_id: 'bot5d', fund_code: '510300', committed_on: '2025-10-21', commit_until: '2025-10-28', min_holding_days: 7, thesis: 't' } as any
  assert.equal(decideSellCommitment({ commitment: legacy, tradeDate: day, unlockedByResearch: true, unlockedByForcedRiskControl: false }).allowed, true)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/deep-research-commitment.test.ts`
Expected: FAIL — `decideSellCommitment` 未导出。

- [ ] **Step 3: 实现 decideSellCommitment**

在 `world/src/deep-research-commitment.ts` 的 `isCommitmentActive`(第 87-89 行)之后新增:

```typescript
export interface SellCommitmentDecision {
  allowed: boolean
  message?: string
}

/** 卖出闸门双轨判定（纯函数）：
 *   到期 → 放行；硬风控 → 两轨都放；重研（unlockedByResearch）只放 deep_research 轨。
 *   min_hold 轨窗内只认硬风控 + 到期，主动深研不解锁。 */
export function decideSellCommitment(input: {
  commitment: DeepResearchCommitment | undefined
  tradeDate: string
  unlockedByResearch: boolean
  unlockedByForcedRiskControl: boolean
}): SellCommitmentDecision {
  const { commitment, tradeDate, unlockedByResearch, unlockedByForcedRiskControl } = input
  if (!isCommitmentActive(commitment, tradeDate)) return { allowed: true }
  if (unlockedByForcedRiskControl) return { allowed: true }
  const kind = commitment!.kind ?? 'deep_research'
  if (kind === 'deep_research' && unlockedByResearch) return { allowed: true }
  const window = commitment!.committed_on + '→' + commitment!.commit_until
  const message = kind === 'min_hold'
    ? '基金 ' + commitment!.fund_code + ' 在最短持有承诺期（' + window + '）。窗内普通交易日禁止卖出，仅系统硬风控（急跌/账户回撤越线）可提前放行，或等承诺到期。'
    : '基金 ' + commitment!.fund_code + ' 仍在深研持仓承诺期（' + window + '）。今天不是深度研究日，普通指标走弱不能推翻深研建仓结论；等待承诺到期，或由系统风险事件触发强制深研后重新判断。'
  return { allowed: false, message }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/deep-research-commitment.test.ts`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab && git add world/src/deep-research-commitment.ts world/test/deep-research-commitment.test.ts && git commit -m "$(cat <<'EOF'
feat(world): 抽出 decideSellCommitment 双轨卖出判定纯函数

Co-Authored-By: claude-opus-4-8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 承诺生成计划 planCommitmentUpsert(纯函数)

抽出「哪些买入建承诺、建什么 kind、最短持有几天」的纯逻辑:深研日 → deep_research(所有 bot);普通日 + 单指数 → min_hold;普通日 + 多指数 → 不建。

**Files:**
- Modify: `world/src/run.ts`(新增导出函数;import `botKindOf`)
- Test: `world/test/run.test.ts`

**Interfaces:**
- Consumes: `SuccessfulFundAction`(已在 run.ts import,来自 deep-research-commitment.ts);`botKindOf`(来自 message.ts)
- Produces:
  - `minHoldingDaysFor(feeWindowDays: number | undefined): number` → `Math.max(7, feeWindowDays ?? 0)`
  - `feeWindowByFundFrom(fundFees): Map<string, number>` → 每基金赎回费窗口天数(无费=0)
  - `planCommitmentUpsert(input: { botId: string; deepResearchFired: boolean; successfulBuys: SuccessfulFundAction[] | undefined; feeWindowByFund: Map<string, number> }): { kind: 'deep_research' | 'min_hold'; buys: Array<SuccessfulFundAction & { minHoldingDays: number }> } | null`

- [ ] **Step 1: 写失败测试**

在 `world/test/run.test.ts` 顶部第 8 行的 import 里追加 `planCommitmentUpsert, minHoldingDaysFor, feeWindowByFundFrom`:

```typescript
import { runWorld, requestPause, requestStop, botServerArgv, openclawJsonSource, loopConfigPath, patchPiOpenclawJsonMemory, seedPiAgentBot, isResearchDay, isChatDayAt, previousChatCursor, isoWeekKey, proxyEnvSupplement, recordedRunDays, planCommitmentUpsert, minHoldingDaysFor, feeWindowByFundFrom } from '../src/run.ts'
```

在文件末尾追加:

```typescript
test('minHoldingDaysFor：至少 7，赎回窗口更长则取窗口', () => {
  assert.equal(minHoldingDaysFor(undefined), 7)
  assert.equal(minHoldingDaysFor(0), 7)
  assert.equal(minHoldingDaysFor(5), 7)
  assert.equal(minHoldingDaysFor(30), 30)
})

test('feeWindowByFundFrom：取有惩罚档的最大 max_days', () => {
  const m = feeWindowByFundFrom([
    { fund_code: '510300', fund_name: 'A', redeem_tiers: [{ max_days: 7, rate_pct: 1.5 }, { max_days: null, rate_pct: 0 }] },
    { fund_code: '008591', fund_name: 'B', redeem_tiers: [{ max_days: null, rate_pct: 0 }] }, // 零赎费
  ] as any)
  assert.equal(m.get('510300'), 7)
  assert.equal(m.get('008591'), 0)
})

test('planCommitmentUpsert：深研日→deep_research(所有bot)；普通日单指数→min_hold；普通日多指数→null', () => {
  const fw = new Map([['510300', 30], ['008591', 0]])
  const buys = [{ fundCode: '510300', reason: 'x' }, { fundCode: '008591', reason: 'y' }]

  // 深研日，单指数
  const a = planCommitmentUpsert({ botId: 'bot5d', deepResearchFired: true, successfulBuys: buys, feeWindowByFund: fw })!
  assert.equal(a.kind, 'deep_research')
  assert.equal(a.buys.find(b => b.fundCode === '510300')!.minHoldingDays, 30)
  assert.equal(a.buys.find(b => b.fundCode === '008591')!.minHoldingDays, 7)

  // 普通日，单指数
  const b = planCommitmentUpsert({ botId: 'bot5d', deepResearchFired: false, successfulBuys: buys, feeWindowByFund: fw })!
  assert.equal(b.kind, 'min_hold')
  assert.equal(b.buys.length, 2)

  // 深研日，多指数 → 仍 deep_research(现状不变)
  const c = planCommitmentUpsert({ botId: 'bot105d', deepResearchFired: true, successfulBuys: buys, feeWindowByFund: fw })!
  assert.equal(c.kind, 'deep_research')

  // 普通日，多指数 → null(现状不变)
  assert.equal(planCommitmentUpsert({ botId: 'bot105d', deepResearchFired: false, successfulBuys: buys, feeWindowByFund: fw }), null)

  // 无买入 → null
  assert.equal(planCommitmentUpsert({ botId: 'bot5d', deepResearchFired: true, successfulBuys: [], feeWindowByFund: fw }), null)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/run.test.ts`
Expected: FAIL — `planCommitmentUpsert` 等未导出。

- [ ] **Step 3: 实现纯函数**

在 `world/src/run.ts` 第 23 行的 import 下面(deep-research-commitment 那行之后)确保引入 `botKindOf` 与 `FundFee`。在文件已有 import 区补:

```typescript
import { botKindOf } from './message.ts'
import type { FundFee } from './daily-context.ts'
```

（若 `botKindOf` / `FundFee` 已被 import,则跳过重复引入。）

在 `recordedRunDays`(第 1305 行附近)之后新增三个导出函数:

```typescript
/** 最短持有天数 = max(7, 赎回费窗口)。零赎费/无窗口也至少锁 7 自然日。 */
export function minHoldingDaysFor(feeWindowDays: number | undefined): number {
  return Math.max(7, feeWindowDays ?? 0)
}

/** 每只基金的赎回费窗口天数（有 rate>0 且 max_days 非空的最大 max_days；无惩罚档=0）。 */
export function feeWindowByFundFrom(fundFees: FundFee[] | undefined): Map<string, number> {
  return new Map((fundFees ?? []).map(f => [
    f.fund_code,
    Math.max(0, ...(f.redeem_tiers ?? []).filter(t => t.rate_pct > 0 && t.max_days != null).map(t => t.max_days as number)),
  ]))
}

/** 决定当日成功买入要建哪种承诺：
 *   深研日（deepResearchFired）→ deep_research（所有 bot，保持现状）；
 *   普通日 + 单指数 → min_hold（新）；
 *   普通日 + 多指数 → null（现状不变）。 */
export function planCommitmentUpsert(input: {
  botId: string
  deepResearchFired: boolean
  successfulBuys: SuccessfulFundAction[] | undefined
  feeWindowByFund: Map<string, number>
}): { kind: 'deep_research' | 'min_hold'; buys: Array<SuccessfulFundAction & { minHoldingDays: number }> } | null {
  const raw = input.successfulBuys ?? []
  if (!raw.length) return null
  const buys = raw.map(b => ({ ...b, minHoldingDays: minHoldingDaysFor(input.feeWindowByFund.get(b.fundCode)) }))
  if (input.deepResearchFired) return { kind: 'deep_research', buys }
  if (botKindOf(input.botId) === 'single-fund') return { kind: 'min_hold', buys }
  return null
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/run.test.ts`
Expected: PASS(新用例通过;原有 e2e 用例不受影响)。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab && git add world/src/run.ts world/test/run.test.ts && git commit -m "$(cat <<'EOF'
feat(world): 抽出 planCommitmentUpsert 决定买入建哪种承诺

Co-Authored-By: claude-opus-4-8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: runLoop 接线——全买入建承诺 + 双标志 + proxy 双轨

把 Task 2/3 的纯函数接进 `runLoop` 与 proxy 回调:每日两个解锁标志、proxy 用 `decideSellCommitment`、承诺创建走 `planCommitmentUpsert`。

**Files:**
- Modify: `world/src/run.ts`
  - gate 结构 第 523 行、第 574-576 行
  - notification 钩子 第 619-622 行
  - proxy checkSellCommitment 回调 第 688-694 行
  - `DayBotStatus` 第 849 行
  - 每日重置与硬风控标志 第 1469 行
  - 承诺计算(替换 deepResearchBuys)第 1654-1657 行
  - 承诺 upsert 第 1724-1727 行
- Test: 已有 `world/test/deep-research-commitment.test.ts` 的 proxy 硬闸 e2e(第 69-94 行)覆盖 proxy 透传;本 Task 逻辑由 Task 2/3 纯函数单测保障 + 类型检查。

**Interfaces:**
- Consumes: `decideSellCommitment`、`planCommitmentUpsert`(前置 Task)
- Produces: 无新导出。gate 结构字段 `unlockedByResearch` / `unlockedByForcedRiskControl` 内部使用。

- [ ] **Step 1: import 补充**

确认 `world/src/run.ts` 第 23 行的 deep-research-commitment import 含 `decideSellCommitment`:

```typescript
import { activeCommitmentsForBot, decideSellCommitment, extractSuccessfulFundActions, isCommitmentActive, renderDeepResearchCommitmentBlock, upsertDeepResearchCommitments, type DeepResearchCommitmentsByBot, type SuccessfulFundAction } from './deep-research-commitment.ts'
```

- [ ] **Step 2: 改 gate 结构类型(SetupResult)**

`world/src/run.ts` 第 523 行,把:

```typescript
  deepResearchCommitmentGate: { commitmentsByBot: DeepResearchCommitmentsByBot; unlockedByBot: Record<string, boolean> }
```

改为:

```typescript
  deepResearchCommitmentGate: { commitmentsByBot: DeepResearchCommitmentsByBot; unlockedByResearch: Record<string, boolean>; unlockedByForcedRiskControl: Record<string, boolean> }
```

- [ ] **Step 3: 改 gate 初始化(setup)**

第 574-576 行,把:

```typescript
  const deepResearchCommitmentGate: SetupResult['deepResearchCommitmentGate'] = {
    commitmentsByBot: persistedCommitments, unlockedByBot: {},
  }
```

改为:

```typescript
  const deepResearchCommitmentGate: SetupResult['deepResearchCommitmentGate'] = {
    commitmentsByBot: persistedCommitments, unlockedByResearch: {}, unlockedByForcedRiskControl: {},
  }
```

- [ ] **Step 4: 改 notification 钩子(改名 unlockedByBot→unlockedByResearch)**

第 619-622 行,把:

```typescript
          if (method === 'tool.call' && params.name === 'start_research') {
            deepResearchCommitmentGate.unlockedByBot[botId] = true
            log(worldRoot, runId, 'bot ' + botId + ': deep-research commitment sell gate unlocked after start_research')
          }
```

改为:

```typescript
          if (method === 'tool.call' && params.name === 'start_research') {
            deepResearchCommitmentGate.unlockedByResearch[botId] = true
            log(worldRoot, runId, 'bot ' + botId + ': deep-research commitment sell gate unlocked after start_research')
          }
```

- [ ] **Step 5: 改 proxy checkSellCommitment 回调**

第 688-694 行,把:

```typescript
      checkSellCommitment: ({ botId, fundCode, tradeDate }) => {
        const commitment = deepResearchCommitmentGate.commitmentsByBot[botId]?.[fundCode]
        if (!isCommitmentActive(commitment, tradeDate) || deepResearchCommitmentGate.unlockedByBot[botId] === true) return { allowed: true }
        return { allowed: false, message: '基金 ' + fundCode + ' 仍在深研持仓承诺期（' + commitment.committed_on + '→' + commitment.commit_until + '）。今天不是深度研究日，普通指标走弱不能推翻深研建仓结论；等待承诺到期，或由系统风险事件触发强制深研后重新判断。' }
      },
```

改为:

```typescript
      checkSellCommitment: ({ botId, fundCode, tradeDate }) => decideSellCommitment({
        commitment: deepResearchCommitmentGate.commitmentsByBot[botId]?.[fundCode],
        tradeDate,
        unlockedByResearch: deepResearchCommitmentGate.unlockedByResearch[botId] === true,
        unlockedByForcedRiskControl: deepResearchCommitmentGate.unlockedByForcedRiskControl[botId] === true,
      }),
```

（`isCommitmentActive` 若因此在 run.ts 变为未使用,保留 import 无害;Task 2 的 decideSellCommitment 内部仍用它。若 `npm run check` 报未使用,再从 run.ts 的 import 移除 `isCommitmentActive`。）

- [ ] **Step 6: 改 DayBotStatus(把 deepResearchBuys 换成 commitmentPlan)**

第 849 行的 `DayBotStatus` 接口,把末尾的 `deepResearchBuys?: Array<SuccessfulFundAction & { minHoldingDays: number }>` 换成:

```typescript
  commitmentPlan?: { kind: 'deep_research' | 'min_hold'; buys: Array<SuccessfulFundAction & { minHoldingDays: number }> }
```

- [ ] **Step 7: 每日重置双标志 + 置硬风控标志**

第 1469 行,把:

```typescript
        setupRes.deepResearchCommitmentGate.unlockedByBot[b.botId] = false
```

改为:

```typescript
        // 每日 chat 前重置两个解锁标志：unlockedByResearch 由 start_research notification 置真；
        // unlockedByForcedRiskControl 仅在硬风控（急跌 target-move / 账户回撤越线 account-drawdown）
        // 强制深研时置真——它解 min_hold 与 deep_research 两轨；主动深研只解 deep_research 轨。
        setupRes.deepResearchCommitmentGate.unlockedByResearch[b.botId] = false
        setupRes.deepResearchCommitmentGate.unlockedByForcedRiskControl[b.botId] =
          drState.reasons.some(r => r === 'target-move' || r === 'account-drawdown')
```

- [ ] **Step 8: 承诺计算(替换 deepResearchBuys 块)**

第 1654-1657 行,把:

```typescript
        if (status.deepResearchFired && status.successfulBuys?.length) {
          const feeWindowByFund = new Map((dailyContext.fundFees ?? []).map(f => [f.fund_code, Math.max(0, ...(f.redeem_tiers ?? []).filter(t => t.rate_pct > 0 && t.max_days != null).map(t => t.max_days as number))]))
          status.deepResearchBuys = status.successfulBuys.map(buy => ({ ...buy, minHoldingDays: Math.max(7, feeWindowByFund.get(buy.fundCode) ?? 0) }))
        }
```

改为:

```typescript
        status.commitmentPlan = planCommitmentUpsert({
          botId: b.botId,
          deepResearchFired: status.deepResearchFired ?? false,
          successfulBuys: status.successfulBuys,
          feeWindowByFund: feeWindowByFundFrom(dailyContext.fundFees),
        }) ?? undefined
```

- [ ] **Step 9: 承诺 upsert(第 1724-1727 行)**

把:

```typescript
        if (status.deepResearchFired && status.deepResearchBuys?.length) {
          nextDeepCommitments = upsertDeepResearchCommitments(nextDeepCommitments, status.bot, date, status.deepResearchBuys)
          log(worldRoot, runId, 'bot ' + status.bot + ' ' + date + ': deep-research holding commitment created for ' + status.deepResearchBuys.map(b => b.fundCode).join(','))
        }
```

改为:

```typescript
        if (status.commitmentPlan) {
          nextDeepCommitments = upsertDeepResearchCommitments(nextDeepCommitments, status.bot, date, status.commitmentPlan.buys, status.commitmentPlan.kind)
          log(worldRoot, runId, 'bot ' + status.bot + ' ' + date + ': ' + status.commitmentPlan.kind + ' holding commitment created for ' + status.commitmentPlan.buys.map(b => b.fundCode).join(','))
        }
```

- [ ] **Step 10: 类型检查 + 全量测试**

Run: `cd world && npm run check`
Expected: 无类型错误(若报 `isCommitmentActive` 未使用,按 Step 5 括注移除该 import 名)。

Run: `cd world && npm test`
Expected: 全绿(含 Task 1/2/3 新用例与既有 e2e)。

- [ ] **Step 11: 提交**

```bash
cd /home/rooot/agent_invest_lab && git add world/src/run.ts && git commit -m "$(cat <<'EOF'
feat(world): 承诺生成扩到所有单指数买入 + 双轨解锁接线

任何单指数买入建 min_hold 硬锁,proxy 按 kind 双轨判定;
硬风控(急跌/回撤)解两轨,主动深研只解深研轨。

Co-Authored-By: claude-opus-4-8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 建仓前告知块 holdCommitmentBlock 升级

把每日告知块口径从「赎回费免赎档」升到「硬锁 max(7,窗口)」,覆盖零赎费基金,深研可触发日补重研通道句。

**Files:**
- Modify: `world/src/message.ts`(`holdCommitmentBlock` 第 961-981 行;调用点第 1282 行)
- Test: `world/test/message.test.ts`

**Interfaces:**
- Consumes: `DailyContextData.fundFees`(已有);`botKindOf`(已在 message.ts 定义);`redeemPenaltyOf` / `inWindowGateLines`(同文件已有)
- Produces: `holdCommitmentBlock(dc, botId, asOfDate, deepResearchEligible: boolean)`(新增末位参数)

- [ ] **Step 1: 写失败测试**

在 `world/test/message.test.ts` 末尾追加(通过 `renderDailyMessage` 端到端断言,与文件既有风格一致):

```typescript
test('holdCommitmentBlock：单指数硬锁口径 max(7,窗口)、零赎费也列、深研日补重研句', () => {
  const w = '/tmp'
  const dc = {
    fundFees: [
      { fund_code: '510300', fund_name: '沪深300', redeem_tiers: [{ max_days: 7, rate_pct: 1.5 }, { max_days: null, rate_pct: 0 }] },
      { fund_code: '008591', fund_name: '零赎费基', redeem_tiers: [{ max_days: null, rate_pct: 0 }] },
    ],
  } as any

  // 单指数、普通日：显示硬锁口径，两只都列（含零赎费），无重研句
  const single = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot5d', quotesPath: '/q.json', dailyContext: dc })
  assert.match(single, /建仓 = 最短持有承诺/)
  assert.match(single, /510300/)
  assert.match(single, /锁 7 自然日/)           // 510300 窗口7 → max(7,7)=7
  assert.match(single, /008591/)                 // 零赎费基金也列
  assert.doesNotMatch(single, /可在后续深研日重研/)

  // 单指数、深研可触发日：补重研通道句
  const deepDay = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot5d', quotesPath: '/q.json', dailyContext: dc, deepResearchAuthorized: true })
  assert.match(deepDay, /可在后续深研日重研/)

  // 多指数 bot：不渲染该块
  const multi = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot105d', quotesPath: '/q.json', dailyContext: dc })
  assert.doesNotMatch(multi, /建仓 = 最短持有承诺/)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/message.test.ts`
Expected: FAIL — 现块头是「持有承诺核对（系统核算 · 早赎红线）」,匹配不到「建仓 = 最短持有承诺」;零赎费基金现被空串跳过。

- [ ] **Step 3: 重写 holdCommitmentBlock**

把 `world/src/message.ts` 第 961-981 行的 `holdCommitmentBlock` 整体替换为:

```typescript
function holdCommitmentBlock(dc: DailyContextData | undefined, botId: string, asOfDate: string, deepResearchEligible: boolean): string {
  if (botKindOf(botId) !== 'single-fund') return ''
  const fees = dc?.fundFees
  if (!fees?.length) return ''
  // 硬锁承诺行：对每只可买基金列锁定天数 = max(7, 赎回费窗口)。零赎费基金也列（仍锁 7 天）。
  const commit: string[] = []
  for (const f of fees) {
    const pen = redeemPenaltyOf(f)
    const windowDays = pen?.windowDays ?? 0
    const lockDays = Math.max(7, windowDays)
    const feeNote = pen ? `不足 ${windowDays} 天确定亏 ${fmtNum(pen.rateForDays(0), 2)}% 早赎费` : '无赎回费'
    commit.push(`  - ${f.fund_code}${f.fund_name ? `（${f.fund_name}）` : ''}：今日建仓/加仓 → 锁 ${lockDays} 自然日（${feeNote}）。`)
  }
  if (!commit.length) return ''
  const lines: string[] = []
  lines.push('────────── 建仓 = 最短持有承诺（系统硬闸 · 跨日生效） ──────────')
  lines.push('【买之前想清楚能不能拿住】今日买入/加仓即被锁定；锁定天数 = max(7, 赎回费窗口)。窗内普通交易日想卖，交易代理会直接拒单（blocked_by:deep_research_commitment）；只有系统硬风控（急跌/账户回撤越线）能提前放行，或等承诺到期。拿不住就别在今天建：')
  lines.push(...commit)
  if (deepResearchEligible) lines.push('  （今日若经 start_research 深研后建仓，该仓位属深研承诺：可在后续深研日重研明确证伪后平仓；普通日建仓不享此通道。）')
  // 牙齿②窗内持仓「今日卖出确切早赎费 + 思考闸」（赎回费维度，与硬锁并存）。
  const gate = inWindowGateLines(dc, asOfDate, fees)
  if (gate.length) lines.push(...gate)
  return `\n\n${lines.join('\n')}`
}
```

- [ ] **Step 4: 改调用点传 deepResearchEligible**

`world/src/message.ts` 第 1282 行,把:

```typescript
  const holdCommit = holdCommitmentBlock(ctx.dailyContext, ctx.botId, ctx.date)
```

改为:

```typescript
  const holdCommit = holdCommitmentBlock(ctx.dailyContext, ctx.botId, ctx.date, Boolean(ctx.deepResearchForced ?? ctx.deepResearchDay) || Boolean(ctx.deepResearchAuthorized))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/message.test.ts`
Expected: PASS。

- [ ] **Step 6: 类型检查 + 全量测试**

Run: `cd world && npm run check && npm test`
Expected: 无类型错误;全部测试通过。

- [ ] **Step 7: 提交**

```bash
cd /home/rooot/agent_invest_lab && git add world/src/message.ts world/test/message.test.ts && git commit -m "$(cat <<'EOF'
feat(world): 建仓前告知块升级为硬锁口径,覆盖零赎费基金+深研日补重研句

Co-Authored-By: claude-opus-4-8 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review 记录

- **Spec 覆盖**:3.1 承诺扩全买入→Task 3+4;3.2 kind/upsert→Task 1;3.3 双轨解锁→Task 2(纯函数)+Task 4(接线);3.4 拒单措辞→Task 2(message 按 kind);3.5 告知块→Task 5;状态兼容(缺省 deep_research)→Task 1/2 覆盖并单测。
- **类型一致**:`kind` 类型 `'deep_research' | 'min_hold'` 全程一致;`upsertDeepResearchCommitments` 第 5 参 `kind` 在 Task 1 定义、Task 4 Step 9 调用一致;`decideSellCommitment` 入参与 Task 4 Step 5 调用一致;`planCommitmentUpsert` 产出与 `DayBotStatus.commitmentPlan` 及 upsert 调用一致。
- **无占位符**:所有步骤含实际代码与命令。
- **风险**:黑天鹅陷仓依赖 `config.crashTriggerEnabled` / `accountDrawdownTriggerEnabled` 开启(见 spec §6);本计划不改触发器配置,执行后需在目标 run 的 yaml 确认触发器已启用,否则 min_hold 锁只能等到期。
