import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeDeepResearchState, isAccountDrawdownActive, isCrashForced } from '../src/run.ts'

const DATES = ['2025-01-02','2025-01-03','2025-01-06','2025-01-07','2025-01-08','2025-01-09','2025-01-10']
type TriggerSignal = NonNullable<Parameters<typeof isCrashForced>[0]>
function signal(overrides: Partial<TriggerSignal> = {}): TriggerSignal {
  return {
    targetDailyMovePct: null,
    accountDrawdownPct: null,
    accountDrawdownWasActive: false,
    dailyMoveThreshold: 3,
    drawdownThreshold: 8,
    ...overrides,
  }
}


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
    crashSignal: signal({ targetDailyMovePct: -3.5, accountDrawdownPct: -1 }),
  })
  assert.equal(s.authorized, true)
  assert.equal(s.forced, true)
})

test('急涨 +3.1% 也触发', () => {
  assert.equal(isCrashForced(signal({ targetDailyMovePct: 3.1 })), true)
})

test('回撤 -8.2% 触发', () => {
  assert.equal(isCrashForced(signal({ accountDrawdownPct: -8.2 })), true)
})
test('账户持续处于 -8% 以下不会每天重复触发', () => {
  assert.equal(isCrashForced(signal({ accountDrawdownPct: -9, accountDrawdownWasActive: true })), false)
})

test('账户回撤数据临时缺失时保留上一日越线状态', () => {
  assert.equal(isAccountDrawdownActive(signal({ accountDrawdownWasActive: true })), true)
})


test('move=-2% dd=-5% 都不触发', () => {
  assert.equal(isCrashForced(signal({ targetDailyMovePct: -2, accountDrawdownPct: -5 })), false)
})

test('null 字段安全：不触发', () => {
  assert.equal(isCrashForced(signal()), false)
  assert.equal(isCrashForced(undefined), false)
})

test('ordinal 分支：崩盘日也放行 authorized', () => {
  const s = computeDeepResearchState({
    mode: 'ordinal', ordinal: 1, every: 4, maxGapDays: 5, // ordinal=1 非深研日
    todayDate: '2025-01-03', lastDeepDate: '2025-01-02', tradingDates: DATES,
    crashSignal: signal({ targetDailyMovePct: -4 }),
  })
  assert.equal(s.forced, true)
  assert.equal(s.authorized, true) // 强制了就必须放行 start_research
})
