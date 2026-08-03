import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  activeCommitmentsForBot,
  decideSellCommitment,
  extractSuccessfulFundActions,
  renderDeepResearchCommitmentBlock,
  upsertDeepResearchCommitments,
} from '../src/deep-research-commitment.ts'
import { createFundPortfolioProxy } from '../src/fund-portfolio-proxy/server.ts'
import { renderDailyMessage } from "../src/message.ts"

test('深研成功买入形成承诺：失败单不计、同基金加仓只延长不缩短', () => {
  const trace = [
    { type: 'tool_start', name: 'start_research', arguments: { topic: 'x' } },
    { type: 'tool_end', name: 'start_research', content_preview: '{}' },
    { type: 'tool_start', name: 'mcp__fund_portfolio_mcp__portfolio_place_buy_order', arguments: { fund_code: '008591', amount: 200000, reason: '深研确认宽基上行，轻仓建立观察位' } },
    { type: 'tool_end', name: 'mcp__fund_portfolio_mcp__portfolio_place_buy_order', content_preview: JSON.stringify({ success: true, order_id: 1 }) },
    { type: 'tool_start', name: 'mcp__fund_portfolio_mcp__portfolio_place_buy_order', arguments: { fund_code: '019875', amount: 100000, reason: '会失败' } },
    { type: 'tool_end', name: 'mcp__fund_portfolio_mcp__portfolio_place_buy_order', content_preview: JSON.stringify({ success: false }) },
  ]
  const actions = extractSuccessfulFundActions(trace)
  assert.deepEqual(actions.buys, [{ fundCode: '008591', amount: 200000, reason: '深研确认宽基上行，轻仓建立观察位' }])

  let state = upsertDeepResearchCommitments({}, 'bot16d', '2025-10-21', actions.buys.map(b => ({ ...b, minHoldingDays: 7 })), 'deep_research')
  assert.equal(state.bot16d['008591'].commit_until, '2025-10-28')
  state = upsertDeepResearchCommitments(state, 'bot16d', '2025-10-22', [{ ...actions.buys[0], minHoldingDays: 3 }], 'deep_research')
  assert.equal(state.bot16d['008591'].commit_until, '2025-10-28')

  const active = activeCommitmentsForBot(state, 'bot16d', '2025-10-22', ['008591'])
  const block = renderDeepResearchCommitmentBlock(active, '2025-10-22', false)
  assert.match(block, /深研持仓承诺（系统状态 · 跨日生效）/)
  assert.match(block, /普通指标走弱.*不能推翻/)
  assert.match(block, /承诺至 2025-10-28（还剩 6 自然日）/)
  assert.equal(activeCommitmentsForBot(state, 'bot16d', '2025-10-28', ['008591']).length, 0)
})

async function startSellUpstream(): Promise<{ url: string; calls: unknown[]; close(): Promise<void> }> {
  const calls: unknown[] = []
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    void (async () => {
      const chunks: Buffer[] = []
      for await (const c of req) chunks.push(c as Buffer)
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>
      calls.push(body)
      res.writeHead(200, { 'content-type': 'text/event-stream' })
      res.end(`event: message\ndata: ${JSON.stringify({ jsonrpc: '2.0', id: body.id, result: { content: [{ type: 'text', text: '{"success":true}' }] } })}\n\n`)
    })()
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const port = (server.address() as AddressInfo).port
  return {
    url: `http://127.0.0.1:${port}/mcp`, calls,
    close: () => new Promise<void>((resolve, reject) => server.close(err => err ? reject(err) : resolve())),
  }
}

async function callSell(url: string): Promise<Record<string, unknown>> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'tools/call', params: { name: 'portfolio_place_sell_order', arguments: { bot_id: 'bot16d', fund_code: '008591', shares: 100 } } }),
  })
  const line = (await response.text()).split('\n').find(x => x.startsWith('data:'))!
  return JSON.parse(line.slice(5).trim()) as Record<string, unknown>
}

test('交易代理硬闸：普通日承诺期卖单不触达上游；深研日解锁', async () => {
  const upstream = await startSellUpstream()
  let unlocked = false
  const proxy = await createFundPortfolioProxy({
    upstreamUrl: upstream.url,
    runId: 'commit-run',
    getTradeDate: () => '2025-10-22',
    checkSellCommitment: () => unlocked
      ? { allowed: true }
      : { allowed: false, message: '008591 承诺至 2025-10-28' },
  })
  try {
    const blocked = await callSell(proxy.url) as { result: { content: Array<{ text: string }> } }
    const blockedBody = JSON.parse(blocked.result.content[0].text)
    assert.equal(blockedBody.success, false)
    assert.equal(blockedBody.blocked_by, 'deep_research_commitment')
    assert.equal(upstream.calls.length, 0)

    unlocked = true
    await callSell(proxy.url)
    assert.equal(upstream.calls.length, 1)
  } finally {
    await proxy.close()
    await upstream.close()
  }
})


test("深研承诺块在 Day 1 和后续交易日都注入每日消息", () => {
  const commitmentBlock = "────────── 深研持仓承诺（系统状态 · 跨日生效） ──────────\n- 008591：承诺至 2025-10-28"
  const base = {
    worldRoot: "/tmp",
    date: "2025-10-22",
    botId: "bot16d",
    quotesPath: "/tmp/quotes.json",
    deepResearchEnabled: true,
    deepResearchCommitmentBlock: commitmentBlock,
  }
  const day1 = renderDailyMessage({ ...base, isFirstDay: true })
  const dayN = renderDailyMessage({ ...base, isFirstDay: false })
  assert.match(day1, /深研持仓承诺（系统状态 · 跨日生效）/)
  assert.match(dayN, /深研持仓承诺（系统状态 · 跨日生效）/)
  assert.match(day1, /承诺至 2025-10-28/)
  assert.match(dayN, /承诺至 2025-10-28/)
})

test('kind 只升不降：min_hold 加仓不降级活跃 deep_research；min_hold 遇 deep_research 升级', () => {
  // deep_research 建仓 → 之后 min_hold 加仓：kind 保持 deep_research，commit_until 取更晚
  let s = upsertDeepResearchCommitments({}, 'bot16d', '2025-10-21', [{ fundCode: '008591', reason: '深研', minHoldingDays: 7 }], 'deep_research')
  assert.equal(s.bot16d['008591'].kind, 'deep_research')
  assert.equal(s.bot16d['008591'].commit_until, '2025-10-28')
  s = upsertDeepResearchCommitments(s, 'bot16d', '2025-10-22', [{ fundCode: '008591', reason: '普通加仓', minHoldingDays: 7 }], 'min_hold')
  assert.equal(s.bot16d['008591'].kind, 'deep_research', '活跃 deep_research 不被降级')
  assert.equal(s.bot16d['008591'].commit_until, '2025-10-29', 'commit_until 延长到 min_hold 到期')

  // min_hold 建仓 → deep_research 加仓：升级为 deep_research
  let t = upsertDeepResearchCommitments({}, 'bot5d', '2025-10-21', [{ fundCode: '510300', reason: '普通', minHoldingDays: 7 }], 'min_hold')
  assert.equal(t.bot5d['510300'].kind, 'min_hold')
  t = upsertDeepResearchCommitments(t, 'bot5d', '2025-10-22', [{ fundCode: '510300', reason: '深研', minHoldingDays: 7 }], 'deep_research')
  assert.equal(t.bot5d['510300'].kind, 'deep_research', 'min_hold 被深研买入升级')
})

test('decideSellCommitment 双轨：min_hold 只认硬风控/到期；deep_research 额外认重研', () => {
  const mk = (kind: 'deep_research' | 'min_hold') => ({
    bot_id: 'bot5d', fund_code: '510300', committed_on: '2025-10-21',
    commit_until: '2025-10-28', min_holding_days: 7, kind,
    thesis: 't',
  })
  const day = '2025-10-23' // 窗内

  // 到期：无条件放行
  assert.equal(decideSellCommitment({ commitment: mk('min_hold'), tradeDate: '2025-10-28', unlockedByResearch: false, unlockedByForcedRiskControl: false }).allowed, true)

  // min_hold 窗内、无解锁 → 拒
  const blocked = decideSellCommitment({ commitment: mk('min_hold'), tradeDate: day, unlockedByResearch: false, unlockedByForcedRiskControl: false })
  assert.equal(blocked.allowed, false)
  assert.match(blocked.message!, /最短持有承诺/)

  // min_hold 窗内、主动深研(unlockedByResearch) → 仍拒(不解普通锁)
  assert.equal(decideSellCommitment({ commitment: mk('min_hold'), tradeDate: day, unlockedByResearch: true, unlockedByForcedRiskControl: false }).allowed, false)

  // min_hold 窗内、硬风控 → 放行
  assert.equal(decideSellCommitment({ commitment: mk('min_hold'), tradeDate: day, unlockedByResearch: false, unlockedByForcedRiskControl: true }).allowed, true)

  // deep_research 窗内、主动深研 → 放行(重研可证伪)
  assert.equal(decideSellCommitment({ commitment: mk('deep_research'), tradeDate: day, unlockedByResearch: true, unlockedByForcedRiskControl: false }).allowed, true)

  // deep_research 窗内、无解锁 → 拒
  const dr = decideSellCommitment({ commitment: mk('deep_research'), tradeDate: day, unlockedByResearch: false, unlockedByForcedRiskControl: false })
  assert.equal(dr.allowed, false)
  assert.match(dr.message!, /深研持仓承诺期/)

  // 旧承诺无 kind → 缺省 deep_research：主动深研可解锁
  const legacy = { bot_id: 'bot5d', fund_code: '510300', committed_on: '2025-10-21', commit_until: '2025-10-28', min_holding_days: 7, thesis: 't' } as any
  assert.equal(decideSellCommitment({ commitment: legacy, tradeDate: day, unlockedByResearch: true, unlockedByForcedRiskControl: false }).allowed, true)
})
