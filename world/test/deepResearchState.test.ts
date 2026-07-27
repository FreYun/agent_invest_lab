import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeDeepResearchState, tradingDaysBetween } from '../src/run.ts'

const DATES = [
  '2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08',
  '2025-01-09', '2025-01-10', '2025-01-13', '2025-01-14', '2025-01-15',
]

test('tradingDaysBetween: 缺失 fromDate 锚定 dates[0]，Day1 gap=0', () => {
  assert.equal(tradingDaysBetween(DATES, undefined, '2025-01-02'), 0)
  assert.equal(tradingDaysBetween(DATES, undefined, '2025-01-08'), 4)
})

test('tradingDaysBetween: 正常区间按交易日计（不含 fromDate 当日）', () => {
  assert.equal(tradingDaysBetween(DATES, '2025-01-02', '2025-01-03'), 1)
  assert.equal(tradingDaysBetween(DATES, '2025-01-03', '2025-01-10'), 5)
  assert.equal(tradingDaysBetween(DATES, '2025-01-06', '2025-01-06'), 0)
})

test('tradingDaysBetween: 日期不在日历 / 次序颠倒 → MAX_SAFE_INTEGER', () => {
  assert.equal(tradingDaysBetween(DATES, '2024-12-31', '2025-01-06'), Number.MAX_SAFE_INTEGER)
  assert.equal(tradingDaysBetween(DATES, '2025-01-06', '2025-01-04'), Number.MAX_SAFE_INTEGER)
  assert.equal(tradingDaysBetween(DATES, '2025-01-06', '2025-01-02'), Number.MAX_SAFE_INTEGER)
})

const base = { every: 0, maxGapDays: 5, tradingDates: DATES }

test('agent-triggered: Day1 未到间隔，不授权也不强制', () => {
  const s = computeDeepResearchState({ ...base, mode: 'agent-triggered', ordinal: 1, todayDate: '2025-01-02', lastDeepDate: undefined })
  assert.equal(s.authorized, false)
  assert.equal(s.forced, false)
  assert.equal(s.gapDays, 0)
})

test('agent-triggered: 从未深研时第 5 个交易日（gap=4）才强制', () => {
  // Day2..Day4 (gap 1..3) 不强制
  for (const [i, d] of [['2025-01-03', 1], ['2025-01-06', 2], ['2025-01-07', 3]].entries()) {
    const s = computeDeepResearchState({ ...base, mode: 'agent-triggered', ordinal: i + 2, todayDate: d[0] as string, lastDeepDate: undefined })
    assert.equal(s.authorized, false, `day ${d[0]} 不应授权`)
    assert.equal(s.forced, false, `day ${d[0]} 不应强制`)
    assert.equal(s.gapDays, d[1])
  }
  // Day5 gap=4 → 强制
  const s5 = computeDeepResearchState({ ...base, mode: 'agent-triggered', ordinal: 5, todayDate: '2025-01-08', lastDeepDate: undefined })
  assert.equal(s5.authorized, true)
  assert.equal(s5.forced, true)
  assert.equal(s5.gapDays, 4)
})

test('agent-triggered: 深研后 gap 归零重新累积，再到 5 再强制', () => {
  // 01-06 深研过 → 01-07 gap=1 不强制
  const s1 = computeDeepResearchState({ ...base, mode: 'agent-triggered', ordinal: 4, todayDate: '2025-01-07', lastDeepDate: '2025-01-06' })
  assert.equal(s1.authorized, false)
  assert.equal(s1.forced, false)
  assert.equal(s1.gapDays, 1)
  // 01-13 gap=5（06→07,08,09,10,13）→ 强制
  const s2 = computeDeepResearchState({ ...base, mode: 'agent-triggered', ordinal: 8, todayDate: '2025-01-13', lastDeepDate: '2025-01-06' })
  assert.equal(s2.authorized, true)
  assert.equal(s2.forced, true)
  assert.equal(s2.gapDays, 5)
})

test('agent-triggered: forced 日跳过后次日仍强制（gap 继续累加）', () => {
  const s = computeDeepResearchState({ ...base, mode: 'agent-triggered', ordinal: 6, todayDate: '2025-01-09', lastDeepDate: undefined })
  assert.equal(s.forced, true)
  assert.equal(s.gapDays, 5)
})

test('agent-triggered: 配置 7 时 gap=6 禁止、gap=7 才授权并强制', () => {
  const before = computeDeepResearchState({ ...base, maxGapDays: 7, mode: 'agent-triggered', ordinal: 7, todayDate: '2025-01-10', lastDeepDate: '2025-01-02' })
  assert.equal(before.gapDays, 6)
  assert.equal(before.authorized, false)
  assert.equal(before.forced, false)
  const due = computeDeepResearchState({ ...base, maxGapDays: 7, mode: 'agent-triggered', ordinal: 8, todayDate: '2025-01-13', lastDeepDate: '2025-01-02' })
  assert.equal(due.gapDays, 7)
  assert.equal(due.authorized, true)
  assert.equal(due.forced, true)
})

test('agent-triggered: lastDeepDate 不在日历中（数据异常）→ 强制兜底', () => {
  const s = computeDeepResearchState({ ...base, mode: 'agent-triggered', ordinal: 2, todayDate: '2025-01-03', lastDeepDate: '2024-06-01' })
  assert.equal(s.forced, true)
  assert.equal(s.gapDays, Number.MAX_SAFE_INTEGER)
})

test('ordinal 回归: authorized=forced=isDeepResearchDay，every=1 每决策日都深研', () => {
  const s = computeDeepResearchState({ ...base, mode: 'ordinal', every: 1, ordinal: 3, todayDate: '2025-01-06', lastDeepDate: undefined })
  assert.equal(s.authorized, true)
  assert.equal(s.forced, true)
})

test('ordinal 回归: every=4 只在 4/8/12… 序数强制', () => {
  const on = computeDeepResearchState({ ...base, mode: 'ordinal', every: 4, ordinal: 8, todayDate: '2025-01-13', lastDeepDate: undefined })
  assert.equal(on.forced, true)
  const off = computeDeepResearchState({ ...base, mode: 'ordinal', every: 4, ordinal: 7, todayDate: '2025-01-10', lastDeepDate: undefined })
  assert.equal(off.forced, false)
  assert.equal(off.authorized, false)
})

test('ordinal 回归: every=0 恒不深研', () => {
  const s = computeDeepResearchState({ ...base, mode: 'ordinal', every: 0, ordinal: 4, todayDate: '2025-01-07', lastDeepDate: undefined })
  assert.equal(s.forced, false)
  assert.equal(s.authorized, false)
})
