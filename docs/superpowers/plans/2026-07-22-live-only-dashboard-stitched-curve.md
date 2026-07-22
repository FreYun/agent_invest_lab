# live-only 看板拼接连续曲线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `runs.html` 看板改成只展示 54 个 `live-` 单指数 run，每行是「源 dash 回测历史 + live 每日增量」几何拼接的连续曲线，指标按拼接序列重算，人工评级/点评继承源 dash run。

**Architecture:** 抽出纯函数 `stitchSeriesRows`（首点归一 + 末点 rebase 续接）与 `computeStitchedMetrics`（累计/回撤/252 年化），让现有 OOS 视图（`loadOosExtendedSeries`）与新的 live 汇总共用同一套数学。服务端新增 `loadLiveRunsSummary` + `/api/backtest/live-runs`（扫 `state.json` 取 `source_run_id`）；`/api/backtest/bot` 加 live 分支返回拼接曲线。前端 `runs.html` 反转过滤：只渲染 live run，按 `source_run_id|bot` 从评测表继承源 dash 的评级/点评/指数归类。

**Tech Stack:** TypeScript（Node ≥22.6，`--experimental-strip-types` 直接跑 .ts）、`node:sqlite` 的 `DatabaseSync`（同步查询）、`node:test` + `node:assert/strict`、原生 HTML/JS（无框架）。

## Global Constraints

- Node ≥ 22.6.0；.ts 用 `node --experimental-strip-types` 直接运行，无编译步骤。
- 全程中文注释/回复（代码标识符除外）。
- SQLite 查询一律经 `queryRows<T>(dbPath, sql)`，字符串值用 `quoteSql(...)` 转义，禁止裸拼接用户输入。
- DB 路径：测试用 `join(HERE, '../../data/fund.db')`（真实库，只读查询）。
- worldRoot = dashboard 启动时的 `runtime` 目录；live run 状态在 `<worldRoot>/runs/live-*/state.json`。
- 拼接段标签必须保持：历史段 `segment='backtest'`，OOS 实盘段 `segment='daily_oos'`（`market-reports.html` 按此着色，不可改）；新 live 页用 `segment='live'`。
- 现有文件：`world/src/backtest-dashboard/server.ts`（含 `finiteNumber`、`queryRows`、`num`、`quoteSql`、`loadOosExtendedSeries` at ~1155、`loadBotForRun`、`listRunsForBot`、`OOS_BACKTEST_HISTORY_RUNS` at 1072、`OOS_HISTORY_START='2026-04-01'`）；`world/src/backtest-dashboard/runs.html`（`loadEvalRecords` 417、`ingestHistory` 425、`rowHtml` 628、`fetchBot` 754）。

---

### Task 1: 纯拼接函数 `stitchSeriesRows`

把 `loadOosExtendedSeries`（server.ts:1155-1204）里的「归一 + rebase」数学抽成不碰 DB 的纯函数，供 OOS 与 live 复用、可脱库单测。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（在 `finiteNumber`（~1086）之后、`loadOosExtendedSeries`（~1155）之前新增导出函数）
- Test: `world/test/stitch-series.test.ts`（新建）

**Interfaces:**
- Consumes: 模块内已有 `finiteNumber(v): number | null`。
- Produces: `export function stitchSeriesRows(historyRows: Array<Record<string, unknown>>, liveRows: Array<Record<string, unknown>>, opts?: { liveSegment?: string }): Array<Record<string, unknown>>` — 返回拼接后逐行，每行含改写后的 `net_value`（历史段归一到首点=1.0，实盘段 ×`lastHistoryNav/firstLiveNav`）、`cumulative_return_pct=(net_value-1)*100`、`segment`（`'backtest'` 或 `opts.liveSegment ?? 'live'`）、`raw_net_value`（原始净值）。

- [ ] **Step 1: 写失败测试**

```ts
// world/test/stitch-series.test.ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { stitchSeriesRows } from '../src/backtest-dashboard/server.ts'

test('历史段归一到首点=1.0，实盘段续接在历史末点上', () => {
  const history = [
    { trade_date: '2026-05-01', net_value: 2.0 },
    { trade_date: '2026-05-02', net_value: 2.2 },  // 相对首点 +10%
  ]
  const live = [
    { trade_date: '2026-07-22', net_value: 1.0 },   // live 自己的基线 1.0
    { trade_date: '2026-07-23', net_value: 1.05 },  // live 内 +5%
  ]
  const out = stitchSeriesRows(history, live)
  assert.equal(out.length, 4)
  assert.equal(out[0].segment, 'backtest')
  assert.equal(Number(out[0].net_value).toFixed(4), '1.0000')       // 首点归一
  assert.equal(Number(out[1].net_value).toFixed(4), '1.1000')       // +10%
  assert.equal(out[2].segment, 'live')
  assert.equal(Number(out[2].net_value).toFixed(4), '1.1000')       // 续接：= 历史末点
  assert.equal(Number(out[3].net_value).toFixed(4), '1.1550')       // 1.1 * 1.05
  assert.equal(Number(out[3].cumulative_return_pct).toFixed(2), '15.50')
  assert.equal(out[0].raw_net_value, 2.0)
})

test('空实盘段：退化为纯历史归一曲线', () => {
  const history = [
    { trade_date: '2026-05-01', net_value: 2.0 },
    { trade_date: '2026-05-02', net_value: 2.4 },
  ]
  const out = stitchSeriesRows(history, [])
  assert.equal(out.length, 2)
  assert.ok(out.every(r => r.segment === 'backtest'))
  assert.equal(Number(out[1].net_value).toFixed(4), '1.2000')
})

test('自定义 liveSegment 标签（OOS 用 daily_oos）', () => {
  const out = stitchSeriesRows(
    [{ trade_date: '2026-05-01', net_value: 1.0 }],
    [{ trade_date: '2026-07-22', net_value: 1.0 }],
    { liveSegment: 'daily_oos' },
  )
  assert.equal(out[1].segment, 'daily_oos')
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/stitch-series.test.ts`
Expected: FAIL — `stitchSeriesRows` 未从 server.ts 导出（`SyntaxError` / `undefined is not a function`）。

- [ ] **Step 3: 实现纯函数**

在 server.ts 的 `finiteNumber` 定义之后插入：

```ts
/** 把「历史净值序列 + 实盘净值序列」几何拼接成一条连续曲线（纯函数，不碰 DB）。
 *  历史段：首点净值归一到 1.0；实盘段：整体 ×(历史末点/实盘首点)，从历史末点续上。
 *  liveSegment 默认 'live'；OOS 传 'daily_oos' 以保持 market-reports.html 的着色。 */
export function stitchSeriesRows(
  historyRows: Array<Record<string, unknown>>,
  liveRows: Array<Record<string, unknown>>,
  opts: { liveSegment?: string } = {},
): Array<Record<string, unknown>> {
  const liveSegment = opts.liveSegment ?? 'live'
  const extended: Array<Record<string, unknown>> = []
  const firstHistoryNav = finiteNumber(historyRows[0]?.net_value)
  if (firstHistoryNav != null && firstHistoryNav > 0) {
    for (const row of historyRows) {
      const rawNav = finiteNumber(row.net_value)
      if (rawNav == null) continue
      const nav = rawNav / firstHistoryNav
      extended.push({ ...row, net_value: nav, cumulative_return_pct: (nav - 1) * 100, segment: 'backtest', raw_net_value: rawNav })
    }
  }
  const filteredLive = liveRows.filter(r => typeof r.trade_date === 'string' && finiteNumber(r.net_value) != null)
  const firstLiveNav = finiteNumber(filteredLive[0]?.net_value)
  const lastHistoryNav = finiteNumber(extended[extended.length - 1]?.net_value)
  const liveScale = firstLiveNav != null && firstLiveNav > 0 && lastHistoryNav != null && lastHistoryNav > 0
    ? lastHistoryNav / firstLiveNav : 1
  for (const row of filteredLive) {
    const rawNav = finiteNumber(row.net_value)
    if (rawNav == null) continue
    const nav = rawNav * liveScale
    extended.push({ ...row, net_value: nav, cumulative_return_pct: (nav - 1) * 100, segment: liveSegment, raw_net_value: rawNav })
  }
  return extended
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/stitch-series.test.ts`
Expected: PASS（3 个测试全绿）。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/stitch-series.test.ts
git commit -m "feat(dashboard): 抽出纯函数 stitchSeriesRows 供拼接曲线复用"
```

---

### Task 2: 拼接序列指标 `computeStitchedMetrics`

在拼接后的完整序列上算绝对收益 / 最大回撤（跑动峰值法）/ 252 年化，口径与 `loadAllRunsSummary`（server.ts:931-935）一致。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（紧接 Task 1 的 `stitchSeriesRows` 之后）
- Test: `world/test/stitch-series.test.ts`（追加）

**Interfaces:**
- Consumes: `finiteNumber`。
- Produces: `export function computeStitchedMetrics(extended: Array<Record<string, unknown>>): { absReturnPct: number | null; annReturnPct: number | null; maxDrawdownPct: number | null; days: number }`。

- [ ] **Step 1: 写失败测试（追加到 test/stitch-series.test.ts）**

```ts
import { computeStitchedMetrics } from '../src/backtest-dashboard/server.ts'

test('拼接指标：绝对收益 / 回撤 / 年化', () => {
  const series = [
    { trade_date: '2026-05-01', net_value: 1.0 },
    { trade_date: '2026-05-02', net_value: 1.2 },  // 峰值
    { trade_date: '2026-05-03', net_value: 1.08 }, // 从 1.2 回撤 -10%
    { trade_date: '2026-05-04', net_value: 1.26 }, // 末点 +26%
  ]
  const m = computeStitchedMetrics(series)
  assert.equal(m.days, 4)
  assert.equal(Number(m.absReturnPct).toFixed(2), '26.00')
  assert.equal(Number(m.maxDrawdownPct).toFixed(2), '-10.00')
  // 252 年化 = (1.26)^(252/4) - 1，为正
  assert.ok(m.annReturnPct != null && m.annReturnPct > 0)
})

test('拼接指标：空序列返回 null', () => {
  const m = computeStitchedMetrics([])
  assert.equal(m.days, 0)
  assert.equal(m.absReturnPct, null)
  assert.equal(m.maxDrawdownPct, null)
  assert.equal(m.annReturnPct, null)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/stitch-series.test.ts`
Expected: FAIL — `computeStitchedMetrics` 未定义。

- [ ] **Step 3: 实现**

紧接 `stitchSeriesRows` 之后插入：

```ts
/** 拼接后完整序列上的业绩指标：绝对收益（末点-1）、最大回撤（跑动峰值法）、252 年化。
 *  与 loadAllRunsSummary 同口径：base=1+abs/100，base>0 且 days>0 才算年化，否则 null。 */
export function computeStitchedMetrics(extended: Array<Record<string, unknown>>): {
  absReturnPct: number | null; annReturnPct: number | null; maxDrawdownPct: number | null; days: number
} {
  const navs: number[] = []
  for (const r of extended) { const n = finiteNumber(r.net_value); if (n != null) navs.push(n) }
  const days = navs.length
  if (!days) return { absReturnPct: null, annReturnPct: null, maxDrawdownPct: null, days: 0 }
  const absReturnPct = (navs[days - 1] - 1) * 100
  let peak = -Infinity, mdd = 0
  for (const n of navs) { if (n > peak) peak = n; const dd = n / peak - 1; if (dd < mdd) mdd = dd }
  const maxDrawdownPct = mdd * 100
  const base = 1 + absReturnPct / 100
  const annReturnPct = base > 0 && days > 0 ? (Math.pow(base, 252 / days) - 1) * 100 : null
  return { absReturnPct, annReturnPct, maxDrawdownPct, days }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/stitch-series.test.ts`
Expected: PASS（5 个测试全绿）。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/stitch-series.test.ts
git commit -m "feat(dashboard): computeStitchedMetrics 拼接序列业绩指标"
```

---

### Task 3: `loadOosExtendedSeries` 改用 `stitchSeriesRows`（零回归）

把现有 OOS 拼接改成薄封装，复用纯函数，同时保持输出字段完全一致（`segment` 用 `daily_oos`、带 `source_run_id`）。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts:1155-1204`（`loadOosExtendedSeries` 函数体）
- Test: `world/test/backtest-dashboard.test.ts`（已有 spawn 测试，跑通即验证不回归；本任务不新增断言）

**Interfaces:**
- Consumes: `stitchSeriesRows`（Task 1）、`OOS_BACKTEST_HISTORY_RUNS`、`OOS_HISTORY_START`、`queryRows`、`quoteSql`、`finiteNumber`。
- Produces: 保持 `loadOosExtendedSeries(dbPath, botId, runId, liveSeries)` 签名与返回结构不变（每行含 `segment`/`source_run_id`/`raw_net_value`/归一后的 `net_value`/`cumulative_return_pct`）。

- [ ] **Step 1: 记录改前基线（人工快照，用于比对）**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | tail -5`
Expected: 现有测试全绿（记下 pass 数作为基线）。

- [ ] **Step 2: 替换函数体**

把 server.ts:1155-1204 的整个 `loadOosExtendedSeries` 换成：

```ts
function loadOosExtendedSeries(dbPath: string, botId: string, runId: string, liveSeries: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  const historyRunId = OOS_BACKTEST_HISTORY_RUNS[botId]
  const liveRows = liveSeries.filter(r => typeof r.trade_date === 'string' && finiteNumber(r.net_value) != null)
  const firstLiveDate = typeof liveRows[0]?.trade_date === 'string' ? String(liveRows[0].trade_date) : ''
  const historyRows = historyRunId ? queryRows<Record<string, unknown>>(dbPath,
    'SELECT trade_date, total_value, net_value, daily_return_pct, cumulative_return_pct, ' +
    'max_drawdown_pct, equity_weight, bond_weight, gold_weight, cash_weight ' +
    'FROM fund_bot_daily_snapshots WHERE bot_id = ' + quoteSql(botId) +
    ' AND run_id = ' + quoteSql(historyRunId) +
    ' AND trade_date >= ' + quoteSql(OOS_HISTORY_START) +
    (firstLiveDate ? ' AND trade_date < ' + quoteSql(firstLiveDate) : '') +
    ' ORDER BY trade_date ASC') : []

  const extended = stitchSeriesRows(historyRows, liveRows, { liveSegment: 'daily_oos' })
  if (!extended.length) {
    return liveRows.map(row => ({ ...row, segment: 'daily_oos', source_run_id: runId, raw_net_value: row.net_value }))
  }
  // 保持原字段：历史段挂 historyRunId、实盘段挂当前 runId。
  return extended.map(r => ({ ...r, source_run_id: r.segment === 'backtest' ? historyRunId : runId }))
}
```

- [ ] **Step 3: 跑 OOS 相关测试确认不回归**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | tail -5`
Expected: PASS 数与 Step 1 基线一致。

- [ ] **Step 4: 冒烟核对 OOS 曲线段字段**

Run:
```bash
cd world && node --experimental-strip-types -e "
const { stitchSeriesRows } = await import('./src/backtest-dashboard/server.ts')
const out = stitchSeriesRows([{trade_date:'2026-04-01',net_value:1.0}],[{trade_date:'2026-07-22',net_value:1.0}],{liveSegment:'daily_oos'})
console.log(JSON.stringify(out.map(r=>r.segment)))
"
```
Expected: 输出 `["backtest","daily_oos"]`。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts
git commit -m "refactor(dashboard): loadOosExtendedSeries 复用 stitchSeriesRows，保持段标签不变"
```

---

### Task 4: live run 发现与源映射 `listLiveRuns` / `readLiveRunLink`

扫 `<worldRoot>/runs/live-*/state.json`，取每个 live run 的单 bot 与 `source_run_id`。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（新增两个导出函数 + 常量，放在 `finiteNumber` 附近）
- Test: `world/test/live-runs.test.ts`（新建，用临时 worldRoot 造 state.json）

**Interfaces:**
- Consumes: 模块顶部已 `import { existsSync, readdirSync, readFileSync } from 'node:fs'`；`join` 来自 `node:path`（确认已导入，否则补 `import { join } from 'node:path'`）。
- Produces:
  - `export function isLiveRunId(runId: string): boolean` — `runId.startsWith('live-')`。
  - `export function readLiveRunLink(worldRoot: string, liveRunId: string): { botId: string; sourceRunId: string } | null` — 读单个 `runs/<liveRunId>/state.json`，取 `bots[0]` 与 `source_run_id`；缺任一 → null。
  - `export function listLiveRuns(worldRoot: string): Array<{ liveRunId: string; botId: string; sourceRunId: string }>` — 遍历 `runs/` 下 `live-` 前缀目录，逐个 `readLiveRunLink`，跳过 null。

- [ ] **Step 1: 写失败测试**

```ts
// world/test/live-runs.test.ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { isLiveRunId, readLiveRunLink, listLiveRuns } from '../src/backtest-dashboard/server.ts'

function makeWorld(): string {
  const world = mkdtempSync(join(tmpdir(), 'world-'))
  const mk = (rid: string, body: unknown) => {
    const d = join(world, 'runs', rid); mkdirSync(d, { recursive: true })
    writeFileSync(join(d, 'state.json'), JSON.stringify(body))
  }
  mk('live-bot10-20260616T095923', { run_id: 'live-bot10-20260616T095923', bots: ['bot10'], source_run_id: 'dash-2026-06-16T09-59-23' })
  mk('live-bot11-20260610T031936', { run_id: 'live-bot11-20260610T031936', bots: ['bot11'], source_run_id: 'dash-2026-06-10T03-19-36' })
  mk('dash-2026-06-16T09-59-23', { run_id: 'dash-2026-06-16T09-59-23', bots: ['bot10'] }) // 非 live，应忽略
  mk('live-broken', { bots: [] })  // 缺字段，应跳过
  return world
}

test('isLiveRunId 只认 live- 前缀', () => {
  assert.equal(isLiveRunId('live-bot10-x'), true)
  assert.equal(isLiveRunId('dash-2026-06-16T09-59-23'), false)
})

test('readLiveRunLink 取单 bot 与 source_run_id', () => {
  const world = makeWorld()
  try {
    const link = readLiveRunLink(world, 'live-bot10-20260616T095923')
    assert.deepEqual(link, { botId: 'bot10', sourceRunId: 'dash-2026-06-16T09-59-23' })
    assert.equal(readLiveRunLink(world, 'live-broken'), null)
    assert.equal(readLiveRunLink(world, 'live-does-not-exist'), null)
  } finally { rmSync(world, { recursive: true, force: true }) }
})

test('listLiveRuns 只列有效 live run', () => {
  const world = makeWorld()
  try {
    const runs = listLiveRuns(world).sort((a, b) => a.liveRunId.localeCompare(b.liveRunId))
    assert.equal(runs.length, 2)
    assert.equal(runs[0].botId, 'bot10')
    assert.equal(runs[1].sourceRunId, 'dash-2026-06-10T03-19-36')
  } finally { rmSync(world, { recursive: true, force: true }) }
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/live-runs.test.ts`
Expected: FAIL — 三个函数未导出。

- [ ] **Step 3: 实现**

在 server.ts 的 `finiteNumber` 之后插入（若文件顶部未 `import { join } from 'node:path'` 则补上）：

```ts
export function isLiveRunId(runId: string): boolean {
  return runId.startsWith('live-')
}

/** 读单个 live run 的 state.json → { botId=bots[0], sourceRunId=source_run_id }；缺字段返回 null。 */
export function readLiveRunLink(worldRoot: string, liveRunId: string): { botId: string; sourceRunId: string } | null {
  const path = join(worldRoot, 'runs', liveRunId, 'state.json')
  if (!existsSync(path)) return null
  try {
    const st = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
    const bots = Array.isArray(st.bots) ? st.bots : []
    const botId = typeof bots[0] === 'string' ? bots[0] : ''
    const sourceRunId = typeof st.source_run_id === 'string' ? st.source_run_id : ''
    if (!botId || !sourceRunId) return null
    return { botId, sourceRunId }
  } catch { return null }
}

/** 遍历 <worldRoot>/runs 下 live- 前缀目录，返回全部有效 live run 的 {liveRunId, botId, sourceRunId}。 */
export function listLiveRuns(worldRoot: string): Array<{ liveRunId: string; botId: string; sourceRunId: string }> {
  const runsDir = join(worldRoot, 'runs')
  let names: string[]
  try { names = readdirSync(runsDir) } catch { return [] }
  const out: Array<{ liveRunId: string; botId: string; sourceRunId: string }> = []
  for (const name of names) {
    if (!isLiveRunId(name)) continue
    const link = readLiveRunLink(worldRoot, name)
    if (link) out.push({ liveRunId: name, ...link })
  }
  return out
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/live-runs.test.ts`
Expected: PASS（3 个测试全绿）。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/live-runs.test.ts
git commit -m "feat(dashboard): listLiveRuns/readLiveRunLink 从 state.json 取源映射"
```

---

### Task 5: `loadLiveRunsSummary` + `/api/backtest/live-runs` 端点

对每个 live run：拼接源 dash 与 live 净值 → 算指标 → 输出汇总；挂 GET 路由。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（新增 `loadLiveRunsSummary`，放在 `loadAllRunsSummary`（~958）之后；在路由区 `/api/backtest/all-runs`（~1470）之后加 GET 分支）
- Test: `world/test/backtest-dashboard.test.ts`（追加一个 spawn 用例断言端点返回结构）

**Interfaces:**
- Consumes: `listLiveRuns`（Task 4）、`stitchSeriesRows`（Task 1）、`computeStitchedMetrics`（Task 2）、`queryRows`、`quoteSql`。
- Produces: `export async function loadLiveRunsSummary(dbPath: string, worldRoot: string): Promise<{ runs: Array<{ liveRunId: string; botId: string; sourceRunId: string; absReturnPct: number | null; annReturnPct: number | null; maxDrawdownPct: number | null; liveDays: number; lastLiveDate: string; status: string }> }>`；GET `/api/backtest/live-runs`。

- [ ] **Step 1: 写失败测试（追加到 test/backtest-dashboard.test.ts）**

```ts
test('/api/backtest/live-runs 返回 live run 汇总', async () => {
  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /backtest dashboard listening on http:\/\//)
    const base = `http://127.0.0.1:${out.match(/http:\/\/127\.0\.0\.1:(\d+)\//)![1]}`
    const r = await fetch(`${base}/api/backtest/live-runs`, { cache: 'no-store' })
    assert.equal(r.status, 200)
    const body = await r.json() as { runs: Array<Record<string, unknown>> }
    assert.ok(Array.isArray(body.runs))
    for (const run of body.runs) {
      assert.equal(typeof run.liveRunId, 'string')
      assert.ok(String(run.liveRunId).startsWith('live-'))
      assert.equal(typeof run.sourceRunId, 'string')
      assert.equal(typeof run.liveDays, 'number')
      assert.ok('absReturnPct' in run && 'maxDrawdownPct' in run)
    }
  } finally { proc.kill() }
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | grep -A3 live-runs`
Expected: FAIL — 端点 404 → `assert.equal(r.status, 200)` 失败。

- [ ] **Step 3: 实现汇总函数**

在 `loadAllRunsSummary` 之后插入：

```ts
/** /api/backtest/live-runs：54 个 live run 的拼接汇总。每 run 取源 dash + live 两段净值几何拼接，
 *  在完整序列上算 绝对收益/年化/最大回撤。liveDays=0（今日首次 decide 前）时退化为源 dash 曲线本身，
 *  指标即源 dash 的；status 标 '待启动'。评级/点评由前端按 sourceRunId 继承，本接口不含。 */
export async function loadLiveRunsSummary(dbPath: string, worldRoot: string): Promise<{ runs: Array<Record<string, unknown>> }> {
  const links = listLiveRuns(worldRoot)
  const runs: Array<Record<string, unknown>> = []
  const fetchSeries = (botId: string, runId: string) => queryRows<Record<string, unknown>>(dbPath,
    'SELECT trade_date, net_value FROM fund_bot_daily_snapshots WHERE bot_id = ' + quoteSql(botId) +
    ' AND run_id = ' + quoteSql(runId) + ' ORDER BY trade_date ASC')
  for (const { liveRunId, botId, sourceRunId } of links) {
    const historyRows = fetchSeries(botId, sourceRunId)
    const liveRows = fetchSeries(botId, liveRunId)
    const extended = stitchSeriesRows(historyRows, liveRows)
    const m = computeStitchedMetrics(extended)
    const liveDays = liveRows.length
    const lastLiveDate = liveDays ? String(liveRows[liveRows.length - 1].trade_date) : ''
    runs.push({
      liveRunId, botId, sourceRunId,
      absReturnPct: m.absReturnPct, annReturnPct: m.annReturnPct, maxDrawdownPct: m.maxDrawdownPct,
      liveDays, lastLiveDate, status: liveDays ? '实盘中' : '待启动',
    })
  }
  return { runs }
}
```

- [ ] **Step 4: 挂路由**

在 server.ts 路由区 `/api/backtest/all-runs` 分支之后插入：

```ts
      if (req.method === 'GET' && url.pathname === '/api/backtest/live-runs') {
        sendJson(res, 200, await loadLiveRunsSummary(dbPath, worldRoot))
        return
      }
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | tail -6`
Expected: PASS，含新 live-runs 用例（当前 liveDays 多为 0、status='待启动'）。

- [ ] **Step 6: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): loadLiveRunsSummary + /api/backtest/live-runs 拼接汇总端点"
```

---

### Task 6: `runs.html` 只列 live run + 继承源 dash 评测

反转过滤：不再渲染 dash 行；评测表（含 dash）只进 lookup；每个 live run 克隆源 dash 评测记录、覆盖 run_id 与拼接指标。

**Files:**
- Modify: `world/src/backtest-dashboard/runs.html`（`loadEvalRecords` 417-422、`ingestHistory` 425-453；`rowHtml` 636 的选中提示文案可不动）
- Test: 手动端到端（见 Step 4/5；纯前端无单测框架）

**Interfaces:**
- Consumes: `/api/backtest/run-evals`（评测表，含 dash）、`/api/backtest/live-runs`（Task 5）、已有 `buildEvalRecord`/`computeDefaultPass`/`isPass`/`buildIndexSidebar`/`render`/`num`。
- Produces: `records` 只含 `_source==='live'` 的行；新增模块级 `const evalByKey = new Map()`（`run_id|bot` → 原始评测 record）。

- [ ] **Step 1: 评测表改为「只进 lookup、不建行」**

把 `loadEvalRecords`（417-422）替换为：

```js
// 评测记录（fund_bot_run_eval，含 dash）：不再直接成行，只建 lookup 供 live run 继承评级/点评/指数归类。
const evalByKey = new Map()   // `run_id|bot` -> buildEvalRecord 后的记录
async function loadEvalRecords(){
  let rows = []
  try { const r = await fetch('/api/backtest/run-evals', { cache: 'no-store' }); if (r.ok) rows = (await r.json()).rows || [] }
  catch { rows = [] }
  for (const raw of rows){ const rec = buildEvalRecord(raw); if (rec) evalByKey.set(rec._key, rec) }
}
```

- [ ] **Step 2: `ingestHistory` → `ingestLive`（继承源 dash + 拼接指标）**

把 `ingestHistory`（425-453）整体替换为：

```js
// 只纳入 live run（/api/backtest/live-runs）。每个 live run 克隆其 source dash 的评测记录（评级/点评/
// 指数归类），覆盖 run_id、bot、指标（按拼接序列重算）与合格判定（继承源 dash 的 isPass）。
async function ingestLive(){
  let data
  try { const r = await fetch('/api/backtest/live-runs', { cache: 'no-store' }); if (!r.ok) return; data = await r.json() }
  catch { return }
  const have = new Set(records.map(r => r._key))
  let added = 0
  for (const h of (data.runs || [])){
    const key = `${h.liveRunId}|${h.botId}`
    if (have.has(key)) continue
    const srcKey = `${h.sourceRunId}|${h.botId}`
    const src = evalByKey.get(srcKey)
    // 基底：优先克隆源 dash 评测记录（继承评级/点评/指数/可买基金/状态）；查不到则最小降级。
    const r = src ? { ...src } : {
      '策略': '', '对标指数': '', '可买基金': '', '状态': '',
      '普通投资者评级': '—', '综合点评': '', _ix: (typeof UNCLASSED !== 'undefined' ? UNCLASSED : '未分类'), _ixName: '未分类', _ixCode: '',
    }
    // 覆盖为 live 行身份 + 拼接指标。
    r._source = 'live'
    r._key = key
    r.run_id = h.liveRunId
    r.bot = h.botId
    r._sourceRunId = h.sourceRunId
    r['状态'] = h.status || r['状态'] || ''
    r['绝对收益%'] = h.absReturnPct ?? ''
    r['年化收益%'] = h.annReturnPct ?? ''
    r['最大回撤%'] = h.maxDrawdownPct ?? ''
    // 卡玛 = 年化/|最大回撤|（与 CSV 行同口径）。
    { const ann = num(r['年化收益%']), dd = num(r['最大回撤%'])
      r._calmar = (ann != null && ann > 0 && dd != null && dd < 0) ? ann / Math.abs(dd) : null }
    // 合格判定：继承源 dash 的最终 isPass（含他人手动覆盖）；查不到源则默认合格。
    r._defaultPass = src ? isPass(src) : true
    r._launch = src ? src._launch : null
    records.push(r); have.add(key); added++
  }
  if (added){ buildIndexSidebar(); render() }
}
```

- [ ] **Step 3: 改调用点**

在文件底部启动流程里，把调用 `ingestHistory()` 的那一处改为 `ingestLive()`。

Run 定位: `cd world && grep -n 'ingestHistory' src/backtest-dashboard/runs.html`
Expected: 找到调用点（loadEvalRecords 之后）；把 `ingestHistory` 改为 `ingestLive`，并确保 `loadEvalRecords()` 在其之前 await 完成（保证 `evalByKey` 已就绪）。

- [ ] **Step 4: 起 dashboard 端到端核对（只列 live、继承评级）**

Run:
```bash
cd world && node --experimental-strip-types src/backtest-dashboard/server.ts --host 127.0.0.1 --port 18899 --db ../data/fund.db &
sleep 1
curl -s http://127.0.0.1:18899/api/backtest/live-runs | head -c 300
```
Expected: JSON 含 `runs` 数组、每项 `liveRunId` 以 `live-` 开头。浏览器打开 `http://127.0.0.1:18899/backtest-dashboard/runs.html` 应只见 live run 行（run_id 列全为 `live-…`），评级/点评与其源 dash 一致；今日各行状态「待启动」。核对后 `kill %1`。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/runs.html
git commit -m "feat(dashboard): runs.html 只列 live run，继承源 dash 评测、拼接指标"
```

---

### Task 7: `/api/backtest/bot` live 分支 → 拼接详情曲线

live run 行点开的右侧净值图走拼接后的连续曲线（源段 + 实盘段）。做法：委托源 dash run 的 `loadBotForRun` 拿基底（基准线/持仓/买卖点），把 `series` 换成拼接序列。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（新增 `loadLiveBotForRun`，放在 `loadBotForRun` 之后；在 `/api/backtest/bot` 路由分支（~1540）前加 live 分支）
- Test: `world/test/backtest-dashboard.test.ts`（追加断言：live run_id 请求返回带 `segment` 的 series）

**Interfaces:**
- Consumes: `readLiveRunLink`（Task 4）、`loadBotForRun`、`stitchSeriesRows`（Task 1）、`listRunsForBot`、`queryRows`、`quoteSql`、`isLiveRunId`。
- Produces: `async function loadLiveBotForRun(dbPath: string, worldRoot: string, liveRunId: string): Promise<Record<string, unknown> | null>` — 返回与 `loadBotForRun` 同形的对象，但 `runId=liveRunId`、`series` 为拼接序列（含 `segment` 字段）。

- [ ] **Step 1: 写失败测试（追加到 test/backtest-dashboard.test.ts）**

```ts
test('/api/backtest/bot 支持 live run，返回拼接 series', async () => {
  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /backtest dashboard listening on http:\/\//)
    const base = `http://127.0.0.1:${out.match(/http:\/\/127\.0\.0\.1:(\d+)\//)![1]}`
    const list = await (await fetch(`${base}/api/backtest/live-runs`, { cache: 'no-store' })).json() as { runs: Array<Record<string, unknown>> }
    if (!list.runs.length) return  // 无 live run 环境则跳过（不视为失败）
    const one = list.runs[0]
    const r = await fetch(`${base}/api/backtest/bot?bot_id=${one.botId}&run_id=${encodeURIComponent(String(one.liveRunId))}`, { cache: 'no-store' })
    assert.equal(r.status, 200)
    const bot = await r.json() as { runId: string; series: Array<Record<string, unknown>> }
    assert.equal(bot.runId, one.liveRunId)
    assert.ok(Array.isArray(bot.series))
    if (bot.series.length) assert.ok(bot.series.every(p => p.segment === 'backtest' || p.segment === 'live'))
  } finally { proc.kill() }
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | grep -A3 '拼接 series'`
Expected: FAIL — 当前 `/api/backtest/bot` 对 live run_id 走 `listRunsForBot`（排除 live）→ 404。

- [ ] **Step 3: 实现 `loadLiveBotForRun`**

在 `loadBotForRun` 之后插入：

```ts
/** live run 的 bot 详情：委托源 dash run 拿基底（基准/持仓/买卖点/series），把 series 换成
 *  「源 dash + live」拼接序列（带 segment）。live 段的买卖点/持仓叠加留待后续，不影响连续曲线展示。 */
async function loadLiveBotForRun(dbPath: string, worldRoot: string, liveRunId: string): Promise<Record<string, unknown> | null> {
  const link = readLiveRunLink(worldRoot, liveRunId)
  if (!link) return null
  const { botId, sourceRunId } = link
  const sourceRuns = await listRunsForBot(dbPath, worldRoot, botId)   // dash 源 run 未被 live 过滤排除
  const base = await loadBotForRun(dbPath, worldRoot, botId, sourceRunId, sourceRuns)
  if (!base) return null
  const historyRows = Array.isArray(base.series) ? base.series as Array<Record<string, unknown>> : []
  const liveRows = queryRows<Record<string, unknown>>(dbPath,
    'SELECT trade_date, total_value, net_value, daily_return_pct, cumulative_return_pct, ' +
    'max_drawdown_pct, equity_weight, bond_weight, gold_weight, cash_weight ' +
    'FROM fund_bot_daily_snapshots WHERE bot_id = ' + quoteSql(botId) +
    ' AND run_id = ' + quoteSql(liveRunId) + ' ORDER BY trade_date ASC')
  const series = stitchSeriesRows(historyRows, liveRows)
  return {
    ...base,
    runId: liveRunId,
    series,
    availableRuns: [{ runId: liveRunId, latestDate: liveRows.length ? String(liveRows[liveRows.length - 1].trade_date) : (base.latestTradeDate ?? '') }],
  }
}
```

- [ ] **Step 4: 在 `/api/backtest/bot` 路由加 live 分支**

在 `/api/backtest/bot` 分支体最前面（取到 `botId`/`runId`、非空校验之后、`listRunsForBot` 之前）插入：

```ts
        if (isLiveRunId(runId)) {
          const liveBot = await loadLiveBotForRun(dbPath, worldRoot, runId)
          if (!liveBot) { sendJson(res, 404, { error: 'live run not found' }); return }
          sendJson(res, 200, liveBot)
          return
        }
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | tail -6`
Expected: PASS（含新拼接 series 用例；无 live 数据环境下该用例提前 return 也算过）。

- [ ] **Step 6: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): /api/backtest/bot 支持 live run，返回拼接详情曲线"
```

---

## 验证（全量回归）

- [ ] **全测**：`cd world && node --experimental-strip-types --test test/stitch-series.test.ts test/live-runs.test.ts test/backtest-dashboard.test.ts` 全绿。
- [ ] **不回归**：既有 `test/backtest-dashboard.test.ts` 原有用例数不减；`market-reports.html` 的 OOS 卡（bot101/102/103）曲线段仍按 `backtest`/`daily_oos` 着色（起服务肉眼核对 agents 页 OOS 卡）。
- [ ] **端到端**：起 dashboard，`runs.html` 只列 live run、评级继承源 dash、今日状态「待启动」、指标=各自源 dash；今天 18:30 settle 后挑一个 run 复查 `liveDays≥1`、右侧曲线源段后接上实盘段。
- [ ] **孤儿降级**：若某 live run 的 `source_run_id` 不在评测表 → 该行无评级、指标仍按拼接算、默认合格，不报错。

## 后续（不在本计划范围）

- live 段的买卖点 marker 与持仓堆叠叠加（Task 7 目前只连净值线）。
- live run 的「重评」UI（本次按用户决定：只继承、不重评）。
