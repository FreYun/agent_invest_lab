import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeDeepResearchState, isCrashForced } from '../src/run.ts'

const DATES = ['2025-01-02','2025-01-03','2025-01-06','2025-01-07','2025-01-08','2025-01-09','2025-01-10']

test('无 crashSignal：未到固定间隔不授权', () => {
  const s = computeDeepResearchState({
    mode: 'agent-triggered', ordinal: 1, every: 0, maxGapDays: 5,
    todayDate: '2025-01-03', lastDeepDate: '2025-01-02', tradingDates: DATES,
  })
  assert.equal(s.authorized, false)
  assert.equal(s.forced, false) // gap=1 < 5，无崩盘
})

test('单日 -3.5% 触发 forced（gap 未到上限也强制）', () => {
  const s = computeDeepResearchState({
    mode: 'agent-triggered', ordinal: 1, every: 0, maxGapDays: 5,
    todayDate: '2025-01-03', lastDeepDate: '2025-01-02', tradingDates: DATES,
    crashSignal: { dailyMovePct: -3.5, drawdownPct: -1, dailyMoveThreshold: 3, drawdownThreshold: 8 },
  })
  assert.equal(s.authorized, true)
  assert.equal(s.forced, true)
})

test('急涨 +3.1% 也触发', () => {
  assert.equal(isCrashForced({ dailyMovePct: 3.1, drawdownPct: 0, dailyMoveThreshold: 3, drawdownThreshold: 8 }), true)
})

test('回撤 -8.2% 触发', () => {
  assert.equal(isCrashForced({ dailyMovePct: -1, drawdownPct: -8.2, dailyMoveThreshold: 3, drawdownThreshold: 8 }), true)
})

test('move=-2% dd=-5% 都不触发', () => {
  assert.equal(isCrashForced({ dailyMovePct: -2, drawdownPct: -5, dailyMoveThreshold: 3, drawdownThreshold: 8 }), false)
})

test('null 字段安全：不触发', () => {
  assert.equal(isCrashForced({ dailyMovePct: null, drawdownPct: null, dailyMoveThreshold: 3, drawdownThreshold: 8 }), false)
  assert.equal(isCrashForced(undefined), false)
})

test('ordinal 分支：崩盘日也放行 authorized', () => {
  const s = computeDeepResearchState({
    mode: 'ordinal', ordinal: 1, every: 4, maxGapDays: 5, // ordinal=1 非深研日
    todayDate: '2025-01-03', lastDeepDate: '2025-01-02', tradingDates: DATES,
    crashSignal: { dailyMovePct: -4, drawdownPct: 0, dailyMoveThreshold: 3, drawdownThreshold: 8 },
  })
  assert.equal(s.forced, true)
  assert.equal(s.authorized, true) // 强制了就必须放行 start_research
})
