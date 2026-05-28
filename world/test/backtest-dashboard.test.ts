import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import assert from 'node:assert/strict'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { writeState, readState, type WorldState } from '../src/state.ts'
import { pauseFile } from '../src/paths.ts'
import { pickBenchmarkFund, allocateQuota, selectRepresentative, classifyBusinType, mergeUserTxns } from '../src/backtest-dashboard/server.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const DB = join(HERE, '../../data/fund.db')
const SERVER = join(HERE, '../src/backtest-dashboard/server.ts')

function waitForOutput(proc: ReturnType<typeof spawn> & { stdout: NonNullable<ReturnType<typeof spawn>['stdout']>; stderr: NonNullable<ReturnType<typeof spawn>['stderr']> }, pattern: RegExp, timeoutMs = 10000): Promise<string> {
  return new Promise((resolveP, reject) => {
    let buf = ''
    const timer = setTimeout(() => reject(new Error(`timeout waiting for server output: ${buf}`)), timeoutMs)
    proc.stdout.on('data', (d: Buffer) => {
      buf += d.toString()
      if (pattern.test(buf)) {
        clearTimeout(timer)
        resolveP(buf)
      }
    })
    proc.stderr.on('data', (d: Buffer) => { buf += d.toString() })
    proc.on('exit', (code: number | null) => {
      clearTimeout(timer)
      if (!pattern.test(buf)) reject(new Error(`exit ${code}: ${buf}`))
    })
  })
}

test('backtest dashboard serves data', async () => {
  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /backtest dashboard listening on http:\/\//)
    const match = out.match(/http:\/\/127\.0\.0\.1:(\d+)\//)
    assert.ok(match)
    const base = `http://127.0.0.1:${match[1]}`

    const rootRes = await fetch(base)
    assert.equal(rootRes.status, 200)
    const html = await rootRes.text()
    assert.match(html, /Bot 回测看板/)

    type BotRun = { runId: string; latestDate: string }
    type Bot = {
      botId: string
      runId: string
      availableRuns: BotRun[]
      benchmark: {
        fundCode: string
        fundName: string
        anchorDate: string
        baselineNav: number
        series: Array<{ trade_date: string; nav: number; net_value: number }>
      } | null
    }
    type Payload = { summary: { botCount: number }; bots: Bot[] }

    // /api/backtest/data: returns every bot (across all runs), each with its own
    // current-run snapshot + the list of run_ids that bot has been seen in.
    const defRes = await fetch(`${base}/api/backtest/data`)
    assert.equal(defRes.status, 200)
    const def = await defRes.json() as Payload
    assert.ok(def.bots.length >= 1, 'at least one bot is listed by default')
    for (const b of def.bots) {
      assert.ok(b.botId, 'each bot has a botId')
      assert.ok(typeof b.runId === 'string' && b.runId.length > 0, `bot ${b.botId} has a runId`)
      assert.equal((b as { realUsers?: unknown }).realUsers, undefined, `summary 不含 realUsers (${b.botId})`)
      assert.ok(Array.isArray(b.availableRuns) && b.availableRuns.length > 0, `bot ${b.botId} has availableRuns`)
      assert.ok(b.availableRuns.some(r => r.runId === b.runId), `bot ${b.botId} runId is in its availableRuns`)
      // sorted newest-first by runId (timestamps are lexicographically chronological)
      for (let i = 1; i < b.availableRuns.length; i++) {
        assert.ok(b.availableRuns[i - 1].runId >= b.availableRuns[i].runId, `bot ${b.botId} availableRuns sorted desc`)
      }
    }
    // bot discovery must UNION across all 8 fund_bot_* tables — bots with only
    // performance/holdings/orders records (no daily snapshots) must still appear.
    // Looser bound to survive ad-hoc DB cleanup; the underlying union behavior
    // is still exercised by the per-bot fields below.

    // /api/backtest/bot returns a single bot scoped to the requested run_id.
    const target = def.bots[0]
    const oneRes = await fetch(`${base}/api/backtest/bot?bot_id=${encodeURIComponent(target.botId)}&run_id=${encodeURIComponent(target.runId)}`)
    assert.equal(oneRes.status, 200)
    const one = await oneRes.json() as Bot
    assert.equal(one.botId, target.botId)
    assert.equal(one.runId, target.runId)

    // realUsers: 仅 per-bot 详情返回；数组,数量受 MAX_REAL_USERS(10) 约束
    const oneRU = one as Bot & {
      realUsers?: Array<{ fundCode: string; cycleId: string; clearReturn2: number; series: Array<{ trade_date: string; net_value: number }> }>
    }
    assert.ok(Array.isArray(oneRU.realUsers), 'per-bot 响应含 realUsers 数组')
    assert.ok(oneRU.realUsers!.length <= 10, 'realUsers ≤ 10')
    for (const u of oneRU.realUsers!) {
      assert.ok(typeof u.fundCode === 'string' && u.fundCode.length > 0, '用户有 fundCode')
      assert.ok(Array.isArray(u.series), '用户有 series 数组')
    }

    type HoldingByDateRow = { fund_code: string; fund_name: string; weight: number; market_value: number }
    type BotWithExt = Bot & {
      holdingsByDate: Record<string, HoldingByDateRow[]>
      holdings: HoldingByDateRow[]
      actions: Array<{
        action_id: number
        action_date: string
        fund_code: string
        weight_before: number
        weight_after: number
        weight_delta: number
      }>
      series: Array<{ trade_date: string }>
    }
    const oneExt = one as BotWithExt
    assert.ok(oneExt.holdingsByDate && typeof oneExt.holdingsByDate === 'object', 'response has holdingsByDate')
    for (const s of oneExt.series.slice(-3)) {
      const day = oneExt.holdingsByDate[s.trade_date]
      assert.ok(day === undefined || Array.isArray(day), `holdingsByDate['${s.trade_date}'] is array or absent`)
    }
    assert.ok(Array.isArray(oneExt.holdings), 'legacy holdings array preserved')
    for (const a of oneExt.actions) {
      assert.ok(typeof a.weight_before === 'number', `action ${a.action_id} has weight_before`)
      assert.ok(typeof a.weight_after === 'number', `action ${a.action_id} has weight_after`)
      assert.ok(typeof a.weight_delta === 'number', `action ${a.action_id} has weight_delta`)
      assert.ok(Math.abs((a.weight_after - a.weight_before) - a.weight_delta) < 1e-9, 'weight_delta = after - before')
    }

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

    // bad bot_id / run_id combo → 404
    const badRes = await fetch(`${base}/api/backtest/bot?bot_id=does-not-exist&run_id=nope`)
    assert.equal(badRes.status, 404)
  } finally {
    proc.kill('SIGTERM')
  }
})

test('dashboard exposes live run control (GET /api/backtest/runs, POST pause/stop)', async () => {
  const worldRoot = mkdtempSync(join(tmpdir(), 'dash-runs-'))
  const baseState: WorldState = {
    run_id: 'x', status: 'running', current_date: '2024-03-15',
    trading_dates: ['2024-03-14', '2024-03-15', '2024-03-18'], cursor: 1,
    bots: ['bot1'], memory_port: 0,
    started_at: '2026-05-11T10:00:00.000Z', updated_at: '2026-05-11T10:00:00.000Z',
    loop: 'research-loop',
  }
  writeState(worldRoot, 'rrun', { ...baseState, run_id: 'rrun', status: 'running', started_at: '2026-05-11T08:00:00.000Z' })
  writeState(worldRoot, 'rpaused', { ...baseState, run_id: 'rpaused', status: 'paused', started_at: '2026-05-11T12:00:00.000Z' })
  writeState(worldRoot, 'rdone', { ...baseState, run_id: 'rdone', status: 'done', started_at: '2026-05-11T14:00:00.000Z' })

  const proc = spawn(process.execPath, ['--experimental-strip-types', SERVER, '--host', '127.0.0.1', '--port', '0', '--db', DB, '--world-root', worldRoot], { stdio: ['ignore', 'pipe', 'pipe'] })
  try {
    const out = await waitForOutput(proc, /backtest dashboard listening on http:\/\//)
    const base = `http://127.0.0.1:${out.match(/http:\/\/127\.0\.0\.1:(\d+)\//)![1]}`

    // GET /api/backtest/runs → controllable runs (running + paused), newest first, done excluded
    const listRes = await fetch(`${base}/api/backtest/runs`)
    assert.equal(listRes.status, 200)
    const { runs } = await listRes.json() as { runs: Array<{ runId: string; status: string; cursor: number; total: number }> }
    assert.deepEqual(runs.map(r => r.runId), ['rpaused', 'rrun'])
    const rrun = runs.find(r => r.runId === 'rrun')!
    assert.equal(rrun.status, 'running')
    assert.equal(rrun.cursor, 1)
    assert.equal(rrun.total, 3)

    // POST pause without runId → 400
    const noId = await fetch(`${base}/api/backtest/runs/pause`, { method: 'POST', body: '{}' })
    assert.equal(noId.status, 400)

    // POST pause a running run → 200 ok, PAUSE sentinel written
    const pauseRes = await fetch(`${base}/api/backtest/runs/pause`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ runId: 'rrun' }) })
    assert.equal(pauseRes.status, 200)
    assert.deepEqual(await pauseRes.json(), { ok: true })
    assert.ok(existsSync(pauseFile(worldRoot, 'rrun')), 'PAUSE sentinel written')

    // POST pause a non-running (done) run → 409 conflict
    const pauseDone = await fetch(`${base}/api/backtest/runs/pause`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ runId: 'rdone' }) })
    assert.equal(pauseDone.status, 409)

    // POST stop a paused run → flips straight to aborted (terminal)
    const stopRes = await fetch(`${base}/api/backtest/runs/stop`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ runId: 'rpaused' }) })
    assert.equal(stopRes.status, 200)
    assert.equal(readState(worldRoot, 'rpaused').status, 'aborted')
  } finally {
    proc.kill('SIGTERM')
    rmSync(worldRoot, { recursive: true, force: true })
  }
})

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

test('allocateQuota: 单池上限封顶到候选数', () => {
  const q = allocateQuota(new Map([['A', 12]]), 10)
  assert.equal(q.get('A'), 10)
})

test('allocateQuota: 双池平均分', () => {
  const q = allocateQuota(new Map([['A', 20], ['B', 20]]), 10)
  assert.equal(q.get('A'), 5)
  assert.equal(q.get('B'), 5)
})

test('allocateQuota: 余数给候选更多的池,且不超可用', () => {
  const q = allocateQuota(new Map([['A', 3], ['B', 20]]), 10)
  assert.equal(q.get('A'), 3)            // capped by availability
  assert.equal(q.get('B'), 7)            // base 5 + remainder 2
  assert.equal(q.get('A')! + q.get('B')!, 10)
})

test('allocateQuota: 候选总量不足时不超总可用', () => {
  const q = allocateQuota(new Map([['A', 4], ['B', 4]]), 10)
  assert.equal(q.get('A'), 4)
  assert.equal(q.get('B'), 4)
})

test('selectRepresentative: quota>=n 全取', () => {
  const arr = [1, 2, 3]
  assert.deepEqual(selectRepresentative(arr, 5), [1, 2, 3])
})

test('selectRepresentative: 覆盖最差/最好/中位,去重,且为输入子集', () => {
  const arr = Array.from({ length: 20 }, (_, i) => i)  // 升序 0..19
  const picked = selectRepresentative(arr, 10)
  assert.equal(picked.length, 10)
  assert.ok(picked.includes(0), '含最差(0)')
  assert.ok(picked.includes(19), '含最好(19)')
  assert.ok(picked.includes(10), '含中位(round(0.5*19))')
  assert.equal(new Set(picked).size, picked.length, '无重复')
  for (const v of picked) assert.ok(arr.includes(v), '是输入子集')
  for (let i = 1; i < picked.length; i++) assert.ok(picked[i] > picked[i - 1])
})

test('selectRepresentative: quota<=0 或空输入 → 空', () => {
  assert.deepEqual(selectRepresentative([1, 2, 3], 0), [])
  assert.deepEqual(selectRepresentative([], 5), [])
})

test('classifyBusinType: 定投/申购=买, 赎回/强赎=卖, 其余=null', () => {
  assert.equal(classifyBusinType('139'), 'buy')   // 定时定额投资
  assert.equal(classifyBusinType('122'), 'buy')   // 申购
  assert.equal(classifyBusinType('124'), 'sell')  // 赎回
  assert.equal(classifyBusinType('142'), 'sell')  // 强行赎回
  assert.equal(classifyBusinType('129'), null)    // 设置分红方式
  assert.equal(classifyBusinType('136'), null)    // 转换
  assert.equal(classifyBusinType('1T1'), null)    // 转出投资账户
  assert.equal(classifyBusinType(''), null)
})

test('mergeUserTxns: 同日同方向合并 amount/count, 丢弃中性, 按日期再方向升序', () => {
  const rows = [
    { cycle_id: 'A', busin_type: '139', amount: 100, txn_date: '2025-03-20' },
    { cycle_id: 'A', busin_type: '122', amount: 50, txn_date: '2025-03-20' },   // 同日同向(买) → 合并
    { cycle_id: 'A', busin_type: '124', amount: 30, txn_date: '2025-03-20' },   // 同日卖 → 单独
    { cycle_id: 'A', busin_type: '129', amount: 0, txn_date: '2025-03-20' },    // 中性 → 丢弃
    { cycle_id: 'A', busin_type: '139', amount: 100, txn_date: '2025-01-10' },  // 更早的买
    { cycle_id: 'B', busin_type: '124', amount: 999, txn_date: '2025-06-01' },  // 另一个 cycle
  ]
  const out = mergeUserTxns(rows)
  assert.deepEqual(out.get('A'), [
    { date: '2025-01-10', side: 'buy', amount: 100, count: 1 },
    { date: '2025-03-20', side: 'buy', amount: 150, count: 2 },
    { date: '2025-03-20', side: 'sell', amount: 30, count: 1 },
  ])
  assert.deepEqual(out.get('B'), [
    { date: '2025-06-01', side: 'sell', amount: 999, count: 1 },
  ])
})

test('mergeUserTxns: 全中性或空 → 该 cycle 不出现', () => {
  const out = mergeUserTxns([
    { cycle_id: 'A', busin_type: '129', amount: 0, txn_date: '2025-03-20' },
    { cycle_id: 'A', busin_type: '136', amount: 5, txn_date: '2025-03-21' },
  ])
  assert.equal(out.has('A'), false)
  assert.equal(out.size, 0)
})

test('mergeUserTxns: amount 为 null 当 0 处理', () => {
  const out = mergeUserTxns([
    { cycle_id: 'A', busin_type: '139', amount: null, txn_date: '2025-03-20' },
    { cycle_id: 'A', busin_type: '139', amount: 100, txn_date: '2025-03-20' },
  ])
  assert.deepEqual(out.get('A'), [{ date: '2025-03-20', side: 'buy', amount: 100, count: 2 }])
})
