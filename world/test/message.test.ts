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

test('first-day message: full cold-start rules, mcp-only tool policy, points to simworld-data + portfolio_*; no inline overview', () => {
  const w = tmpWorldWithOverview('2024-03-15', '上证 +1.2%，半导体领涨。')
  const m = renderDailyMessage({ worldRoot: w, date: '2024-03-15', isFirstDay: true, quotesPath: '/abs/world/days/2024-03-15/quotes.json', journalRelPath: 'memory/trading/journal.md' })
  assert.match(m, /当前世界日期：2024-03-15（Friday）/)
  assert.match(m, /discover_tools/)
  assert.match(m, /mem0_search/)
  assert.match(m, /mem0_add/)
  assert.match(m, /mcp__\*/)
  assert.match(m, /文件读写、web_fetch、bash/)
  // 行情走 simworld-data；账户走 portfolio_* 自助查
  assert.match(m, /simworld-data/)
  assert.match(m, /portfolio_get_my_history/)
  assert.match(m, /portfolio_place_buy_order/)
  // No file-IO references
  assert.doesNotMatch(m, /memory\/trading\/journal\.md/)
  // 静态 overview 不再注入
  assert.doesNotMatch(m, /上证 \+1\.2%，半导体领涨。/)
  rmSync(w, { recursive: true, force: true })
})

test('non-first-day message: concise rules + AUTONOMY block (bot self-paces), no overview, no day-type banner', () => {
  const w = tmpWorldWithOverview('2024-03-18', '上证 -0.4%。')
  const full = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.ok(brief.length < full.length)
  assert.match(brief, /当前世界日期：2024-03-18（Monday）/)
  assert.match(brief, /规则同前/)
  assert.match(brief, /mem0_search/)
  assert.match(brief, /mem0_add/)
  assert.match(brief, /simworld-data/)
  // AUTONOMY 是新引导：让 bot 自己定节奏；旧的两个 banner 都消失
  assert.match(brief, /今天的节奏由你定/)
  assert.doesNotMatch(brief, /今天是普通交易日/)
  assert.doesNotMatch(brief, /今天是研究日/)
  // No inline overview, no journal reference
  assert.doesNotMatch(brief, /上证 -0\.4%。/)
  assert.doesNotMatch(brief, /memory\/trading\/journal\.md/)
  rmSync(w, { recursive: true, force: true })
})

test('first-day broadcasts the per-run curated buyable funds list; later days do not (bot self-queries via portfolio_get_buyable_funds)', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const codes = ['510300', '159915', '002611']
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md', buyableFundCodes: codes })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md', buyableFundCodes: codes })
  // Day 1: explicit list of codes appears
  for (const c of codes) assert.match(first, new RegExp(c))
  assert.match(first, /本轮可买基金/)
  // Day 2+: not re-broadcast
  assert.doesNotMatch(brief, /本轮可买基金/)
  for (const c of codes) assert.doesNotMatch(brief, new RegExp(c))
  // Day 1 without list (e.g., world.yaml didn't set buyable_fund_codes): block omitted
  const firstNoList = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.doesNotMatch(firstNoList, /本轮可买基金/)
  rmSync(w, { recursive: true, force: true })
})

test('first-day has no AUTONOMY block (cold start needs explicit handholding, not self-pacing)', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.doesNotMatch(first, /今天的节奏由你定/)
  assert.doesNotMatch(first, /今天是普通交易日/)
  assert.doesNotMatch(first, /今天是研究日/)
  rmSync(w, { recursive: true, force: true })
})
