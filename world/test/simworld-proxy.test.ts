import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { createSimworldProxy } from '../src/simworld-proxy/server.ts'

// Minimal fake simworld-data upstream:
//   - tools/list returns 3 tools: one with simulated_datetime in props+required,
//     one with it in props but not required, one without it at all.
//   - tools/call echoes back the arguments it received (so the test can verify
//     injection).
//   - responds with SSE-shaped body (matches real simworld-data wire format).
async function startStubUpstream(): Promise<{ url: string; close: () => Promise<void>; calls: Array<{ name: string; arguments: Record<string, unknown> }> }> {
  const calls: Array<{ name: string; arguments: Record<string, unknown> }> = []
  let sessionCounter = 0
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    void (async () => {
      const chunks: Buffer[] = []
      for await (const c of req) chunks.push(c as Buffer)
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown> : {}
      const id = body.id as number | string | null | undefined
      const method = body.method as string | undefined
      const sse = (payload: Record<string, unknown>): void => {
        const headers: Record<string, string> = { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' }
        if (method === 'initialize') headers['mcp-session-id'] = `sess-${++sessionCounter}`
        // Preserve session id on subsequent calls
        const inSid = req.headers['mcp-session-id']
        if (typeof inSid === 'string') headers['mcp-session-id'] = inSid
        res.writeHead(200, headers)
        res.end(`event: message\ndata: ${JSON.stringify(payload)}\n\n`)
      }
      if (method === 'initialize') {
        return sse({ jsonrpc: '2.0', id, result: { protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'stub', version: '0' } } })
      }
      if (method === 'tools/list') {
        return sse({
          jsonrpc: '2.0', id,
          result: {
            tools: [
              {
                name: 'with_required_dt',
                description: 't1',
                inputSchema: {
                  type: 'object',
                  properties: { fund_codes: { type: 'array' }, simulated_datetime: { type: 'string' } },
                  required: ['fund_codes', 'simulated_datetime'],
                },
              },
              {
                name: 'with_optional_dt',
                description: 't2',
                inputSchema: {
                  type: 'object',
                  properties: { x: { type: 'string' }, simulated_datetime: { type: 'string' } },
                  required: ['x'],
                },
              },
              {
                name: 'no_dt',
                description: 't3',
                inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] },
              },
            ],
          },
        })
      }
      if (method === 'tools/call') {
        const params = (body.params ?? {}) as Record<string, unknown>
        const name = typeof params.name === 'string' ? params.name : ''
        const args = (params.arguments ?? {}) as Record<string, unknown>
        calls.push({ name, arguments: args })
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
    close: () => new Promise<void>((resolve, reject) => server.close(err => err ? reject(err) : resolve())),
  }
}

async function postJson(url: string, body: unknown, headers: Record<string, string> = {}): Promise<{ status: number; headers: Headers; text: string }> {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream', ...headers }, body: JSON.stringify(body) })
  const text = await r.text()
  return { status: r.status, headers: r.headers, text }
}

function parseSseMessage(text: string): Record<string, unknown> {
  // Find the first `data: ` line and parse as JSON.
  for (const line of text.split('\n')) {
    if (line.startsWith('data:')) return JSON.parse(line.slice(5).trimStart()) as Record<string, unknown>
  }
  throw new Error(`no data line in SSE: ${text.slice(0, 200)}`)
}

test('tools/list strips simulated_datetime from properties and required', async () => {
  const up = await startStubUpstream()
  const proxy = await createSimworldProxy({ upstreamUrl: up.url, getCurrentDate: () => '2024-03-15' })
  try {
    const r = await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'tools/list' })
    assert.equal(r.status, 200)
    const msg = parseSseMessage(r.text) as { result: { tools: Array<Record<string, unknown>> } }
    const tools = msg.result.tools
    assert.equal(tools.length, 3)
    const t1 = tools.find(t => t.name === 'with_required_dt')!
    const schema1 = t1.inputSchema as { properties: Record<string, unknown>; required: string[] }
    assert.equal('simulated_datetime' in schema1.properties, false)
    assert.deepEqual(schema1.required, ['fund_codes'])
    assert.equal('fund_codes' in schema1.properties, true)

    const t2 = tools.find(t => t.name === 'with_optional_dt')!
    const schema2 = t2.inputSchema as { properties: Record<string, unknown>; required: string[] }
    assert.equal('simulated_datetime' in schema2.properties, false)
    assert.deepEqual(schema2.required, ['x'])

    const t3 = tools.find(t => t.name === 'no_dt')!
    const schema3 = t3.inputSchema as { properties: Record<string, unknown>; required: string[] }
    assert.deepEqual(schema3.required, ['text'])
    assert.deepEqual(Object.keys(schema3.properties), ['text'])
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('tools/call injects simulated_datetime when schema has it; skips when it does not (after tools/list seeded cache)', async () => {
  const up = await startStubUpstream()
  let day = '2024-03-15'
  const proxy = await createSimworldProxy({ upstreamUrl: up.url, getCurrentDate: () => day })
  try {
    // Prime the schema cache.
    await postJson(proxy.url, { jsonrpc: '2.0', id: 0, method: 'tools/list' })

    // Tool with simulated_datetime → injected.
    await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'with_required_dt', arguments: { fund_codes: ['000001'] } } })
    // Tool with optional simulated_datetime → also injected.
    await postJson(proxy.url, { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'with_optional_dt', arguments: { x: 'hi' } } })
    // Tool without simulated_datetime → not injected.
    await postJson(proxy.url, { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'no_dt', arguments: { text: 'hello' } } })

    assert.equal(up.calls.length, 3)
    assert.deepEqual(up.calls[0], { name: 'with_required_dt', arguments: { fund_codes: ['000001'], simulated_datetime: '2024-03-15 15:00:00' } })
    assert.deepEqual(up.calls[1], { name: 'with_optional_dt', arguments: { x: 'hi', simulated_datetime: '2024-03-15 15:00:00' } })
    assert.deepEqual(up.calls[2], { name: 'no_dt', arguments: { text: 'hello' } })

    // Advance the world day; next call must pick it up.
    day = '2024-03-16'
    await postJson(proxy.url, { jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'with_required_dt', arguments: { fund_codes: ['000001'] } } })
    assert.equal(up.calls[3].arguments.simulated_datetime, '2024-03-16 15:00:00')
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('tools/call force-overwrites bot-supplied simulated_datetime (bot cannot dictate PIT cursor)', async () => {
  const up = await startStubUpstream()
  const proxy = await createSimworldProxy({ upstreamUrl: up.url, getCurrentDate: () => '2024-03-15' })
  try {
    await postJson(proxy.url, { jsonrpc: '2.0', id: 0, method: 'tools/list' })
    await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'with_required_dt', arguments: { fund_codes: ['000001'], simulated_datetime: '2099-01-01 09:00:00' } } })
    assert.equal(up.calls[0].arguments.simulated_datetime, '2024-03-15 15:00:00')
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('without tools/list seed, tools/call still injects (conservative default for time-gated upstream)', async () => {
  const up = await startStubUpstream()
  const proxy = await createSimworldProxy({ upstreamUrl: up.url, getCurrentDate: () => '2024-03-15' })
  try {
    await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'with_required_dt', arguments: { fund_codes: ['000001'] } } })
    assert.equal(up.calls[0].arguments.simulated_datetime, '2024-03-15 15:00:00')
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('mcp-session-id is round-tripped from upstream to client and back', async () => {
  const up = await startStubUpstream()
  const proxy = await createSimworldProxy({ upstreamUrl: up.url, getCurrentDate: () => '2024-03-15' })
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

// Regression for "0 tools captured for daily prompt" → bot hallucinates tool
// names. Real upstream runs FastMCP stateless_http=True (no mcp-session-id
// header on initialize, plain JSON body), and the probe used to require sid →
// throw → return []. Probe must accept both stateful and stateless upstreams.
async function startStatelessStubUpstream(): Promise<{ url: string; close: () => Promise<void> }> {
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    void (async () => {
      const chunks: Buffer[] = []
      for await (const c of req) chunks.push(c as Buffer)
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown> : {}
      const id = body.id as number | string | null | undefined
      const method = body.method as string | undefined
      const jsonOut = (payload: Record<string, unknown>): void => {
        // NO mcp-session-id header — stateless_http mode.
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify(payload))
      }
      if (method === 'initialize') {
        return jsonOut({ jsonrpc: '2.0', id, result: { protocolVersion: '2024-11-05', capabilities: {}, serverInfo: { name: 'stateless-stub', version: '0' } } })
      }
      if (method === 'tools/list') {
        return jsonOut({ jsonrpc: '2.0', id, result: { tools: [{ name: 'stateless_tool_a', description: 'a' }, { name: 'stateless_tool_b', description: 'b' }] } })
      }
      // notifications/initialized would 404 on a real stateless server — but
      // probe must not send it. If we get any other method, fail loudly so the
      // test catches a regression where probe still sends notifications.
      res.writeHead(400, { 'content-type': 'text/plain' })
      res.end(`stateless stub got unexpected method: ${method}`)
    })().catch(() => { res.writeHead(500); res.end() })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', () => resolve()))
  const port = (server.address() as AddressInfo).port
  return { url: `http://127.0.0.1:${port}/mcp`, close: () => new Promise<void>((resolve, reject) => server.close(err => err ? reject(err) : resolve())) }
}

test('probe captures tools from stateless upstream (no mcp-session-id)', async () => {
  const up = await startStatelessStubUpstream()
  const proxy = await createSimworldProxy({ upstreamUrl: up.url, getCurrentDate: () => '2024-03-15' })
  try {
    assert.equal(proxy.tools.length, 2)
    assert.deepEqual(proxy.tools.map(t => t.name).sort(), ['stateless_tool_a', 'stateless_tool_b'])
    assert.equal(proxy.tools[0].description, 'a')
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('initialize / prompts / resources pass through unchanged', async () => {
  const up = await startStubUpstream()
  const proxy = await createSimworldProxy({ upstreamUrl: up.url, getCurrentDate: () => '2024-03-15' })
  try {
    const init = await postJson(proxy.url, { jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })
    const initMsg = parseSseMessage(init.text) as { result: { serverInfo: { name: string } } }
    assert.equal(initMsg.result.serverInfo.name, 'stub')

    const prompts = await postJson(proxy.url, { jsonrpc: '2.0', id: 2, method: 'prompts/list' })
    const pMsg = parseSseMessage(prompts.text)
    assert.deepEqual(pMsg.result, {})
  } finally {
    await proxy.close()
    await up.close()
  }
})

test('GET /health returns ok with upstream URL', async () => {
  const up = await startStubUpstream()
  const proxy = await createSimworldProxy({ upstreamUrl: up.url, getCurrentDate: () => '2024-03-15' })
  try {
    const proxyBase = proxy.url.replace(/\/mcp$/, '')
    const r = await fetch(`${proxyBase}/health`)
    assert.equal(r.status, 200)
    const body = await r.json() as { status: string; upstream: string }
    assert.equal(body.status, 'ok')
    assert.equal(body.upstream, up.url)
  } finally {
    await proxy.close()
    await up.close()
  }
})
