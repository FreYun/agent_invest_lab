import { mkdtempSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test, type TestContext } from 'node:test'
import assert from 'node:assert/strict'
import { createStrategyServer, type StrategyServerHandle } from '../src/strategy-server/server.ts'
import { strategyFile, strategyRevisionsFile } from '../src/paths.ts'

interface Rpc {
  jsonrpc: '2.0'
  id?: number | string | null
  method?: string
  params?: Record<string, unknown>
}

async function rpc(url: string, body: Rpc): Promise<{ status: number; body: unknown }> {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  const status = r.status
  const text = await r.text()
  return { status, body: text ? JSON.parse(text) : null }
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

test('initialize returns protocol info and tools capability', async (t) => {
  const { s } = await freshServer(t)
  const { status, body } = await rpc(s.url, { jsonrpc: '2.0', id: 1, method: 'initialize' })
  assert.equal(status, 200)
  const result = (body as { result: Record<string, unknown> }).result
  assert.equal(result.protocolVersion, '2024-11-05')
  assert.deepEqual(result.capabilities, { tools: {} })
  assert.equal((result.serverInfo as { name: string }).name, 'strategy-server')
})

test('tools/list lists update_my_strategy and get_my_strategy with required fields and search keywords', async (t) => {
  const { s } = await freshServer(t)
  const { status, body } = await rpc(s.url, { jsonrpc: '2.0', id: 2, method: 'tools/list' })
  assert.equal(status, 200)
  const tools = ((body as { result: { tools: Array<{ name: string; description: string; inputSchema: { required: string[] } }> } }).result).tools
  const names = tools.map(t => t.name).sort()
  assert.deepEqual(names, ['get_my_strategy', 'update_my_strategy'])

  const update = tools.find(t => t.name === 'update_my_strategy')!
  assert.deepEqual(update.inputSchema.required.sort(), ['bot_id', 'reason', 'strategy'])
  // description 里要有 discover_tools 常用关键词，便于宽 query 命中
  assert.match(update.description, /strategy/)
  assert.match(update.description, /revision/)

  const get = tools.find(t => t.name === 'get_my_strategy')!
  assert.deepEqual(get.inputSchema.required, ['bot_id'])
})

test('update_my_strategy writes the strategy file + revisions audit; get_my_strategy reads it back', async (t) => {
  const { s, worldRoot, runId } = await freshServer(t)
  const strategy = '# MY_STRATEGY\n核心信念：长期持有 + 月度再平衡。\n买入：低估时定投。\n卖出：估值百分位>90% 减仓。\n仓位：满仓 90%。'
  const { status, body } = await rpc(s.url, {
    jsonrpc: '2.0', id: 3, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy, reason: '初版策略：长期 + 估值规则' } },
  })
  assert.equal(status, 200)
  const result = (body as { result: { content: Array<{ type: string; text: string }>; isError?: boolean } }).result
  assert.ok(!result.isError, `update should succeed, got: ${JSON.stringify(result)}`)
  assert.match(result.content[0].text, /策略已更新/)

  // 文件落盘
  const stratPath = strategyFile(worldRoot, runId, 'bot7')
  assert.ok(existsSync(stratPath))
  const onDisk = readFileSync(stratPath, 'utf8')
  assert.match(onDisk, /核心信念：长期持有/)
  assert.match(onDisk, /^# MY_STRATEGY/)

  // 审计日志落盘
  const revPath = strategyRevisionsFile(worldRoot, runId, 'bot7')
  assert.ok(existsSync(revPath))
  const revLines = readFileSync(revPath, 'utf8').trim().split('\n')
  assert.equal(revLines.length, 1)
  const rev = JSON.parse(revLines[0])
  assert.equal(rev.ts, '2024-03-15')
  assert.equal(rev.reason, '初版策略：长期 + 估值规则')
  assert.equal(rev.new_size, strategy.length)
  assert.equal(rev.prior_size, null)

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
  // server 写盘时给没 `\n` 结尾的 strategy 补一个，所以 prior_size = 磁盘字节 = strategy.length + 1
  const v1 = '# MY_STRATEGY\nv1: 第一版'
  const v2 = '# MY_STRATEGY\nv2: 第二版，更激进\n仓位上限 100%\n止损 -10%'

  await rpc(s.url, {
    jsonrpc: '2.0', id: 10, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy: v1, reason: '初版' } },
  })
  day = '2024-03-20'
  await rpc(s.url, {
    jsonrpc: '2.0', id: 11, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy: v2, reason: '上一版止损太严，调到 -10%' } },
  })

  const onDisk = readFileSync(strategyFile(worldRoot, runId, 'bot7'), 'utf8')
  assert.match(onDisk, /v2: 第二版/)
  assert.doesNotMatch(onDisk, /v1: 第一版/)

  const revs = readFileSync(strategyRevisionsFile(worldRoot, runId, 'bot7'), 'utf8').trim().split('\n').map(line => JSON.parse(line))
  assert.equal(revs.length, 2)
  assert.equal(revs[0].ts, '2024-03-15')
  assert.equal(revs[0].prior_size, null)
  assert.equal(revs[1].ts, '2024-03-20')
  assert.equal(revs[1].prior_size, v1.length + 1)
  assert.equal(revs[1].new_size, v2.length)
  assert.match(revs[1].reason, /止损太严/)
})

test('update_my_strategy rejects missing fields, non-prefixed strategy, and path-traversal bot_id', async (t) => {
  const { s } = await freshServer(t)

  const r1 = await rpc(s.url, {
    jsonrpc: '2.0', id: 20, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { strategy: '# MY_STRATEGY\nx', reason: 'r' } },
  })
  assert.equal(((r1.body as { result: { isError: boolean } }).result).isError, true, 'missing bot_id should error')

  const r2 = await rpc(s.url, {
    jsonrpc: '2.0', id: 21, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy: '随便写的策略', reason: 'r' } },
  })
  const result2 = (r2.body as { result: { isError: boolean; content: Array<{ text: string }> } }).result
  assert.equal(result2.isError, true)
  assert.match(result2.content[0].text, /必须以.*MY_STRATEGY/)

  const r3 = await rpc(s.url, {
    jsonrpc: '2.0', id: 22, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: 'bot7', strategy: '# MY_STRATEGY\nx' } },
  })
  assert.equal(((r3.body as { result: { isError: boolean } }).result).isError, true, 'missing reason should error')

  const r4 = await rpc(s.url, {
    jsonrpc: '2.0', id: 23, method: 'tools/call',
    params: { name: 'update_my_strategy', arguments: { bot_id: '../../etc', strategy: '# MY_STRATEGY\nx', reason: 'r' } },
  })
  const result4 = (r4.body as { result: { isError: boolean; content: Array<{ text: string }> } }).result
  assert.equal(result4.isError, true)
  assert.match(result4.content[0].text, /非法字符/)
})

test('get_my_strategy returns isError when no strategy file exists yet', async (t) => {
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
  assert.deepEqual(j.tools.sort(), ['get_my_strategy', 'update_my_strategy'])
})
