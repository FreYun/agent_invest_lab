import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { loadCalendar, computeTradingDates } from '../src/calendar.ts'

function tmpFile(name: string, content: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'cal-'))
  const p = join(dir, name)
  writeFileSync(p, content)
  return p
}

test('loadCalendar parses sorted trading_days', () => {
  const p = tmpFile('c.json', JSON.stringify({ trading_days: ['2024-01-02', '2024-01-03', '2024-01-04'] }))
  const cal = loadCalendar(p)
  assert.deepEqual(cal.tradingDays, ['2024-01-02', '2024-01-03', '2024-01-04'])
  rmSync(p, { force: true })
})

test('loadCalendar rejects unsorted or malformed dates', () => {
  const bad1 = tmpFile('b1.json', JSON.stringify({ trading_days: ['2024-01-03', '2024-01-02'] }))
  assert.throws(() => loadCalendar(bad1), /sorted/)
  const bad2 = tmpFile('b2.json', JSON.stringify({ trading_days: ['2024-1-2'] }))
  assert.throws(() => loadCalendar(bad2), /YYYY-MM-DD/)
  const bad3 = tmpFile('b3.json', JSON.stringify({ foo: 1 }))
  assert.throws(() => loadCalendar(bad3), /trading_days/)
})

test('computeTradingDates filters inclusive range', () => {
  const cal = { tradingDays: ['2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05'] }
  assert.deepEqual(computeTradingDates(cal, '2024-01-03', '2024-01-04'), ['2024-01-03', '2024-01-04'])
  assert.deepEqual(computeTradingDates(cal, '2024-01-01', '2024-12-31'), cal.tradingDays)
})

test('computeTradingDates throws on empty result', () => {
  const cal = { tradingDays: ['2024-01-02'] }
  assert.throws(() => computeTradingDates(cal, '2025-01-01', '2025-12-31'), /no trading days/i)
})
