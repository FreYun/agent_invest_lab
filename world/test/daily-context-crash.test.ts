import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeCrashSignalFromCloses } from '../src/daily-context.ts'

test('单日跌幅：最后两日 -3.5%', () => {
  const r = computeCrashSignalFromCloses([
    { date: '2025-01-02', close: 100 },
    { date: '2025-01-03', close: 96.5 },
  ])
  assert.equal(r?.lastDate, '2025-01-03')
  assert.ok(r && Math.abs(r.lastDayMovePct! - (-3.5)) < 1e-9)
})

test('回撤：从 100 高点跌到 91 = -9%', () => {
  const r = computeCrashSignalFromCloses([
    { date: '2025-01-02', close: 100 },
    { date: '2025-01-03', close: 95 },
    { date: '2025-01-06', close: 91 },
  ])
  assert.ok(r && Math.abs(r.drawdownFromRecentHighPct! - (-9)) < 1e-9)
})

test('急涨也算：+3.2%', () => {
  const r = computeCrashSignalFromCloses([
    { date: '2025-01-02', close: 100 },
    { date: '2025-01-03', close: 103.2 },
  ])
  assert.ok(r && Math.abs(r.lastDayMovePct! - 3.2) < 1e-9)
})

test('空/单点序列返回 null 或 null 字段', () => {
  assert.equal(computeCrashSignalFromCloses([]), null)
  const one = computeCrashSignalFromCloses([{ date: '2025-01-02', close: 100 }])
  assert.equal(one?.lastDayMovePct, null)         // 无前一日 → 日涨跌 null
  assert.equal(one?.drawdownFromRecentHighPct, 0) // 单点回撤 0
})
