import { test } from 'node:test'
import assert from 'node:assert/strict'
import { accountDrawdownLevel, computeDeepResearchState, deriveDeepResearchAnomalyReadings, isAccountDrawdownActive, isCrashForced } from '../src/run.ts'

const DATES = ['2025-01-02','2025-01-03','2025-01-06','2025-01-07','2025-01-08','2025-01-09','2025-01-10']
type TriggerSignal = NonNullable<Parameters<typeof isCrashForced>[0]>
function signal(overrides: Partial<TriggerSignal> = {}): TriggerSignal {
  return {
    targetDailyMovePct: null,
    accountDrawdownPct: null,
    accountDrawdownWasActive: false,
    accountDrawdownPreviousLevel: 0,
    dailyMoveThreshold: 3,
    drawdownThreshold: 8,
    ...overrides,
  }
}


test('agent-triggered：未到固定间隔仍授权 agent 按异常自主深研，但不强制', () => {
  const s = computeDeepResearchState({
    mode: 'agent-triggered', ordinal: 1, every: 0, maxGapDays: 5,
    todayDate: '2025-01-03', lastDeepDate: '2025-01-02', tradingDates: DATES,
  })
  assert.equal(s.authorized, true)
  assert.equal(s.forced, false) // gap=1 < 5，无崩盘
})

test('组合单日损失、最差持仓和风险篮子均可提前强制深研', () => {
  const base = signal({
    portfolioDailyLossThreshold: 3,
    worstHoldingDailyLossThreshold: 5,
    riskBasketDailyLossThreshold: 3,
  })
  assert.equal(isCrashForced({ ...base, portfolioDailyReturnPct: -3.1 }), true)
  assert.equal(isCrashForced({ ...base, worstHoldingDailyMovePct: -5.1 }), true)
  assert.equal(isCrashForced({ ...base, riskBasketWorstDailyMovePct: -3.1 }), true)
  assert.equal(isCrashForced({
    ...base,
    portfolioDailyReturnPct: -2.9,
    worstHoldingDailyMovePct: -4.9,
    riskBasketWorstDailyMovePct: -2.9,
  }), false)
})

test('从 dailyContext 提取组合、最差持仓及多指数篮子异常读数', () => {
  const readings = deriveDeepResearchAnomalyReadings({
    performance: {
      asOfDate: '2026-07-03', summary: null, trades: null, intervals: null, completedPositions: [],
      dailySeries: [{ trade_date: '2026-07-02', total_value: 94, net_value: 0.94, daily_return_pct: -6.8, cumulative_return_pct: -6 }],
    },
    fundSeries: [
      { fund_code: 'A', nav_series: [{ date: '2026-07-02', nav: 1, daily_return_pct: -4 }], return_1m_pct: null, return_3m_pct: null },
      { fund_code: 'B', nav_series: [{ date: '2026-07-02', nav: 1, daily_return_pct: -7.6 }], return_1m_pct: null, return_3m_pct: null },
    ],
    indices: [
      { code: '000300.SH', name: '沪深300', latest_date: '2026-07-02', latest_close: 1, daily_move_pct: -2.9, ma5: null, ma20: null, vs_ma5_pct: null, vs_ma20_pct: null, ma60: null, ma120: null, ma200: null, vs_ma60_pct: null, vs_ma200_pct: null, trend: null },
      { code: '399006.SZ', name: '创业板指', latest_date: '2026-07-02', latest_close: 1, daily_move_pct: -5.7, ma5: null, ma20: null, vs_ma5_pct: null, vs_ma20_pct: null, ma60: null, ma120: null, ma200: null, vs_ma60_pct: null, vs_ma200_pct: null, trend: null },
    ],
  }, ['000300.SH', '399006.SZ'])
  assert.equal(readings.portfolioDailyReturnPct, -6.8)
  assert.equal(readings.worstHoldingDailyMovePct, -7.6)
  assert.equal(readings.worstHoldingCode, 'B')
  assert.equal(readings.riskBasketWorstDailyMovePct, -5.7)
  assert.equal(readings.riskBasketWorstCode, '399006.SZ')
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
  assert.equal(isCrashForced(signal({ accountDrawdownPct: -9, accountDrawdownWasActive: true, accountDrawdownPreviousLevel: 1 })), false)
})

test('账户回撤数据临时缺失时保留上一日越线状态', () => {
  assert.equal(isAccountDrawdownActive(signal({ accountDrawdownWasActive: true, accountDrawdownPreviousLevel: 1 })), true)
})

test('账户回撤跨入 2x 阈值（-16%）时再次强制深研', () => {
  const s = signal({ accountDrawdownPct: -16, accountDrawdownWasActive: true, accountDrawdownPreviousLevel: 1 })
  assert.equal(accountDrawdownLevel(s), 2)
  assert.equal(isCrashForced(s), true)
})

test('账户回撤停留在 2x 档内不重复触发，跨入 3x 才再触发', () => {
  assert.equal(isCrashForced(signal({ accountDrawdownPct: -18, accountDrawdownWasActive: true, accountDrawdownPreviousLevel: 2 })), false)
  assert.equal(isCrashForced(signal({ accountDrawdownPct: -24.1, accountDrawdownWasActive: true, accountDrawdownPreviousLevel: 2 })), true)
})

test('旧 run 只有 active=true 时迁移为第一档', () => {
  const legacy = signal({ accountDrawdownPct: -9, accountDrawdownWasActive: true })
  delete legacy.accountDrawdownPreviousLevel
  assert.equal(accountDrawdownLevel(legacy), 1)
  assert.equal(isCrashForced(legacy), false)
  legacy.accountDrawdownPct = -16
  assert.equal(isCrashForced(legacy), true)
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
