import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as P from '../src/paths.ts'
import { runStateFile } from '../src/paths.ts'

const W = '/tmp/wr'
const R = 'run1'

test('path helpers compose under worldRoot', () => {
  assert.equal(P.calendarFile(W), '/tmp/wr/calendar.json')
  assert.equal(P.dayDir(W, '2024-03-15'), '/tmp/wr/days/2024-03-15')
  assert.equal(P.quotesFile(W, '2024-03-15'), '/tmp/wr/days/2024-03-15/quotes.json')
  assert.equal(P.overviewFile(W, '2024-03-15'), '/tmp/wr/days/2024-03-15/overview.md')
  assert.equal(P.eventsFile(W, '2024-03-15'), '/tmp/wr/days/2024-03-15/events.json')
  assert.equal(P.runDir(W, R), '/tmp/wr/runs/run1')
  assert.equal(P.runConfigFile(W, R), '/tmp/wr/runs/run1/trading-rl-config.json')
  assert.equal(P.shadowWorkspaceDir(W, R, 'bot7'), '/tmp/wr/runs/run1/workspaces/bot7')
  assert.equal(P.rlOpenclawDir(W, R), '/tmp/wr/runs/run1/rl-openclaw')
  assert.equal(P.memoryStoreFile(W, R), '/tmp/wr/runs/run1/memory/store.jsonl')
  assert.equal(P.memoryRuntimeFile(W, R), '/tmp/wr/runs/run1/memory/runtime.json')
  assert.equal(P.botDayDir(W, R, '2024-03-15', 'bot7'), '/tmp/wr/runs/run1/2024-03-15/bot7')
  assert.equal(P.sentFile(W, R, '2024-03-15', 'bot7'), '/tmp/wr/runs/run1/2024-03-15/bot7/sent.md')
  assert.equal(P.replyFile(W, R, '2024-03-15', 'bot7'), '/tmp/wr/runs/run1/2024-03-15/bot7/reply.json')
  assert.equal(P.statusFile(W, R, '2024-03-15', 'bot7'), '/tmp/wr/runs/run1/2024-03-15/bot7/status.json')
  assert.equal(P.runLogFile(W, R), '/tmp/wr/runs/run1/run.log')
  assert.equal(P.summaryFile(W, R), '/tmp/wr/runs/run1/summary.json')
  assert.equal(P.stopFile(W, R), '/tmp/wr/runs/run1/STOP')
})

test('runStateFile lives under runDir, not worldRoot', () => {
  const w = '/tmp/wroot'
  const runId = 'dash-2026-05-15T09-13-27'
  const p = runStateFile(w, runId)
  assert.equal(p, '/tmp/wroot/runs/dash-2026-05-15T09-13-27/state.json')
})

test('stateFile (legacy) no longer exists as an export', async () => {
  const mod = await import('../src/paths.ts')
  assert.equal((mod as Record<string, unknown>).stateFile, undefined)
})
