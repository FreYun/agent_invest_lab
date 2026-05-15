import { mkdtempSync, rmSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeState, readState, stateExists, listActiveRuns, type WorldState } from '../src/state.ts'
import { runDir, runStateFile } from '../src/paths.ts'

function tmpWorldRoot(): string {
  return mkdtempSync(join(tmpdir(), 'wstate-'))
}

const sample: WorldState = {
  run_id: 'r1',
  status: 'running',
  current_date: '2024-03-15',
  trading_dates: ['2024-03-14', '2024-03-15', '2024-03-18'],
  cursor: 1,
  bots: ['bot1', 'bot7'],
  memory_port: 41873,
  started_at: '2026-05-11T10:00:00.000Z',
  updated_at: '2026-05-11T10:01:00.000Z',
  loop: 'research-loop',
}

test('writeState/readState round-trip lives under runDir(runId)', () => {
  const w = tmpWorldRoot()
  assert.equal(stateExists(w, 'r1'), false)
  writeState(w, 'r1', sample)
  assert.equal(stateExists(w, 'r1'), true)
  assert.deepEqual(readState(w, 'r1'), sample)
  const onDisk = JSON.parse(readFileSync(runStateFile(w, 'r1'), 'utf8'))
  assert.ok('current_date' in onDisk && 'trading_dates' in onDisk)
  rmSync(w, { recursive: true, force: true })
})

test('readState throws when missing', () => {
  const w = tmpWorldRoot()
  assert.throws(() => readState(w, 'missing'), /state\.json/)
  rmSync(w, { recursive: true, force: true })
})

test('two runs write independent state files', () => {
  const w = tmpWorldRoot()
  writeState(w, 'r1', sample)
  writeState(w, 'r2', { ...sample, run_id: 'r2', cursor: 9 })
  assert.equal(readState(w, 'r1').cursor, 1)
  assert.equal(readState(w, 'r2').cursor, 9)
  rmSync(w, { recursive: true, force: true })
})

test('readState back-fills loop=research-loop for legacy state without the field', () => {
  const w = tmpWorldRoot()
  mkdirSync(runDir(w, 'r1'), { recursive: true })
  const legacy = { ...sample } as Record<string, unknown>
  delete legacy.loop
  writeFileSync(runStateFile(w, 'r1'), JSON.stringify(legacy))
  assert.equal(readState(w, 'r1').loop, 'research-loop')
  rmSync(w, { recursive: true, force: true })
})

test('listActiveRuns returns empty when no runs', () => {
  const w = tmpWorldRoot()
  assert.deepEqual(listActiveRuns(w), [])
  rmSync(w, { recursive: true, force: true })
})

test('listActiveRuns returns only status=running, sorted by started_at desc', () => {
  const w = tmpWorldRoot()
  writeState(w, 'old-done', { ...sample, run_id: 'old-done', status: 'done', started_at: '2026-05-10T10:00:00.000Z' })
  writeState(w, 'early', { ...sample, run_id: 'early', status: 'running', started_at: '2026-05-11T08:00:00.000Z' })
  writeState(w, 'late', { ...sample, run_id: 'late', status: 'running', started_at: '2026-05-11T12:00:00.000Z' })
  writeState(w, 'failed', { ...sample, run_id: 'failed', status: 'failed', started_at: '2026-05-11T13:00:00.000Z' })
  const active = listActiveRuns(w)
  assert.deepEqual(active.map(s => s.run_id), ['late', 'early'])
  rmSync(w, { recursive: true, force: true })
})

test('listActiveRuns skips run dirs without state.json and corrupt state.json', () => {
  const w = tmpWorldRoot()
  mkdirSync(runDir(w, 'no-state'), { recursive: true })
  mkdirSync(runDir(w, 'corrupt'), { recursive: true })
  writeFileSync(runStateFile(w, 'corrupt'), '{not json')
  writeState(w, 'ok', { ...sample, run_id: 'ok', status: 'running' })
  const active = listActiveRuns(w)
  assert.deepEqual(active.map(s => s.run_id), ['ok'])
  rmSync(w, { recursive: true, force: true })
})
