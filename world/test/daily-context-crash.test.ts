import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  benchmarkLookupCandidates,
  computeCrashSignalFromCloses,
  selectBenchmarkDailyState,
} from '../src/daily-context.ts'

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

test('旧 A500 后缀不可用时命中 000510.CSI alias，并包含当日暴跌', () => {
  const candidates = benchmarkLookupCandidates('000510.SH', ['000300.SH'])
  assert.deepEqual(candidates, [
    { code: '000510.SH', sourceKind: 'requested' },
    { code: '000510.CSI', sourceKind: 'alias' },
    { code: '000300.SH', sourceKind: 'market-proxy' },
  ])
  const state = selectBenchmarkDailyState('000510.SH', candidates, [
    { 指数标识: '000510.SH', 是否可用: false, 行情记录: [] },
    { 指数标识: '000510.CSI', 是否可用: true, 行情记录: [
      { 日期: '2025-04-03', 收盘: 4554.2481 },
      { 日期: '2025-04-07', 收盘: 4199.0917 },
    ] },
    { 指数标识: '000300.SH', 是否可用: true, 行情记录: [
      { 日期: '2025-04-03', 收盘: 100 },
      { 日期: '2025-04-07', 收盘: 99 },
    ] },
  ])
  assert.equal(state?.sourceCode, '000510.CSI')
  assert.equal(state?.sourceKind, 'alias')
  assert.equal(state?.lastDate, '2025-04-07')
  assert.ok(state && state.lastDayMovePct !== null && state.lastDayMovePct < -7.7)
})

test('目标及 alias 都不可用时使用宽基代理，不再静默返回 n/a', () => {
  const candidates = benchmarkLookupCandidates('000922.CSI', ['000300.SH'])
  const state = selectBenchmarkDailyState('000922.CSI', candidates, [
    { 指数标识: '000922.CSI', 是否可用: false, 行情记录: [] },
    { 指数标识: '000922.SH', 是否可用: false, 行情记录: [] },
    { 指数标识: '000300.SH', 是否可用: true, 行情记录: [
      { 日期: '2025-04-03', 收盘: 100 },
      { 日期: '2025-04-07', 收盘: 92 },
    ] },
  ])
  assert.equal(state?.sourceCode, '000300.SH')
  assert.equal(state?.sourceKind, 'market-proxy')
  assert.equal(state?.lastDayMovePct, -8)
})
