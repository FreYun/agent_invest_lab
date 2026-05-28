import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { realignRunDates } from '../src/realign.ts'
import { readState, writeState, type WorldState } from '../src/state.ts'
import * as P from '../src/paths.ts'

// Build a fake run on disk: trading_dates list, world per-day dirs for a subset
// of those, plus openclaw sessions.json + per-day jsonl/state.json sidecars for
// a DIFFERENT subset. realignRunDates should leave the world side alone, trim
// openclaw to match, and pull state.json's cursor onto the first missing date.

function setupRun(worldDates: string[], openclawDates: string[]): { worldRoot: string; runId: string; cleanup: () => void } {
  const root = mkdtempSync(join(tmpdir(), 'world-realign-'))
  const worldRoot = join(root, 'world')
  const runId = 'r-realign-1'
  const tradingDates = ['2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08']
  const runDir = P.runDir(worldRoot, runId)
  mkdirSync(runDir, { recursive: true })
  for (const d of worldDates) mkdirSync(join(runDir, d, 'bot14'), { recursive: true })
  const state: WorldState = {
    run_id: runId, status: 'paused', current_date: tradingDates[tradingDates.length - 1],
    trading_dates: tradingDates, cursor: tradingDates.length, bots: ['bot14'],
    memory_port: 0, started_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    loop: 'research-loop', aborted_reason: 'old orphan note',
  }
  writeState(worldRoot, runId, state)
  // pretend teardown ran
  writeFileSync(P.summaryFile(worldRoot, runId), JSON.stringify({ run_id: runId, status: 'failed', days: [] }))
  // openclaw side
  const sessDir = join(P.rlOpenclawDir(worldRoot, runId), 'agents', 'bot14', 'sessions')
  mkdirSync(sessDir, { recursive: true })
  const sessions: Record<string, { sessionId: string; sessionFile: string; updatedAt: number }> = {}
  // unrelated chat key — must survive realign untouched
  sessions['agent:bot14:adhoc-debug-chat'] = { sessionId: 'sid-adhoc', sessionFile: join(sessDir, 'sid-adhoc.jsonl'), updatedAt: 1 }
  writeFileSync(join(sessDir, 'sid-adhoc.jsonl'), '{"keep":"me"}\n')
  writeFileSync(join(sessDir, 'sid-adhoc.state.json'), '{}')
  for (const d of openclawDates) {
    const sid = `sid-${d}`
    sessions[`agent:bot14:trading-${runId}-${d}`] = { sessionId: sid, sessionFile: join(sessDir, `${sid}.jsonl`), updatedAt: Date.parse(`${d}T00:00:00Z`) }
    writeFileSync(join(sessDir, `${sid}.jsonl`), `${d} jsonl\n`)
    writeFileSync(join(sessDir, `${sid}.state.json`), '{}')
  }
  writeFileSync(join(sessDir, 'sessions.json'), JSON.stringify(sessions, null, 2))
  return { worldRoot, runId, cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

test('realign: openclaw orphan dates are deleted, in-sync dates kept, unrelated keys untouched', () => {
  const { worldRoot, runId, cleanup } = setupRun(
    ['2025-01-02', '2025-01-03'],                           // world has only 2 days
    ['2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07'] // openclaw has 4
  )
  const r = realignRunDates(worldRoot, runId)
  const bot = r.perBot.find(b => b.botId === 'bot14')
  assert.ok(bot)
  assert.deepEqual(bot.removedSessionKeys.sort(), [
    `agent:bot14:trading-${runId}-2025-01-06`,
    `agent:bot14:trading-${runId}-2025-01-07`,
  ])
  // unrelated chat key must still be in the kept set
  assert.ok(bot.keptSessionKeys.includes('agent:bot14:adhoc-debug-chat'))
  // orphan jsonl + sidecar gone from disk
  const sessDir = join(P.rlOpenclawDir(worldRoot, runId), 'agents', 'bot14', 'sessions')
  assert.equal(existsSync(join(sessDir, 'sid-2025-01-06.jsonl')), false)
  assert.equal(existsSync(join(sessDir, 'sid-2025-01-06.state.json')), false)
  assert.equal(existsSync(join(sessDir, 'sid-2025-01-07.jsonl')), false)
  // in-sync ones survive
  assert.equal(existsSync(join(sessDir, 'sid-2025-01-02.jsonl')), true)
  assert.equal(existsSync(join(sessDir, 'sid-2025-01-03.jsonl')), true)
  // unrelated stays
  assert.equal(existsSync(join(sessDir, 'sid-adhoc.jsonl')), true)
  // sessions.json no longer lists the orphans
  const after = JSON.parse(readFileSync(join(sessDir, 'sessions.json'), 'utf8'))
  assert.equal(Object.keys(after).some(k => k.endsWith('2025-01-06') || k.endsWith('2025-01-07')), false)
  cleanup()
})

test('realign: state.json cursor/current_date snap to first missing date; aborted_reason cleared; summary.json removed', () => {
  const { worldRoot, runId, cleanup } = setupRun(
    ['2025-01-02', '2025-01-03'],
    ['2025-01-02', '2025-01-03', '2025-01-06']
  )
  const r = realignRunDates(worldRoot, runId)
  assert.equal(r.stateAfter.cursor, 2)                   // first missing = trading_dates[2] = 2025-01-06
  assert.equal(r.stateAfter.current_date, '2025-01-06')
  assert.equal(r.stateAfter.status, 'paused')
  assert.equal(r.stateAfter.aborted_reason, undefined)
  assert.equal(r.summaryRemoved, true)
  const st = readState(worldRoot, runId)
  assert.equal(st.cursor, 2)
  assert.equal(st.current_date, '2025-01-06')
  assert.equal(st.aborted_reason, undefined)
  assert.equal(existsSync(P.summaryFile(worldRoot, runId)), false)
  cleanup()
})

test('realign: dry-run reports the same shape but touches no files', () => {
  const { worldRoot, runId, cleanup } = setupRun(
    ['2025-01-02'],
    ['2025-01-02', '2025-01-03', '2025-01-06']
  )
  const before = readState(worldRoot, runId)
  const sessDir = join(P.rlOpenclawDir(worldRoot, runId), 'agents', 'bot14', 'sessions')
  const r = realignRunDates(worldRoot, runId, { dryRun: true })
  assert.equal(r.perBot[0].removedSessionKeys.length, 2)
  // state.json unchanged
  const after = readState(worldRoot, runId)
  assert.equal(after.cursor, before.cursor)
  assert.equal(after.current_date, before.current_date)
  assert.equal(after.aborted_reason, before.aborted_reason)
  // jsonls still there
  assert.equal(existsSync(join(sessDir, 'sid-2025-01-03.jsonl')), true)
  assert.equal(existsSync(join(sessDir, 'sid-2025-01-06.jsonl')), true)
  assert.equal(existsSync(P.summaryFile(worldRoot, runId)), true)
  cleanup()
})

test('realign: --no-state (preserveState) only syncs openclaw; state.json and summary.json left alone', () => {
  const { worldRoot, runId, cleanup } = setupRun(
    ['2025-01-02'],
    ['2025-01-02', '2025-01-06']
  )
  const before = readState(worldRoot, runId)
  const r = realignRunDates(worldRoot, runId, { preserveState: true })
  assert.equal(r.perBot[0].removedSessionKeys.length, 1)
  // state.json untouched even though world only has 1 day
  const after = readState(worldRoot, runId)
  assert.equal(after.cursor, before.cursor)
  assert.equal(after.aborted_reason, before.aborted_reason)
  assert.equal(r.summaryRemoved, false)
  assert.equal(existsSync(P.summaryFile(worldRoot, runId)), true)
  // openclaw orphan still gone
  const sessDir = join(P.rlOpenclawDir(worldRoot, runId), 'agents', 'bot14', 'sessions')
  assert.equal(existsSync(join(sessDir, 'sid-2025-01-06.jsonl')), false)
  cleanup()
})

test('realign: terminal statuses (done/aborted/failed) keep their status — realign does not revive', () => {
  const { worldRoot, runId, cleanup } = setupRun(['2025-01-02'], ['2025-01-02', '2025-01-06'])
  const s = readState(worldRoot, runId)
  writeState(worldRoot, runId, { ...s, status: 'done' })
  const r = realignRunDates(worldRoot, runId)
  assert.equal(r.stateAfter.status, 'done') // still done, not flipped to paused
  cleanup()
})
