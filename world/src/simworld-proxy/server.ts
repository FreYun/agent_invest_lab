import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'

// simworld-data MCP (streamable-http) wrapper:
// - Strip `simulated_datetime` from every tool's inputSchema in tools/list.
// - On tools/call, force-inject `simulated_datetime = <world_date> 15:00:00`
//   so the bot can never set it (would-be future-function leak) and never
//   sees it as a knob.
// Session model: bot-side `mcp-session-id` is forwarded 1:1 as upstream
// session id; we never mint our own. That keeps the proxy stateless w.r.t.
// session bookkeeping — schema cache below is the only mutable state.

export interface SimworldProxyOptions {
  upstreamUrl: string
  getCurrentDate: () => string
  /** HH:MM:SS suffix appended to getCurrentDate(). Default '15:00:00' (A股收盘). */
  timeOfDay?: string
  host?: string
  port?: number
}

export interface SimworldProxyHandle {
  port: number
  url: string
  /** Upstream-advertised tools (name + one-line description). Empty if probe
   *  failed at startup — daily prompt renders without the tool list block. */
  tools: SimworldToolSummary[]
  close(): Promise<void>
}

export interface SimworldToolSummary {
  name: string
  description: string
}

const INJECT_KEY = 'simulated_datetime'

interface JsonRpcMessage {
  jsonrpc?: string
  id?: number | string | null
  method?: string
  params?: Record<string, unknown>
  result?: unknown
  error?: unknown
}

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x)
}

/** Mutate a tool's inputSchema to drop simulated_datetime. Returns true if the
 *  tool had simulated_datetime (caller uses this to update the inject cache). */
function stripFromToolSchema(tool: Record<string, unknown>): boolean {
  const schema = tool.inputSchema
  if (!isObject(schema)) return false
  let had = false
  const props = schema.properties
  if (isObject(props) && INJECT_KEY in props) {
    delete props[INJECT_KEY]
    had = true
  }
  const required = schema.required
  if (Array.isArray(required)) {
    const idx = required.indexOf(INJECT_KEY)
    if (idx >= 0) {
      required.splice(idx, 1)
      had = true
    }
  }
  return had
}

/** Parse one chunk of SSE body, extract data: <json> payloads, run mutator on
 *  each parsed JSON, return rewritten SSE text. Lines we don't understand are
 *  preserved as-is. */
function rewriteSseBody(body: string, mutate: (msg: JsonRpcMessage) => void): string {
  const events = body.split(/\n\n/)
  const out: string[] = []
  for (const ev of events) {
    if (!ev) { out.push(ev); continue }
    const lines = ev.split('\n')
    const newLines: string[] = []
    for (const line of lines) {
      if (line.startsWith('data:')) {
        const dataStr = line.slice(5).replace(/^ /, '')
        try {
          const parsed = JSON.parse(dataStr) as JsonRpcMessage
          mutate(parsed)
          newLines.push(`data: ${JSON.stringify(parsed)}`)
        } catch {
          newLines.push(line) // not JSON — leave it
        }
      } else {
        newLines.push(line)
      }
    }
    out.push(newLines.join('\n'))
  }
  return out.join('\n\n')
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = []
  for await (const c of req) chunks.push(c as Buffer)
  return Buffer.concat(chunks).toString('utf8')
}

export async function createSimworldProxy(opts: SimworldProxyOptions): Promise<SimworldProxyHandle> {
  const host = opts.host ?? '127.0.0.1'
  const timeOfDay = opts.timeOfDay ?? '15:00:00'
  // Tools whose upstream schema contains simulated_datetime. Empty until we
  // see a tools/list response. Until then, tools/call requests get injected
  // unconditionally (simworld-data is overwhelmingly time-gated; the 2-3
  // tools that don't accept it would have called tools/list first in normal
  // MCP client flow). After the cache is populated, we only inject for known
  // members.
  const needInjection = new Set<string>()
  let schemaSeen = false

  const server = createServer((req, res) => {
    void handle(req, res).catch(err => {
      const msg = err instanceof Error ? err.message : String(err)
      res.writeHead(502, { 'content-type': 'text/plain' })
      res.end(`simworld-proxy upstream error: ${msg}`)
    })
  })

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ status: 'ok', upstream: opts.upstreamUrl }))
      return
    }
    if (req.method !== 'POST') {
      // streamable-http MCP also defines GET (push) and DELETE (end session).
      // We forward those transparently — no rewriting needed.
      return forward(req, res, null)
    }

    const raw = await readBody(req)
    let parsed: JsonRpcMessage | null = null
    try { parsed = raw ? (JSON.parse(raw) as JsonRpcMessage) : null }
    catch { parsed = null }

    // Request-side rewrite: inject simulated_datetime on tools/call.
    if (parsed && parsed.method === 'tools/call' && isObject(parsed.params)) {
      const name = typeof parsed.params.name === 'string' ? parsed.params.name : ''
      const shouldInject = schemaSeen ? needInjection.has(name) : true
      if (shouldInject) {
        const args = isObject(parsed.params.arguments) ? parsed.params.arguments : {}
        // Force-overwrite even if bot supplied a value — bot must not be able
        // to dictate PIT cursor.
        args[INJECT_KEY] = `${opts.getCurrentDate()} ${timeOfDay}`
        parsed.params.arguments = args
      }
    }

    const outBody = parsed === null ? raw : JSON.stringify(parsed)
    await forward(req, res, outBody)
  }

  async function forward(req: IncomingMessage, res: ServerResponse, body: string | null): Promise<void> {
    const headers: Record<string, string> = {}
    // Forward only headers we know are safe; in particular do NOT forward
    // `host` (would point upstream at us) or `connection`/`keep-alive`
    // (managed by fetch).
    for (const [k, v] of Object.entries(req.headers)) {
      if (v === undefined) continue
      const lk = k.toLowerCase()
      if (['host', 'connection', 'keep-alive', 'content-length', 'transfer-encoding'].includes(lk)) continue
      headers[lk] = Array.isArray(v) ? v.join(', ') : v
    }
    if (!headers['accept']) headers['accept'] = 'application/json, text/event-stream'
    if (body !== null && !headers['content-type']) headers['content-type'] = 'application/json'

    const init: RequestInit = { method: req.method ?? 'GET', headers }
    if (body !== null) init.body = body

    const upstream = await fetch(opts.upstreamUrl, init)
    const respHeaders: Record<string, string> = {}
    upstream.headers.forEach((v, k) => {
      const lk = k.toLowerCase()
      // content-length will be wrong after rewrite, content-encoding similarly;
      // let node:http compute via Transfer-Encoding chunked.
      if (['content-length', 'content-encoding', 'transfer-encoding', 'connection', 'keep-alive'].includes(lk)) return
      respHeaders[lk] = v
    })

    const ct = upstream.headers.get('content-type') ?? ''
    const text = await upstream.text()
    let outText = text

    if (ct.includes('text/event-stream') || ct.includes('application/json')) {
      // Both SSE and pure JSON paths can carry tools/list result.
      const rewrite = (msg: JsonRpcMessage): void => {
        if (!isObject(msg.result)) return
        const tools = (msg.result as Record<string, unknown>).tools
        if (!Array.isArray(tools)) return
        let sawSchema = false
        for (const t of tools) {
          if (!isObject(t)) continue
          const had = stripFromToolSchema(t)
          const name = typeof t.name === 'string' ? t.name : ''
          if (name) {
            sawSchema = true
            if (had) needInjection.add(name); else needInjection.delete(name)
          }
        }
        if (sawSchema) schemaSeen = true
      }
      if (ct.includes('text/event-stream')) {
        outText = rewriteSseBody(text, rewrite)
      } else {
        // pure JSON-RPC response body
        try {
          const parsed = JSON.parse(text) as JsonRpcMessage
          rewrite(parsed)
          outText = JSON.stringify(parsed)
        } catch { /* leave as-is */ }
      }
    }

    res.writeHead(upstream.status, respHeaders)
    res.end(outText)
  }

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(opts.port ?? 0, host, () => { server.off('error', reject); resolve() })
  })
  const port = (server.address() as AddressInfo).port

  // One-shot upstream tools/list probe at startup so daily prompts can list
  // every simworld-data tool. Best-effort: on failure bot still has the
  // discover_tools fallback hint in the prompt.
  const tools = await probeUpstreamTools(opts.upstreamUrl).catch(err => {
    const msg = err instanceof Error ? err.message : String(err)
    process.stderr.write(`simworld-proxy: tools/list probe failed (${msg}); daily prompt will omit tool list\n`)
    return [] as SimworldToolSummary[]
  })

  return {
    port,
    url: `http://${host}:${port}/mcp`,
    tools,
    // server.close() alone waits for ALL active connections to drain — bot 的 MCP
    // streamable-http long session 不主动断 → close() callback 永远不 fire → teardown 卡死。
    // closeAllConnections() 强制 reset 所有 socket，再 close()。3s 硬顶兜底（极端情况）。
    close: () => new Promise<void>((resolve) => {
      const done = () => resolve()
      const timer = setTimeout(done, 3000)
      try { (server as { closeAllConnections?: () => void }).closeAllConnections?.() } catch { /* not available pre-Node 18.2 */ }
      server.close(() => { clearTimeout(timer); done() })
    }),
  }
}

/** MCP handshake against upstream → tools/list → [{name, first-line desc}].
 *  Streamable-HTTP transport: initialize returns an mcp-session-id header that
 *  every subsequent request must carry. Body may be SSE (`event: …\ndata: …`)
 *  or pure JSON; we accept both. */
async function probeUpstreamTools(upstreamUrl: string): Promise<SimworldToolSummary[]> {
  const baseHeaders = { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream' }
  const initResp = await fetch(upstreamUrl, {
    method: 'POST',
    headers: baseHeaders,
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'world-simworld-proxy', version: '0.1' } } }),
  })
  if (!initResp.ok) throw new Error(`initialize HTTP ${initResp.status}`)
  const sid = initResp.headers.get('mcp-session-id')
  await initResp.text() // drain body to free the connection
  if (!sid) throw new Error('upstream did not return mcp-session-id')

  const sessionHeaders = { ...baseHeaders, 'mcp-session-id': sid }
  await fetch(upstreamUrl, { method: 'POST', headers: sessionHeaders, body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }) }).then(r => r.text())

  const listResp = await fetch(upstreamUrl, { method: 'POST', headers: sessionHeaders, body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} }) })
  if (!listResp.ok) throw new Error(`tools/list HTTP ${listResp.status}`)
  const text = await listResp.text()
  const payload = parseToolsListPayload(text)
  const tools = (payload && typeof payload === 'object' && 'result' in payload && isObject((payload as Record<string, unknown>).result))
    ? ((payload as { result: Record<string, unknown> }).result.tools)
    : null
  if (!Array.isArray(tools)) throw new Error('tools/list result missing tools array')

  // Try graceful session teardown so the upstream doesn't leak a dangling
  // session for every run. Failure is silent — server will GC eventually.
  fetch(upstreamUrl, { method: 'DELETE', headers: sessionHeaders }).catch(() => { /* best-effort */ })

  return tools
    .filter((t): t is Record<string, unknown> => isObject(t) && typeof t.name === 'string')
    .map(t => ({
      name: String(t.name),
      description: (typeof t.description === 'string' ? t.description : '').trim().split('\n')[0].trim(),
    }))
}

function parseToolsListPayload(text: string): unknown {
  if (text.startsWith('event:') || text.includes('\ndata:')) {
    for (const ev of text.split(/\n\n/)) {
      const dataLine = ev.split('\n').find(l => l.startsWith('data:'))
      if (!dataLine) continue
      try { return JSON.parse(dataLine.slice(5).trim()) } catch { /* try next event */ }
    }
    return null
  }
  try { return JSON.parse(text) } catch { return null }
}
