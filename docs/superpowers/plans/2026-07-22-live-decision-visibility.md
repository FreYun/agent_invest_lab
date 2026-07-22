# live run 决策可见性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 dashboard 显示 live run 的盘中决策——一级页外透今日 pending 决策、侧栏三色标恢复并扩到 live、决策流水面板、详情曲线 live 段买卖点。

**Architecture:** 服务端新增 2 个只读端点 + 修 1 个既有端点 + 给 `loadLiveBotForRun` 补 live 段 actions；前端 runs.html 加「今日决策」列与流水面板。决策数据全部已在库（`fund_bot_orders` 今日 pending、`fund_bot_actions` 历史），本计划只做「读取 + 展示」，不改决策/结算管线。

**Tech Stack:** Node≥22.6 `--experimental-strip-types` 跑 .ts；`node:sqlite` DatabaseSync（`queryRows` 同步）；纯手写 SVG/HTML 前端（runs.html 单文件，无框架无构建）。

## Global Constraints

- Node≥22.6，`.ts` 用 `node:test` + `--experimental-strip-types` 直接跑，无编译步骤。
- `queryRows<T>(dbPath, sql)` **同步**返回 `T[]`（`node:sqlite` DatabaseSync）；SQL 字面量用 `quoteSql(s)`（server.ts:219）转义，不要拼裸字符串。
- `server.ts` 在 ~17533 偏移处含一个 null 字节 → Grep 工具会判为二进制。定位用 `grep -an` 或 Read+行号，改用 Edit。
- **段标签硬约束**：dash 详情图的 `backtest`/`daily_oos` 段着色与 marker 不可改；live 段用 `'live'`。多基金 bot101/102/103 的现有行为不可回归。
- **今日**（Asia/Shanghai）：`new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10)`。本机 `date` 为 +08，但 pending 表含**陈旧 pending**（实测 2026-05-29 3 条、06-23 11 条）→ 取「今日决策」**必须**用 `order_date = <今日>` 过滤，不能只用 `status='pending'`。
- 动作方向映射复用 `classifyAction(action_type, final_decision)`（server.ts:303）：ADD→buy、REDUCE→sell。挂单 `order_type` 本就是 `buy`/`sell`。
- 回复中文（代码/命令/标识符除外）。
- 全测命令：`cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts`。

---

### Task 1: 服务端 `loadTodayDecisions` + `/api/backtest/today-decisions`

今日 pending 挂单按 `run_id|bot` 分组，供一级页「今日决策」列与侧栏 live 三色标复用。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（在 `loadLatestDecisions` 之后 ~1094 插入 `shanghaiToday` + `loadTodayDecisions`；路由在 `/api/backtest/latest-decisions` 分支后 ~1784 加）
- Test: `world/test/backtest-dashboard.test.ts`

**Interfaces:**
- Consumes: `queryRows`、`quoteSql`、`num`、`sendJson`、`tableExists`。
- Produces: `function shanghaiToday(): string`；`async function loadTodayDecisions(dbPath: string): Promise<{ today: string; decisions: Record<string, { dir: 'buy' | 'sell' | 'mixed'; items: Array<{ fund: string; type: 'buy' | 'sell'; amount: number; reason: string }> }> }>`。key = `order_run_id|bot_id`，仅含 `order_date=今日 AND status='pending' AND order_run_id LIKE 'live-%'`。

- [ ] **Step 1: 写失败测试（追加到 test/backtest-dashboard.test.ts）**

```ts
test('/api/backtest/today-decisions 返回今日 pending 决策（结构）', async () => {
  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /backtest dashboard listening on http:\/\//)
    const base = `http://127.0.0.1:${out.match(/http:\/\/127\.0\.0\.1:(\d+)\//)![1]}`
    const r = await fetch(`${base}/api/backtest/today-decisions`, { cache: 'no-store' })
    assert.equal(r.status, 200)
    const body = await r.json() as { today: string; decisions: Record<string, { dir: string; items: unknown[] }> }
    assert.match(body.today, /^\d{4}-\d{2}-\d{2}$/)
    assert.equal(typeof body.decisions, 'object')
    for (const [k, v] of Object.entries(body.decisions)) {
      assert.ok(k.includes('|'), 'key 形如 run_id|bot')
      assert.ok(['buy', 'sell', 'mixed'].includes(v.dir))
      assert.ok(Array.isArray(v.items) && v.items.length > 0)
    }
  } finally { proc.kill() }
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | grep -A3 'today-decisions'`
Expected: FAIL — 端点不存在 → 404 → `assert.equal(r.status, 200)` 失败。

- [ ] **Step 3: 实现 helper（在 server.ts `loadLatestDecisions` 之后 ~1094 插入）**

```ts
/** 今日（Asia/Shanghai）日期 YYYY-MM-DD。本机为 +08，用固定偏移避免依赖进程 TZ。 */
function shanghaiToday(): string {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10)
}

/** 今日 pending 盘中决策：只取 order_date=今日 且 status=pending 的 live 挂单，按 run_id|bot 分组。
 *  含陈旧 pending（历史未结算单）必须靠 order_date=今日 排除。方向 dir：全买=buy/全卖=sell/混合=mixed。 */
async function loadTodayDecisions(dbPath: string): Promise<{
  today: string
  decisions: Record<string, { dir: 'buy' | 'sell' | 'mixed'; items: Array<{ fund: string; type: 'buy' | 'sell'; amount: number; reason: string }> }>
}> {
  const today = shanghaiToday()
  if (!(await tableExists(dbPath, 'fund_bot_orders'))) return { today, decisions: {} }
  const rows = queryRows<{ order_run_id: string; bot_id: string; fund_code: string; fund_name: string | null; order_type: string; order_amount: number | null; action_reason: string | null }>(dbPath,
    'SELECT o.order_run_id, o.bot_id, o.fund_code, ' +
    "COALESCE(i.fund_name, o.fund_code) AS fund_name, o.order_type, o.order_amount, o.action_reason " +
    'FROM fund_bot_orders o LEFT JOIN fund_info i ON i.fund_code = o.fund_code ' +
    "WHERE o.status = 'pending' AND o.order_date = " + quoteSql(today) +
    " AND o.order_run_id LIKE 'live-%' ORDER BY o.order_id ASC")
  const decisions: Record<string, { dir: 'buy' | 'sell' | 'mixed'; items: Array<{ fund: string; type: 'buy' | 'sell'; amount: number; reason: string }> }> = {}
  for (const r of rows) {
    const type: 'buy' | 'sell' = r.order_type === 'sell' ? 'sell' : 'buy'
    const key = `${r.order_run_id}|${r.bot_id}`
    const g = decisions[key] ?? (decisions[key] = { dir: type, items: [] })
    g.items.push({ fund: r.fund_name ?? r.fund_code, type, amount: num(r.order_amount), reason: r.action_reason ?? '' })
    if (g.dir !== type) g.dir = 'mixed'
  }
  return { today, decisions }
}
```

- [ ] **Step 4: 加路由（在 `/api/backtest/latest-decisions` 分支后 ~1784）**

```ts
      // 今日盘中决策：今天的 pending 挂单（方向/标的/金额/理由），供一级页「今日决策」列。
      if (req.method === 'GET' && url.pathname === '/api/backtest/today-decisions') {
        sendJson(res, 200, await loadTodayDecisions(dbPath))
        return
      }
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | tail -6`
Expected: PASS（含新 today-decisions 用例）。

- [ ] **Step 6: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): 新增 /api/backtest/today-decisions（今日 pending 盘中决策）"
```

---

### Task 2: 修 `loadLatestDecisions` — 恢复并扩到 live（回归修复）

侧栏三色标的服务端锚在 `fund_bot_daily_snapshots`（live 为 0 条）→ live 行无标。保留 dash 的 snapshot 路径不变，新增 live 路径：今日 pending 优先，否则 `fund_bot_actions` 末日动作。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（`loadLatestDecisions` 1049-1094）
- Test: `world/test/backtest-dashboard.test.ts`

**Interfaces:**
- Consumes: `loadTodayDecisions`（Task 1）、`queryRows`、`num`、`tableExists`。
- Produces: `loadLatestDecisions` 返回的 `decisions` 现同时含 dash key（`dash-…|bot`，行为不变）与 live key（`live-…|bot`）。值仍 `'add' | 'reduce' | 'clear'`。

- [ ] **Step 1: 写失败测试（追加）**

```ts
test('/api/backtest/latest-decisions 覆盖 live run（今日有 pending 的 live 出现在结果里）', async () => {
  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /backtest dashboard listening on http:\/\//)
    const base = `http://127.0.0.1:${out.match(/http:\/\/127\.0\.0\.1:(\d+)\//)![1]}`
    const td = await (await fetch(`${base}/api/backtest/today-decisions`, { cache: 'no-store' })).json() as { decisions: Record<string, unknown> }
    const ld = await (await fetch(`${base}/api/backtest/latest-decisions`, { cache: 'no-store' })).json() as { decisions: Record<string, 'add' | 'reduce' | 'clear'> }
    // 今日有 pending 的每个 live run，必须在 latest-decisions 里有三色标
    for (const k of Object.keys(td.decisions)) {
      assert.ok(k in ld.decisions, `live ${k} 应出现在 latest-decisions`)
      assert.ok(['add', 'reduce', 'clear'].includes(ld.decisions[k]))
    }
  } finally { proc.kill() }
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | grep -A3 '覆盖 live run'`
Expected: FAIL — 当前 `loadLatestDecisions` 只出 dash key（依赖 snapshots），live key 缺失。

- [ ] **Step 3: 在 `loadLatestDecisions` 末尾（`return { decisions }` 之前，1092-1093 处）合入 live 路径**

把 1080-1093 段：
```ts
  const decisions: Record<string, 'add' | 'reduce' | 'clear'> = {}
  for (const r of rows) {
    const hasAdd = num(r.n_add) > 0, hasReduce = num(r.n_reduce) > 0
    if (!hasAdd && !hasReduce) continue
    const cashRatio = r.cash_weight != null ? num(r.cash_weight)
      : (num(r.total_value) > 0 ? num(r.cash) / num(r.total_value) : 0)
    let kind: 'add' | 'reduce' | 'clear'
    if (hasReduce && !hasAdd && cashRatio >= 0.995) kind = 'clear'
    else if (num(r.add_amt) - num(r.reduce_amt) >= 0 && hasAdd) kind = 'add'
    else kind = 'reduce'
    decisions[`${r.run_id}|${r.bot_id}`] = kind
  }
  return { decisions }
```
改为（保留原 dash 循环不动，在其后插入 live 合入块）：
```ts
  const decisions: Record<string, 'add' | 'reduce' | 'clear'> = {}
  for (const r of rows) {
    const hasAdd = num(r.n_add) > 0, hasReduce = num(r.n_reduce) > 0
    if (!hasAdd && !hasReduce) continue
    const cashRatio = r.cash_weight != null ? num(r.cash_weight)
      : (num(r.total_value) > 0 ? num(r.cash) / num(r.total_value) : 0)
    let kind: 'add' | 'reduce' | 'clear'
    if (hasReduce && !hasAdd && cashRatio >= 0.995) kind = 'clear'
    else if (num(r.add_amt) - num(r.reduce_amt) >= 0 && hasAdd) kind = 'add'
    else kind = 'reduce'
    decisions[`${r.run_id}|${r.bot_id}`] = kind
  }
  // —— live 路径：snapshots 无 live 行，dash 循环覆盖不到。今日 pending 优先（buy→add/sell→reduce/
  //    混合按净额），否则回退 fund_bot_actions 末日动作（after_weight≈0 → clear）。dash key 不受影响。 ——
  const today = await loadTodayDecisions(dbPath)
  for (const [key, g] of Object.entries(today.decisions)) {
    if (g.dir === 'buy') decisions[key] = 'add'
    else if (g.dir === 'sell') decisions[key] = 'reduce'
    else {
      const net = g.items.reduce((s, it) => s + (it.type === 'buy' ? it.amount : -it.amount), 0)
      decisions[key] = net >= 0 ? 'add' : 'reduce'
    }
  }
  if (await tableExists(dbPath, 'fund_bot_actions')) {
    const liveActs = queryRows<{ run_id: string; bot_id: string; n_add: number; n_reduce: number; add_amt: number | null; reduce_amt: number | null; last_after: number | null }>(dbPath, `
      WITH last_day AS (
        SELECT run_id, bot_id, MAX(action_date) AS d FROM fund_bot_actions
        WHERE run_id LIKE 'live-%' GROUP BY run_id, bot_id
      )
      SELECT a.run_id, a.bot_id,
             SUM(CASE WHEN a.action_type='ADD' THEN 1 ELSE 0 END) AS n_add,
             SUM(CASE WHEN a.action_type='REDUCE' THEN 1 ELSE 0 END) AS n_reduce,
             SUM(CASE WHEN a.action_type='ADD' THEN COALESCE(a.amount,0) ELSE 0 END) AS add_amt,
             SUM(CASE WHEN a.action_type='REDUCE' THEN COALESCE(a.amount,0) ELSE 0 END) AS reduce_amt,
             MIN(a.after_weight) AS last_after
      FROM fund_bot_actions a
      JOIN last_day l ON l.run_id=a.run_id AND l.bot_id=a.bot_id AND a.action_date=l.d
      GROUP BY a.run_id, a.bot_id
    `)
    for (const r of liveActs) {
      const key = `${r.run_id}|${r.bot_id}`
      if (key in decisions) continue   // 今日 pending 已定，优先
      const hasAdd = num(r.n_add) > 0, hasReduce = num(r.n_reduce) > 0
      if (!hasAdd && !hasReduce) continue
      if (hasReduce && !hasAdd && r.last_after != null && num(r.last_after) <= 0.005) decisions[key] = 'clear'
      else if (num(r.add_amt) - num(r.reduce_amt) >= 0 && hasAdd) decisions[key] = 'add'
      else decisions[key] = 'reduce'
    }
  }
  return { decisions }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | tail -6`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "fix(dashboard): loadLatestDecisions 恢复并扩到 live run（今日 pending + 末日动作）"
```

---

### Task 3: 服务端 `/api/backtest/run-decisions` — 单 live run 决策流水

供决策流水面板（Task 6）与 marker（Task 4 复用其口径）。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（`loadTodayDecisions` 之后加 `loadRunDecisions`；路由在 today-decisions 后加）
- Test: `world/test/backtest-dashboard.test.ts`

**Interfaces:**
- Consumes: `queryRows`、`quoteSql`、`num`、`isLiveRunId`、`sendJson`、`shanghaiToday`。
- Produces: `async function loadRunDecisions(dbPath: string, runId: string, botId: string): Promise<{ rows: Array<{ date: string; side: 'buy' | 'sell'; fund: string; beforeWeight: number | null; afterWeight: number | null; amount: number; reason: string; status: 'confirmed' | 'pending' }> }>`。历史（fund_bot_actions，confirmed）按 date 升序 + 今日 pending 追加在末尾。

- [ ] **Step 1: 写失败测试（追加）**

```ts
test('/api/backtest/run-decisions 返回某 live run 的决策流水', async () => {
  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /backtest dashboard listening on http:\/\//)
    const base = `http://127.0.0.1:${out.match(/http:\/\/127\.0\.0\.1:(\d+)\//)![1]}`
    const list = await (await fetch(`${base}/api/backtest/live-runs`, { cache: 'no-store' })).json() as { runs: Array<Record<string, unknown>> }
    if (!list.runs.length) return
    const one = list.runs[0]
    const r = await fetch(`${base}/api/backtest/run-decisions?run_id=${encodeURIComponent(String(one.liveRunId))}&bot=${one.botId}`, { cache: 'no-store' })
    assert.equal(r.status, 200)
    const body = await r.json() as { rows: Array<Record<string, unknown>> }
    assert.ok(Array.isArray(body.rows))
    for (const p of body.rows) {
      assert.ok(['buy', 'sell'].includes(p.side as string))
      assert.ok(['confirmed', 'pending'].includes(p.status as string))
    }
  } finally { proc.kill() }
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | grep -A3 'run-decisions'`
Expected: FAIL — 端点不存在 → 404。

- [ ] **Step 3: 实现 `loadRunDecisions`（`loadTodayDecisions` 之后插入）**

```ts
/** 单 live run 决策流水：fund_bot_actions 历史（confirmed）+ 今日 pending 挂单。供流水面板与曲线 marker。 */
async function loadRunDecisions(dbPath: string, runId: string, botId: string): Promise<{
  rows: Array<{ date: string; side: 'buy' | 'sell'; fund: string; beforeWeight: number | null; afterWeight: number | null; amount: number; reason: string; status: 'confirmed' | 'pending' }>
}> {
  const rows: Array<{ date: string; side: 'buy' | 'sell'; fund: string; beforeWeight: number | null; afterWeight: number | null; amount: number; reason: string; status: 'confirmed' | 'pending' }> = []
  if (await tableExists(dbPath, 'fund_bot_actions')) {
    const acts = queryRows<{ action_date: string; action_type: string; final_decision: string | null; fund_name: string | null; fund_code: string; before_weight: number | null; after_weight: number | null; amount: number | null; reason: string | null }>(dbPath,
      'SELECT a.action_date, a.action_type, a.final_decision, ' +
      'COALESCE(i.fund_name, a.fund_code) AS fund_name, a.fund_code, a.before_weight, a.after_weight, a.amount, a.reason ' +
      'FROM fund_bot_actions a LEFT JOIN fund_info i ON i.fund_code = a.fund_code ' +
      'WHERE a.run_id = ' + quoteSql(runId) + ' AND a.bot_id = ' + quoteSql(botId) +
      ' ORDER BY a.action_date ASC, a.action_id ASC')
    for (const a of acts) {
      const side = classifyAction(a.action_type, a.final_decision)
      if (side === 'hold') continue
      rows.push({ date: a.action_date, side, fund: a.fund_name ?? a.fund_code, beforeWeight: a.before_weight, afterWeight: a.after_weight, amount: num(a.amount), reason: a.reason ?? '', status: 'confirmed' })
    }
  }
  if (await tableExists(dbPath, 'fund_bot_orders')) {
    const today = shanghaiToday()
    const pend = queryRows<{ fund_name: string | null; fund_code: string; order_type: string; order_amount: number | null; action_reason: string | null; order_date: string }>(dbPath,
      'SELECT COALESCE(i.fund_name, o.fund_code) AS fund_name, o.fund_code, o.order_type, o.order_amount, o.action_reason, o.order_date ' +
      'FROM fund_bot_orders o LEFT JOIN fund_info i ON i.fund_code = o.fund_code ' +
      'WHERE o.order_run_id = ' + quoteSql(runId) + ' AND o.bot_id = ' + quoteSql(botId) +
      " AND o.status = 'pending' AND o.order_date = " + quoteSql(today) + ' ORDER BY o.order_id ASC')
    for (const o of pend) {
      rows.push({ date: o.order_date, side: o.order_type === 'sell' ? 'sell' : 'buy', fund: o.fund_name ?? o.fund_code, beforeWeight: null, afterWeight: null, amount: num(o.order_amount), reason: o.action_reason ?? '', status: 'pending' })
    }
  }
  return { rows }
}
```

- [ ] **Step 4: 加路由（在 today-decisions 分支后）**

```ts
      // 单 live run 决策流水：历史动作 + 今日 pending。前端决策流水面板与曲线 marker 用。
      if (req.method === 'GET' && url.pathname === '/api/backtest/run-decisions') {
        const runId = url.searchParams.get('run_id') ?? ''
        const botId = url.searchParams.get('bot') ?? ''
        if (!runId || !botId) { sendJson(res, 400, { error: 'run_id and bot required' }); return }
        sendJson(res, 200, await loadRunDecisions(dbPath, runId, botId))
        return
      }
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | tail -6`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): 新增 /api/backtest/run-decisions（单 live run 决策流水）"
```

---

### Task 4: `loadLiveBotForRun` 补 live 段 actions → 详情图 live 段买卖点

详情图 `renderChartBody`（runs.html:965）已按 `bot.actions`（`{action_date, side, amount, fund_name}`）画买卖点。`loadLiveBotForRun` 现只带源 dash actions（历史段），需追加 live 段动作 + 今日 pending。

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`（`loadLiveBotForRun` 874-894）
- Test: `world/test/backtest-dashboard.test.ts`

**Interfaces:**
- Consumes: `queryRows`、`quoteSql`、`num`、`classifyAction`、`shanghaiToday`、既有 `base.actions`。
- Produces: `loadLiveBotForRun` 返回对象的 `actions` = 源 dash actions（历史段日期）+ live actions（live 段日期，`fund_bot_actions run_id=liveRunId`）+ 今日 pending（`side/action_date/amount/fund_name`）。形状与 `loadBotForRun` 的 action 元素兼容（至少含 `action_date`/`side`/`amount`/`fund_name`）。

- [ ] **Step 1: 写失败测试（追加）**

```ts
test('/api/backtest/bot live run 的 series 与 actions live 段对齐', async () => {
  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /backtest dashboard listening on http:\/\//)
    const base = `http://127.0.0.1:${out.match(/http:\/\/127\.0\.0\.1:(\d+)\//)![1]}`
    const list = await (await fetch(`${base}/api/backtest/live-runs`, { cache: 'no-store' })).json() as { runs: Array<Record<string, unknown>> }
    if (!list.runs.length) return
    // 找一个有 live 动作的 run（否则跳过）：任一 run 的 run-decisions 有 confirmed/pending 行即可
    let picked: Record<string, unknown> | null = null
    for (const one of list.runs) {
      const rd = await (await fetch(`${base}/api/backtest/run-decisions?run_id=${encodeURIComponent(String(one.liveRunId))}&bot=${one.botId}`, { cache: 'no-store' })).json() as { rows: unknown[] }
      if (rd.rows.length) { picked = one; break }
    }
    if (!picked) return
    const bot = await (await fetch(`${base}/api/backtest/bot?bot_id=${picked.botId}&run_id=${encodeURIComponent(String(picked.liveRunId))}`, { cache: 'no-store' })).json() as { actions: Array<Record<string, unknown>> }
    assert.ok(Array.isArray(bot.actions))
    assert.ok(bot.actions.every(a => a.side === 'buy' || a.side === 'sell' || a.side === 'hold'))
  } finally { proc.kill() }
})
```

- [ ] **Step 2: 跑测试确认失败或探明现状**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | grep -A3 'actions live 段对齐'`
Expected: 现状 `bot.actions` 仅源 dash（历史段），无 live 段动作。测试断言较宽（结构级）可能已过；实现后 live 段动作应出现。若结构测试已 PASS，用 Step 4 的数据核对确认 live 段动作数增加。

- [ ] **Step 3: 在 `loadLiveBotForRun` 的 `return` 前补 actions（894 前）**

把 887-893 段：
```ts
  const series = stitchSeriesRows(historyRows, liveRows)
  return {
    ...base,
    runId: liveRunId,
    series,
    availableRuns: [{ runId: liveRunId, latestDate: liveRows.length ? String(liveRows[liveRows.length - 1].trade_date) : (base.latestTradeDate ?? '') }],
  }
```
改为：
```ts
  const series = stitchSeriesRows(historyRows, liveRows)
  // live 段买卖点：源 dash actions（历史段日期）保留，追加 live run 自己的动作 + 今日 pending。
  const baseActions = Array.isArray(base.actions) ? base.actions as Array<Record<string, unknown>> : []
  const liveActs = queryRows<{ action_date: string; action_type: string; final_decision: string | null; amount: number | null; fund_name: string | null; fund_code: string }>(dbPath,
    'SELECT a.action_date, a.action_type, a.final_decision, a.amount, ' +
    'COALESCE(i.fund_name, a.fund_code) AS fund_name, a.fund_code ' +
    'FROM fund_bot_actions a LEFT JOIN fund_info i ON i.fund_code = a.fund_code ' +
    'WHERE a.run_id = ' + quoteSql(liveRunId) + ' AND a.bot_id = ' + quoteSql(botId) +
    ' ORDER BY a.action_date ASC, a.action_id ASC')
  const today = shanghaiToday()
  const pend = queryRows<{ order_date: string; order_type: string; order_amount: number | null; fund_name: string | null; fund_code: string }>(dbPath,
    'SELECT o.order_date, o.order_type, o.order_amount, ' +
    'COALESCE(i.fund_name, o.fund_code) AS fund_name, o.fund_code ' +
    'FROM fund_bot_orders o LEFT JOIN fund_info i ON i.fund_code = o.fund_code ' +
    'WHERE o.order_run_id = ' + quoteSql(liveRunId) + ' AND o.bot_id = ' + quoteSql(botId) +
    " AND o.status = 'pending' AND o.order_date = " + quoteSql(today) + ' ORDER BY o.order_id ASC')
  const liveActions: Array<Record<string, unknown>> = [
    ...liveActs.map(a => ({ action_date: a.action_date, side: classifyAction(a.action_type, a.final_decision), amount: num(a.amount), fund_name: a.fund_name ?? a.fund_code, fund_code: a.fund_code, status: 'confirmed' })),
    ...pend.map(o => ({ action_date: o.order_date, side: (o.order_type === 'sell' ? 'sell' : 'buy'), amount: num(o.order_amount), fund_name: o.fund_name ?? o.fund_code, fund_code: o.fund_code, status: 'pending' })),
  ]
  return {
    ...base,
    runId: liveRunId,
    series,
    actions: [...baseActions, ...liveActions],
    availableRuns: [{ runId: liveRunId, latestDate: liveRows.length ? String(liveRows[liveRows.length - 1].trade_date) : (base.latestTradeDate ?? '') }],
  }
```

- [ ] **Step 4: 跑测试 + 数据核对**

Run: `cd world && node --experimental-strip-types --test test/backtest-dashboard.test.ts 2>&1 | tail -6`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(dashboard): loadLiveBotForRun 补 live 段 actions（详情图 live 买卖点）"
```

---

### Task 5: 前端 runs.html「今日决策」列

一级页表格加一列，读 `/api/backtest/today-decisions`，显示 买▲/卖▼ 徽标 + hover 理由；pending 灰/虚线。

**Files:**
- Modify: `world/src/backtest-dashboard/runs.html`（`TH` 676-679；`rowHtml` 656-674；boot 序列 1181-1204 加 `loadTodayDecisions`；CSS 顶部 `<style>` 内加 `.today-dec` 样式）
- Test: 手动端到端（纯前端无单测框架）

**Interfaces:**
- Consumes: `/api/backtest/today-decisions`（Task 1）、`esc`、`render`。
- Produces: 模块级 `let todayDecisions = {}`（`run_id|bot` → `{dir, items}`）；`async function loadTodayDecisions()`；`rowHtml` 多一列。

- [ ] **Step 1: 加数据加载（在 `loadLatestDecisions` 前端函数 1194 附近插入）**

```js
// 今日盘中决策（今天的 pending 挂单）：run_id|bot -> {dir, items:[{fund,type,amount,reason}]}。
let todayDecisions = {}
async function loadTodayDecisions(){
  try {
    const r = await fetch('/api/backtest/today-decisions', { cache: 'no-store' })
    if (r.ok) todayDecisions = (await r.json()).decisions || {}
  } catch { todayDecisions = {} }
}
```

- [ ] **Step 2: 接入 boot 序列（1202 的 `Promise.all` 加一项）**

把：
```js
  await Promise.all([loadVerdicts(), loadEvalRecords(), loadLatestDecisions()])  // 并行拉「合格覆盖」+「评测记录」+「末日决策标识」
```
改为：
```js
  await Promise.all([loadVerdicts(), loadEvalRecords(), loadLatestDecisions(), loadTodayDecisions()])  // +「今日决策」
```

- [ ] **Step 3: `TH` 加列头（676-679）**

把：
```js
const TH = [
  ['pass','合格','l'],['run_id','run_id','l'],['bot','bot','l'],['状态','状态','l'],
  ['绝对收益%','收益%',''],['最大回撤%','回撤%',''],['_calmar','卡玛',''],['普通投资者评级','评级','l'],
]
```
改为（在「状态」后插「今日决策」，非排序列 key 用 `_today`）：
```js
const TH = [
  ['pass','合格','l'],['run_id','run_id','l'],['bot','bot','l'],['状态','状态','l'],
  ['_today','今日决策','l'],
  ['绝对收益%','收益%',''],['最大回撤%','回撤%',''],['_calmar','卡玛',''],['普通投资者评级','评级','l'],
]
```

- [ ] **Step 4: `rowHtml` 加 `<td>`（668 的状态 td 之后插入）**

在 `rowHtml` 里，`<td class="l"><span class="status-pill…</td>` 之后插入：
```js
    <td class="l">${todayDecHtml(r._key)}</td>
```
并在 `rowHtml` 函数之前定义：
```js
// 今日决策徽标：买▲红 / 卖▼绿 / 混合⇅；pending 恒为灰/虚线（未结算）；无今日决策显「—」。
function todayDecHtml(key){
  const g = todayDecisions[key]
  if (!g || !g.items || !g.items.length) return '<span class="today-dec none">—</span>'
  const sym = g.dir === 'sell' ? '▼ 卖' : g.dir === 'buy' ? '▲ 买' : '⇅ 调'
  const cls = g.dir === 'sell' ? 'sell' : g.dir === 'buy' ? 'buy' : 'mixed'
  const title = g.items.map(it => `${it.type === 'sell' ? '卖' : '买'} ${it.fund} ¥${new Intl.NumberFormat('zh-CN',{maximumFractionDigits:0}).format(it.amount)}｜${it.reason}`).join('\n')
  return `<span class="today-dec ${cls} pending" title="${esc(title)}">${sym}<span class="td-pend">待确认</span></span>`
}
```

- [ ] **Step 5: 加 CSS（顶部 `<style>` 内追加）**

```css
.today-dec{ font-size:12px; padding:1px 5px; border-radius:4px; white-space:nowrap; }
.today-dec.none{ color:var(--muted,#888); }
.today-dec.buy{ color:var(--gain,#e5484d); }
.today-dec.sell{ color:var(--loss,#30a46c); }
.today-dec.mixed{ color:var(--accent,#66d9e8); }
/* pending=未结算：灰边虚线 + 小字标注 */
.today-dec.pending{ border:1px dashed #999; opacity:.85; }
.today-dec .td-pend{ margin-left:4px; font-size:10px; color:#999; }
```

- [ ] **Step 6: 起 dashboard 端到端核对**

Run:
```bash
cd world && node --experimental-strip-types src/backtest-dashboard/server.ts --host 127.0.0.1 --port 18899 --db ../data/fund.db &
sleep 1
curl -s http://127.0.0.1:18899/api/backtest/today-decisions | head -c 400
```
Expected: JSON `decisions` 含若干 `live-…|botN` 键。浏览器开 `http://127.0.0.1:18899/backtest-dashboard/runs.html`：今天有 pending 的 live 行「今日决策」列显示 ▲买/▼卖 + 灰虚线「待确认」，hover 出理由；无今日决策的行显「—」。核对后 `kill %1`。

- [ ] **Step 7: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/runs.html
git commit -m "feat(dashboard): runs.html 一级页加「今日决策」列（今日 pending，pending 标灰虚线）"
```

---

### Task 6: 前端 runs.html 决策流水面板

点开某 run 时，右侧详情区加「决策流水」表：日期 · 方向 · 标的 · 仓位前→后 · 金额 · 理由；今日 pending 行标灰。

**Files:**
- Modify: `world/src/backtest-dashboard/runs.html`（详情渲染处 `renderChartInto`/`selectRun` 附近 ~1015；`botCache` 同款加 `decCache`）
- Test: 手动端到端

**Interfaces:**
- Consumes: `/api/backtest/run-decisions?run_id=&bot=`（Task 3）、`esc`、现有详情面板容器。
- Produces: `async function loadRunDecisions(runId, bot)`（前端，带 `decCache`）；`function decTableHtml(rows)`；在打开 run 详情后把流水表插入详情容器。

- [ ] **Step 1: 定位详情打开入口**

Run: `cd world && grep -n 'renderChartInto\|botCache\|panel\b' src/backtest-dashboard/runs.html | head`
Expected: 找到点开 run 后渲染净值图的函数（`renderChartInto` ~1015 及其调用点 786-811）。决策流水表插在该详情面板净值图之后。

- [ ] **Step 2: 加前端数据加载 + 表渲染（在 `renderChartInto` 之前定义）**

```js
const decCache = new Map()   // `${run_id}|${bot}` -> run-decisions rows
async function loadRunDecisionsFE(runId, bot){
  const k = `${runId}|${bot}`
  if (decCache.has(k)) return decCache.get(k)
  let rows = []
  try { const r = await fetch(`/api/backtest/run-decisions?run_id=${encodeURIComponent(runId)}&bot=${encodeURIComponent(bot)}`, { cache: 'no-store' }); if (r.ok) rows = (await r.json()).rows || [] }
  catch { rows = [] }
  decCache.set(k, rows); return rows
}
function pctW(v){ return v == null ? '—' : (Number(v) * 100).toFixed(1) + '%' }
function decTableHtml(rows){
  if (!rows.length) return '<div class="dec-empty">暂无决策记录</div>'
  const body = rows.map(p => {
    const sideCls = p.side === 'sell' ? 'sell' : 'buy'
    const sideTxt = p.side === 'sell' ? '▼ 卖' : '▲ 买'
    const pend = p.status === 'pending' ? ' dec-pending' : ''
    const w = (p.beforeWeight != null || p.afterWeight != null) ? `${pctW(p.beforeWeight)}→${pctW(p.afterWeight)}` : '—'
    const amt = new Intl.NumberFormat('zh-CN',{maximumFractionDigits:0}).format(Number(p.amount||0))
    return `<tr class="${pend.trim()}"><td>${esc(p.date)}${p.status==='pending'?' <span class="dec-tag">待确认</span>':''}</td>`
      + `<td class="${sideCls}">${sideTxt}</td><td>${esc(p.fund)}</td><td>${w}</td><td>¥${amt}</td><td class="dec-reason" title="${esc(p.reason)}">${esc(String(p.reason).slice(0,60))}</td></tr>`
  }).join('')
  return `<div class="dec-panel"><div class="dec-title">决策流水</div><table class="dec-table"><thead><tr><th>日期</th><th>方向</th><th>标的</th><th>仓位</th><th>金额</th><th>理由</th></tr></thead><tbody>${body}</tbody></table></div>`
}
```

- [ ] **Step 3: 在详情渲染后插入流水表**

在打开 run 详情、`renderChartInto(panel, rec, bot)` 调用之后（786-811 的分支里，成功渲染净值图那一处），追加：
```js
      loadRunDecisionsFE(rec['run_id'], rec['bot']).then(rows => {
        const holder = panel.querySelector('.dec-holder') || (() => { const d = document.createElement('div'); d.className = 'dec-holder'; panel.appendChild(d); return d })()
        holder.innerHTML = decTableHtml(rows)
      })
```
（若 `panel` 变量名或容器结构不同，按 Step 1 实际定位调整；核心是把 `decTableHtml(rows)` 注入详情面板尾部。）

- [ ] **Step 4: 加 CSS**

```css
.dec-panel{ margin-top:10px; }
.dec-title{ font-weight:600; margin:6px 0; }
.dec-table{ width:100%; border-collapse:collapse; font-size:12px; }
.dec-table th,.dec-table td{ padding:3px 6px; border-bottom:1px solid var(--border,#333); text-align:left; }
.dec-table td.buy{ color:var(--gain,#e5484d); } .dec-table td.sell{ color:var(--loss,#30a46c); }
.dec-table tr.dec-pending{ opacity:.7; } .dec-tag{ font-size:10px; color:#999; border:1px dashed #999; border-radius:3px; padding:0 3px; }
.dec-reason{ max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.dec-empty{ color:var(--muted,#888); font-size:12px; padding:6px 0; }
```

- [ ] **Step 5: 端到端核对**

Run: 同 Task 5 Step 6 起服务。浏览器点开一个今天有决策的 live run（如 live-bot18），详情面板净值图下应出现「决策流水」表：历史 ADD/REDUCE 行 + 今日 pending 行（标灰「待确认」），仓位前→后、金额、理由齐全。`kill %1`。

- [ ] **Step 6: 提交**

```bash
cd /home/rooot/agent_invest_lab
git add world/src/backtest-dashboard/runs.html
git commit -m "feat(dashboard): runs.html 详情加决策流水面板（历史动作 + 今日 pending）"
```

---

## 验证（全量回归）

- [ ] **全测**：`cd world && node --experimental-strip-types --test test/stitch-series.test.ts test/live-runs.test.ts test/backtest-dashboard.test.ts` 全绿；原有用例数不减。
- [ ] **回归修复生效**：起服务，`runs.html` 侧栏指数目录项对「今天有 pending 的 live run」重新出现三色标（加▲/减▼/清⊘）。
- [ ] **一级页外透**：live-bot7（今日卖）行「今日决策」列显 ▼卖 + 灰虚线待确认 + hover 理由；无今日决策的行显「—」。
- [ ] **流水面板**：点开 live-bot18，详情见历史 ADD/REDUCE（至 07-20）+ 今日 pending 行。
- [ ] **曲线 marker**：点开一个今天有决策的 run，详情净值图 live 段（右侧）出现买卖点；hover 出日期/方向/金额/标的。
- [ ] **不回归**：`index.html` / `market-reports.html` 的 dash 详情净值图、`backtest`/`daily_oos` 段着色与既有 marker 不变；多基金 bot101/102/103 不受影响。
- [ ] **settle 后复查**（今天 18:30 后）：pending 转 confirmed，一级页灰虚线转实、流水面板 pending 行转历史行、net_value 点补上、拼接曲线延伸出 live 段。

## 后续（不在本计划范围）

- live run 重评 UI（本次只继承源 dash 评测，不重评）。
- 决策与总仓位堆叠子图叠加（本次只做净值线上的买卖点）。
- 多基金 bot101-103 的决策展示（本次聚焦单指数 live run）。
