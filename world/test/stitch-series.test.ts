// world/test/stitch-series.test.ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { stitchSeriesRows } from '../src/backtest-dashboard/server.ts'

test('历史段归一到首点=1.0，实盘段续接在历史末点上', () => {
  const history = [
    { trade_date: '2026-05-01', net_value: 2.0 },
    { trade_date: '2026-05-02', net_value: 2.2 },  // 相对首点 +10%
  ]
  const live = [
    { trade_date: '2026-07-22', net_value: 1.0 },   // live 自己的基线 1.0
    { trade_date: '2026-07-23', net_value: 1.05 },  // live 内 +5%
  ]
  const out = stitchSeriesRows(history, live)
  assert.equal(out.length, 4)
  assert.equal(out[0].segment, 'backtest')
  assert.equal(Number(out[0].net_value).toFixed(4), '1.0000')       // 首点归一
  assert.equal(Number(out[1].net_value).toFixed(4), '1.1000')       // +10%
  assert.equal(out[2].segment, 'live')
  assert.equal(Number(out[2].net_value).toFixed(4), '1.1000')       // 续接：= 历史末点
  assert.equal(Number(out[3].net_value).toFixed(4), '1.1550')       // 1.1 * 1.05
  assert.equal(Number(out[3].cumulative_return_pct).toFixed(2), '15.50')
  assert.equal(out[0].raw_net_value, 2.0)
})

test('空实盘段：退化为纯历史归一曲线', () => {
  const history = [
    { trade_date: '2026-05-01', net_value: 2.0 },
    { trade_date: '2026-05-02', net_value: 2.4 },
  ]
  const out = stitchSeriesRows(history, [])
  assert.equal(out.length, 2)
  assert.ok(out.every(r => r.segment === 'backtest'))
  assert.equal(Number(out[1].net_value).toFixed(4), '1.2000')
})

test('自定义 liveSegment 标签（OOS 用 daily_oos）', () => {
  const out = stitchSeriesRows(
    [{ trade_date: '2026-05-01', net_value: 1.0 }],
    [{ trade_date: '2026-07-22', net_value: 1.0 }],
    { liveSegment: 'daily_oos' },
  )
  assert.equal(out[1].segment, 'daily_oos')
})

import { computeStitchedMetrics } from '../src/backtest-dashboard/server.ts'

test('拼接指标：绝对收益 / 回撤 / 年化', () => {
  const series = [
    { trade_date: '2026-05-01', net_value: 1.0 },
    { trade_date: '2026-05-02', net_value: 1.2 },  // 峰值
    { trade_date: '2026-05-03', net_value: 1.08 }, // 从 1.2 回撤 -10%
    { trade_date: '2026-05-04', net_value: 1.26 }, // 末点 +26%
  ]
  const m = computeStitchedMetrics(series)
  assert.equal(m.days, 4)
  assert.equal(Number(m.absReturnPct).toFixed(2), '26.00')
  assert.equal(Number(m.maxDrawdownPct).toFixed(2), '-10.00')
  // 252 年化 = (1.26)^(252/4) - 1，为正
  assert.ok(m.annReturnPct != null && m.annReturnPct > 0)
})

test('拼接指标：空序列返回 null', () => {
  const m = computeStitchedMetrics([])
  assert.equal(m.days, 0)
  assert.equal(m.absReturnPct, null)
  assert.equal(m.maxDrawdownPct, null)
  assert.equal(m.annReturnPct, null)
})
