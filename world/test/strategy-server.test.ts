import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test, type TestContext } from 'node:test'
import assert from 'node:assert/strict'
import { createStrategyServer, type StrategyServerHandle } from '../src/strategy-server/server.ts'
import { shadowWorkspaceDir, strategyRevisionsFile } from '../src/paths.ts'

function seedMethodology(worldRoot: string, runId: string, botId: string, content: string): string {
  const dir = shadowWorkspaceDir(worldRoot, runId, botId)
  mkdirSync(dir, { recursive: true })
  const p = join(dir, 'METHODOLOGY.md')
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
async function freshServer(t: TestContext, opts: { day?: () => string } = {}): Promise<{ s: StrategyServerHandle; worldRoot: string; runId: string }> {
  const worldRoot = mkdtempSync(join(tmpdir(), 'strat-'))
  const runId = 'r1'
  const s = await createStrategyServer({ worldRoot, runId, getCurrentDate: opts.day ?? (() => '2024-03-15') })
  t.after(async () => {
    await s.close()
    rmSync(worldRoot, { recursive: true, force: true })
  })
  return { s, worldRoot, runId }
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
  assert.deepEqual(names, ['get_active_strategy', 'get_my_strategy', 'get_strategy', 'list_strategies', 'update_my_strategy'])

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
  assert.deepEqual(j.tools.sort(), ['get_active_strategy', 'get_my_strategy', 'get_strategy', 'list_strategies', 'update_my_strategy'])
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
