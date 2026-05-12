import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveOverview } from '../src/overview.ts'
import { dayDir } from '../src/paths.ts'

function tmpWorld(date: string, files: { overviewMd?: string; quotes?: unknown; events?: unknown }): string {
  const w = mkdtempSync(join(tmpdir(), 'ov-'))
  const d = dayDir(w, date)
  mkdirSync(d, { recursive: true })
  if (files.overviewMd !== undefined) writeFileSync(join(d, 'overview.md'), files.overviewMd)
  if (files.quotes !== undefined) writeFileSync(join(d, 'quotes.json'), JSON.stringify(files.quotes))
  if (files.events !== undefined) writeFileSync(join(d, 'events.json'), JSON.stringify(files.events))
  return w
}

test('prefers overview.md when present', () => {
  const w = tmpWorld('2024-03-15', { overviewMd: '上证 +1.2%，半导体领涨。', quotes: { whatever: 1 } })
  assert.match(resolveOverview(w, '2024-03-15'), /半导体领涨/)
  rmSync(w, { recursive: true, force: true })
})

test('falls back to quotes.json "summary" string and appends events digest', () => {
  const w = tmpWorld('2024-03-15', { quotes: { summary: '主要指数小幅收涨' }, events: { events: ['央行 MLF 续作', { title: 'AI 算力新政发布' }] } })
  const o = resolveOverview(w, '2024-03-15')
  assert.match(o, /主要指数小幅收涨/)
  assert.match(o, /央行 MLF 续作/)
  assert.match(o, /AI 算力新政发布/)
  rmSync(w, { recursive: true, force: true })
})

test('graceful degradation when no overview.md and quotes.json has unknown shape', () => {
  const w = tmpWorld('2024-03-15', { quotes: { rows: [[1, 2, 3]] } })
  assert.match(resolveOverview(w, '2024-03-15'), /概览不可用|直接读取/)
  rmSync(w, { recursive: true, force: true })
})

test('degradation when quotes.json missing entirely', () => {
  const w = mkdtempSync(join(tmpdir(), 'ov2-'))
  mkdirSync(dayDir(w, '2024-03-15'), { recursive: true })
  assert.match(resolveOverview(w, '2024-03-15'), /概览不可用/)
  rmSync(w, { recursive: true, force: true })
})
