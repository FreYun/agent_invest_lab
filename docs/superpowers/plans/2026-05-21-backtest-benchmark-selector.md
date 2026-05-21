# 回测看板:参照曲线可选 + 空仓日持仓修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 bot 回测看板的参照曲线对未建仓 bot 也能显示、能在前端切换宽基指数,并修复悬停空仓日时持仓面板回退到最新持仓的 bug。

**Architecture:** 后端 `server.ts` 把参照基准的「选标的」与「拉净值序列」拆成两个函数,新增一个轻量 `/api/backtest/benchmark` 接口按需返回任意基金的归一化序列;前端 `index.html` 用一个不计算进 `bot.benchmark`、而是叠加在其上的「有效基准」选择层,改图例下拉框时只重渲染当前详情。

**Tech Stack:** Node 22 (`--experimental-strip-types` 跑 TS),`node:test` + `node:assert/strict`,纯 SVG 前端(无构建步骤,inline JS in `index.html`),sqlite3 CLI(经 `queryRows` 调用),Playwright(浏览器实测)。

**Spec:** `docs/superpowers/specs/2026-05-21-backtest-benchmark-selector-design.md`

**Precondition:** 同日买卖点合并的改动已在 `world/src/backtest-dashboard/index.html` 里(未提交)。开始本计划前先把它单独提交,保持分支干净:
```bash
git add world/src/backtest-dashboard/index.html
git commit -m "fix(dashboard): merge same-day same-side trade markers; align crosshair"
```

---

## File Structure

- `world/src/backtest-dashboard/server.ts` — 改:拆出 `pickBenchmarkFund`(纯函数,导出)+ `loadBenchmarkSeries`(DB 查询);`loadBenchmark` 改为调用二者;新增默认指数兜底常量;路由加 `/api/backtest/benchmark`。
- `world/test/backtest-dashboard.test.ts` — 改:加 `pickBenchmarkFund` 单测 + `/api/backtest/benchmark` 接口集成测试。
- `world/src/backtest-dashboard/index.html` — 改:`renderHoldings` 空仓日修复;新增 `effectiveBenchmark()` 选择层、图例下拉、`onChange` 拉取与重渲染、`state` 字段、`selectBot` 重置。

---

## Task 1: 默认指数兜底(server,纯函数 TDD)

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`(`loadBenchmark`,约 `:206-244`)
- Test: `world/test/backtest-dashboard.test.ts`

- [ ] **Step 1: 写失败测试**

在 `world/test/backtest-dashboard.test.ts` 顶部 import 区加:
```ts
import { pickBenchmarkFund } from '../src/backtest-dashboard/server.ts'
```
在文件末尾(最后一个 `test(...)` 之后)追加:
```ts
test('pickBenchmarkFund: 首笔 buy 的基金优先', () => {
  const actions = [
    { side: 'sell', fund_code: 'A' },
    { side: 'buy', fund_code: 'B' },
    { side: 'buy', fund_code: 'C' },
  ] as unknown as Parameters<typeof pickBenchmarkFund>[0]
  assert.equal(pickBenchmarkFund(actions, [] as never[]), 'B')
})

test('pickBenchmarkFund: 无 buy 时退到首个持仓', () => {
  const actions = [{ side: 'hold', fund_code: 'A' }] as unknown as Parameters<typeof pickBenchmarkFund>[0]
  const holdings = [{ fund_code: 'H1' }, { fund_code: 'H2' }] as unknown as Parameters<typeof pickBenchmarkFund>[1]
  assert.equal(pickBenchmarkFund(actions, holdings), 'H1')
})

test('pickBenchmarkFund: 未建仓退到默认指数沪深300', () => {
  assert.equal(pickBenchmarkFund([] as never[], [] as never[]), '510300')
})
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd world && node --experimental-strip-types --test --test-name-pattern='pickBenchmarkFund' test/backtest-dashboard.test.ts`
Expected: FAIL —— `pickBenchmarkFund` 未从 server.ts 导出(`SyntaxError`/`undefined is not a function` 或导入报错)。

- [ ] **Step 3: 实现**

在 `server.ts` 中 `loadBenchmark` 函数**上方**新增常量与导出的纯函数:
```ts
// 未建仓 bot 没有首买基金时,用沪深300(510300 沪深300ETF华泰柏瑞)作默认参照,
// 这样任何 bot 都至少有一条参照曲线。
const DEFAULT_BENCHMARK_FUND = '510300'

export function pickBenchmarkFund(actions: BotAction[], holdings: HoldingRow[]): string {
  const firstBuy = actions.find(a => a.side === 'buy')
  return firstBuy?.fund_code || holdings[0]?.fund_code || DEFAULT_BENCHMARK_FUND
}
```

把 `loadBenchmark` 开头的挑选逻辑替换为调用它。原代码:
```ts
  // Pick fund_code: first buy action wins; else fall back to first holding.
  const firstBuy = actions.find(a => a.side === 'buy')
  const fundCode = firstBuy?.fund_code || holdings[0]?.fund_code
  if (!fundCode) return null
```
改为:
```ts
  // Pick fund_code: first buy wins; else first holding; else default broad index.
  const fundCode = pickBenchmarkFund(actions, holdings)
```
（删掉 `if (!fundCode) return null`,因为现在恒有值;`anchorDate` 那行里 `firstBuy?.action_date` 改成 `actions.find(a => a.side === 'buy')?.action_date`。）

- [ ] **Step 4: 跑测试,确认通过**

Run: `cd world && node --experimental-strip-types --test --test-name-pattern='pickBenchmarkFund' test/backtest-dashboard.test.ts`
Expected: PASS（3 个用例全过）。

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts`
Expected: 既有的 `backtest dashboard serves data` 仍 PASS（每个 bot 的 `benchmark` 现在恒非 null）。
```bash
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): default to CSI300 benchmark for un-invested bots"
```

---

## Task 2: 按需基准接口(server,集成测试)

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`(拆 `loadBenchmarkSeries`;路由新增 `/api/backtest/benchmark`,约 `:496-507`)
- Test: `world/test/backtest-dashboard.test.ts`

- [ ] **Step 1: 写失败测试**

在 `backtest-dashboard.test.ts` 的 `test('backtest dashboard serves data', ...)` 里,`badRes` 那段断言**之前**插入:
```ts
    // /api/backtest/benchmark: 任意基金按 [from,to] 归一化,起点对齐 1.0
    const bmRes = await fetch(`${base}/api/backtest/benchmark?fund=510300&from=2025-01-02&to=2025-06-30`)
    assert.equal(bmRes.status, 200)
    const bm = await bmRes.json() as { fundCode: string; series: Array<{ trade_date: string; net_value: number }> } | null
    assert.ok(bm && bm.fundCode === '510300', 'benchmark endpoint returns the requested fund')
    assert.ok(bm.series.length > 0, 'benchmark series non-empty')
    assert.ok(Math.abs(bm.series[0].net_value - 1) < 1e-9, 'benchmark normalized to 1.0 at start')
    for (let i = 1; i < bm.series.length; i++) {
      assert.ok(bm.series[i].trade_date >= bm.series[i - 1].trade_date, 'benchmark series sorted by date')
    }

    // 缺参数 → 400
    const bmBad = await fetch(`${base}/api/backtest/benchmark?fund=510300`)
    assert.equal(bmBad.status, 400)
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts`
Expected: FAIL —— `/api/backtest/benchmark` 当前返回 404,断言 `status===200` 不成立。

- [ ] **Step 3: 实现 —— 拆出 `loadBenchmarkSeries`**

把 `loadBenchmark` 里「查 nav + 归一化」的部分抽成独立函数。在 `loadBenchmark` 上方新增:
```ts
async function loadBenchmarkSeries(
  dbPath: string,
  fundCode: string,
  anchorDate: string,
  endDate: string,
): Promise<BotBenchmark | null> {
  if (!fundCode || !anchorDate || !endDate) return null
  const rows = await queryRows<{ nav_date: string; nav: number | null; fund_name: string | null }>(dbPath, `
    SELECT n.nav_date, n.nav, COALESCE(i.fund_name, n.fund_code) AS fund_name
    FROM fund_nav n
    LEFT JOIN fund_info i ON i.fund_code = n.fund_code
    WHERE n.fund_code = ${quoteSql(fundCode)}
      AND n.nav_date >= ${quoteSql(anchorDate)}
      AND n.nav_date <= ${quoteSql(endDate)}
      AND n.nav IS NOT NULL
    ORDER BY n.nav_date ASC
  `)
  if (!rows.length) return null
  const baselineNav = num(rows[0].nav)
  if (baselineNav <= 0) return null
  const fundName = rows[0].fund_name ?? fundCode
  const series: BenchmarkPoint[] = rows.map(r => ({
    trade_date: r.nav_date,
    nav: num(r.nav),
    net_value: num(r.nav) / baselineNav,
  }))
  return { fundCode, fundName, anchorDate, baselineNav, series }
}
```
把 `loadBenchmark` 改成薄封装(挑标的 + 算 anchor + 委托):
```ts
async function loadBenchmark(
  dbPath: string,
  actions: BotAction[],
  holdings: HoldingRow[],
  firstTradeDate: string,
  latestTradeDate: string,
): Promise<BotBenchmark | null> {
  const fundCode = pickBenchmarkFund(actions, holdings)
  const anchorDate = firstTradeDate || actions.find(a => a.side === 'buy')?.action_date || ''
  if (!anchorDate || !latestTradeDate) return null
  return loadBenchmarkSeries(dbPath, fundCode, anchorDate, latestTradeDate)
}
```

- [ ] **Step 4: 实现 —— 路由加接口**

在 `server.ts` 的请求处理里,`/api/backtest/bot` 分支之后、最后的 `sendJson(res, 404, ...)` 之前,插入:
```ts
      if (req.method === 'GET' && url.pathname === '/api/backtest/benchmark') {
        const fund = url.searchParams.get('fund') ?? ''
        const from = url.searchParams.get('from') ?? ''
        const to = url.searchParams.get('to') ?? ''
        if (!fund || !from || !to) { sendJson(res, 400, { error: 'fund, from, to required' }); return }
        sendJson(res, 200, await loadBenchmarkSeries(dbPath, fund, from, to))
        return
      }
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts`
Expected: PASS（含新加的 benchmark 接口断言 + 既有断言）。

- [ ] **Step 6: 类型检查 + 提交**

Run: `cd world && npx tsc -p tsconfig.json --noEmit`
Expected: 无错误。
```bash
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): add on-demand /api/backtest/benchmark endpoint"
```

---

## Task 3: 空仓日持仓面板修复(frontend,浏览器实测)

**Files:**
- Modify: `world/src/backtest-dashboard/index.html`(`renderHoldings`,约 `:544-548`)

- [ ] **Step 1: 改实现**

把 `renderHoldings` 开头的回退逻辑:
```js
function renderHoldings(bot, dateOverride){
  const list = dateOverride && bot.holdingsByDate && bot.holdingsByDate[dateOverride]
    ? bot.holdingsByDate[dateOverride]
    : bot.holdings
  if (!list || !list.length) return '<div class="empty">没有持仓快照</div>'
```
改为:
```js
function renderHoldings(bot, dateOverride){
  // 传了具体日期就严格用当天快照:无记录=当日空仓,绝不回退到最新持仓。
  // 只有默认视图(dateOverride 为空)才显示最新持仓。
  const list = dateOverride ? (bot.holdingsByDate?.[dateOverride] || []) : bot.holdings
  if (!list || !list.length) return `<div class="empty">${dateOverride ? '当日空仓' : '没有持仓快照'}</div>`
```

- [ ] **Step 2: 起服务**

Run: `cd world && npm run backtest -- --port 48088`（后台运行;用 `--port 48088` 避开默认端口冲突）
Expected: 输出 `backtest dashboard listening on http://0.0.0.0:48088/`。

- [ ] **Step 3: 浏览器实测(Playwright)**

用 Playwright 打开 `http://127.0.0.1:48088/`,选一个建仓前有空仓日的 bot(如 bot11),把光标移到建仓前的早期日期(净值平台期、tooltip 显示「总仓位 0.0%」的那段),用 `browser_evaluate` 读取:
```js
() => {
  const title = document.getElementById('holdingsTitle')?.textContent || ''
  const body = document.getElementById('holdingsBody')?.textContent || ''
  return { title, body };
}
```
Expected:`title` 形如「持仓快照（2025-01-xx）」,`body` 含「当日空仓」,**不**含最新持仓的基金名/权重。
再把光标移到有持仓的日期,确认 `body` 显示当天的持仓快照(有基金行、权重)。

- [ ] **Step 4: 提交**

```bash
git add world/src/backtest-dashboard/index.html
git commit -m "fix(dashboard): show empty position on no-holding days, not stale latest"
```

---

## Task 4: 前端参照曲线选择器(frontend,浏览器实测)

**Files:**
- Modify: `world/src/backtest-dashboard/index.html`(`state` 初始化;`effectiveBenchmark` 新增;`renderChart` `:247-328`;`renderMetricsPanel` `:587-600`;`attachCursor` `:446-462`;`selectBot` `:658-663`;全局 `change` 监听 `:762-778`)

**说明 — 「有效基准」选择层:** 不改 `bot.benchmark`(它始终是服务端默认基准),而是叠加一个选择层。`state.benchSel` 记当前选择(`'default'` 或基金代码),切 bot 时重置;`state.benchCache` 缓存已拉取的非默认基准,避免 10s 轮询时重复请求。渲染处统一读 `effectiveBenchmark(bot)`。

- [ ] **Step 1: state 加字段**

找到 `state` 对象定义(含 `selected`、`selectedRuns`、`expandedActions` 等字段),加入:
```js
  benchSel: 'default',       // 当前参照基准选择:'default' 或基金代码
  benchCache: {},            // `${botId}|${fundCode}` -> benchmark 对象(避免轮询重复拉取)
```

- [ ] **Step 2: 加常量 + `effectiveBenchmark` 工具函数**

在 `renderChart` 函数**上方**新增:
```js
// 可选的宽基参照基准(写死;数据来自 fund_nav 里覆盖回测窗口的指数型基金)
const BENCH_PRESETS = [
  ['510300', '沪深300'],
  ['000962', '中证500'],
  ['001592', '创业板指'],
  ['006486', '中证1000'],
  ['001548', '上证50'],
]
// 当前生效的参照基准:默认用服务端给的 bot.benchmark;选了别的就用缓存里的那条。
function effectiveBenchmark(bot){
  if (state.benchSel === 'default') return bot.benchmark
  return state.benchCache[bot.botId + '|' + state.benchSel] || bot.benchmark
}
```

- [ ] **Step 3: `renderChart` 读有效基准 + 图例加下拉**

在 `renderChart` 里把 `const benchmark = bot.benchmark` 改为:
```js
  const benchmark = effectiveBenchmark(bot)
```
把构造 `legend` 的整段(`const legend = benchmark ? ... : ''`)替换为:
```js
  const benchOptions = [`<option value="default"${state.benchSel === 'default' ? ' selected' : ''}>${bot.buyCount > 0 ? '首买基金' : '默认基准'}${benchmark ? '（' + esc(benchmark.fundName) + '）' : ''}</option>`]
    .concat(BENCH_PRESETS.map(([code, name]) => `<option value="${code}"${state.benchSel === code ? ' selected' : ''}>${esc(name)}</option>`))
    .join('')
  const benchSelect = `<select class="bench-select" title="选择参照基准">${benchOptions}</select>`
  const legend = `<div class="chart-legend"><span class="lg-item"><span class="lg-swatch lg-bot"></span>${esc(bot.botId)} 净值</span>`
    + (benchmark ? `<span class="lg-item"><span class="lg-swatch lg-bench"></span>基准 ${esc(benchmark.fundCode)} ${esc(benchmark.fundName)}</span>` : '')
    + `<span class="lg-item" style="margin-left:auto">参照 ${benchSelect}</span></div>`
```

- [ ] **Step 4: `renderMetricsPanel` 用有效基准**

把 `renderMetricsPanel` 里:
```js
  const benchFiltered = filterByRange(bot.benchmark?.series ?? [], state.rangeFrom, state.rangeTo)
  ...
  const benchLabel = bot.benchmark ? `${esc(bot.benchmark.fundCode)} ${esc(bot.benchmark.fundName)}` : '基准'
```
改为:
```js
  const bench = effectiveBenchmark(bot)
  const benchFiltered = filterByRange(bench?.series ?? [], state.rangeFrom, state.rangeTo)
  ...
  const benchLabel = bench ? `${esc(bench.fundCode)} ${esc(bench.fundName)}` : '基准'
```
（`km`/对比行不变,它们读的是 `benchFiltered`。）

- [ ] **Step 5: `attachCursor` 用有效基准**

在 `attachCursor` 里,基准点查找处:
```js
    if (benchCoords) {
      const benchSeries = bot.benchmark?.series ?? []
```
改为:
```js
    if (benchCoords) {
      const benchSeries = effectiveBenchmark(bot)?.series ?? []
```
以及 tooltip 里:
```js
    const benchPt = bot.benchmark?.series?.find(p => p.trade_date === date)
```
改为:
```js
    const benchPt = effectiveBenchmark(bot)?.series?.find(p => p.trade_date === date)
```

- [ ] **Step 6: 切 bot 时重置选择**

在 `selectBot` 函数开头(`state.selected = ...` 之前)加:
```js
  if (botId && botId !== state.selected) state.benchSel = 'default'
```

- [ ] **Step 7: 下拉 `onChange` 处理**

在全局 `change` 监听器(`document.addEventListener('change', e => {...})`)里,`.bot-run-select` 分支之后加:
```js
  if (e.target.classList?.contains('bench-select')) {
    const bot = state.data?.bots.find(b => b.botId === state.selected)
    if (!bot) return
    const val = e.target.value
    state.benchSel = val
    if (val === 'default') { renderDetail(bot); return }
    const key = bot.botId + '|' + val
    if (state.benchCache[key]) { renderDetail(bot); return }
    fetch(`/api/backtest/benchmark?fund=${encodeURIComponent(val)}&from=${encodeURIComponent(bot.firstTradeDate)}&to=${encodeURIComponent(bot.latestTradeDate)}`, { cache: 'no-store' })
      .then(r => r.json())
      .then(bm => { if (bm) state.benchCache[key] = bm; renderDetail(bot) })
      .catch(() => { /* 拉取失败保持默认基准 */ renderDetail(bot) })
    return
  }
```

- [ ] **Step 8: 浏览器实测(Playwright)**

服务仍在 48088(Task 3 起的)。重新加载页面,然后:
1. **切换基准**:选中一个 bot,在图例下拉里选「沪深300」。用 `browser_evaluate` 确认:
   ```js
   () => {
     const bench = document.querySelector('.chart-legend')?.textContent || '';
     const sel = document.querySelector('.bench-select')?.value || '';
     const benchLine = !!document.querySelector('svg.nav-chart .bench-line');
     const metric = document.querySelector('.metrics-table thead')?.textContent || '';
     return { sel, benchHasCsi: bench.includes('510300'), benchLine, metric };
   }
   ```
   Expected:`sel==='510300'`,`benchHasCsi===true`,`benchLine===true`,指标表表头含 `510300`(对比列已用新基准)。
2. **切到别的指数**(如「创业板指」)→ 参照曲线与指标表同步变化。
3. **切 bot** → 下拉重置为 `default`(`document.querySelector('.bench-select').value === 'default'`)。
4. **未建仓 bot**(若有):默认下拉项文案为「默认基准（沪深300...）」,且仍有参照曲线。
5. 等 >10s 让自动轮询跑一次,确认手动选的「沪深300」**没有**被重置(`.bench-select` 仍为 510300)。

- [ ] **Step 9: 提交**

```bash
git add world/src/backtest-dashboard/index.html
git commit -m "feat(dashboard): frontend benchmark selector over curated broad-base indices"
```

---

## Task 5: 端到端回归 + 收尾

**Files:** 无新增改动,纯验证。

- [ ] **Step 1: 全量测试**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts && npx tsc -p tsconfig.json --noEmit`
Expected: 测试全 PASS,tsc 无错误。

- [ ] **Step 2: 三项联测(Playwright)**

在同一会话里依次确认三项都成立:
- **A** 未建仓 bot 有参照曲线(若 DB 无此类 bot,则由 Task 1 单测覆盖,跳过界面验证)。
- **B** 悬停空仓日 → 「当日空仓」;悬停持仓日 → 当天快照。
- **C** 下拉切宽基 → 曲线 + 指标表同步;切 bot 重置;轮询不丢选择。

同时 `browser_console_messages` 确认无新增 JS 报错(favicon 404 可忽略)。

- [ ] **Step 3: 关服务 + 清理**

停掉 48088 的后台服务;删除验证期间产生的临时截图(若有)。

- [ ] **Step 4: 确认分支状态**

Run: `git -C .. log --oneline -6 && git -C .. status --short`
Expected: 看到本计划各 task 的提交;`world/src/backtest-dashboard/` 无遗留未提交改动。

---

## Self-Review Notes

- **Spec 覆盖**:A→Task 1;B→Task 3;C→Task 2(接口)+ Task 4(前端);测试→各 task + Task 5。全覆盖。
- **类型一致**:`pickBenchmarkFund(actions, holdings)`、`loadBenchmarkSeries(dbPath, fundCode, anchorDate, endDate)`、`effectiveBenchmark(bot)`、`state.benchSel`、`state.benchCache`、`BENCH_PRESETS` 在引用处命名一致。`BenchmarkPoint` / `BotBenchmark` 沿用 server.ts 既有类型。
- **无占位符**:每个改代码的步骤都给了完整代码与确切命令/预期。
- **接口形状**:`/api/backtest/benchmark` 返回 `BotBenchmark | null`,与 `bot.benchmark` 同形,前端可直接替换使用。
