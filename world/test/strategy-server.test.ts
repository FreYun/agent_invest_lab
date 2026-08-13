import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test, type TestContext } from 'node:test'
import assert from 'node:assert/strict'
import { createStrategyServer, runSqlite, type StrategyServerHandle } from '../src/strategy-server/server.ts'
import { shadowWorkspaceDir, strategyRevisionsFile, userRevisionsFile } from '../src/paths.ts'

function seedMethodology(worldRoot: string, runId: string, botId: string, content: string): string {
  const dir = shadowWorkspaceDir(worldRoot, runId, botId)
  mkdirSync(dir, { recursive: true })
  const p = join(dir, 'METHODOLOGY.md')
  writeFileSync(p, content)
  return p
}

function seedUser(worldRoot: string, runId: string, botId: string, content: string): string {
  const dir = shadowWorkspaceDir(worldRoot, runId, botId)
  mkdirSync(dir, { recursive: true })
  const p = join(dir, 'USER.md')
  writeFileSync(p, content)
  return p
}

function seedStrategyLibrary(worldRoot: string, runId: string, botId: string): void {
  const dir = join(shadowWorkspaceDir(worldRoot, runId, botId), 'strategies', 'index-products')
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'hs300.md'), '# HS300 shared strategy\n')
  writeFileSync(join(dir, 'semi.md'), '# Semiconductor shared strategy\n')
  writeFileSync(join(dir, 'manifest.yaml'), [
    'version: 1',
    'strategies:',
    '  hs300:',
    '    title: 沪深300指数投资框架',
    '    methodology: hs300.md',
    '    target_index: "000300.SH"',
    '    default_buyable_fund_codes: ["000051"]',
    '  semiconductor:',
    '    title: 半导体设备指数投资框架',
    '    methodology: semi.md',
    '    target_index: "931865.CSI"',
    '    default_buyable_fund_codes: ["014854"]',
  ].join('\n') + '\n')
}

interface Rpc {
  jsonrpc: '2.0'
  id?: number | string | null
  method?: string
  params?: Record<string, unknown>
}

// 默认走 application/json 响应（spec 允许；测试里更好对付）。需要测 SSE 路径时
// 用 rpcSse。两种应该返回相同的 RPC payload，只是 wire format 不同。
async function rpc(url: string, body: Rpc, sessionId?: string): Promise<{ status: number; headers: Headers; body: unknown }> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    accept: 'application/json',
  }
  if (sessionId) headers['mcp-session-id'] = sessionId
  const r = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) })
  const text = await r.text()
  return { status: r.status, headers: r.headers, body: text ? JSON.parse(text) : null }
}

async function rpcSse(url: string, body: Rpc, sessionId?: string): Promise<{ status: number; headers: Headers; body: unknown; rawSse: string }> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    accept: 'application/json, text/event-stream',
  }
  if (sessionId) headers['mcp-session-id'] = sessionId
  const r = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) })
  const text = await r.text()
  // SSE: parse single `event: message\ndata: <json>\n\n`
  const m = text.match(/^data:\s*(.+)$/m)
  const payload = m ? JSON.parse(m[1]) : null
  return { status: r.status, headers: r.headers, body: payload, rawSse: text }
}

// 每个 test 起一个独立的临时 worldRoot + strategy-server，通过 t.after() 确保
// 即使 assertion fail 也会关 server / 清目录——否则未关的 HTTP server 会让
// node:test 进程不退出，整个 suite 挂。
async function freshServer(t: TestContext, opts: { day?: () => string; enableUserSelfEdit?: boolean } = {}): Promise<{ s: StrategyServerHandle; worldRoot: string; runId: string; fundDbPath: string }> {
  const worldRoot = mkdtempSync(join(tmpdir(), 'strat-'))
  const runId = 'r1'
  // 临时 fund.db（market_reports 读写指向它，避免落到真实 <repo>/data/fund.db）。
  const fundDbPath = join(worldRoot, 'market_reports_test.db')
  const s = await createStrategyServer({ worldRoot, runId, fundDbPath, getCurrentDate: opts.day ?? (() => '2024-03-15'), enableUserSelfEdit: opts.enableUserSelfEdit })
  t.after(async () => {
    await s.close()
    rmSync(worldRoot, { recursive: true, force: true })
  })
  return { s, worldRoot, runId, fundDbPath }
}

test('initialize returns full capabilities (experimental/prompts/resources/tools), instructions, and mints mcp-session-id header', async (t) => {
  const { s } = await freshServer(t)
  const { status, headers, body } = await rpc(s.url, { jsonrpc: '2.0', id: 1, method: 'initialize' })
  assert.equal(status, 200)
  const result = (body as { result: Record<string, unknown> }).result
  assert.equal(result.protocolVersion, '2024-11-05')
  // FastMCP-style capabilities：四类都展开，便于客户端能力门控
  const caps = result.capabilities as Record<string, unknown>
  assert.ok('experimental' in caps)
  assert.ok('prompts' in caps)
  assert.ok('resources' in caps)
  assert.ok('tools' in caps)
  assert.equal((result.serverInfo as { name: string }).name, 'strategy-server')
  // instructions 字段给客户端做服务描述
  assert.match(result.instructions as string, /策略文档服务/)
  // mcp-session-id header 在 initialize 时签发
  const sid = headers.get('mcp-session-id')
  assert.ok(sid && sid.length > 0, 'initialize response must mint mcp-session-id header')
})

test('initialize over SSE Accept returns text/event-stream body with the same payload', async (t) => {
  const { s } = await freshServer(t)
  const { status, headers, body, rawSse } = await rpcSse(s.url, { jsonrpc: '2.0', id: 1, method: 'initialize' })
  assert.equal(status, 200)
  // FastMCP 同款 wire format
  assert.match(headers.get('content-type') ?? '', /text\/event-stream/)
  assert.match(rawSse, /^event: message$/m)
  assert.match(rawSse, /^data: \{/m)
  // payload 解出来应该跟 JSON 路径一致
  const result = (body as { result: { protocolVersion: string } }).result
  assert.equal(result.protocolVersion, '2024-11-05')
  // session id 在 SSE 路径上也必须签发
  assert.ok(headers.get('mcp-session-id'))
})

test('subsequent requests echo back the provided mcp-session-id header', async (t) => {
  const { s } = await freshServer(t)
  const init = await rpc(s.url, { jsonrpc: '2.0', id: 1, method: 'initialize' })
  const sid = init.headers.get('mcp-session-id')!
  // 后续请求带上 sid → 响应也应该带 sid（spec：客户端用它做关联）
  const list = await rpc(s.url, { jsonrpc: '2.0', id: 2, method: 'tools/list' }, sid)
  assert.equal(list.headers.get('mcp-session-id'), sid)
  // 不带 sid 也应该正常工作（我们无认证）
  const nosid = await rpc(s.url, { jsonrpc: '2.0', id: 3, method: 'tools/list' })
  assert.equal(nosid.status, 200)
})

test('tools/list returns FastMCP-style tool declarations: title on properties + inputSchema.title + outputSchema', async (t) => {
  const { s } = await freshServer(t)
  const { status, body } = await rpc(s.url, { jsonrpc: '2.0', id: 2, method: 'tools/list' })
  assert.equal(status, 200)
  const tools = ((body as { result: { tools: Array<Record<string, unknown>> } }).result).tools
  const names = tools.map(t => t.name as string).sort()
  assert.deepEqual(names, ['get_active_strategy', 'get_market_report', 'get_my_strategy', 'get_strategy', 'get_v5_mainline_plan', 'list_strategies', 'submit_market_report', 'update_my_strategy'])

  const update = tools.find(t => t.name === 'update_my_strategy')!
  // FastMCP 风格：inputSchema 自带 title
  const inSchema = update.inputSchema as { type: string; title: string; properties: Record<string, { type: string; title: string; description: string }>; required: string[] }
  assert.equal(inSchema.type, 'object')
  assert.equal(inSchema.title, 'update_my_strategyArguments')
  assert.deepEqual(inSchema.required.sort(), ['bot_id', 'reason', 'strategy'])
  // 每个 property 都有 title（FastMCP 风格）+ description（我们额外补充给 LLM 看的语义）
  for (const key of ['bot_id', 'strategy', 'reason']) {
    const p = inSchema.properties[key]
    assert.equal(p.type, 'string', `${key}.type`)
    assert.ok(p.title && p.title.length > 0, `${key}.title must be present`)
    assert.ok(p.description && p.description.length > 0, `${key}.description must be present`)
  }
  // outputSchema 描述返回结构（FastMCP 默认包成 {result: string}）
  const outSchema = update.outputSchema as { type: string; title: string; required: string[] } | undefined
  assert.ok(outSchema, 'outputSchema must be present')
  assert.equal(outSchema!.type, 'object')
  assert.match(outSchema!.title, /update_my_strategy/)
  assert.deepEqual(outSchema!.required, ['result'])

  // description 含 discover_tools 常用关键词
  assert.match(update.description as string, /strategy/)
  assert.match(update.description as string, /revision/)

  const get = tools.find(t => t.name === 'get_my_strategy')!
  const getIn = get.inputSchema as { required: string[] }
  assert.deepEqual(getIn.required, ['bot_id'])
})

test('update_my_strategy writes shadow METHODOLOGY.md + revisions audit; get_my_strategy reads it back', async (t) => {
  const { s, worldRoot, runId } = await freshServer(t)
  // shadow workspace 的 METHODOLOGY.md 起手是占位文本（模拟 buildShadowWorkspace 已经拷过去了）
  const seedPath = seedMethodology(worldRoot, runId, 'bot7', '# 初始 methodology\n占位\n')

  const strategy = '# 我的 methodology\n核心信念：长期持有 + 月度再平衡。\n买入：低估时定投。\n卖出：估值百分位>90% 减仓。\n仓位：满仓 90%。'
  const { status, body } = await rpc(s.url, {
    jsonrpc: '2.0', id: 3, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy, reason: '初版策略：长期 + 估值规则' } },
  })
  assert.equal(status, 200)
  const result = (body as { result: { content: Array<{ type: string; text: string }>; isError?: boolean } }).result
  assert.ok(!result.isError, `update should succeed, got: ${JSON.stringify(result)}`)
  assert.match(result.content[0].text, /METHODOLOGY\.md 已更新/)

  // shadow METHODOLOGY.md 被覆写
  const onDisk = readFileSync(seedPath, 'utf8')
  assert.match(onDisk, /核心信念：长期持有/)
  assert.doesNotMatch(onDisk, /初始 methodology/)

  // 审计日志落盘
  const revPath = strategyRevisionsFile(worldRoot, runId, 'bot7')
  assert.ok(existsSync(revPath))
  const revLines = readFileSync(revPath, 'utf8').trim().split('\n')
  assert.equal(revLines.length, 1)
  const rev = JSON.parse(revLines[0])
  assert.equal(rev.ts, '2024-03-15')
  assert.equal(rev.reason, '初版策略：长期 + 估值规则')
  assert.equal(rev.new_size, strategy.length)
  // prior_size 是占位 methodology 在磁盘上的字节数（不含 trailing newline 补全——seed 已自带 \n）
  assert.ok(typeof rev.prior_size === 'number' && rev.prior_size > 0)

  // get_my_strategy 读回
  const get = await rpc(s.url, {
    jsonrpc: '2.0', id: 4, method: 'tools/call',
    params: { name: 'get_my_strategy', arguments: { bot_id: 'bot7' } },
  })
  const getResult = (get.body as { result: { content: Array<{ text: string }>; isError?: boolean } }).result
  assert.ok(!getResult.isError)
  assert.match(getResult.content[0].text, /核心信念：长期持有/)
})

// 回归：自进化(update_my_strategy)不许把顶部「当前回测任务」锚(target_index / buyable_fund_codes)弄丢。
// 事故背景：robot bot 自改方法论时丢了 H30590.CSI 锚，漂到 000813.CSI(化工)，拿化工估值给机器人择时。
function seedHeaderPin(worldRoot: string, runId: string, botId: string, targetIndex: string, codes: string): void {
  const dir = shadowWorkspaceDir(worldRoot, runId, botId)
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, '.methodology-header.md'), [
    '# 当前回测任务', '',
    `- bot_id: ${botId}`,
    '- strategy_id: robot',
    `- target_index: ${targetIndex}`,
    `- buyable_fund_codes: ${codes}`,
    '', '---', '',
  ].join('\n'))
}

test('update_my_strategy 重写时重新锚定 target_index（bot 提交无头正文，pin 头被拼回）', async (t) => {
  const { s, worldRoot, runId } = await freshServer(t)
  const seedPath = seedMethodology(worldRoot, runId, 'bot18', '# 当前回测任务\n- target_index: H30590.CSI\n\n---\n\n# 旧正文\n')
  seedHeaderPin(worldRoot, runId, 'bot18', 'H30590.CSI', '014881')

  // bot 自改：只交正文、无任务头，并（错误地）在正文里提到 000813 化工
  const strategy = '# 机器人方法论 v2\n估值极端预警。参考 000813 的估值消化。\n仓位 40% 底仓。'
  const { status, body } = await rpc(s.url, {
    jsonrpc: '2.0', id: 30, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot18', strategy, reason: '简化框架' } },
  })
  assert.equal(status, 200)
  const result = (body as { result: { content: Array<{ text: string }>; isError?: boolean } }).result
  assert.ok(!result.isError)

  const onDisk = readFileSync(seedPath, 'utf8')
  // 权威锚被拼回：target_index 仍是 H30590.CSI，且出现在顶部任务头里
  assert.match(onDisk, /# 当前回测任务/)
  assert.match(onDisk, /- target_index: H30590\.CSI/)
  assert.match(onDisk, /- buyable_fund_codes: 014881/)
  // bot 的新正文保留
  assert.match(onDisk, /机器人方法论 v2/)
  assert.match(onDisk, /40% 底仓/)
  // 顶部任务头这一行里不能出现 000813（正文里 bot 自己提到不管，但锚必须是 H30590）
  const headerBlock = onDisk.split('---')[0]
  assert.doesNotMatch(headerBlock, /target_index:.*000813/)
})

test('update_my_strategy 剥掉 bot 篡改的任务头，换回权威 pin 头', async (t) => {
  const { s, worldRoot, runId } = await freshServer(t)
  const seedPath = seedMethodology(worldRoot, runId, 'bot18', '# 当前回测任务\n- target_index: H30590.CSI\n\n---\n\n# 旧正文\n')
  seedHeaderPin(worldRoot, runId, 'bot18', 'H30590.CSI', '014881')

  // bot 提交时自带一个被篡改的任务头（把标的改成 000813 化工）+ 正文
  const tampered = '# 当前回测任务\n- target_index: 000813.CSI\n- buyable_fund_codes: 020274\n\n---\n\n# 我的新正文\n改到化工去。'
  await rpc(s.url, {
    jsonrpc: '2.0', id: 31, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot18', strategy: tampered, reason: '想换标的' } },
  })

  const onDisk = readFileSync(seedPath, 'utf8')
  const headerBlock = onDisk.split('---')[0]
  // 篡改头被剥掉、换回权威 H30590；化工代码不得进任务头
  assert.match(headerBlock, /- target_index: H30590\.CSI/)
  assert.doesNotMatch(headerBlock, /000813/)
  assert.doesNotMatch(headerBlock, /020274/)
  // 只保留一个任务头（没重复）
  assert.equal(onDisk.match(/# 当前回测任务/g)?.length, 1)
  // bot 正文仍在
  assert.match(onDisk, /我的新正文/)
})

test('update_my_strategy second time replaces content and appends a second revision with prior_size', async (t) => {
  let day = '2024-03-15'
  const { s, worldRoot, runId } = await freshServer(t, { day: () => day })
  const seedPath = seedMethodology(worldRoot, runId, 'bot7', '# 占位\n')
  // server 写盘时给没 `\n` 结尾的 strategy 补一个，所以 prior_size = 磁盘字节 = strategy.length + 1
  const v1 = '# methodology\nv1: 第一版'
  const v2 = '# methodology\nv2: 第二版，更激进\n仓位上限 100%\n止损 -10%'

  await rpc(s.url, {
    jsonrpc: '2.0', id: 10, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy: v1, reason: '初版' } },
  })
  day = '2024-03-20'
  await rpc(s.url, {
    jsonrpc: '2.0', id: 11, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy: v2, reason: '上一版止损太严，调到 -10%' } },
  })

  const onDisk = readFileSync(seedPath, 'utf8')
  assert.match(onDisk, /v2: 第二版/)
  assert.doesNotMatch(onDisk, /v1: 第一版/)

  const revs = readFileSync(strategyRevisionsFile(worldRoot, runId, 'bot7'), 'utf8').trim().split('\n').map(line => JSON.parse(line))
  assert.equal(revs.length, 2)
  assert.equal(revs[0].ts, '2024-03-15')
  // prior_size 是 seed 占位文件的字节数
  assert.ok(typeof revs[0].prior_size === 'number' && revs[0].prior_size > 0)
  assert.equal(revs[1].ts, '2024-03-20')
  assert.equal(revs[1].prior_size, v1.length + 1)
  assert.equal(revs[1].new_size, v2.length)
  assert.match(revs[1].reason, /止损太严/)
})

test('update_my_strategy rejects missing fields and path-traversal bot_id', async (t) => {
  const { s, worldRoot, runId } = await freshServer(t)
  seedMethodology(worldRoot, runId, 'bot7', '# 占位\n')

  const r1 = await rpc(s.url, {
    jsonrpc: '2.0', id: 20, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { strategy: '随便写的', reason: 'r' } },
  })
  assert.equal(((r1.body as { result: { isError: boolean } }).result).isError, true, 'missing bot_id should error')

  const r2 = await rpc(s.url, {
    jsonrpc: '2.0', id: 21, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy: '', reason: 'r' } },
  })
  const result2 = (r2.body as { result: { isError: boolean; content: Array<{ text: string }> } }).result
  assert.equal(result2.isError, true)
  assert.match(result2.content[0].text, /strategy 必填/)

  const r3 = await rpc(s.url, {
    jsonrpc: '2.0', id: 22, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy: '完整 methodology' } },
  })
  assert.equal(((r3.body as { result: { isError: boolean } }).result).isError, true, 'missing reason should error')

  const r4 = await rpc(s.url, {
    jsonrpc: '2.0', id: 23, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: '../../etc', strategy: 'x', reason: 'r' } },
  })
  const result4 = (r4.body as { result: { isError: boolean; content: Array<{ text: string }> } }).result
  assert.equal(result4.isError, true)
  assert.match(result4.content[0].text, /非法字符/)
})

test('get_my_strategy returns isError when shadow METHODOLOGY.md is missing', async (t) => {
  const { s } = await freshServer(t)
  const r = await rpc(s.url, {
    jsonrpc: '2.0', id: 30, method: 'tools/call',
    params: { name: 'get_my_strategy', arguments: { bot_id: 'bot7' } },
  })
  const result = (r.body as { result: { isError: boolean; content: Array<{ text: string }> } }).result
  assert.equal(result.isError, true)
  assert.match(result.content[0].text, /找不到/)
})

test('unknown tool / unknown method / notifications behave per spec', async (t) => {
  const { s } = await freshServer(t)

  const r1 = await rpc(s.url, {
    jsonrpc: '2.0', id: 40, method: 'tools/call',
    params: { name: 'no_such_tool', arguments: {} },
  })
  assert.equal(((r1.body as { result: { isError: boolean } }).result).isError, true)

  const r2 = await rpc(s.url, { jsonrpc: '2.0', id: 41, method: 'no_such_method' })
  assert.equal((r2.body as { error: { code: number } }).error.code, -32601)

  const r3 = await rpc(s.url, { jsonrpc: '2.0', method: 'notifications/initialized' })
  assert.equal(r3.status, 202)
})

test('health endpoint returns ok with tools list (smoke / dashboard helper)', async (t) => {
  const { s } = await freshServer(t)
  const healthUrl = s.url.replace(/\/mcp$/, '/health')
  const r = await fetch(healthUrl)
  assert.equal(r.status, 200)
  const j = await r.json() as { status: string; tools: string[] }
  assert.equal(j.status, 'ok')
  assert.deepEqual(j.tools.sort(), ['get_active_strategy', 'get_market_report', 'get_my_strategy', 'get_strategy', 'get_v5_mainline_plan', 'list_strategies', 'submit_market_report', 'update_my_strategy'])
})



test('strategy-server lists shared strategies, reads shared strategy, and reads active strategy', async (t) => {
  const { s, worldRoot, runId } = await freshServer(t)
  seedMethodology(worldRoot, runId, 'bot7', '# Active methodology\nstrategy_id: hs300\n')
  seedStrategyLibrary(worldRoot, runId, 'bot7')

  const list = await rpc(s.url, {
    jsonrpc: '2.0', id: 50, method: 'tools/call',
    params: { name: 'list_strategies', arguments: { bot_id: 'bot7' } },
  })
  const listResult = (list.body as { result: { content: Array<{ text: string }>; isError?: boolean } }).result
  assert.ok(!listResult.isError)
  const catalog = JSON.parse(listResult.content[0].text)
  assert.deepEqual(catalog.strategies.map((x: { strategy_id: string }) => x.strategy_id), ['hs300', 'semiconductor'])
  assert.equal(catalog.strategies[0].target_index, '000300.SH')

  const shared = await rpc(s.url, {
    jsonrpc: '2.0', id: 51, method: 'tools/call',
    params: { name: 'get_strategy', arguments: { bot_id: 'bot7', strategy_id: 'semiconductor' } },
  })
  const sharedResult = (shared.body as { result: { content: Array<{ text: string }>; isError?: boolean } }).result
  assert.ok(!sharedResult.isError)
  assert.match(sharedResult.content[0].text, /Semiconductor shared strategy/)

  const active = await rpc(s.url, {
    jsonrpc: '2.0', id: 52, method: 'tools/call',
    params: { name: 'get_active_strategy', arguments: { bot_id: 'bot7' } },
  })
  const activeResult = (active.body as { result: { content: Array<{ text: string }>; isError?: boolean } }).result
  assert.ok(!activeResult.isError)
  assert.match(activeResult.content[0].text, /# Active methodology/)
})

// ── market_reports（get/submit_market_report）────────────────────────────────

function callTool(url: string, id: number, name: string, args: Record<string, unknown>) {
  return rpc(url, { jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args } })
}
function toolResult(r: { body: unknown }) {
  return (r.body as { result: { content: Array<{ text: string }>; isError?: boolean } }).result
}

test('tools/list includes get_market_report + submit_market_report with PIT-relevant schemas', async (t) => {
  const { s } = await freshServer(t)
  const { body } = await rpc(s.url, { jsonrpc: '2.0', id: 2, method: 'tools/list' })
  const tools = ((body as { result: { tools: Array<Record<string, unknown>> } }).result).tools
  const get = tools.find(t => t.name === 'get_market_report')!
  const getIn = get.inputSchema as { required: string[] }
  assert.deepEqual(getIn.required.sort(), ['bot_id', 'report_type'])
  const sub = tools.find(t => t.name === 'submit_market_report')!
  const subIn = sub.inputSchema as { required: string[] }
  assert.deepEqual(subIn.required.sort(), ['bot_id', 'content_md', 'report_type'])
})

test('submit_market_report writes (reporter only) and get_market_report reads back the same day', async (t) => {
  const { s } = await freshServer(t, { day: () => '2024-03-15' })
  const sub = await callTool(s.url, 1, 'submit_market_report', {
    bot_id: 'reporter-context', report_type: 'market_context',
    content_md: '# 行情报告\nregime: 震荡\nrisk_state: neutral',
    structured_json: JSON.stringify({ regime: 'range', risk_state: 'neutral' }),
  })
  const subRes = toolResult(sub)
  assert.ok(!subRes.isError, JSON.stringify(subRes))
  assert.match(subRes.content[0].text, /已写入：market_context @ 2024-03-15/)

  const get = await callTool(s.url, 2, 'get_market_report', { bot_id: 'bot101', report_type: 'market_context' })
  const getRes = toolResult(get)
  assert.ok(!getRes.isError)
  assert.match(getRes.content[0].text, /regime: 震荡/)
  assert.match(getRes.content[0].text, /report_type=market_context as_of=2024-03-15/)
})

test('get_market_report enforces PIT: never returns a report dated after the world day', async (t) => {
  // 注意：用 mainline_rotation 而非 market_mainline——后者 submit 时强制走 v5 确定性规范化
  // （调 scripts/v5_mainline_plan.py），测试夹具的临时 worldRoot 调不到该脚本会直接 err。
  // PIT 语义与 report_type 无关，换不走规范化的类型测同一件事。
  let day = '2024-03-15'
  const { s } = await freshServer(t, { day: () => day })
  // 在 03-15 写一份
  const s1 = toolResult(await callTool(s.url, 1, 'submit_market_report', { bot_id: 'reporter-rotation', report_type: 'mainline_rotation', content_md: '主线@0315' }))
  assert.ok(!s1.isError, JSON.stringify(s1))
  // 时间前移到 04-15 再写一份
  day = '2024-04-15'
  const s2 = toolResult(await callTool(s.url, 2, 'submit_market_report', { bot_id: 'reporter-rotation', report_type: 'mainline_rotation', content_md: '主线@0415' }))
  assert.ok(!s2.isError, JSON.stringify(s2))

  // 世界日回到 03-20：只能读到 03-15 那份，绝不能读到未来的 04-15
  day = '2024-03-20'
  const g1 = toolResult(await callTool(s.url, 3, 'get_market_report', { bot_id: 'bot101', report_type: 'mainline_rotation' }))
  assert.match(g1.content[0].text, /主线@0315/)
  assert.doesNotMatch(g1.content[0].text, /主线@0415/)

  // 世界日到 05-01：读到最近一期 04-15
  day = '2024-05-01'
  const g2 = toolResult(await callTool(s.url, 4, 'get_market_report', { bot_id: 'bot101', report_type: 'mainline_rotation' }))
  assert.match(g2.content[0].text, /主线@0415/)
})

test('submit_market_report(market_mainline) errs when v5 plan source is unavailable (规范化 fail-fast)', async (t) => {
  const { s } = await freshServer(t, { day: () => '2024-03-15' })
  const r = toolResult(await callTool(s.url, 1, 'submit_market_report', { bot_id: 'reporter-mainline', report_type: 'market_mainline', content_md: '主线正文' }))
  assert.equal(r.isError, true)
  assert.match(r.content[0].text, /v5 规范化失败/)
})

test('submit_market_report is idempotent: same (type,as_of) overwrites', async (t) => {
  const { s } = await freshServer(t, { day: () => '2024-03-15' })
  await callTool(s.url, 1, 'submit_market_report', { bot_id: 'reporter-rotation', report_type: 'mainline_rotation', content_md: 'v1' })
  await callTool(s.url, 2, 'submit_market_report', { bot_id: 'reporter-rotation', report_type: 'mainline_rotation', content_md: 'v2-覆盖' })
  const g = toolResult(await callTool(s.url, 3, 'get_market_report', { bot_id: 'bot101', report_type: 'mainline_rotation' }))
  assert.match(g.content[0].text, /v2-覆盖/)
  assert.doesNotMatch(g.content[0].text, /v1/)
})

test('get_market_report report_type=all returns all three, with placeholder for missing', async (t) => {
  const { s } = await freshServer(t, { day: () => '2024-03-15' })
  await callTool(s.url, 1, 'submit_market_report', { bot_id: 'reporter-context', report_type: 'market_context', content_md: 'CTX正文' })
  const g = toolResult(await callTool(s.url, 2, 'get_market_report', { bot_id: 'bot101', report_type: 'all' }))
  assert.match(g.content[0].text, /CTX正文/)
  assert.match(g.content[0].text, /\[market_mainline\] 暂无报告/)
  assert.match(g.content[0].text, /\[mainline_rotation\] 暂无报告/)
})

test('get_market_report daily-alias: 主线走日度、rotation 只读月度真源（日度别名已移除）', async (t) => {
  const { s, fundDbPath } = await freshServer(t, { day: () => '2024-03-20' })
  const esc = (v: string): string => `'${v.replace(/'/g, "''")}'`
  const insert = (type: string, asOf: string, md: string): void => {
    runSqlite(fundDbPath, `INSERT INTO market_reports (report_type, as_of_date, scope, content_md) VALUES (${esc(type)}, ${esc(asOf)}, 'global', ${esc(md)});`)
  }
  // 2026-08-04：mainline_rotation 的日度别名已移除。别名语义是「日度更好，优先用」，但取数是
  // 「命中即 break」——日度表哪怕只有一行远古记录，月度回退也永不执行。实测 2025 回测里
  // mainline_rotation_daily 只有 15 天数据，却让这条路径整年返回 2025-01-22 的快照。
  // 现在 rotation 直读月度真源；日度视角的组合状态机已并入 market_mainline_daily。
  insert('mainline_rotation', '2024-03-15', '月度rotation正文')
  insert('mainline_rotation_daily', '2024-03-19', '日度rotation正文')
  insert('market_mainline', '2024-03-15', '月度主线正文')
  insert('market_mainline_daily', '2024-03-19', '日度主线正文')

  // rotation：日度表里即便有更新的一行，也必须拿月度（这正是回归点）
  const bot = toolResult(await callTool(s.url, 1, 'get_market_report', { bot_id: 'bot101', report_type: 'mainline_rotation' }))
  assert.match(bot.content[0].text, /月度rotation正文/)
  assert.match(bot.content[0].text, /report_type=mainline_rotation as_of=2024-03-15/)
  assert.doesNotMatch(bot.content[0].text, /日度rotation正文/)

  // 主线：日度别名保留，bot 拿日度
  const mainline = toolResult(await callTool(s.url, 2, 'get_market_report', { bot_id: 'bot101', report_type: 'market_mainline' }))
  assert.match(mainline.content[0].text, /日度主线正文/)
  assert.match(mainline.content[0].text, /report_type=market_mainline_daily as_of=2024-03-19/)

  // reporter-* 仍只读月度真源（月度管线不能被日度结果污染）
  const reporter = toolResult(await callTool(s.url, 3, 'get_market_report', { bot_id: 'reporter-rotation', report_type: 'market_mainline' }))
  assert.match(reporter.content[0].text, /月度主线正文/)
  assert.doesNotMatch(reporter.content[0].text, /日度主线正文/)
})

test('submit_market_report rejects non-reporter bot_id, bad report_type, bad json', async (t) => {
  const { s } = await freshServer(t)
  const notReporter = toolResult(await callTool(s.url, 1, 'submit_market_report', { bot_id: 'bot101', report_type: 'market_context', content_md: 'x' }))
  assert.equal(notReporter.isError, true)
  assert.match(notReporter.content[0].text, /仅供系统 reporter agent/)

  const badType = toolResult(await callTool(s.url, 2, 'submit_market_report', { bot_id: 'reporter-x', report_type: 'nope', content_md: 'x' }))
  assert.equal(badType.isError, true)
  assert.match(badType.content[0].text, /非法/)

  const badJson = toolResult(await callTool(s.url, 3, 'submit_market_report', { bot_id: 'reporter-x', report_type: 'market_context', content_md: 'x', structured_json: '{not json' }))
  assert.equal(badJson.isError, true)
  assert.match(badJson.content[0].text, /合法 JSON/)
})

test('get_market_report returns placeholder (not error) when no report exists yet', async (t) => {
  const { s } = await freshServer(t)
  const g = toolResult(await callTool(s.url, 1, 'get_market_report', { bot_id: 'bot101', report_type: 'market_context' }))
  assert.ok(!g.isError)
  assert.match(g.content[0].text, /暂无报告/)
})

// ── update_my_user / get_my_user（仅 enableUserSelfEdit=true 暴露）────────────────
test('enableUserSelfEdit=false：tools/list 不含 update_my_user/get_my_user，调用被拒', async (t) => {
  const { s } = await freshServer(t)  // 默认不开
  const { body } = await rpc(s.url, { jsonrpc: '2.0', id: 2, method: 'tools/list' })
  const names = ((body as { result: { tools: Array<{ name: string }> } }).result).tools.map(x => x.name)
  assert.ok(!names.includes('update_my_user'), 'update_my_user 不应出现在默认 tools/list')
  assert.ok(!names.includes('get_my_user'), 'get_my_user 不应出现在默认 tools/list')
  // 即便强行调用也被拒（gate）
  const r = toolResult(await callTool(s.url, 3, 'update_my_user', { bot_id: 'bot7', user_md: '# x', reason: 'y' }))
  assert.ok(r.isError, 'update_my_user 在未开启时应报错')
  assert.match(r.content[0].text, /未启用/)
})

test('enableUserSelfEdit=true：update_my_user 覆写 shadow USER.md + 审计；get_my_user 读回', async (t) => {
  const { s, worldRoot, runId } = await freshServer(t, { enableUserSelfEdit: true })
  const seedPath = seedUser(worldRoot, runId, 'bot101t', '# USER.md\n## 风险偏好\n占位\n')

  // tools/list 现在应含两个新工具
  const { body } = await rpc(s.url, { jsonrpc: '2.0', id: 2, method: 'tools/list' })
  const names = ((body as { result: { tools: Array<{ name: string }> } }).result).tools.map(x => x.name).sort()
  assert.ok(names.includes('update_my_user') && names.includes('get_my_user'))

  const userMd = '# USER.md\n## 风险偏好\n低风险——最大回撤红线 5%，以保本为先。\n## 工作准则\n按 METHODOLOGY 走。'
  const up = toolResult(await callTool(s.url, 3, 'update_my_user', { bot_id: 'bot101t', user_md: userMd, reason: '建档：分配到低风险偏好' }))
  assert.ok(!up.isError, `update should succeed: ${JSON.stringify(up)}`)
  assert.match(up.content[0].text, /USER\.md 已更新/)

  // shadow USER.md 被覆写
  const onDisk = readFileSync(seedPath, 'utf8')
  assert.match(onDisk, /最大回撤红线 5%/)
  assert.doesNotMatch(onDisk, /占位/)

  // 审计日志落 users/<bot>.revisions.jsonl
  const revPath = userRevisionsFile(worldRoot, runId, 'bot101t')
  assert.ok(existsSync(revPath))
  const rev = JSON.parse(readFileSync(revPath, 'utf8').trim().split('\n')[0])
  assert.equal(rev.reason, '建档：分配到低风险偏好')
  assert.equal(rev.new_size, userMd.length)

  // get_my_user 读回
  const got = toolResult(await callTool(s.url, 4, 'get_my_user', { bot_id: 'bot101t' }))
  assert.ok(!got.isError)
  assert.match(got.content[0].text, /最大回撤红线 5%/)
})
