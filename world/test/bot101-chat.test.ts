import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildToolRegistry, buildLocalMarketReportTool, planLocalMarketReport } from '../src/backtest-dashboard/bot101-chat.ts'
import { resolveChatAsOf } from '../src/backtest-dashboard/server.ts'

const SIM = 'http://sim/mcp'
const FUND = 'http://fund/mcp'

// 仿真上游 tools/list 的子集，覆盖每条要校验的分支。
const simTools = [
  { name: 'market_index_quote', description: '指数行情', inputSchema: { type: 'object', properties: { market: { type: 'string' }, symbols: { type: 'array' }, simulated_datetime: { type: 'string' } }, required: ['market', 'symbols', 'simulated_datetime'] } },
  { name: 'health_check', description: '健康', inputSchema: { type: 'object', properties: {} } }, // 无 simulated_datetime
  { name: 'fund_index_subscription_redemption', description: '申赎(隐藏)', inputSchema: { type: 'object', properties: { simulated_datetime: { type: 'string' } } } },
]
const fundTools = [
  { name: 'portfolio_get_my_trades', description: '成交', inputSchema: { type: 'object', properties: { run_id: { type: 'string' }, bot_id: { type: 'string' } }, required: ['run_id'] } },
  { name: 'portfolio_place_buy_order', description: '下单买(写)', inputSchema: { type: 'object', properties: { run_id: { type: 'string' }, fund_code: { type: 'string' } } } },
  { name: 'portfolio_place_sell_order', description: '下单卖(写)', inputSchema: { type: 'object', properties: { run_id: { type: 'string' } } } },
]

test('simulated_datetime 从 schema 抹除，但记录到注入表', () => {
  const { openai, registry } = buildToolRegistry(simTools, fundTools, SIM, FUND)
  const quote = openai.find(t => t.function.name === 'market_index_quote')
  assert.ok(quote, 'market_index_quote 应在工具列表里')
  const props = quote!.function.parameters.properties as Record<string, unknown>
  assert.ok(!('simulated_datetime' in props), 'simulated_datetime 必须从 properties 抹掉')
  assert.ok(!(quote!.function.parameters.required as string[]).includes('simulated_datetime'), 'simulated_datetime 必须从 required 抹掉')
  assert.equal(registry.get('market_index_quote')?.injectDate, true)
  assert.equal(registry.get('market_index_quote')?.server, 'simworld')
})

test('无 simulated_datetime 的工具 injectDate=false', () => {
  const { registry } = buildToolRegistry(simTools, fundTools, SIM, FUND)
  assert.equal(registry.get('health_check')?.injectDate, false)
})

test('隐藏工具(申赎)既不进 openai 列表也不进 registry', () => {
  const { openai, registry } = buildToolRegistry(simTools, fundTools, SIM, FUND)
  assert.ok(!openai.some(t => t.function.name === 'fund_index_subscription_redemption'))
  assert.equal(registry.has('fund_index_subscription_redemption'), false)
})

test('fund 写工具(下单)被整条排除——chat 无法下单', () => {
  const { openai, registry } = buildToolRegistry(simTools, fundTools, SIM, FUND)
  for (const w of ['portfolio_place_buy_order', 'portfolio_place_sell_order']) {
    assert.ok(!openai.some(t => t.function.name === w), `${w} 不该暴露给 LLM`)
    assert.equal(registry.has(w), false, `${w} 不该在 registry`)
  }
})

test('fund 只读工具 run_id 抹除 + 注入标记 + 指向 fund 上游', () => {
  const { openai, registry } = buildToolRegistry(simTools, fundTools, SIM, FUND)
  const trades = openai.find(t => t.function.name === 'portfolio_get_my_trades')
  assert.ok(trades)
  const props = trades!.function.parameters.properties as Record<string, unknown>
  assert.ok(!('run_id' in props), 'run_id 必须抹掉')
  assert.ok(!(trades!.function.parameters.required as string[]).includes('run_id'))
  const e = registry.get('portfolio_get_my_trades')
  assert.equal(e?.injectRunId, true)
  assert.equal(e?.server, 'fund')
  assert.equal(e?.url, FUND)
})

test('不改动传入的上游 schema 对象（克隆）', () => {
  buildToolRegistry(simTools, fundTools, SIM, FUND)
  // 原对象仍带 simulated_datetime / run_id，证明 strip 作用在克隆上
  assert.ok('simulated_datetime' in (simTools[0].inputSchema!.properties as Record<string, unknown>))
  assert.ok('run_id' in (fundTools[0].inputSchema!.properties as Record<string, unknown>))
})

// ── 本地合成工具 get_market_report（查当前/历史市场研报）─────────────────────────
test('本地 get_market_report 工具 schema：report_type 必填、as_of_date 选填', () => {
  const t = buildLocalMarketReportTool()
  assert.equal(t.function.name, 'get_market_report')
  const props = t.function.parameters.properties as Record<string, unknown>
  assert.ok('report_type' in props && 'as_of_date' in props)
  assert.deepEqual(t.function.parameters.required, ['report_type'])
})

test('planLocalMarketReport：all 展开为三份日报（不含 macro_news）', () => {
  const p = planLocalMarketReport({ report_type: 'all' }, '2026-06-29')
  assert.deepEqual(p.wanted, ['market_context', 'market_mainline', 'mainline_rotation'])
  assert.equal(p.effAsOf, '2026-06-29')
  assert.equal(p.error, undefined)
})

test('planLocalMarketReport：单类型 + macro_news 合法', () => {
  assert.deepEqual(planLocalMarketReport({ report_type: 'market_mainline' }, '2026-06-29').wanted, ['market_mainline'])
  assert.deepEqual(planLocalMarketReport({ report_type: 'macro_news' }, '2026-06-29').wanted, ['macro_news'])
})

test('planLocalMarketReport：历史 as_of_date 可往回看', () => {
  const p = planLocalMarketReport({ report_type: 'market_mainline', as_of_date: '2026-06-20' }, '2026-06-29')
  assert.equal(p.effAsOf, '2026-06-20')
})

test('planLocalMarketReport：未来/缺省/非法 as_of_date 一律夹到 turnAsOf（防穿越）', () => {
  assert.equal(planLocalMarketReport({ report_type: 'all', as_of_date: '2026-07-15' }, '2026-06-29').effAsOf, '2026-06-29')
  assert.equal(planLocalMarketReport({ report_type: 'all', as_of_date: '2026-06-29' }, '2026-06-29').effAsOf, '2026-06-29') // 等于也不放过
  assert.equal(planLocalMarketReport({ report_type: 'all', as_of_date: 'garbage' }, '2026-06-29').effAsOf, '2026-06-29')
  assert.equal(planLocalMarketReport({ report_type: 'all' }, '2026-06-29').effAsOf, '2026-06-29')
})

test('planLocalMarketReport：非法 report_type 报错且不取任何报告', () => {
  const p = planLocalMarketReport({ report_type: 'bogus' }, '2026-06-29')
  assert.equal(p.wanted.length, 0)
  assert.match(p.error ?? '', /非法/)
})

// ── resolveChatAsOf（把前端请求日夹到最近一个完整决策日）──────────────────────────
test('resolveChatAsOf：请求当天但当天还没数据 → 夹到最近决策日', () => {
  const dates = ['2026-06-29', '2026-06-26', '2026-06-25'] // DESC
  assert.equal(resolveChatAsOf(dates, '2026-06-30'), '2026-06-29')
})

test('resolveChatAsOf：请求日就是一个决策日 → 原样保留（可回看历史）', () => {
  const dates = ['2026-06-29', '2026-06-26', '2026-06-25']
  assert.equal(resolveChatAsOf(dates, '2026-06-26'), '2026-06-26')
})

test('resolveChatAsOf：请求日落在两决策日之间 → 取 ≤ 的最近一个', () => {
  const dates = ['2026-06-29', '2026-06-26', '2026-06-25']
  assert.equal(resolveChatAsOf(dates, '2026-06-28'), '2026-06-26')
})

test('resolveChatAsOf：空请求日 → 最新决策日；早于所有 → 原样返回不前跳', () => {
  const dates = ['2026-06-29', '2026-06-26', '2026-06-25']
  assert.equal(resolveChatAsOf(dates, ''), '2026-06-29')
  assert.equal(resolveChatAsOf(dates, '2026-06-01'), '2026-06-01')
  assert.equal(resolveChatAsOf([], '2026-06-30'), '2026-06-30')
})
