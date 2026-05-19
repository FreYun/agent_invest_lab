import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildHoldingsByDate } from '../src/backtest-dashboard/positions.ts'
import { computeActionWeights } from '../src/backtest-dashboard/positions.ts'

test('buildHoldingsByDate groups rows by trade_date', () => {
  const rows = [
    { trade_date: '2026-01-05', fund_code: '020251', fund_name: 'A', weight: 0.6, shares: 100, nav: 1.0, market_value: 600, theme: '', asset_class: 'equity', role: 'main', daily_pnl: 0, cumulative_return_pct: 0, holding_days: 1 },
    { trade_date: '2026-01-05', fund_code: '012345', fund_name: 'B', weight: 0.2, shares: 50, nav: 1.0, market_value: 200, theme: '', asset_class: 'bond', role: 'sat', daily_pnl: 0, cumulative_return_pct: 0, holding_days: 1 },
    { trade_date: '2026-01-06', fund_code: '020251', fund_name: 'A', weight: 0.5, shares: 100, nav: 1.0, market_value: 500, theme: '', asset_class: 'equity', role: 'main', daily_pnl: 0, cumulative_return_pct: 0, holding_days: 2 },
  ]
  const idx = buildHoldingsByDate(rows)
  assert.deepEqual(Object.keys(idx).sort(), ['2026-01-05', '2026-01-06'])
  assert.equal(idx['2026-01-05'].length, 2)
  assert.equal(idx['2026-01-06'].length, 1)
  // each row has trade_date stripped (we keep it as the index key, not in the row body)
  assert.equal((idx['2026-01-05'][0] as any).trade_date, undefined)
  assert.equal(idx['2026-01-05'][0].fund_code, '020251')
})

const hbd = {
  '2026-01-05': [{ fund_code: '020251', fund_name: 'A', weight: 0.6 } as any],
  '2026-01-06': [{ fund_code: '020251', fund_name: 'A', weight: 0.6 } as any],
  '2026-02-03': [{ fund_code: '020251', fund_name: 'A', weight: 0.68 } as any],
  '2026-04-07': [{ fund_code: '020251', fund_name: 'A', weight: 0.47 } as any],
}

test('computeActionWeights: first buy → before=0, after=current-day weight', () => {
  const w = computeActionWeights(hbd, '2026-01-05', '020251')
  assert.equal(w.weight_before, 0)
  assert.equal(w.weight_after, 0.6)
  assert.equal(w.weight_delta, 0.6)
})

test('computeActionWeights: incremental buy → before=prev-trade-date, after=current', () => {
  const w = computeActionWeights(hbd, '2026-02-03', '020251')
  assert.equal(w.weight_before, 0.6)
  assert.equal(w.weight_after, 0.68)
  assert.ok(Math.abs(w.weight_delta - 0.08) < 1e-9)
})

test('computeActionWeights: weekend action date → use latest snapshot ≤ action_date', () => {
  // 2026-02-04 is not a snapshot date; should fall back to 2026-02-03
  const w = computeActionWeights(hbd, '2026-02-04', '020251')
  assert.equal(w.weight_before, 0.6)
  assert.equal(w.weight_after, 0.68)
  assert.ok(Math.abs(w.weight_delta - 0.08) < 1e-9)
})

test('computeActionWeights: sell-all → after=0 if fund absent from current snapshot', () => {
  const hbd2 = {
    '2026-01-05': [{ fund_code: '020251', fund_name: 'A', weight: 0.5 } as any],
    '2026-01-06': [],
  }
  const w = computeActionWeights(hbd2, '2026-01-06', '020251')
  assert.equal(w.weight_before, 0.5)
  assert.equal(w.weight_after, 0)
  assert.equal(w.weight_delta, -0.5)
})

test('computeActionWeights: unknown fund → all zeros', () => {
  const w = computeActionWeights(hbd, '2026-01-05', 'NEVER_HELD')
  assert.equal(w.weight_before, 0)
  assert.equal(w.weight_after, 0)
  assert.equal(w.weight_delta, 0)
})
