// world/test/live-runs.test.ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { isLiveRunId, loadReflectionForRun, readLiveRunLink, listLiveRuns, sourceRunIdFromLiveRunId } from '../src/backtest-dashboard/server.ts'

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

test('readLiveRunLink 兜底：state.json 丢了 source_run_id 时从 run id 反推源 dash', () => {
  const world = mkdtempSync(join(tmpdir(), 'world-'))
  try {
    const d = join(world, 'runs', 'live-bot20-20260601T120000'); mkdirSync(d, { recursive: true })
    // 模拟实盘引擎重写后的 state.json：有 bots、无 source_run_id。
    writeFileSync(join(d, 'state.json'), JSON.stringify({ run_id: 'live-bot20-20260601T120000', bots: ['bot20'], status: 'done' }))
    const link = readLiveRunLink(world, 'live-bot20-20260601T120000')
    assert.deepEqual(link, { botId: 'bot20', sourceRunId: 'dash-2026-06-01T12-00-00' })
  } finally { rmSync(world, { recursive: true, force: true }) }
})

test('sourceRunIdFromLiveRunId 反推与无法解析', () => {
  assert.equal(sourceRunIdFromLiveRunId('live-bot10-20260616T095923'), 'dash-2026-06-16T09-59-23')
  assert.equal(sourceRunIdFromLiveRunId('live-broken'), '')
})


test("live 历史决策在本 run 无内容时回退 source run，live 当日内容优先", () => {
  const world = mkdtempSync(join(tmpdir(), "world-"))
  const liveRun = "live-bot20-20260601T120000"
  const sourceRun = "dash-2026-06-01T12-00-00"
  const date = "2026-05-29"
  try {
    const liveDir = join(world, "runs", liveRun)
    const sourceDay = join(world, "runs", sourceRun, date, "bot20")
    mkdirSync(liveDir, { recursive: true })
    mkdirSync(sourceDay, { recursive: true })
    writeFileSync(join(liveDir, "state.json"), JSON.stringify({ bots: ["bot20"], source_run_id: sourceRun }))
    writeFileSync(join(sourceDay, "sent.md"), "【交易记忆窗口】\n源回测记忆")
    writeFileSync(join(sourceDay, "reply.json"), JSON.stringify({ reply: "源回测决策" }))

    assert.deepEqual(loadReflectionForRun(world, liveRun, "bot20", date), {
      tradeDate: date,
      memoryWindow: "【交易记忆窗口】\n源回测记忆",
      decision: "源回测决策",
    })

    const liveDay = join(liveDir, date, "bot20")
    mkdirSync(liveDay, { recursive: true })
    writeFileSync(join(liveDay, "reply.json"), JSON.stringify({ reply: "live 当日决策" }))
    assert.deepEqual(loadReflectionForRun(world, liveRun, "bot20", date), {
      tradeDate: date,
      memoryWindow: "",
      decision: "live 当日决策",
    })
  } finally { rmSync(world, { recursive: true, force: true }) }
})
