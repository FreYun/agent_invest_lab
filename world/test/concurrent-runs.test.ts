import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeState, readState, listActiveRuns, type WorldState } from '../src/state.ts'
import { runStateFile } from '../src/paths.ts'

function tmpWorldRoot(): string {
  return mkdtempSync(join(tmpdir(), 'concurrent-runs-'))
}

const base: Omit<WorldState, 'run_id'> = {
  status: 'running',
  current_date: '2024-03-15',
  trading_dates: ['2024-03-14', '2024-03-15'],
  cursor: 0,
  bots: ['bot1'],
  memory_port: 0,
  started_at: '',
  updated_at: '',
  loop: 'research-loop',
}

test('two runs of the same bot have independent state files and dont overwrite each other', () => {
  const w = tmpWorldRoot()
  writeState(w, 'runA', { ...base, run_id: 'runA', started_at: '2026-05-15T10:00:00.000Z', updated_at: '2026-05-15T10:00:00.000Z' })
  writeState(w, 'runB', { ...base, run_id: 'runB', cursor: 5, started_at: '2026-05-15T10:05:00.000Z', updated_at: '2026-05-15T10:05:00.000Z' })

  // Both files exist independently
  assert.ok(existsSync(runStateFile(w, 'runA')))
  assert.ok(existsSync(runStateFile(w, 'runB')))

  // Reads are independent
  assert.equal(readState(w, 'runA').cursor, 0)
  assert.equal(readState(w, 'runB').cursor, 5)

  // listActiveRuns sees both, B first (newer started_at)
  const active = listActiveRuns(w)
  assert.deepEqual(active.map(s => s.run_id), ['runB', 'runA'])

  // Updating runA must not affect runB
  writeState(w, 'runA', { ...base, run_id: 'runA', cursor: 99, started_at: '2026-05-15T10:00:00.000Z', updated_at: '2026-05-15T10:30:00.000Z' })
  assert.equal(readState(w, 'runA').cursor, 99)
  assert.equal(readState(w, 'runB').cursor, 5)

  // Marking A done removes it from listActiveRuns but keeps B
  writeState(w, 'runA', { ...readState(w, 'runA'), status: 'done' })
  assert.deepEqual(listActiveRuns(w).map(s => s.run_id), ['runB'])

  rmSync(w, { recursive: true, force: true })
})
