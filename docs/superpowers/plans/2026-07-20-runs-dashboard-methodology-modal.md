# runs.html 方法论进化弹窗 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 runs.html 右侧净值图上加「展开」按钮，弹出一个弹窗，内置放大净值图、方法论进化时间线、初始/最新方法论全文，以及一个与主看板逻辑一致的当日反思面板。

**Architecture:** 服务端新增一个只读接口 `GET /api/backtest/bot-methodology`，从 run 冻结的策略库快照忠实重建「初始方法论」、读 workspace 的 `METHODOLOGY.md` 得「最新方法论」、读 `<bot>.revisions.jsonl` 得进化轨迹。前端在 runs.html 内联脚本里新增一个全屏遮罩弹窗，懒加载该接口 + 复用现成的 `/api/backtest/bot-reflection`，并把 index.html 的反思面板逻辑移植进弹窗（独立状态，不与净值图光标联动）。

**Tech Stack:** TypeScript（`node --experimental-strip-types`，无编译步骤）、Node 内置 http/sqlite/fs、自包含 HTML（内联 `<script>`/`<style>`，无构建）、`node:test`。

## Global Constraints

- Node >= 22.6.0；world 全部 `.ts` 用 `node --experimental-strip-types` 直接运行，**不引入编译步骤**。
- runs.html / index.html 是**自包含单文件**：所有 JS 内联在 `<script>`（runs.html 为 L222–L1071），所有 CSS 内联在 `<style>`；**不得引入第三方库或外部资源**。
- 服务端只读：新接口**只读文件，不写**；ID 参数走白名单正则 `^[A-Za-z0-9._-]+$` 挡 `../` 路径穿越。
- 任何数据缺失（旧 run 无 workspace / 无冻结库 / 无 revisions / 反思 404）→ **优雅降级返回空或占位，HTTP 仍 200，绝不抛错、不白屏**。
- 复用既有函数，禁止重复实现：初始方法论必须用 `loadStrategyLibrary` + `renderActiveMethodology`（`world/src/strategy-library.ts`）。
- 沿用 runs.html 既有 CSS 变量：`--panel --border --accent2 --dim --gain --loss --radius`。
- 全程中文交流（代码/命令/标识符除外）。

---

## File Structure

- `world/src/backtest-dashboard/server.ts`（修改）
  - 新增导出纯函数 `loadMethodology(worldRoot, runId, botId): MethodologyPayload`（Task 1）。
  - 新增路由 `GET /api/backtest/bot-methodology`（Task 2）。
- `world/test/backtest-dashboard.test.ts`（修改）
  - `loadMethodology` 的 fixture 单测（Task 1）+ 接口集成测（Task 2）。
- `world/src/backtest-dashboard/runs.html`（修改，内联脚本 L222–L1071 / 内联样式）
  - 弹窗 CSS + 遮罩/卡片骨架 + 开关（Task 3）。
  - 方法论区渲染 `renderMethodologyHtml(data)` + 懒加载（Task 4）。
  - 反思面板移植 `mountReflection(container, bot)` + 辅助 `mdLite/clipMemoryWindow/stripBeliefYaml`（Task 5）。
- `docs/superpowers/specs/2026-07-20-runs-dashboard-methodology-modal-design.md`（已存在，参考）

---

## Task 1: 服务端 `loadMethodology` 纯函数

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（在文件靠近其它 helper 处新增；顶部补 import）
- Test: `world/test/backtest-dashboard.test.ts`（追加）

**Interfaces:**
- Consumes（既有）:
  - `world/src/paths.ts`：`shadowWorkspaceDir(w, runId, bot)`、`runDir(w, runId)`、`strategyRevisionsFile(w, runId, bot)`
  - `world/src/strategy-library.ts`：`loadStrategyLibrary(root): StrategyLibrary`、`renderActiveMethodology({botId, strategy, buyableFundCodes}): string`、类型 `StrategyDefinition`
- Produces（后续 Task 2 与前端依赖）:
  - `export interface MethodologyRevision { ts: string; reason: string; new_size: number | null; prior_size: number | null }`
  - `export interface MethodologyPayload { botId: string; runId: string; strategyId: string | null; strategyTitle: string | null; initial: string | null; latest: string | null; revised: boolean; revisions: MethodologyRevision[] }`
  - `export function loadMethodology(worldRoot: string, runId: string, botId: string): MethodologyPayload`

- [ ] **Step 1: 写失败测试**

在 `world/test/backtest-dashboard.test.ts` 顶部 import 里补上 `loadMethodology`（与既有 `pickBenchmarkFund` 同一 import 行来自 `../src/backtest-dashboard/server.ts`），并 import `mkdirSync`：

```ts
// 顶部已有：import { mkdtempSync, rmSync, existsSync } from 'node:fs'
// 追加：
import { mkdirSync } from 'node:fs'
import { loadMethodology } from '../src/backtest-dashboard/server.ts'
```

在文件末尾追加：

```ts
function seedMethodologyFixture() {
  const worldRoot = mkdtempSync(join(tmpdir(), 'dash-meth-'))
  const runId = 'r1', botId = 'bot6'
  const ws = join(worldRoot, 'runs', runId, 'workspaces', botId)
  const lib = join(ws, 'strategies', 'index-products')
  mkdirSync(lib, { recursive: true })
  // 冻结策略库：manifest + 一个 methodology 文件
  writeFileSync(join(lib, 'manifest.yaml'),
    'version: 1\nstrategies:\n  liquor:\n    title: 中证酒指数投资框架\n    methodology: liquor.md\n    target_index: "399987.SZ"\n    default_buyable_fund_codes: ["012043"]\n')
  writeFileSync(join(lib, 'liquor.md'), '# 中证酒指数投资框架\n\n初始正文。\n')
  // 最新方法论（被 bot 改写过）
  writeFileSync(join(ws, 'METHODOLOGY.md'), '# 当前回测任务\n- bot_id: bot6\n---\n改写后的正文。\n')
  // 指派
  writeFileSync(join(worldRoot, 'runs', runId, 'strategy-assignments.json'),
    JSON.stringify({ run_id: runId, bots: { bot6: { strategy_id: 'liquor', strategy_title: '中证酒指数投资框架', target_index: '399987.SZ', buyable_fund_codes: ['012043'] } } }))
  // 修订日志：两条
  const stratDir = join(worldRoot, 'runs', runId, 'strategies')
  mkdirSync(stratDir, { recursive: true })
  writeFileSync(join(stratDir, 'bot6.revisions.jsonl'),
    JSON.stringify({ ts: '2025-10-20', reason: '第一次改', new_size: 100, prior_size: 500 }) + '\n' +
    JSON.stringify({ ts: '2025-10-27', reason: '第二次改', new_size: 120, prior_size: 100 }) + '\n')
  return { worldRoot, runId, botId }
}

test('loadMethodology: 重建初始、读最新、解析修订', () => {
  const { worldRoot, runId, botId } = seedMethodologyFixture()
  try {
    const m = loadMethodology(worldRoot, runId, botId)
    assert.equal(m.strategyId, 'liquor')
    assert.equal(m.strategyTitle, '中证酒指数投资框架')
    assert.ok(m.initial && m.initial.includes('初始正文'), 'initial 应含冻结库正文')
    assert.ok(m.initial && m.initial.includes('bot_id: bot6'), 'initial 应含重建的任务头')
    assert.ok(m.latest && m.latest.includes('改写后的正文'), 'latest 读 workspace METHODOLOGY.md')
    assert.equal(m.revised, true)
    assert.equal(m.revisions.length, 2)
    assert.deepEqual(m.revisions[0], { ts: '2025-10-20', reason: '第一次改', new_size: 100, prior_size: 500 })
  } finally { rmSync(worldRoot, { recursive: true, force: true }) }
})

test('loadMethodology: 缺 workspace/冻结库/修订 → 优雅降级', () => {
  const worldRoot = mkdtempSync(join(tmpdir(), 'dash-meth2-'))
  try {
    const m = loadMethodology(worldRoot, 'ghost', 'bot999')
    assert.equal(m.initial, null)
    assert.equal(m.latest, null)
    assert.equal(m.strategyId, null)
    assert.equal(m.revised, false)
    assert.deepEqual(m.revisions, [])
  } finally { rmSync(worldRoot, { recursive: true, force: true }) }
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd world && node --test --experimental-strip-types test/backtest-dashboard.test.ts`
Expected: FAIL —— `loadMethodology` 未从 server.ts 导出（import 报错 / 未定义）。

- [ ] **Step 3: 实现 `loadMethodology` 并导出**

在 `world/src/backtest-dashboard/server.ts` 顶部 import 区：
- 把第 6 行 `from '../paths.ts'` 的解构里补上 `runDir`、`strategyRevisionsFile`（`shadowWorkspaceDir` 已在）。
- 新增一行：`import { loadStrategyLibrary, renderActiveMethodology } from '../strategy-library.ts'`

在文件中（靠近其它 `export function` helper，例如 `pickBenchmarkFund` 附近）插入：

```ts
export interface MethodologyRevision { ts: string; reason: string; new_size: number | null; prior_size: number | null }
export interface MethodologyPayload {
  botId: string; runId: string
  strategyId: string | null; strategyTitle: string | null
  initial: string | null; latest: string | null
  revised: boolean; revisions: MethodologyRevision[]
}

// 只读重建某 (bot, run) 的方法论三件套：初始（从 run 冻结策略库快照重建）、最新
// （workspace METHODOLOGY.md）、进化轨迹（<bot>.revisions.jsonl）。任何缺失均降级为
// null / 空数组，不抛错。
export function loadMethodology(worldRoot: string, runId: string, botId: string): MethodologyPayload {
  const ws = shadowWorkspaceDir(worldRoot, runId, botId)

  let latest: string | null = null
  const latestPath = join(ws, 'METHODOLOGY.md')
  if (existsSync(latestPath)) { try { latest = readFileSync(latestPath, 'utf8') } catch { /* ignore */ } }

  let strategyId: string | null = null
  let strategyTitle: string | null = null
  let buyableCodes: string[] = []
  const assignPath = join(runDir(worldRoot, runId), 'strategy-assignments.json')
  if (existsSync(assignPath)) {
    try {
      const a = JSON.parse(readFileSync(assignPath, 'utf8')) as { bots?: Record<string, Record<string, unknown>> }
      const b = a.bots?.[botId]
      if (b) {
        if (typeof b.strategy_id === 'string') strategyId = b.strategy_id
        if (typeof b.strategy_title === 'string') strategyTitle = b.strategy_title
        if (Array.isArray(b.buyable_fund_codes)) buyableCodes = (b.buyable_fund_codes as unknown[]).filter((c): c is string => typeof c === 'string')
      }
    } catch { /* ignore malformed assignments */ }
  }

  let initial: string | null = null
  const frozenRoot = join(ws, 'strategies', 'index-products')
  if (strategyId && existsSync(join(frozenRoot, 'manifest.yaml'))) {
    try {
      const lib = loadStrategyLibrary(frozenRoot)
      const strat = lib.strategies.get(strategyId)
      if (strat) initial = renderActiveMethodology({ botId, strategy: strat, buyableFundCodes: buyableCodes })
    } catch { /* frozen lib unreadable → leave initial null */ }
  }

  const revisions: MethodologyRevision[] = []
  const revPath = strategyRevisionsFile(worldRoot, runId, botId)
  if (existsSync(revPath)) {
    for (const line of readFileSync(revPath, 'utf8').split('\n')) {
      const s = line.trim()
      if (!s) continue
      try {
        const o = JSON.parse(s) as Record<string, unknown>
        revisions.push({
          ts: typeof o.ts === 'string' ? o.ts : '',
          reason: typeof o.reason === 'string' ? o.reason : '',
          new_size: typeof o.new_size === 'number' ? o.new_size : null,
          prior_size: typeof o.prior_size === 'number' ? o.prior_size : null,
        })
      } catch { /* skip bad line */ }
    }
  }

  return { botId, runId, strategyId, strategyTitle, initial, latest, revised: revisions.length > 0, revisions }
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd world && node --test --experimental-strip-types test/backtest-dashboard.test.ts`
Expected: PASS —— 新增 2 项通过，既有全部仍通过。

- [ ] **Step 5: 提交**

```bash
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): loadMethodology 重建初始/最新方法论与修订轨迹"
```

---

## Task 2: 服务端路由 `GET /api/backtest/bot-methodology`

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（在 `/api/backtest/bot-reflection` 路由块之后，约 L1439 后插入）
- Test: `world/test/backtest-dashboard.test.ts`（追加集成测）

**Interfaces:**
- Consumes: Task 1 的 `loadMethodology`；既有 `sendJson(res, status, obj)`
- Produces: HTTP `GET /api/backtest/bot-methodology?bot_id=&run_id=` → 200 `MethodologyPayload`；参数非法 → 400

- [ ] **Step 1: 写失败测试（集成，走 spawned server）**

在 `world/test/backtest-dashboard.test.ts` 末尾追加（复用文件已有的 `spawn`、`waitForOutput`、`SERVER`、`DB` 常量与 `--world-root` 临时目录模式，参照既有 “live run control” 测试）：

```ts
test('GET /api/backtest/bot-methodology 返回结构 + 参数校验', async () => {
  const { worldRoot } = seedMethodologyFixture()  // 复用 Task 1 的 fixture
  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB, '--world-root', worldRoot], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /listening on http:\/\/([^\s]+)/)
    const base = out.match(/listening on (http:\/\/[^\s]+)/)![1]
    const good = await (await fetch(`${base}/api/backtest/bot-methodology?bot_id=bot6&run_id=r1`)).json()
    assert.equal(good.strategyId, 'liquor')
    assert.equal(good.revisions.length, 2)
    assert.ok(good.initial.includes('初始正文'))
    const bad = await fetch(`${base}/api/backtest/bot-methodology?bot_id=..%2Fx&run_id=r1`)
    assert.equal(bad.status, 400)
  } finally {
    proc.kill('SIGKILL')
    rmSync(worldRoot, { recursive: true, force: true })
  }
})
```

> 注：`seedMethodologyFixture` 已在 Task 1 定义于同文件，可直接调用。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd world && node --test --experimental-strip-types test/backtest-dashboard.test.ts`
Expected: FAIL —— 路由不存在，`fetch` 命中默认 404 分支，`good.strategyId` 为 undefined 断言失败。

- [ ] **Step 3: 加路由**

在 `server.ts` 的 `/api/backtest/bot-reflection` 处理块（结尾 `return` 之后、`/api/backtest/bot-user-md` 之前）插入：

```ts
// 某 (bot, run) 的方法论三件套：初始（冻结库重建）/ 最新（workspace）/ 修订轨迹。
// 只读；ID 走白名单正则挡路径穿越；数据缺失由 loadMethodology 内部降级。
if (req.method === 'GET' && url.pathname === '/api/backtest/bot-methodology') {
  const botId = url.searchParams.get('bot_id') ?? ''
  const runId = url.searchParams.get('run_id') ?? ''
  const idRe = /^[A-Za-z0-9._-]+$/
  if (!idRe.test(botId) || !idRe.test(runId)) {
    sendJson(res, 400, { error: 'bot_id, run_id required and must be well-formed' })
    return
  }
  sendJson(res, 200, loadMethodology(worldRoot, runId, botId))
  return
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd world && node --test --experimental-strip-types test/backtest-dashboard.test.ts`
Expected: PASS（含新集成测；既有全部通过）。

- [ ] **Step 5: 提交**

```bash
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): 新增 bot-methodology 只读接口"
```

---

## Task 3: runs.html 弹窗骨架（CSS + 遮罩/卡片 + 开关 + ⛶ 按钮）

**Files:**
- Modify: `world/src/backtest-dashboard/runs.html`
  - 内联 `<style>`（弹窗 CSS）
  - 内联 `<script>`：`chartHead`（L996–L1015，加 ⛶ 按钮）、`wireChart`（L902 起，绑定展开点击）、新增 `openMethodologyModal` / `closeMethodologyModal`
  - `<body>`：加一个空的 `#mdlModal` 容器（放在 `.wrap` 之后即可）

**Interfaces:**
- Consumes: 面板 `panel._chart = { rec, bot }`（`renderChartInto` 已写入，L896）；`renderChartBody(bot, win)`（L808）；`esc`
- Produces: 全局函数 `openMethodologyModal(rec, bot)`；DOM `#mdlModal`（`.mdl-backdrop` > `.mdl-card`）

- [ ] **Step 1: 加弹窗 CSS**

在 runs.html 内联 `<style>` 末尾（`@media` 规则附近）追加：

```css
.mdl-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;z-index:50;align-items:flex-start;justify-content:center;overflow:auto;padding:24px}
.mdl-backdrop.open{display:flex}
.mdl-card{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);width:min(1100px,92vw);max-height:90vh;overflow:auto;padding:18px 20px;position:relative}
.mdl-head{display:flex;align-items:baseline;gap:10px;margin-bottom:10px;padding-right:32px}
.mdl-title{font-size:15px;font-weight:600}
.mdl-sub{font-size:12px;color:var(--dim)}
.mdl-close{position:absolute;top:12px;right:14px;cursor:pointer;font-size:18px;color:var(--dim);background:none;border:none;line-height:1}
.mdl-close:hover{color:#fff}
.mdl-sec{margin-top:16px;padding-top:14px;border-top:1px solid rgba(255,255,255,.06)}
.mdl-sec h3{margin:0 0 10px;font-size:13px;color:var(--accent2)}
.cp-expand{cursor:pointer;background:none;border:1px solid var(--border);border-radius:6px;color:var(--dim);font-size:13px;padding:1px 7px;margin-left:8px}
.cp-expand:hover{color:#fff;border-color:var(--accent2)}
/* 进化时间线 */
.mtl{list-style:none;margin:0;padding:0}
.mtl li{position:relative;padding:0 0 14px 18px;border-left:2px solid rgba(255,255,255,.12)}
.mtl li:last-child{border-left-color:transparent}
.mtl li::before{content:'';position:absolute;left:-5px;top:2px;width:8px;height:8px;border-radius:50%;background:var(--accent2)}
.mtl .mtl-hd{font-size:12px;color:var(--dim);margin-bottom:4px}
.mtl .mtl-hd b{color:#fff}
.mtl .mtl-reason{font-size:12px;line-height:1.6;white-space:pre-wrap}
.mdl-full{white-space:pre-wrap;font-size:12px;line-height:1.6;max-height:340px;overflow:auto;background:rgba(0,0,0,.2);border-radius:6px;padding:10px}
.mdl-details summary{cursor:pointer;font-size:13px;color:var(--accent2);margin-top:8px}
.mdl-empty{color:var(--dim);font-size:12px}
/* 反思面板（移植自 index.html，作用域收在弹窗内） */
.rfl-tabs{display:flex;gap:6px;margin:8px 0}
.rfl-tabs button{background:none;border:1px solid var(--border);border-radius:6px;color:var(--dim);font-size:12px;padding:2px 9px;cursor:pointer}
.rfl-tabs button.active{color:#fff;border-color:var(--accent2)}
.rfl-nav{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim);margin:6px 0}
.rfl-nav button{background:none;border:1px solid var(--border);border-radius:6px;color:var(--dim);cursor:pointer;padding:0 7px}
.rfl-body{font-size:12px;line-height:1.7;white-space:pre-wrap;max-height:360px;overflow:auto}
.rfl-body h2,.rfl-body h3{font-size:13px;margin:8px 0 4px}
.rfl-body blockquote{border-left:2px solid var(--border);margin:4px 0;padding-left:8px;color:var(--dim)}
```

- [ ] **Step 2: 加 ⛶ 按钮到 chartHead**

在 runs.html `chartHead`（L1011–L1013）的标题块里，把标题行改成带展开按钮（仅在有 `bot` 时显示）。将 L1011 的返回起始：

```js
  return `<div class="cp-head"><div class="cp-title">${esc(rec['bot'])} · ${esc(rec._ixName)}</div>`
```

改为：

```js
  const expandBtn = bot ? `<button class="cp-expand" data-expand title="展开：方法论进化 + 当日反思">⛶</button>` : ''
  return `<div class="cp-head"><div class="cp-title">${esc(rec['bot'])} · ${esc(rec._ixName)}${expandBtn}</div>`
```

- [ ] **Step 3: 在 wireChart 绑定展开点击**

在 `wireChart`（L902）函数体开头（`const winBar = ...` 之前）插入：

```js
  const expandBtn = panel.querySelector('.cp-expand')
  if (expandBtn) expandBtn.addEventListener('click', () => openMethodologyModal(rec, bot))
```

- [ ] **Step 4: 加弹窗容器与开关函数**

在 `<body>` 内 `.wrap` 结束标签之后加：

```html
<div id="mdlModal" class="mdl-backdrop"><div class="mdl-card"><button class="mdl-close" title="关闭">✕</button><div class="mdl-body"></div></div></div>
```

在内联 `<script>`（`</script>` 之前）加：

```js
// ---- 方法论 + 反思 弹窗 ----
function closeMethodologyModal(){
  const m = document.getElementById('mdlModal')
  m.classList.remove('open'); m.querySelector('.mdl-body').innerHTML = ''
}
function openMethodologyModal(rec, bot){
  const m = document.getElementById('mdlModal')
  const body = m.querySelector('.mdl-body')
  const runId = rec['run_id'], botId = rec['bot']
  body.innerHTML = `<div class="mdl-head"><div class="mdl-title">${esc(botId)} · ${esc(rec._ixName)}</div>`
    + `<div class="mdl-sub">${esc(rec['对标指数'])} · ${esc(runId)}</div></div>`
    + `<div class="mdl-chart"></div>`
    + `<div class="mdl-sec" id="mdlMeth"><h3>📐 方法论进化</h3><div class="mdl-empty">加载中…</div></div>`
    + `<div class="mdl-sec" id="mdlRfl"><h3>当日反思</h3><div class="mdl-empty">加载中…</div></div>`
  // 放大净值图：复用现有 body 渲染（不接光标 wiring，静态展示即可；如需光标可后续接）
  const { html } = renderChartBody(bot, bot && bot.series ? 'all' : 'all')
  body.querySelector('.mdl-chart').innerHTML = html
  m.classList.add('open')
  // 懒加载方法论与反思（Task 4 / Task 5 填充）
  void loadMethodologyInto(body.querySelector('#mdlMeth'), botId, runId)
  mountReflection(body.querySelector('#mdlRfl'), bot)
}
// 关闭：✕ / 背景点击 / Esc
document.getElementById('mdlModal').addEventListener('click', e => {
  if (e.target.id === 'mdlModal' || e.target.classList.contains('mdl-close')) closeMethodologyModal()
})
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMethodologyModal() })
```

> 说明：`loadMethodologyInto` 与 `mountReflection` 在 Task 4 / Task 5 实现；本 Task 先放桩，syntax check 阶段它们未定义会在**运行时**才报错，不影响 `node --check` 语法通过。为让本 Task 可独立验证，先在脚本里加两个临时空桩：

```js
function loadMethodologyInto(el){ el.innerHTML = '<div class="mdl-empty">（待实现）</div>' }
function mountReflection(el){ el.innerHTML = '<div class="mdl-empty">（待实现）</div>' }
```

（Task 4 / Task 5 会用真实实现替换这两个桩。）

- [ ] **Step 5: 语法校验 + 手工验证**

Run: `node -e "const fs=require('fs');const h=fs.readFileSync('world/src/backtest-dashboard/runs.html','utf8');const m=h.match(/<script>([\s\S]*?)<\/script>/);require('vm').compileFunction(m[1]);console.log('script OK')"`
Expected: 打印 `script OK`（内联脚本无语法错误）。

手工：`cd world && npm run backtest` 打开 runs.html，点任一净值图右上 `⛶`，应弹出遮罩卡片，含放大净值图 + 两个「待实现」占位；✕/Esc/点背景可关闭。

- [ ] **Step 6: 提交**

```bash
git add world/src/backtest-dashboard/runs.html
git commit -m "feat(dashboard): runs.html 方法论弹窗骨架与开关"
```

---

## Task 4: 方法论区渲染 + 懒加载

**Files:**
- Modify: `world/src/backtest-dashboard/runs.html`（内联脚本：替换 Task 3 的 `loadMethodologyInto` 桩，新增纯函数 `renderMethodologyHtml`）

**Interfaces:**
- Consumes: 接口 `GET /api/backtest/bot-methodology`（Task 2）返回的 `MethodologyPayload`；`esc`
- Produces: `renderMethodologyHtml(data): string`；`async function loadMethodologyInto(el, botId, runId)`

- [ ] **Step 1: 实现纯函数 `renderMethodologyHtml`**

在内联 `<script>` 里（`openMethodologyModal` 附近）加：

```js
// 把 /api/backtest/bot-methodology 的返回渲染成：进化时间线 + 初始/最新全文折叠块。
function renderMethodologyHtml(d){
  const initHead = '初始版' + (d.strategyTitle ? ` · ${esc(d.strategyTitle)}` : '') + (d.strategyId ? `（${esc(d.strategyId)}）` : '')
  let timeline = `<ul class="mtl"><li><div class="mtl-hd"><b>${initHead}</b></div>`
    + `<div class="mtl-reason mdl-empty">agent 启动时被注入的方法论（全文见下）。</div></li>`
  if (d.revised && d.revisions.length){
    d.revisions.forEach((r, i) => {
      const size = (r.prior_size != null && r.new_size != null) ? `正文 ${r.prior_size}→${r.new_size} 字` : ''
      timeline += `<li><div class="mtl-hd"><b>${esc(r.ts || '—')}</b> · 第 ${i + 1} 次修订${size ? ' · ' + size : ''}</div>`
        + `<div class="mtl-reason">${esc(r.reason || '（无理由）')}</div></li>`
    })
  } else {
    timeline += `<li><div class="mtl-reason mdl-empty">本 run 未改动方法论。</div></li>`
  }
  timeline += `</ul>`
  const initFull = d.initial != null
    ? `<details class="mdl-details"><summary>初始方法论全文</summary><div class="mdl-full">${esc(d.initial)}</div></details>`
    : `<div class="mdl-empty">初始方法论快照不可用（旧 run 或无冻结策略库）。</div>`
  const latestFull = d.latest != null
    ? `<details class="mdl-details"><summary>最新方法论全文${d.revised ? '' : '（与初始一致）'}</summary><div class="mdl-full">${esc(d.latest)}</div></details>`
    : `<div class="mdl-empty">最新方法论不可用。</div>`
  return `<h3>📐 方法论进化</h3>` + timeline + initFull + latestFull
}
```

- [ ] **Step 2: 用真实实现替换 `loadMethodologyInto` 桩**

把 Task 3 里的 `function loadMethodologyInto(el){...}` 桩替换为：

```js
async function loadMethodologyInto(el, botId, runId){
  try {
    const r = await fetch(`/api/backtest/bot-methodology?bot_id=${encodeURIComponent(botId)}&run_id=${encodeURIComponent(runId)}`, { cache: 'no-store' })
    if (!r.ok) throw new Error('http ' + r.status)
    el.innerHTML = renderMethodologyHtml(await r.json())
  } catch {
    el.innerHTML = `<h3>📐 方法论进化</h3><div class="mdl-empty">方法论加载失败。</div>`
  }
}
```

- [ ] **Step 3: 纯函数断言（node 独立 harness）**

Run:
```bash
node --input-type=module -e "
const esc = s => String(s).replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
$(sed -n '/^function renderMethodologyHtml/,/^}/p' world/src/backtest-dashboard/runs.html)
import assert from 'node:assert/strict';
const revised = renderMethodologyHtml({strategyId:'liquor',strategyTitle:'酒',initial:'A',latest:'B',revised:true,revisions:[{ts:'2025-10-20',reason:'r1',new_size:100,prior_size:500}]});
assert.ok(revised.includes('第 1 次修订'));
assert.ok(revised.includes('500→100'));
assert.ok(revised.includes('初始方法论全文'));
const none = renderMethodologyHtml({strategyId:'x',strategyTitle:null,initial:'A',latest:'A',revised:false,revisions:[]});
assert.ok(none.includes('本 run 未改动方法论'));
assert.ok(none.includes('与初始一致'));
const noInit = renderMethodologyHtml({strategyId:null,strategyTitle:null,initial:null,latest:null,revised:false,revisions:[]});
assert.ok(noInit.includes('初始方法论快照不可用'));
console.log('renderMethodologyHtml OK');
"
```
Expected: 打印 `renderMethodologyHtml OK`。（该 harness 用 `sed` 把函数抽出来跑；`esc` 就地定义。）

- [ ] **Step 4: 手工验证**

`npm run backtest` → 展开一个**有修订历史**的 bot（如 bot101 某 run，`revisions.jsonl` 有 7 条）→ 时间线应逐条列出日期 + 第 N 次修订 + 字数变化 + reason 全文；初始/最新全文可折叠展开。再展开一个未改过的 bot → 显示「本 run 未改动方法论」+「与初始一致」。

- [ ] **Step 5: 提交**

```bash
git add world/src/backtest-dashboard/runs.html
git commit -m "feat(dashboard): 弹窗方法论进化时间线与全文渲染"
```

---

## Task 5: 移植当日反思面板进弹窗（独立状态）

**Files:**
- Modify: `world/src/backtest-dashboard/runs.html`（替换 Task 3 的 `mountReflection` 桩；新增辅助 `mdLite`、`clipMemoryWindow`、`stripBeliefYaml`）
- Reference: `world/src/backtest-dashboard/index.html`（要移植的源：`mdLite` L861–L875、`clipMemoryWindow` L906–L912、`stripBeliefYaml` L915–L939、`REFLECTION_TABS` L858、反思日辅助 `rflDates` L962 / `rflLatest` L965、反思接口用法 `/api/backtest/bot-reflection` 见 L1020–L1029）

**Interfaces:**
- Consumes: `bot` 对象（来自 `/api/backtest/bot`，含 `botId` `runId` `reflectionDates` `series[].trade_date` `latestTradeDate`）；接口 `GET /api/backtest/bot-reflection?bot_id=&run_id=&trade_date=` → `{ memoryWindow, decision }`
- Produces: `function mountReflection(container, bot)`（自带独立状态，不用全局 `state`，不与净值图光标联动）；`mdLite/clipMemoryWindow/stripBeliefYaml`

- [ ] **Step 1: 移植三个纯辅助函数**

从 index.html 原样复制以下三个函数体到 runs.html 内联 `<script>`（若同名已存在则跳过）：
- `mdLite(text)`（index.html L861–L875）——极简 markdown。
- `clipMemoryWindow(txt)`（L906–L912）。
- `stripBeliefYaml(text)`（L915–L939）。

> 复制时不改逻辑；它们只依赖入参与 `esc`（runs.html 已有 `esc`）。复制后用 `node --check`（Step 4）确认无重名冲突/语法错误。

- [ ] **Step 2: 实现 `mountReflection`（独立状态版）**

把 Task 3 的 `function mountReflection(el){...}` 桩替换为下面**自包含**实现（用闭包局部状态 `st`，DOM 查询全部 `container.querySelector` 收敛在弹窗内，翻页/日历/tab 与净值图光标互不影响）：

```js
const REFLECTION_TABS = [['memory', '交易记忆窗口'], ['decision', '当天决策']]
function rflDatesOf(bot){ const d = bot && bot.reflectionDates; return (d && d.length) ? d : ((bot.series || []).map(p => p.trade_date)) }
function rflLatestOf(bot){ const d = rflDatesOf(bot); return d.length ? d[d.length - 1] : bot.latestTradeDate }

function mountReflection(container, bot){
  const dates = rflDatesOf(bot)
  const latest = rflLatestOf(bot)
  const st = { tab: 'memory', date: latest, cache: new Map() }   // 独立状态
  const key = d => `${bot.runId}|${bot.botId}|${d}`

  function shell(){
    const tabs = REFLECTION_TABS.map(([k, label]) => `<button data-rt="${k}"${st.tab === k ? ' class="active"' : ''}>${label}</button>`).join('')
    container.innerHTML = `<h3>当日反思 · <span class="rfl-date">${esc(st.date || '--')}</span>${st.date === latest ? '（最新）' : ''}</h3>`
      + `<div class="rfl-nav"><button data-rn="prev">‹</button>`
      + `<input type="date" class="rfl-cal" value="${esc(st.date || '')}">`
      + `<button data-rn="next">›</button><button data-rn="latest">最新</button></div>`
      + `<div class="rfl-tabs">${tabs}</div><div class="rfl-body"><div class="mdl-empty">加载中…</div></div>`
  }
  function paint(){
    const span = container.querySelector('.rfl-date'); if (span) span.textContent = st.date || '--'
    const body = container.querySelector('.rfl-body')
    const data = st.cache.get(key(st.date))
    if (!data){ body.innerHTML = '<div class="mdl-empty">加载中…</div>'; return }
    const raw = { memory: data.memoryWindow, decision: data.decision }[st.tab] || ''
    const txt = st.tab === 'memory' ? clipMemoryWindow(raw) : stripBeliefYaml(raw)
    const hint = { memory: '当日无交易记忆窗口（建仓首日或旧 run 未启用）', decision: '当日无决策回复记录' }[st.tab]
    body.innerHTML = txt.trim() ? mdLite(txt) : `<div class="mdl-empty">${hint}</div>`
  }
  async function load(d){
    if (!st.cache.has(key(d))){
      try {
        const r = await fetch(`/api/backtest/bot-reflection?bot_id=${encodeURIComponent(bot.botId)}&run_id=${encodeURIComponent(bot.runId)}&trade_date=${encodeURIComponent(d)}`, { cache: 'no-store' })
        st.cache.set(key(d), r.ok ? await r.json() : { memoryWindow: '', decision: '' })
      } catch { st.cache.set(key(d), { memoryWindow: '', decision: '' }) }
    }
    if (st.date === d) paint()
  }
  function go(d){                       // 选非决策日 → 跳到 ≥d 的下一个决策日
    if (!dates.length){ st.date = d } else {
      let t = dates.find(x => x >= d); if (!t) t = dates[dates.length - 1]; st.date = t
    }
    shell(); wire(); paint(); void load(st.date)
  }
  function step(dir){
    if (!dates.length) return
    let i = dates.indexOf(st.date); if (i < 0) i = dates.length - 1
    i = Math.min(dates.length - 1, Math.max(0, i + dir)); st.date = dates[i]
    shell(); wire(); paint(); void load(st.date)
  }
  function wire(){
    container.querySelector('.rfl-tabs').addEventListener('click', e => {
      const b = e.target.closest('button[data-rt]'); if (!b) return
      st.tab = b.dataset.rt; shell(); wire(); paint()
    })
    container.querySelector('.rfl-nav').addEventListener('click', e => {
      const b = e.target.closest('button[data-rn]'); if (!b) return
      if (b.dataset.rn === 'prev') step(-1)
      else if (b.dataset.rn === 'next') step(1)
      else if (b.dataset.rn === 'latest'){ st.date = latest; shell(); wire(); paint(); void load(st.date) }
    })
    const cal = container.querySelector('.rfl-cal')
    if (cal) cal.addEventListener('change', () => { if (cal.value) go(cal.value) })
  }
  shell(); wire(); paint(); void load(st.date)
}
```

- [ ] **Step 3: 语法校验**

Run: `node -e "const fs=require('fs');const h=fs.readFileSync('world/src/backtest-dashboard/runs.html','utf8');const m=h.match(/<script>([\s\S]*?)<\/script>/);require('vm').compileFunction(m[1]);console.log('script OK')"`
Expected: 打印 `script OK`。

- [ ] **Step 4: 手工验证（反思与光标独立）**

`npm run backtest` → 展开一个 bot → 反思区默认停在最新反思日，`交易记忆窗口 / 当天决策` 两 tab 可切、`‹ ›` 翻反思日、日历选日、`最新` 回落；**移动上方净值图的光标不影响反思**；反之翻反思日不影响净值图光标（二者相互独立）。缺数据日显示占位提示。

- [ ] **Step 5: 回归 + 提交**

Run: `cd world && node --test --experimental-strip-types test/backtest-dashboard.test.ts`
Expected: PASS（既有全部 + Task 1/2 新增，均通过）。

```bash
git add world/src/backtest-dashboard/runs.html
git commit -m "feat(dashboard): 弹窗内移植独立的当日反思面板"
```

---

## Task 6: 端到端联调与验收

**Files:** 无新增（纯验证）

- [ ] **Step 1: 全量测试**

Run: `cd world && node --test --experimental-strip-types test/*.test.ts`
Expected: 全绿（含 backtest-dashboard.test.ts 的既有 + 新增）。

- [ ] **Step 2: 真实 run 逐条核对方法论时间线**

选一个有修订历史的 run（例如 `world/runtime/runs/dash-2026-06-22T08-55-04` 的 bot101，`strategies/bot101.revisions.jsonl` 有 7 条）。展开弹窗，逐条比对时间线的「日期 / 第 N 次 / 字数 / reason」与 jsonl 文件一致；展开「初始方法论全文」核对与该 run `workspaces/bot101/strategies/index-products` 冻结库重建一致。

- [ ] **Step 3: 降级路径手测**

展开一个旧 run（无 workspace）或多资产 bot（无冻结库/strategy_id）→ 方法论区显示「初始方法论快照不可用」但仍能展示 latest（若有）与时间线；不白屏、不报未捕获异常（看浏览器 console）。

- [ ] **Step 4: 提交（如有联调微调）**

```bash
git add world/src/backtest-dashboard/runs.html
git commit -m "chore(dashboard): 方法论弹窗联调微调"
```

---

## 附：自查（写完计划的 spec 覆盖核对）

- 初始方法论（冻结库重建）→ Task 1/Task 4 ✅
- 最新方法论（workspace METHODOLOGY.md）→ Task 1/Task 4 ✅
- 进化轨迹（revisions.jsonl 时间线）→ Task 1/Task 4 ✅
- 当日反思（复用 bot-reflection，逻辑同 index.html，与光标独立）→ Task 5 ✅
- 从右侧净值图展开的弹窗容器 + 放大净值图 → Task 3 ✅
- 只读接口 + 参数白名单 + 优雅降级 → Task 1/Task 2 ✅
- 测试（服务端单测/集成 + 前端纯函数 harness + 手工）→ 各 Task ✅
- 明确不做：中间版本全文回放、光标↔反思联动、initial vs latest diff 高亮、改 index.html、方法论写接口 → 计划未涉及 ✅
