# bot16d / bot20d 深度研究能力复刻 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复刻 bot105d 的深度研究能力到 bot16/bot20，产出 `bot16d`/`bot20d`，并新增「基准指数崩盘阈值强制深研」引擎功能。

**Architecture:** 深度能力与指数解耦——agent 的研究骨架放 `AGENTS.md`，深研调度/引擎复用 105d 的 config 与二进制；跑哪个指数由 world config 的 `strategy_id` 决定。唯一引擎改动是崩盘触发：日级拉一次基准指数，用「单日涨跌/回撤」阈值在 `computeDeepResearchState` 里 OR 进 `forced`。

**Tech Stack:** TypeScript（Node ≥22.6，`--experimental-strip-types`）、`node:test`、world 回测系统、research-loop（Rust 二进制，复用）。

## Global Constraints

- 全程中文（代码/标识符除外）——项目 CLAUDE.md 硬性要求。
- Node ≥ 22.6.0；测试用 `node --test`（`cd world && npm test`）。
- 不新建深度策略文件、不改策略库/`manifest.yaml`、不动 live bot16/bot20 及其 config。
- 崩盘触发默认关闭（`crashTriggerEnabled` 缺省 false）→ 不影响任何现有 run。
- 崩盘阈值默认值：单日 |涨跌| ≥ **3%**，回撤 ≥ **8%**；默认基准 `{code:'000300.SH', name:'沪深300'}`。
- 深研调度：`deep_research_mode: agent-triggered` + `deep_research_max_gap_days: 5`。
- 模型：`qwen3.6-plus`（world config `bot_models` 为准）。
- 引擎复用：`research_loop_rust_bin: /home/rooot/agent_invest_lab/vendor/research-loop-105d/rust/target/release/research-loop-rust2`；`rl_config_base: trading-rl-config-rsloop-105d.json`。
- 回测区间：`replay: 2025-01-01 → 2026-07-17`；`chat_step_mode: weekly`、`chat_step_days: 1`。
- 中证医疗指数代码是 `399989.SZ`（深市），双创是 `931643.CSI`。

## 文件结构

| 文件 | 职责 | 动作 |
|------|------|------|
| `world/src/config.ts` | 解析崩盘触发 4 字段 | 改 |
| `world/test/config.test.ts` | 崩盘字段解析用例 | 改 |
| `world/src/daily-context.ts` | `fetchBenchmarkDailyState()` | 改 |
| `world/test/daily-context-crash.test.ts` | 日涨跌/回撤计算用例 | 新建 |
| `world/src/run.ts` | `computeDeepResearchState` 崩盘扩展 + 日级调用接线 | 改 |
| `world/test/deep-research-crash.test.ts` | 崩盘判定纯函数用例 | 新建 |
| `bots/bot16d/**` | agent 工作区（医药性格 + 深研骨架） | 新建 |
| `bots/bot20d/**` | agent 工作区（双创性格 + 深研骨架） | 新建 |
| `world/config/world-bot16d-weekly-agenticdeep-2025-01-2026-07.yaml` | bot16d 正式回测 config | 新建 |
| `world/config/world-bot20d-weekly-agenticdeep-2025-01-2026-07.yaml` | bot20d 正式回测 config | 新建 |
| `world/config/world-bot16d-smoke10d-crash.yaml` | 10 天烟测 config | 新建 |

---

## Task 1: config.ts — 崩盘触发字段解析

**Files:**
- Modify: `world/src/config.ts`（接口区 ~43-55；解析区 ~249-253；return ~389）
- Test: `world/test/config.test.ts`

**Interfaces:**
- Produces: `WorldConfig` 新增 4 字段 `crashTriggerEnabled: boolean`、`crashTriggerDailyMovePct: number`、`crashTriggerDrawdownPct: number`、`crashTriggerBenchmark: { code: string; name: string }`（均带默认值，解析后恒有值）。

- [ ] **Step 1: 写失败测试**

在 `world/test/config.test.ts` 末尾追加：

```ts
test('loadWorldConfig 崩盘触发字段：缺省时安全默认', () => {
  const p = tmpYaml(`
research_loop: /opt/rl
bots: [bot16d]
replay: { from: "2025-01-02", to: "2025-06-28" }
simworld_upstream_url: http://127.0.0.1:18078/mcp
`)
  const c = loadWorldConfig(p)
  assert.equal(c.crashTriggerEnabled, false)
  assert.equal(c.crashTriggerDailyMovePct, 3)
  assert.equal(c.crashTriggerDrawdownPct, 8)
  assert.deepEqual(c.crashTriggerBenchmark, { code: '000300.SH', name: '沪深300' })
})

test('loadWorldConfig 崩盘触发字段：显式覆盖', () => {
  const p = tmpYaml(`
research_loop: /opt/rl
bots: [bot16d]
replay: { from: "2025-01-02", to: "2025-06-28" }
simworld_upstream_url: http://127.0.0.1:18078/mcp
crash_trigger_enabled: true
crash_trigger_daily_move_pct: 2.5
crash_trigger_drawdown_pct: 10
crash_trigger_benchmark: { code: "399989.SZ", name: "中证医疗" }
`)
  const c = loadWorldConfig(p)
  assert.equal(c.crashTriggerEnabled, true)
  assert.equal(c.crashTriggerDailyMovePct, 2.5)
  assert.equal(c.crashTriggerDrawdownPct, 10)
  assert.deepEqual(c.crashTriggerBenchmark, { code: '399989.SZ', name: '中证医疗' })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --test --experimental-strip-types test/config.test.ts`
Expected: FAIL —— `c.crashTriggerEnabled` 为 `undefined`，断言不通过。

- [ ] **Step 3: 在 WorldConfig 接口加字段**

在 `world/src/config.ts` 接口里 `deepResearchMaxGapDays?: number`（~55 行）之后插入：

```ts
  // 崩盘阈值强制深研（默认关闭，开则不依赖 agent 自主判断）。基准指数出现
  //   |单日涨跌%| >= crashTriggerDailyMovePct 或 距近高回撤 >= crashTriggerDrawdownPct
  // 时，当日 forced 深研。基准默认沪深300，单指数 bot 应在 config 里改成自己的目标指数。
  crashTriggerEnabled?: boolean
  crashTriggerDailyMovePct?: number
  crashTriggerDrawdownPct?: number
  crashTriggerBenchmark?: { code: string; name: string }
```

- [ ] **Step 4: 加解析逻辑**

在 `deepResearchMaxGapDays` 解析行（~253 行）之后插入：

```ts
  const crashTriggerEnabled = raw.crash_trigger_enabled === true
  const crashTriggerDailyMovePct = typeof raw.crash_trigger_daily_move_pct === 'number' && raw.crash_trigger_daily_move_pct > 0 ? raw.crash_trigger_daily_move_pct : 3
  const crashTriggerDrawdownPct = typeof raw.crash_trigger_drawdown_pct === 'number' && raw.crash_trigger_drawdown_pct > 0 ? raw.crash_trigger_drawdown_pct : 8
  const ctb = raw.crash_trigger_benchmark
  const crashTriggerBenchmark = (ctb && typeof ctb === 'object' && typeof ctb.code === 'string' && typeof ctb.name === 'string')
    ? { code: ctb.code, name: ctb.name }
    : { code: '000300.SH', name: '沪深300' }
```

- [ ] **Step 5: 加进 return 对象**

在 `parseConfig` 末尾 return（~389 行）的 `deepResearchMaxGapDays,` 之后加：

```ts
    crashTriggerEnabled, crashTriggerDailyMovePct, crashTriggerDrawdownPct, crashTriggerBenchmark,
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd world && node --test --experimental-strip-types test/config.test.ts`
Expected: PASS（新增两个用例 + 原有全过）。

- [ ] **Step 7: 提交**

```bash
git add world/src/config.ts world/test/config.test.ts
git commit -m "feat(world): config 解析崩盘触发深研 4 字段"
```

---

## Task 2: daily-context.ts — fetchBenchmarkDailyState()

**Files:**
- Modify: `world/src/daily-context.ts`（`maxDrawdownPct` 在 ~649；`fetchIndexBenchmark` 在 ~698 可参照）
- Test: `world/test/daily-context-crash.test.ts`（新建）

**Interfaces:**
- Consumes: 无（复用文件内 `maxDrawdownPct`、`priorDay`、`callSimworldTool`）。
- Produces: 导出 `interface BenchmarkDailyState { lastDate: string; lastDayMovePct: number | null; drawdownFromRecentHighPct: number | null }` 与 `export async function fetchBenchmarkDailyState(opts: { simworldUrl: string; code: string; asOfDate: string; lookbackDays?: number }): Promise<BenchmarkDailyState | null>`。纯计算部分拆出可测的 `export function computeCrashSignalFromCloses(closes: { date: string; close: number }[]): BenchmarkDailyState | null`。

- [ ] **Step 1: 写失败测试**

新建 `world/test/daily-context-crash.test.ts`：

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeCrashSignalFromCloses } from '../src/daily-context.ts'

test('单日跌幅：最后两日 -3.5%', () => {
  const r = computeCrashSignalFromCloses([
    { date: '2025-01-02', close: 100 },
    { date: '2025-01-03', close: 96.5 },
  ])
  assert.equal(r?.lastDate, '2025-01-03')
  assert.ok(r && Math.abs(r.lastDayMovePct! - (-3.5)) < 1e-9)
})

test('回撤：从 100 高点跌到 91 = -9%', () => {
  const r = computeCrashSignalFromCloses([
    { date: '2025-01-02', close: 100 },
    { date: '2025-01-03', close: 95 },
    { date: '2025-01-06', close: 91 },
  ])
  assert.ok(r && Math.abs(r.drawdownFromRecentHighPct! - (-9)) < 1e-9)
})

test('急涨也算：+3.2%', () => {
  const r = computeCrashSignalFromCloses([
    { date: '2025-01-02', close: 100 },
    { date: '2025-01-03', close: 103.2 },
  ])
  assert.ok(r && Math.abs(r.lastDayMovePct! - 3.2) < 1e-9)
})

test('空/单点序列返回 null 或 null 字段', () => {
  assert.equal(computeCrashSignalFromCloses([]), null)
  const one = computeCrashSignalFromCloses([{ date: '2025-01-02', close: 100 }])
  assert.equal(one?.lastDayMovePct, null)         // 无前一日 → 日涨跌 null
  assert.equal(one?.drawdownFromRecentHighPct, 0) // 单点回撤 0
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --test --experimental-strip-types test/daily-context-crash.test.ts`
Expected: FAIL —— `computeCrashSignalFromCloses` 未导出。

- [ ] **Step 3: 实现纯函数 + fetch 包装**

在 `world/src/daily-context.ts` 里 `fetchIndexBenchmark`（~698 行）之前插入：

```ts
export interface BenchmarkDailyState {
  lastDate: string
  lastDayMovePct: number | null            // (close[n]-close[n-1])/close[n-1]*100；无前一日 → null
  drawdownFromRecentHighPct: number | null // <=0；窗口内 peak→trough，复用 maxDrawdownPct
}

/** 从收盘序列（升序）算崩盘判定的两个标量。纯函数，便于单测。 */
export function computeCrashSignalFromCloses(closes: { date: string; close: number }[]): BenchmarkDailyState | null {
  if (closes.length === 0) return null
  const last = closes[closes.length - 1]
  const prev = closes.length >= 2 ? closes[closes.length - 2] : undefined
  const lastDayMovePct = prev && prev.close > 0 ? (last.close - prev.close) / prev.close * 100 : null
  const drawdownFromRecentHighPct = maxDrawdownPct(closes.map(c => c.close))
  return { lastDate: last.date, lastDayMovePct, drawdownFromRecentHighPct }
}

/** 拉基准指数近 lookbackDays 交易日收盘（PIT：end_date = asOfDate 前一交易日），
 *  返回崩盘判定标量。复用 fetchIndexBenchmark 的 market_index_quote 路径。best-effort。 */
export async function fetchBenchmarkDailyState(opts: {
  simworldUrl: string; code: string; asOfDate: string; lookbackDays?: number
}): Promise<BenchmarkDailyState | null> {
  const lookback = opts.lookbackDays ?? 60
  const startDate = shiftIsoDaysBack(opts.asOfDate, lookback * 2) // 日历日预估，够覆盖 lookback 交易日
  const simDt = `${opts.asOfDate} 15:00:00`
  try {
    const raw = await callSimworldTool(opts.simworldUrl, 'market_index_quote', {
      market: 'cn',
      symbols: [opts.code],
      simulated_datetime: simDt,
      start_date: startDate,
      end_date: priorDay(opts.asOfDate),
    }) as { items?: { 是否可用?: boolean; 行情记录?: { 日期?: string; 收盘?: number }[] }[] } | null
    const item = raw?.items?.[0]
    if (!item || !item['是否可用'] || !Array.isArray(item['行情记录'])) return null
    const closes = item['行情记录']
      .map(r => ({ date: String(r['日期'] ?? '').slice(0, 10), close: Number(r['收盘'] ?? 0) }))
      .filter(r => r.date && r.close > 0)
    return computeCrashSignalFromCloses(closes)
  } catch {
    return null
  }
}
```

> 注：`shiftIsoDaysBack(iso, n)` 若文件内无同名工具，则在本函数上方加一个最小实现：
> ```ts
> function shiftIsoDaysBack(iso: string, n: number): string {
>   const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() - n)
>   return d.toISOString().slice(0, 10)
> }
> ```
> 先 `grep -n "priorDay\|callSimworldTool" world/src/daily-context.ts` 确认这两个符号在文件内已存在（`fetchIndexBenchmark` 用到它们，应当有）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --test --experimental-strip-types test/daily-context-crash.test.ts`
Expected: PASS（4 个用例全过）。

- [ ] **Step 5: 提交**

```bash
git add world/src/daily-context.ts world/test/daily-context-crash.test.ts
git commit -m "feat(world): daily-context 加 fetchBenchmarkDailyState 崩盘信号"
```

---

## Task 3: run.ts — computeDeepResearchState 崩盘扩展

**Files:**
- Modify: `world/src/run.ts`（`DeepResearchStateInput` ~907-915；`computeDeepResearchState` ~928-935）
- Test: `world/test/deep-research-crash.test.ts`（新建）

**Interfaces:**
- Consumes: 现有 `tradingDaysBetween`、`isDeepResearchDay`。
- Produces: `DeepResearchStateInput` 新增可选字段 `crashSignal?: { dailyMovePct: number | null; drawdownPct: number | null; dailyMoveThreshold: number; drawdownThreshold: number }`；`computeDeepResearchState` 的 `forced` 在两分支都 OR 进 `isCrashForced(crashSignal)`；ordinal 分支 `authorized` 也随崩盘放行。导出 `export function isCrashForced(s?: DeepResearchStateInput['crashSignal']): boolean`。

- [ ] **Step 1: 写失败测试**

新建 `world/test/deep-research-crash.test.ts`：

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeDeepResearchState, isCrashForced } from '../src/run.ts'

const DATES = ['2025-01-02','2025-01-03','2025-01-06','2025-01-07','2025-01-08','2025-01-09','2025-01-10']

test('无 crashSignal：等价现有 agent-triggered 行为', () => {
  const s = computeDeepResearchState({
    mode: 'agent-triggered', ordinal: 1, every: 0, maxGapDays: 5,
    todayDate: '2025-01-03', lastDeepDate: '2025-01-02', tradingDates: DATES,
  })
  assert.equal(s.authorized, true)
  assert.equal(s.forced, false) // gap=1 < 5，无崩盘
})

test('单日 -3.5% 触发 forced（gap 未到上限也强制）', () => {
  const s = computeDeepResearchState({
    mode: 'agent-triggered', ordinal: 1, every: 0, maxGapDays: 5,
    todayDate: '2025-01-03', lastDeepDate: '2025-01-02', tradingDates: DATES,
    crashSignal: { dailyMovePct: -3.5, drawdownPct: -1, dailyMoveThreshold: 3, drawdownThreshold: 8 },
  })
  assert.equal(s.forced, true)
})

test('急涨 +3.1% 也触发', () => {
  assert.equal(isCrashForced({ dailyMovePct: 3.1, drawdownPct: 0, dailyMoveThreshold: 3, drawdownThreshold: 8 }), true)
})

test('回撤 -8.2% 触发', () => {
  assert.equal(isCrashForced({ dailyMovePct: -1, drawdownPct: -8.2, dailyMoveThreshold: 3, drawdownThreshold: 8 }), true)
})

test('move=-2% dd=-5% 都不触发', () => {
  assert.equal(isCrashForced({ dailyMovePct: -2, drawdownPct: -5, dailyMoveThreshold: 3, drawdownThreshold: 8 }), false)
})

test('null 字段安全：不触发', () => {
  assert.equal(isCrashForced({ dailyMovePct: null, drawdownPct: null, dailyMoveThreshold: 3, drawdownThreshold: 8 }), false)
  assert.equal(isCrashForced(undefined), false)
})

test('ordinal 分支：崩盘日也放行 authorized', () => {
  const s = computeDeepResearchState({
    mode: 'ordinal', ordinal: 1, every: 4, maxGapDays: 5, // ordinal=1 非深研日
    todayDate: '2025-01-03', lastDeepDate: '2025-01-02', tradingDates: DATES,
    crashSignal: { dailyMovePct: -4, drawdownPct: 0, dailyMoveThreshold: 3, drawdownThreshold: 8 },
  })
  assert.equal(s.forced, true)
  assert.equal(s.authorized, true) // 强制了就必须放行 start_research
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --test --experimental-strip-types test/deep-research-crash.test.ts`
Expected: FAIL —— `isCrashForced` 未导出；`crashSignal` 未处理。

- [ ] **Step 3: 扩展 DeepResearchStateInput**

在 `world/src/run.ts` 的 `DeepResearchStateInput` 接口（~907 行）的 `tradingDates: string[]` 之后加：

```ts
  /** 崩盘阈值信号（可选）。任一命中 → forced。dailyMovePct/drawdownPct 为 null 时该维不触发。 */
  crashSignal?: {
    dailyMovePct: number | null
    drawdownPct: number | null     // <=0
    dailyMoveThreshold: number     // 正数，如 3
    drawdownThreshold: number      // 正数，如 8
  }
```

- [ ] **Step 4: 加 isCrashForced + 改 computeDeepResearchState**

把现有 `computeDeepResearchState`（~928-935 行）整体替换为：

```ts
/** 崩盘信号是否强制深研：|单日涨跌| >= 阈值 或 回撤 <= -阈值。null 字段不触发。 */
export function isCrashForced(s?: DeepResearchStateInput['crashSignal']): boolean {
  if (!s) return false
  const moveHit = s.dailyMovePct != null && Math.abs(s.dailyMovePct) >= s.dailyMoveThreshold
  const ddHit = s.drawdownPct != null && s.drawdownPct <= -s.drawdownThreshold
  return moveHit || ddHit
}

export function computeDeepResearchState(inp: DeepResearchStateInput): DeepResearchStateOut {
  const gapDays = tradingDaysBetween(inp.tradingDates, inp.lastDeepDate, inp.todayDate)
  const crashForced = isCrashForced(inp.crashSignal)
  if (inp.mode === 'agent-triggered') {
    return { authorized: true, forced: (gapDays >= inp.maxGapDays) || crashForced, gapDays }
  }
  const isDR = isDeepResearchDay(inp.ordinal, inp.every)
  const forced = isDR || crashForced
  return { authorized: forced, forced, gapDays } // 崩盘强制时同步放行 start_research
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd world && node --test --experimental-strip-types test/deep-research-crash.test.ts`
Expected: PASS（7 用例全过）。

- [ ] **Step 6: 提交**

```bash
git add world/src/run.ts world/test/deep-research-crash.test.ts
git commit -m "feat(world): 崩盘阈值强制深研 (computeDeepResearchState)"
```

---

## Task 4: run.ts — 日级崩盘信号接线 + 日志标记

**Files:**
- Modify: `world/src/run.ts`（`drState` 调用点 ~1132-1144；`tagBits` 日志 ~1155）

**Interfaces:**
- Consumes: Task 2 的 `fetchBenchmarkDailyState`、Task 1 的 config 崩盘字段、Task 3 的 `crashSignal` 入参。
- Produces: 无新导出；行为 = 崩盘日 `drState.forced=true` 并在日志出现 `[deep-research#crash ...]`。

- [ ] **Step 1: 在 daily-context 导入清单加 fetchBenchmarkDailyState**

先 `grep -n "from './daily-context" world/src/run.ts` 找到现有 import。把 `fetchBenchmarkDailyState` 加进该 import 的花括号列表（与 `fetchDailyContext` 同一 import）。

- [ ] **Step 2: 在 drState 计算前拉崩盘信号（日级一次）**

在 `world/src/run.ts` 的 `const stateNow = readState(worldRoot, runId)`（~1133 行）之后、`const drState = computeDeepResearchState({`（~1134 行）之前插入：

```ts
      let crashSignal: Parameters<typeof computeDeepResearchState>[0]['crashSignal']
      if (config.crashTriggerEnabled) {
        const st = await fetchBenchmarkDailyState({
          simworldUrl: config.simworldUpstreamUrl,
          code: config.crashTriggerBenchmark.code,
          asOfDate: date,
        }).catch(() => null)
        if (st) crashSignal = {
          dailyMovePct: st.lastDayMovePct,
          drawdownPct: st.drawdownFromRecentHighPct,
          dailyMoveThreshold: config.crashTriggerDailyMovePct,
          drawdownThreshold: config.crashTriggerDrawdownPct,
        }
        log(worldRoot, runId, `crash-check ${date} bench=${config.crashTriggerBenchmark.code} move=${st?.lastDayMovePct?.toFixed(2) ?? 'n/a'}% dd=${st?.drawdownFromRecentHighPct?.toFixed(2) ?? 'n/a'}%`)
      }
```

- [ ] **Step 3: 把 crashSignal 传进 computeDeepResearchState**

在 `computeDeepResearchState({ ... })` 调用（~1134-1142 行）的 `tradingDates: dates,` 之后加一行：

```ts
        crashSignal,
```

- [ ] **Step 4: 日志 tagBits 加崩盘标记**

在 `tagBits`（~1155 行）里，把深研标记那段的三元表达式扩展——原本区分 `#forced` / `#authorized`，改成崩盘优先显示。定位 `drState.forced ? \`[deep-research#forced gap=${drState.gapDays}]\``，替换其中的 `#forced` 分支为：

```ts
drState.forced ? `[deep-research#${isCrashForced(crashSignal) ? 'crash' : 'forced'} gap=${drState.gapDays}${isCrashForced(crashSignal) ? ` move=${crashSignal?.dailyMovePct?.toFixed(1) ?? '?'}% dd=${crashSignal?.drawdownPct?.toFixed(1) ?? '?'}%` : ''}]`
```

（`isCrashForced` 已在 Task 3 于同文件导出，直接调用。）

- [ ] **Step 5: 类型检查 + 全量测试**

Run: `cd world && npx tsc --noEmit -p . 2>/dev/null || node --experimental-strip-types --check src/run.ts; npm test`
Expected: 无类型错误；`npm test` 全绿（含前三个 Task 的新测试）。

> 若仓库无 tsconfig 严格检查，退而用 `node --experimental-strip-types --check src/run.ts` 确认语法/剥类型可过。

- [ ] **Step 6: 提交**

```bash
git add world/src/run.ts
git commit -m "feat(world): run.ts 日级崩盘信号接线 + 日志 #crash 标记"
```

---

## Task 5: bots/bot16d 工作区（医药性格 + 深研骨架）

**Files:**
- Create: `bots/bot16d/{USER.md,IDENTITY.md,SOUL.md,AGENTS.md,TOOLS.md,METHODOLOGY.md,MEMORY.md,EQUIPPED_SKILLS.md}`、`bots/bot16d/config/{model.yaml,mcporter.json}`、`bots/bot16d/skills/`、`bots/bot16d/memory/`

**Interfaces:**
- Produces: 一个可被 world config `bots: [bot16d]` 引用的合法 bot 工作区，含深研骨架的 `AGENTS.md` 与 qwen3.6-plus 的 `config/model.yaml`。

- [ ] **Step 1: 复制 bot16 的性格与骨架文件**

```bash
cd /home/rooot/agent_invest_lab
mkdir -p bots/bot16d/config bots/bot16d/skills bots/bot16d/memory
cp bots/bot16/USER.md bots/bot16/IDENTITY.md bots/bot16/SOUL.md \
   bots/bot16/TOOLS.md bots/bot16/METHODOLOGY.md bots/bot16/MEMORY.md \
   bots/bot16/EQUIPPED_SKILLS.md bots/bot16d/
cp bots/bot16/config/mcporter.json bots/bot16d/config/
```

- [ ] **Step 2: 写 config/model.yaml（qwen3.6-plus）**

`bots/bot16d/config/model.yaml`（照 bot105d 结构，注释保留）：

```yaml
# 本 bot 的模型配置（research-loop 的 model.primary）。base_url 由 provider 自动派生，不要手写。
# api_key 用 api_key_from_openclaw 引用 openclaw.json 里的 provider，回测时由 world 注入。
provider: openai_compatible
model: qwen3.6-plus
api_key_from_openclaw: zai-coding-plan
```

> world config 的 `bot_models` 会再覆盖一次；此处是工作区默认，保持一致即可。

- [ ] **Step 3: 用 bot16 的 AGENTS.md 为基底 + 追加深研骨架**

先复制：`cp bots/bot16/AGENTS.md bots/bot16d/AGENTS.md`。
然后在 `bots/bot16d/AGENTS.md` 末尾追加以下小节（内容取自 `bots/bot105d/AGENTS.md` 93-155 行，**已按单指数改写**——去掉「多指数横向比较」，改成「对中证医疗单一指数的多维深研」）：

````markdown
## 深度研究（仅深度研究日）

1. **只有当日消息带【深度研究日】标记时**，才允许调用 `start_research` 进入深度研究模式；其余决策日研究工具会被直接拒绝，不要尝试调用。
2. **一个研究日只做一个命题**，一次讲透；命题由下面的「选题算法」决定，不许凭心情自选。
3. **数据边界与日常完全一致**：深研内仍只有 simworld（PIT）与组合工具。
4. **研究不得挤掉当日决策**：时间吃紧时先完成当日决策与下单。
5. **有纠偏权，无一票否决权**：深研结论覆盖日度『加/减仓』动作，须与至少一条同向一级证据（价格破位/持续资金流出/闸门触发/可验证利空）共振；单独存在只能收紧止损止盈或调档位内力度。

### 选题算法（按优先级取第一个命中的，不许跳）

1. **分歧驱动**（最高）：存在"引擎动作 vs 我的执行"分歧 → 审计该分歧。
2. **错误驱动**：belief 校准显示近 20 日方向连错或 Brier 恶化 → 命题 = "我哪条关键假设错了"。
3. **机会驱动**：当前对中证医疗（器械/院端服务/CXO 三块）哪一块主导、要不要加仓的入场方案设计。
4. **规则有效性**：每 4 期至少 1 期，检验方法论某条规则在当前市况是否有效。

**禁止连续 2 期用同一论点立题**；看空论点被价格证伪后，续题只能是"为什么我错了"。

### 研究记分牌（开题前必做，防死圈）

- `start_research` 前先 `mem0_search` 检索 research-card，逐卡对照结论与之后 20 日实际走势，在 premise 里写明近 3 期命中/证伪。
- 某方向连续 2 期被证伪 → 本期必须以"为什么我错了"开题。

### 全文消化（只读 brief = 流程未完成）

- `start_research` 返回 brief 后，**必须**再各调一次 `expand_research_phase(run_id,'challenge')` 与 `expand_research_phase(run_id,'synthesize')`，拉全反方与综合，才允许做仓位决策。
- 核对具体证据用 `expand_research_evidence(run_id, source_id)`。

### 行动卡（研究的唯一合法交割物）

结论落成固定格式行动卡，`mem0_add`（以 `[research-card]` 开头）：

- **结论/立场**（一句话）
- **仓位含义**：目标档位 + 结构
- **触发价位**：入场/加仓/止损止盈
- **证伪条件**：出现什么数据说明本结论错了
- **有效期**：≤4 个决策日，到期自动作废

"要不要进"命题交割物必须是**入场方案**，禁止以"太贵，不进"作终局。

### 研究维度权重矩阵（开题先声明当前 regime）

| 市况 | 维度优先级 |
|---|---|
| **动量 / uptrend** | 资金与筹码 ＞ 趋势结构 ＞ 产业景气（院端招标月度数据）＞ 情绪拥挤（只定止盈）＞ 估值（只定买点） |
| **震荡** | 估值性价比 ＞ 政策与流动性（集采/DRG/设备更新财政）＞ 结构；情绪极值可逆向 |
| **防御 / 下行** | 流动性拐点 ＞ 政策底信号 ＞ 绝对估值；动量降权 |

医药特有：**院端招标 / 中标金额月度数据是本指数最硬的景气领先指标（领先报表 1–2 季度），深研必查**；集采/反腐/DRG 是第一性政策变量，不是外生扰动。

## 每日执行顺序（深研日加 step 0）

0. **先查最新行动卡，再扫持仓消息面**：`mem0_search` 取最新 `[research-card]`；有效期内且未证伪 = 现行指令，纳入今日决策；过期/证伪则作废、不得引用其立场。再对中证医疗构造检索词调 `research_search`/`research_view` 看重大催化或利空（集采落地/反腐/招标放量），命中重大利空优先减仓。串行调用，勿并发 simworld 工具。
1. `portfolio_get_buyable_funds` 确认可交易基金池（本 bot 单只医药基金）。
2. 判断今天 `risk_on / neutral / risk_off`。
3. 判断中证医疗当前由哪一块（器械/院端服务/CXO）主导、景气方向。
4. 决定组合总仓位与该基金档位。

第 2、3 步说不清时默认**不激进**，而非先买再想。
````

> **关键改写点**：bot105d 原文有「## 多指数比较维度」「对候选指数做横向比较，找出最强 2-4 个」——bot16d 是单指数择时，**不要**保留这些；已在上面替换为「对中证医疗单一指数的多维深研 + 三块结构主导判断」。

- [ ] **Step 3b: 用 shadowWorkspace 测试验证工作区可被加载**

Run: `cd world && node --test --experimental-strip-types test/shadowWorkspace.test.ts`
Expected: PASS（现有测试不应因新增 bot 目录而回归；若该测试硬编码 bot 列表则无关，重点是不破坏）。

- [ ] **Step 4: 提交**

```bash
git add bots/bot16d
git commit -m "feat(bots): 新增 bot16d 工作区（医药性格 + 深研骨架）"
```

---

## Task 6: bots/bot20d 工作区（双创性格 + 深研骨架）

**Files:**
- Create: `bots/bot20d/**`（结构同 Task 5，性格取自 bot20）

**Interfaces:**
- Produces: 可被 `bots: [bot20d]` 引用的合法工作区。

- [ ] **Step 1: 复制 bot20 骨架**

```bash
cd /home/rooot/agent_invest_lab
mkdir -p bots/bot20d/config bots/bot20d/skills bots/bot20d/memory
cp bots/bot20/USER.md bots/bot20/IDENTITY.md bots/bot20/SOUL.md \
   bots/bot20/TOOLS.md bots/bot20/METHODOLOGY.md bots/bot20/MEMORY.md \
   bots/bot20/EQUIPPED_SKILLS.md bots/bot20d/
cp bots/bot20/config/mcporter.json bots/bot20d/config/
cp bots/bot20/AGENTS.md bots/bot20d/AGENTS.md
```

- [ ] **Step 2: 写 config/model.yaml**

`bots/bot20d/config/model.yaml`：内容与 Task 5 Step 2 完全一致（provider/model/api_key_from_openclaw 三行 + 注释）。

- [ ] **Step 3: 追加深研骨架（双创改写版）**

在 `bots/bot20d/AGENTS.md` 末尾追加与 Task 5 Step 3 相同的深研骨架，但把医药特有的措辞换成双创：

- 选题算法第 3 条：`当前双创（半导体/AI/新能源/医药/高端装备）哪个产业贝塔主导、要不要加仓的入场方案`。
- 维度矩阵「医药特有」那行换成：`双创特有：**宏观环境 > 仓位择时 > 选品，周期思维 > 长期持有**；高弹性(牛市 2-3 倍沪深300)、高波动(熊市回撤 50%+)、科技产业 3-4 年一轮——深研必判当前处于产业周期哪一段、流动性方向。科创/创业板不能单看 PE，要看成长性与未来前景。`
- 每日执行顺序 step 3 换成：`判断双创当前由哪个产业(半导体/AI/新能源/…)主导、周期位置`；step 0 的利空检索换成 `产业景气拐点/流动性收紧/科技监管`。
- 同样**删掉**多指数横向比较措辞。

- [ ] **Step 4: 提交**

```bash
git add bots/bot20d
git commit -m "feat(bots): 新增 bot20d 工作区（双创性格 + 深研骨架）"
```

---

## Task 7: world config — bot16d 正式回测 + 烟测

**Files:**
- Create: `world/config/world-bot16d-weekly-agenticdeep-2025-01-2026-07.yaml`
- Create: `world/config/world-bot16d-smoke10d-crash.yaml`

**Interfaces:**
- Consumes: Task 1-6 全部（config 字段、崩盘接线、bot16d 工作区）。
- Produces: 可 `npm start -- --config ...` 跑通的回测 config。

- [ ] **Step 1: 复制 agent-triggered 模板**

```bash
cd /home/rooot/agent_invest_lab/world/config
cp world-bot105d-daily-agenticdeep-2025-01-2026-07.yaml world-bot16d-weekly-agenticdeep-2025-01-2026-07.yaml
```

- [ ] **Step 2: 改关键字段**

编辑 `world-bot16d-weekly-agenticdeep-2025-01-2026-07.yaml`：

- `bots:` → `[bot16d]`
- `bot_assignments:` 下 → `bot16d:` `strategy_id: medical`、`buyable_fund_codes: ['012323']`
- `buyable_fund_codes`（顶层）→ `['012323']`（单只）
- `bot_models:` 下 → `bot16d:` `model: qwen3.6-plus`、`api_key_from_openclaw: zai-coding-plan`
- `deep_research_max_gap_days: 5`（模板是 4）
- `chat_step_mode: weekly`、`chat_step_days: 1`（模板可能是 trading_days，改成 weekly）
- 顶层新增崩盘字段：
  ```yaml
  crash_trigger_enabled: true
  crash_trigger_daily_move_pct: 3
  crash_trigger_drawdown_pct: 8
  crash_trigger_benchmark: { code: "399989.SZ", name: "中证医疗" }
  ```
- 确认 `research_loop_rust_bin`、`rl_config_base`、`replay: {from: 2025-01-01, to: 2026-07-17}` 与模板一致。

- [ ] **Step 3: 用 loadWorldConfig 校验解析**

Run:
```bash
cd /home/rooot/agent_invest_lab/world && node --experimental-strip-types -e "import('./src/config.ts').then(m=>{const c=m.loadWorldConfig('config/world-bot16d-weekly-agenticdeep-2025-01-2026-07.yaml');console.log('bots',c.bots,'crash',c.crashTriggerEnabled,c.crashTriggerBenchmark,'gap',c.deepResearchMaxGapDays,'mode',c.deepResearchMode)})"
```
Expected: 打印 `bots [ 'bot16d' ] crash true { code: '399989.SZ', name: '中证医疗' } gap 5 mode agent-triggered`。

- [ ] **Step 4: 派生 10 天烟测 config**

```bash
cp world-bot16d-weekly-agenticdeep-2025-01-2026-07.yaml world-bot16d-smoke10d-crash.yaml
```
编辑 `world-bot16d-smoke10d-crash.yaml`：`replay:` 改成一个含已知大跌日的 10 交易日窗口（如 `{from: "2025-04-07", to: "2025-04-18"}`，2025-04-07 A股有大跌，便于验证崩盘触发）。其余不变。

- [ ] **Step 5: 提交**

```bash
git add world/config/world-bot16d-weekly-agenticdeep-2025-01-2026-07.yaml world/config/world-bot16d-smoke10d-crash.yaml
git commit -m "feat(world): bot16d 回测 config（medical + agent-triggered + 崩盘触发）"
```

---

## Task 8: world config — bot20d 正式回测 + 烟测

**Files:**
- Create: `world/config/world-bot20d-weekly-agenticdeep-2025-01-2026-07.yaml`
- Create: `world/config/world-bot20d-smoke10d-crash.yaml`

**Interfaces:**
- 同 Task 7，标的换双创。

- [ ] **Step 1: 复制并改字段**

```bash
cd /home/rooot/agent_invest_lab/world/config
cp world-bot105d-daily-agenticdeep-2025-01-2026-07.yaml world-bot20d-weekly-agenticdeep-2025-01-2026-07.yaml
```
改：`bots: [bot20d]`；`bot_assignments.bot20d.strategy_id: shuangchuang`、`buyable_fund_codes: ['011609']`；顶层 `buyable_fund_codes: ['011609']`；`bot_models.bot20d`（qwen3.6-plus / zai-coding-plan）；`deep_research_max_gap_days: 5`；`chat_step_mode: weekly`、`chat_step_days: 1`；崩盘字段（`crash_trigger_benchmark: { code: "931643.CSI", name: "双创" }`，enabled true，3/8）。

- [ ] **Step 2: 校验解析**

Run:
```bash
cd /home/rooot/agent_invest_lab/world && node --experimental-strip-types -e "import('./src/config.ts').then(m=>{const c=m.loadWorldConfig('config/world-bot20d-weekly-agenticdeep-2025-01-2026-07.yaml');console.log('bots',c.bots,'crash',c.crashTriggerBenchmark,'gap',c.deepResearchMaxGapDays)})"
```
Expected: `bots [ 'bot20d' ] crash { code: '931643.CSI', name: '双创' } gap 5`。

- [ ] **Step 3: 派生烟测 + 提交**

```bash
cp world-bot20d-weekly-agenticdeep-2025-01-2026-07.yaml world-bot20d-smoke10d-crash.yaml
# 编辑 replay 为 {from: "2025-04-07", to: "2025-04-18"}
git add world/config/world-bot20d-weekly-agenticdeep-2025-01-2026-07.yaml world/config/world-bot20d-smoke10d-crash.yaml
git commit -m "feat(world): bot20d 回测 config（shuangchuang + agent-triggered + 崩盘触发）"
```

---

## Task 9: 端到端烟测验证

**Files:** 无新增（运行验证）

**Interfaces:**
- Consumes: 全部前序 Task。

- [ ] **Step 1: 确认 MCP 与引擎就绪**

```bash
cd /home/rooot/agent_invest_lab
./restart.sh   # ttjj-data-pit :18078（崩盘信号 market_index_quote 走这里）
ls -la vendor/research-loop-105d/rust/target/release/research-loop-rust2  # 引擎二进制存在
```
Expected: :18078 起来；二进制存在可执行。

- [ ] **Step 2: 跑 bot16d 10 天烟测**

Run: `cd /home/rooot/agent_invest_lab/world && npm start -- --config config/world-bot16d-smoke10d-crash.yaml`
Expected: 10 个交易日跑完；日志（`world/runtime/runs/<runId>/...`）出现 `crash-check` 行；在 2025-04-07 附近出现 `[deep-research#crash ...]` 标记；qwen3.6-plus 端点有正常 chat 返回（无 401）。

- [ ] **Step 3: 核对崩盘触发确实生效**

Run: `grep -E "crash-check|deep-research#crash" world/runtime/runs/*/world.log | tail -20`
Expected: 至少一天 `#crash` 被强制；`crash-check` 打印的 move/dd 数值与阈值关系自洽（触发日 |move|≥3 或 dd≤-8）。

- [ ] **Step 4: 全量单测回归**

Run: `cd world && npm test`
Expected: 全绿（含 Task 1/2/3 新测试）。

- [ ] **Step 5: 提交（若烟测中有小修）**

```bash
git add -A && git commit -m "test(world): bot16d/bot20d 崩盘触发深研烟测通过"
```

（若烟测发现问题，回到对应 Task 修复，不在本 Task 里堆改动。）

---

## Self-Review 记录

- **Spec 覆盖**：§4.1 工作区→Task 5/6；§4.2 复用→Task 7/8 的 config 字段；§4.3 崩盘代码→Task 1/2/3/4；§4.4 模板 config→Task 7/8；§六 测试→Task 1/2/3 单测 + Task 9 烟测。无遗漏。
- **占位符**：无 TBD/TODO；代码步骤均含真实代码。
- **类型一致**：`crashSignal` 字段名（dailyMovePct/drawdownPct/dailyMoveThreshold/drawdownThreshold）在 Task 3 定义、Task 4 消费一致；`fetchBenchmarkDailyState` 返回 `BenchmarkDailyState`（lastDayMovePct/drawdownFromRecentHighPct）在 Task 2 定义、Task 4 映射一致；`isCrashForced` 在 Task 3 导出、Task 4 调用。
- **风险**：Task 4 的接线难以纯单测，靠类型检查 + Task 9 烟测覆盖；`shiftIsoDaysBack`/`priorDay`/`callSimworldTool` 存在性在 Task 2 Step 3 备注要求先 grep 确认。
