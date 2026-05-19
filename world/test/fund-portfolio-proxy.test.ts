import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { createFundPortfolioProxy } from '../src/fund-portfolio-proxy/server.ts'

// 镜像 simworld-proxy.test.ts 的 stub upstream，把注入键换成 run_id。
// 支持模拟"upstream 重启"：调用 stub.restart() 清空 activeSessionIds —— 之后任何带旧 sid
// 的请求都返 HTTP 404 + {"error":{"message":"Session not found"}}（与真实 FastMCP 行为一致）。
async function startStubUpstream(): Promise<{
  url: string
  close: () => Promise<void>
  calls: Array<{ name: string; arguments: Record<string, unknown>; sid?: string }>
  initializeCount: () => number
  restart: () => void
}> {
  const calls: Array<{ name: string; arguments: Record<string, unknown>; sid?: string }> = []
  let sessionCounter = 0
  let initCount = 0
  const activeSessionIds = new Set<string>()
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    void (async () => {
      const chunks: Buffer[] = []
      for await (const c of req) chunks.push(c as Buffer)
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown> : {}
      const id = body.id as number | string | null | undefined
      const method = body.method as string | undefined
      const inSidRaw = req.headers['mcp-session-id']
      const inSid = typeof inSidRaw === 'string' ? inSidRaw : Array.isArray(inSidRaw) ? inSidRaw[0] : undefined
      const sse = (payload: Record<string, unknown>, sidOverride?: string): void => {
        const headers: Record<string, string> = { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' }
        if (sidOverride) headers['mcp-session-id'] = sidOverride
        else if (inSid) headers['mcp-session-id'] = inSid
        res.writeHead(200, headers)
        res.end(`event: message\ndata: ${JSON.stringify(payload)}\n\n`)
      }
      const reject404SessionNotFound = (): void => {
        res.writeHead(404, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ jsonrpc: '2.0', id: 'server-error', error: { code: -32600, message: 'Session not found' } }))
      }
      // 非 initialize 请求带的 sid 必须仍在活跃集里——FastMCP 重启会清空。
      if (method !== 'initialize' && inSid !== undefined && !activeSessionIds.has(inSid)) {
        return reject404SessionNotFound()
      }
      if (method === 'initialize') {
        initCount++
        const newSid = `sess-${++sessionCounter}`
        activeSessionIds.add(newSid)
        return sse({ jsonrpc: '2.0', id, result: { protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'stub-fund', version: '0' } } }, newSid)
      }
      if (method === 'tools/list') {
        return sse({
          jsonrpc: '2.0', id,
          result: {
            tools: [
              {
                name: 'apply_fund_review_and_rebalance',
                description: 'writer',
                inputSchema: {
                  type: 'object',
                  properties: { bot_id: { type: 'string' }, run_id: { type: 'string' } },
                  required: ['bot_id', 'run_id'],
                },
              },
              {
                name: 'portfolio_place_buy_order',
                description: 'bot writer',
                inputSchema: {
                  type: 'object',
                  properties: { bot_id: { type: 'string' }, fund_code: { type: 'string' }, run_id: { type: 'string' } },
                  required: ['bot_id', 'fund_code'],
                },
              },
              {
                name: 'get_fund_pool',
                description: 'pure read',
                inputSchema: { type: 'object', properties: { fund_type: { type: 'string' } }, required: [] },
              },
            ],
          },
        })
      }
      if (method === 'tools/call') {
        const params = (body.params ?? {}) as Record<string, unknown>
        const name = typeof params.name === 'string' ? params.name : ''
        const args = (params.arguments ?? {}) as Record<string, unknown>
        const rec: { name: string; arguments: Record<string, unknown>; sid?: string } = { name, arguments: args }
        if (inSid !== undefined) rec.sid = inSid
        calls.push(rec)
        return sse({ jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: JSON.stringify(args) }] } })
      }
      return sse({ jsonrpc: '2.0', id, result: {} })
    })().catch(() => {
      res.writeHead(500); res.end()
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', () => resolve()))
  const port = (server.address() as AddressInfo).port
  return {
    url: `http://127.0.0.1:${port}/mcp`,
    calls,
    initializeCount: () => initCount,
    restart: () => activeSessionIds.clear(),
    close: () => new Promise<void>((resolve, reject) => server.close(err => err ? reject(err) : resolve())),
  }
}

async function postJson(url: string, body: unknown, headers: Record<string, string> = {}): Promise<{ status: number; headers: Headers; text: string }> {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream', ...headers }, body: JSON.stringify(body) })
  const text = await r.text()
  return { status: r.status, headers: r.headers, text }
}

function parseSseMessage(text: string): Record<string, unknown> {
  for (const line of text.split('\n')) {
    if (line.startsWith('data:')) return JSON.parse(line.slice(5).trimStart()) as Record<string, unknown>
  }
  throw new Error(`no data line in SSE: ${text.slice(0, 200)}`)
}

test('tools/list strips run_id from properties and required', async () => {
  const up = await startStubUpstream()
  const proxy = await createFundPortfolioProxy({ upstreamUrl: up.url, runId: 'run-XYZ' })
  try {
    const r = await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'tools/list' })
    const msg = parseSseMessage(r.text) as { result: { tools: Array<Record<string, unknown>> } }
    const t1 = msg.result.tools.find(t => t.name === 'apply_fund_review_and_rebalance')!
    const s1 = t1.inputSchema as { properties: Record<string, unknown>; required: string[] }
    assert.equal('run_id' in s1.properties, false)
    assert.deepEqual(s1.required, ['bot_id'])

    const t2 = msg.result.tools.find(t => t.name === 'portfolio_place_buy_order')!
    const s2 = t2.inputSchema as { properties: Record<string, unknown>; required: string[] }
    assert.equal('run_id' in s2.properties, false)
    assert.deepEqual(s2.required, ['bot_id', 'fund_code'])

    const t3 = msg.result.tools.find(t => t.name === 'get_fund_pool')!
    const s3 = t3.inputSchema as { properties: Record<string, unknown>; required: string[] }
    assert.deepEqual(Object.keys(s3.properties), ['fund_type'])
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('tools/call injects run_id for writer tools after tools/list seeded cache; skips read-only', async () => {
  const up = await startStubUpstream()
  const proxy = await createFundPortfolioProxy({ upstreamUrl: up.url, runId: 'run-A' })
  try {
    await postJson(proxy.url, { jsonrpc: '2.0', id: 0, method: 'tools/list' })
    await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'apply_fund_review_and_rebalance', arguments: { bot_id: 'bot7' } } })
    await postJson(proxy.url, { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'portfolio_place_buy_order', arguments: { bot_id: 'bot7', fund_code: '000001' } } })
    await postJson(proxy.url, { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'get_fund_pool', arguments: { fund_type: 'equity' } } })

    assert.deepEqual(up.calls[0], { name: 'apply_fund_review_and_rebalance', arguments: { bot_id: 'bot7', run_id: 'run-A' } })
    assert.deepEqual(up.calls[1], { name: 'portfolio_place_buy_order', arguments: { bot_id: 'bot7', fund_code: '000001', run_id: 'run-A' } })
    // 纯读工具不带 run_id —— schema 里没 run_id，proxy 不注入
    assert.deepEqual(up.calls[2], { name: 'get_fund_pool', arguments: { fund_type: 'equity' } })
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('tools/call force-overwrites bot-supplied run_id (audit invariant)', async () => {
  const up = await startStubUpstream()
  const proxy = await createFundPortfolioProxy({ upstreamUrl: up.url, runId: 'real-run' })
  try {
    await postJson(proxy.url, { jsonrpc: '2.0', id: 0, method: 'tools/list' })
    await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'apply_fund_review_and_rebalance', arguments: { bot_id: 'bot7', run_id: 'bot-faked' } } })
    assert.equal(up.calls[0].arguments.run_id, 'real-run')
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('without tools/list seed, tools/call still injects (conservative default)', async () => {
  const up = await startStubUpstream()
  const proxy = await createFundPortfolioProxy({ upstreamUrl: up.url, runId: 'run-B' })
  try {
    await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'apply_fund_review_and_rebalance', arguments: { bot_id: 'bot7' } } })
    assert.equal(up.calls[0].arguments.run_id, 'run-B')
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('mcp-session-id is round-tripped from upstream to client and back', async () => {
  const up = await startStubUpstream()
  const proxy = await createFundPortfolioProxy({ upstreamUrl: up.url, runId: 'run-S' })
  try {
    const init = await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })
    const sid = init.headers.get('mcp-session-id')
    assert.equal(typeof sid, 'string')
    assert.ok(sid && sid.length > 0)
    const list = await postJson(proxy.url, { jsonrpc: '2.0', id: 2, method: 'tools/list' }, { 'mcp-session-id': sid! })
    assert.equal(list.headers.get('mcp-session-id'), sid)
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('GET /health returns ok with upstream URL + runId', async () => {
  const up = await startStubUpstream()
  const proxy = await createFundPortfolioProxy({ upstreamUrl: up.url, runId: 'run-H' })
  try {
    const proxyBase = proxy.url.replace(/\/mcp$/, '')
    const r = await fetch(`${proxyBase}/health`)
    assert.equal(r.status, 200)
    const body = await r.json() as { status: string; upstream: string; runId: string }
    assert.equal(body.status, 'ok')
    assert.equal(body.upstream, up.url)
    assert.equal(body.runId, 'run-H')
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('createFundPortfolioProxy rejects empty runId', async () => {
  await assert.rejects(
    () => createFundPortfolioProxy({ upstreamUrl: 'http://x', runId: '' }),
    /runId is required/,
  )
})

test('upstream session expiry (HTTP 404 Session not found) triggers transparent re-init + retry', async () => {
  // 真实场景：lab-fund-bot-only 在 run 中途被 systemctl restart,FastMCP 内存里旧 sid 全没了,
  // bot 旧请求拿到 404 + "Session not found"。proxy 应该自动重新 initialize 一次新 session,
  // 用新 sid 重试原请求,bot 完全感知不到。
  const up = await startStubUpstream()
  const proxy = await createFundPortfolioProxy({ upstreamUrl: up.url, runId: 'run-RR' })
  try {
    // 1) bot initialize → proxy 缓存 init 请求,upstream 发首个 session
    const init = await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05' } })
    assert.equal(init.status, 200)
    const botSid = init.headers.get('mcp-session-id')
    assert.ok(botSid && botSid.length > 0, `bot sid: ${botSid}`)

    // 2) 正常 tools/call 通,upstream 看到 1 次 initialize
    const r1 = await postJson(proxy.url, { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'portfolio_place_buy_order', arguments: { bot_id: 'b1', fund_code: 'F1' } } }, { 'mcp-session-id': botSid! })
    assert.equal(r1.status, 200, `first call status (text=${r1.text.slice(0, 200)})`)
    assert.equal(up.calls.length, 1)
    assert.equal(up.initializeCount(), 1)

    // 3) "upstream 重启"——清空 sid 表
    up.restart()

    // 4) bot 用同一个 sid 再发请求 → 应该看到成功（proxy 内部 reinit 完透明重试）
    const r2 = await postJson(proxy.url, { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'portfolio_place_buy_order', arguments: { bot_id: 'b1', fund_code: 'F2' } } }, { 'mcp-session-id': botSid! })
    assert.equal(r2.status, 200, `after-restart call status, text=${r2.text.slice(0, 300)}`)
    const msg = parseSseMessage(r2.text) as { result: { content: Array<{ text: string }> } }
    const echoed = JSON.parse(msg.result.content[0].text) as Record<string, unknown>
    assert.equal(echoed.fund_code, 'F2', `echo ${JSON.stringify(echoed)}`)
    assert.equal(echoed.run_id, 'run-RR', 'run_id 注入仍要工作')

    // 5) upstream 此时应该看到 2 次 initialize（首次 + 重启后 proxy 自动 reinit）
    //    和 2 次成功的 tools/call（首次的 sess-1 + 重连后的 sess-2）
    assert.equal(up.initializeCount(), 2, '应该自动 reinit 一次')
    assert.equal(up.calls.length, 2, 'tools/call 都到 upstream')
    assert.notEqual(up.calls[0].sid, up.calls[1].sid, '两次调用用了不同的 upstream sid')
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('bot-facing mcp-session-id stays stable across upstream reconnect', async () => {
  // bot 用 botSid 走完整生命周期 —— 即使 proxy 内部 rotate 了 upstream sid,bot 看到的 sid 不变。
  const up = await startStubUpstream()
  const proxy = await createFundPortfolioProxy({ upstreamUrl: up.url, runId: 'run-stable' })
  try {
    const init = await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })
    const botSid = init.headers.get('mcp-session-id')
    assert.ok(botSid)

    up.restart()

    const r = await postJson(proxy.url, { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'portfolio_place_buy_order', arguments: { bot_id: 'b1', fund_code: 'F1' } } }, { 'mcp-session-id': botSid! })
    // 响应回的 mcp-session-id（如果有）必须仍是 botSid,不能暴露内部新 sid
    const respSid = r.headers.get('mcp-session-id')
    if (respSid !== null) assert.equal(respSid, botSid, `bot 不能看见内部 sid rotate: ${respSid} vs ${botSid}`)
  } finally {
    await proxy.close()
    await up.close()
  }
})
