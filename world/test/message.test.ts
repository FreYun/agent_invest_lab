import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderDailyMessage, weekdayOf } from '../src/message.ts'
import { dayDir } from '../src/paths.ts'

function tmpWorldWithOverview(date: string, overviewMd: string): string {
  const w = mkdtempSync(join(tmpdir(), 'msg-'))
  mkdirSync(dayDir(w, date), { recursive: true })
  writeFileSync(join(dayDir(w, date), 'overview.md'), overviewMd)
  return w
}

test('weekdayOf returns English weekday for a UTC date', () => {
  assert.equal(weekdayOf('2024-03-15'), 'Friday')
  assert.equal(weekdayOf('2024-03-18'), 'Monday')
})

test('first-day message includes full world rules + overview + absolute quotes path', () => {
  const w = tmpWorldWithOverview('2024-03-15', '上证 +1.2%，半导体领涨。')
  const m = renderDailyMessage({ worldRoot: w, date: '2024-03-15', isFirstDay: true, quotesPath: '/abs/world/days/2024-03-15/quotes.json', journalRelPath: 'memory/trading/journal.md' })
  assert.match(m, /当前世界日期：2024-03-15（Friday）/)
  assert.match(m, /回放历史行情的沙盘/)
  assert.match(m, /discover_tools/)
  assert.match(m, /\/abs\/world\/days\/2024-03-15\/quotes\.json/)
  assert.match(m, /memory\/trading\/journal\.md/)
  assert.match(m, /mem0_search/)
  assert.match(m, /mem0_add/)
  assert.match(m, /上证 \+1\.2%，半导体领涨。/)
  assert.match(m, /不要开 start_research 大坑/)
  rmSync(w, { recursive: true, force: true })
})

test('non-first-day message uses the concise rules block but still has date + overview + paths', () => {
  const w = tmpWorldWithOverview('2024-03-18', '上证 -0.4%。')
  const full = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.ok(brief.length < full.length)
  assert.match(brief, /当前世界日期：2024-03-18（Monday）/)
  assert.match(brief, /上证 -0\.4%。/)
  assert.match(brief, /\/q\.json/)
  assert.match(brief, /规则同前/)
  rmSync(w, { recursive: true, force: true })
})
