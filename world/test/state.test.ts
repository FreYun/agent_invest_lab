import { mkdtempSync, rmSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeState, readState, stateExists, type WorldState } from '../src/state.ts'

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

test('writeState then readState round-trips, and on-disk JSON is snake_case', () => {
  const w = tmpWorldRoot()
  assert.equal(stateExists(w), false)
  writeState(w, sample)
  assert.equal(stateExists(w), true)
  const got = readState(w)
  assert.deepEqual(got, sample)
  const onDisk = JSON.parse(readFileSync(join(w, 'state.json'), 'utf8'))
  assert.ok('current_date' in onDisk && 'trading_dates' in onDisk)
  rmSync(w, { recursive: true, force: true })
})

test('readState throws when missing', () => {
  const w = tmpWorldRoot()
  assert.throws(() => readState(w), /state\.json/)
  rmSync(w, { recursive: true, force: true })
})

test('writeState is atomic (no partial file) and overwrites prior state', () => {
  const w = tmpWorldRoot()
  writeState(w, sample)
  writeState(w, { ...sample, cursor: 2 })
  assert.equal(readState(w).cursor, 2)
  rmSync(w, { recursive: true, force: true })
})

test('WorldState round-trips loop field', () => {
  const w = tmpWorldRoot()
  const stateWithLoop: WorldState = { ...sample, loop: 'openclaw-pi' }
  writeState(w, stateWithLoop)
  const got = readState(w)
  assert.equal(got.loop, 'openclaw-pi')
  rmSync(w, { recursive: true, force: true })
})

test('readState back-fills loop=research-loop for legacy state without the field', () => {
  const w = tmpWorldRoot()
  const path = join(w, 'state.json')
  const legacy = { ...sample } as Record<string, unknown>
  delete legacy.loop
  // simulate legacy on-disk format (no loop field)
  writeFileSync(path, JSON.stringify(legacy))
  assert.equal(readState(w).loop, 'research-loop')
  rmSync(w, { recursive: true, force: true })
})
