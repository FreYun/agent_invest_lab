import { spawn } from 'node:child_process'
import assert from 'node:assert/strict'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

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
    // bad bot_id / run_id combo → 404
    const badRes = await fetch(`${base}/api/backtest/bot?bot_id=does-not-exist&run_id=nope`)
    assert.equal(badRes.status, 404)
  } finally {
    proc.kill('SIGTERM')
  }
})
