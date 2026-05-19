import { mkdirSync, mkdtempSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeState, readState, listActiveRuns, type WorldState } from '../src/state.ts'
import { runStateFile, buyableCodesFile, buyableCodesDir } from '../src/paths.ts'
import { writeBuyableCodesFile } from '../src/run.ts'

// 镜像真实布局：worldRoot 是 <repo>/world/runtime。buyableCodesDir 走 '..','..','data'
// 相对路径，所以 tmp 也得有 world/runtime 两层套娃，否则 ../../data 会越出 tmp。
function tmpWorldRoot(): string {
  const repo = mkdtempSync(join(tmpdir(), 'concurrent-runs-'))
  const w = join(repo, 'world', 'runtime')
  mkdirSync(w, { recursive: true })
  return w
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

test('two runs write independent buyable code files; neither overwrites the other', () => {
  const w = tmpWorldRoot()
  // 历史 bug：bot11(半导体 014854) 与 bot16(黄金 000216) 并发跑时，bot16 setup 写的全局文件
  // 把 bot11 的可买池覆盖成黄金，bot11 mid-run 持仓被污染。新机制按 run_id 物理隔离文件。
  const pA = writeBuyableCodesFile(w, 'runA', ['014854'])
  const pB = writeBuyableCodesFile(w, 'runB', ['000216'])
  assert.notEqual(pA, pB)
  assert.equal(pA, buyableCodesFile(w, 'runA'))
  assert.equal(pB, buyableCodesFile(w, 'runB'))
  assert.deepEqual(JSON.parse(readFileSync(pA, 'utf8')).fund_codes, ['014854'])
  assert.deepEqual(JSON.parse(readFileSync(pB, 'utf8')).fund_codes, ['000216'])

  // 覆写 runA 不能影响 runB（核心隔离不变式）
  writeBuyableCodesFile(w, 'runA', ['014854', '110011'])
  assert.deepEqual(JSON.parse(readFileSync(pA, 'utf8')).fund_codes, ['014854', '110011'])
  assert.deepEqual(JSON.parse(readFileSync(pB, 'utf8')).fund_codes, ['000216'])

  // 两个文件都落在同一目录下
  const dir = buyableCodesDir(w)
  assert.ok(pA.startsWith(dir))
  assert.ok(pB.startsWith(dir))

  rmSync(w, { recursive: true, force: true })
})
