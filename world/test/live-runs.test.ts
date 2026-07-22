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
