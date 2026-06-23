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
  /** 调用方标识，作为 X-Client-Id 注入到 upstream 请求头，供 simworld-mcp 调用日志区分
   *  是哪个 run 在打数据（回环短连接无法事后反查 pid，故由此自报）。如 `run-<runId>`。 */
  clientId?: string
}

export interface SimworldProxyHandle {
  port: number
  url: string
  /** Upstream-advertised tools (name + one-line description). Filled by the
   *  startup probe; on first-attempt failure it may populate asynchronously as
   *  background retries succeed. Stays empty if every attempt fails — daily
   *  prompt then renders without the tool list block. Same array reference
   *  throughout, so late reads see the latest probe result. */
  tools: SimworldToolSummary[]
  close(): Promise<void>
}

export interface SimworldToolSummary {
  name: string
  description: string
}

const INJECT_KEY = 'simulated_datetime'

// Abort an upstream round-trip if it hangs past this, so the serialization gate
// (see createSimworldProxy) never stalls the queue behind one stuck call. Set
// just under the bot MCP client's ~8s per-call timeout.
const UPSTREAM_TIMEOUT_MS = 7000

// 对 bot 隐藏的上游工具：申赎原始数据接口。底层 18078 工具保留（research、以及 market_sentiment
// 因子的计算仍直接读，不走本 proxy），但经 proxy 给 bot 的视图里：tools/list 整条剔除、tools/call
// 拒绝，让 agent 既看不到也调不动——申赎数据源对 agent 彻底隐藏，agent 只用加工好的 market_sentiment
// 因子（见 quant_factor）。
export const HIDDEN_TOOLS = new Set<string>([
  'fund_subscription_redemption_summary',
  'fund_index_subscription_redemption',
])

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

/** Filter hidden tools + strip simulated_datetime from each tool schema, updating
 *  the inject-cache. Mutates tool objects in place; returns the bot-visible array
 *  (used both for live tools/list rewrite and the local tools/list cache). */
function processToolsArray(tools: unknown[], needInjection: Set<string>): Record<string, unknown>[] {
  const visible = tools.filter((t): t is Record<string, unknown> =>
    isObject(t) && typeof t.name === 'string' && !HIDDEN_TOOLS.has(t.name))
  for (const t of visible) {
    const had = stripFromToolSchema(t)
    const name = String(t.name)
    if (had) needInjection.add(name); else needInjection.delete(name)
  }
  return visible
}

function sendJsonRpc(res: ServerResponse, obj: JsonRpcMessage): void {
  res.writeHead(200, { 'content-type': 'application/json' })
  res.end(JSON.stringify(obj))
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
  // Serialize upstream round-trips: 1-at-a-time gate so the single-worker upstream
  // never sees concurrent tools/call (concurrency is what trips the ~8s timeout).
  let upstreamGate: Promise<unknown> = Promise.resolve()

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

    // Answer the MCP handshake (initialize / tools/list / ping / initialized) LOCALLY
    // when the upstream is stateless. The bot's research-loop re-activates the
    // simworld-data MCP per turn (deferred MCP); forwarding that handshake to the
    // single-worker upstream means it queues behind whatever blocking `requests`
    // calls are in flight and trips the client's ~8s timeout → the whole MCP is
    // reported "initialization failed" and EVERY simworld tool that turn dies.
    // Serving the handshake from the proxy decouples it from upstream load; only
    // real tools/call still forwards. Stateful upstreams keep 1:1 forwarding (they
    // need a real upstream session), so this is gated on the probed stateless flag.
    if (upstreamStateless && parsed && typeof parsed.method === 'string') {
      const m = parsed.method
      if (m === 'initialize') {
        const pv = isObject(parsed.params) && typeof parsed.params.protocolVersion === 'string'
          ? parsed.params.protocolVersion : '2024-11-05'
        sendJsonRpc(res, { jsonrpc: '2.0', id: parsed.id ?? null, result: { protocolVersion: pv, capabilities: { tools: {} }, serverInfo: { name: 'simworld-data-proxy', version: '1.0.0' } } })
        return
      }
      if (m === 'ping') { sendJsonRpc(res, { jsonrpc: '2.0', id: parsed.id ?? null, result: {} }); return }
      if (m === 'notifications/initialized') { res.writeHead(202); res.end(); return }
      if (m === 'tools/list' && cachedTools) {
        sendJsonRpc(res, { jsonrpc: '2.0', id: parsed.id ?? null, result: { tools: cachedTools } })
        return
      }
    }

    // Request-side rewrite: inject simulated_datetime on tools/call.
    if (parsed && parsed.method === 'tools/call' && isObject(parsed.params)) {
      const name = typeof parsed.params.name === 'string' ? parsed.params.name : ''
      // 对 bot 隐藏的工具（申赎原始接口）：直接拒绝、不转发上游。报成"未知工具"而非"被禁"，
      // 不暴露这里藏了东西。底层 18078 工具仍在（research 直接调用不走本 proxy）。
      if (HIDDEN_TOOLS.has(name)) {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ jsonrpc: '2.0', id: parsed.id ?? null, error: { code: -32601, message: `Unknown tool: ${name}` } }))
        return
      }
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
    // 自报调用方身份（bot 无法覆写：bot 侧请求头里的 x-client-id 已在上面的安全转发
    // 中被原样带过，这里强制以 proxy 配置值覆盖，确保日志里看到的是真实 run）。
    if (opts.clientId) headers['x-client-id'] = opts.clientId

    const init: RequestInit = { method: req.method ?? 'GET', headers, signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS) }
    if (body !== null) init.body = body

    // Funnel the upstream round-trip through the 1-at-a-time gate. The upstream is
    // a single uvicorn worker doing blocking `requests` to ttjj; concurrent
    // tools/call from one bot turn thrash it so every call exceeds the client's
    // ~8s timeout (surfaced to the bot as "initialization failed"). Serializing
    // keeps each call fast (<2s uncontended). Sequential callers never wait — the
    // gate is already resolved. We hold the gate across fetch + body read (the
    // upstream connection is busy until the body drains).
    const run = upstreamGate.then(async () => {
      const resp = await fetch(opts.upstreamUrl, init)
      const text = await resp.text()
      return { resp, text }
    })
    upstreamGate = run.then(() => undefined, () => undefined)
    const { resp: upstream, text } = await run
    const respHeaders: Record<string, string> = {}
    upstream.headers.forEach((v, k) => {
      const lk = k.toLowerCase()
      // content-length will be wrong after rewrite, content-encoding similarly;
      // let node:http compute via Transfer-Encoding chunked.
      if (['content-length', 'content-encoding', 'transfer-encoding', 'connection', 'keep-alive'].includes(lk)) return
      respHeaders[lk] = v
    })

    const ct = upstream.headers.get('content-type') ?? ''
    let outText = text

    if (ct.includes('text/event-stream') || ct.includes('application/json')) {
      // Both SSE and pure JSON paths can carry tools/list result.
      const rewrite = (msg: JsonRpcMessage): void => {
        if (!isObject(msg.result)) return
        const result = msg.result as Record<string, unknown>
        if (!Array.isArray(result.tools)) return
        const processed = processToolsArray(result.tools, needInjection)
        result.tools = processed
        schemaSeen = true
        cachedTools = processed   // keep the local tools/list cache fresh
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

  // Upstream tools/list probe so daily prompts can list every simworld-data
  // tool. The FIRST attempt is awaited — a healthy upstream answers instantly,
  // so the prompt has the full list with zero added latency. On failure we do
  // NOT block run setup: when several runs boot at once the upstream can
  // transiently drop the probe ("fetch failed"), and the backoff window
  // (1+2+3s) would otherwise stall bot dispatch / status.json by that long.
  // Instead the retries run in the background and fill `tools` in place, so a
  // later day's prompt picks them up once the transient failure clears.
  // Best-effort: if every attempt fails the bot still has the discover_tools hint.
  const tools: SimworldToolSummary[] = []
  // Processed (hidden-filtered, simulated_datetime-stripped) tools array, served
  // for local tools/list. Filled by the probe / first live tools/list.
  let cachedTools: unknown[] | null = null
  // Whether the upstream advertised NO mcp-session-id at probe → stateless_http.
  // Only then do we answer the handshake locally (see handle()).
  let upstreamStateless = false
  let probeAborted = false
  const probeOnce = async (): Promise<void> => {
    const { summaries, rawTools, stateless } = await probeUpstreamTools(opts.upstreamUrl)
    if (summaries.length === 0) throw new Error('tools/list returned empty')
    tools.splice(0, tools.length, ...summaries)
    cachedTools = processToolsArray(rawTools, needInjection)
    schemaSeen = true
    upstreamStateless = stateless
  }
  try {
    await probeOnce()
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    process.stderr.write(`simworld-proxy: tools/list probe attempt 1 failed (${msg}); retrying in background\n`)
    void (async () => {
      for (let attempt = 2; attempt <= 4 && !probeAborted; attempt++) {
        // unref so a pending retry never keeps the process (or a test runner) alive.
        await new Promise<void>(r => { const t = setTimeout(r, (attempt - 1) * 1000); t.unref?.() })
        if (probeAborted) return
        try { await probeOnce(); return } catch (e) {
          const m = e instanceof Error ? e.message : String(e)
          if (attempt === 4) process.stderr.write(`simworld-proxy: tools/list probe failed after ${attempt} attempts (${m}); daily prompt will omit tool list\n`)
          else process.stderr.write(`simworld-proxy: tools/list probe attempt ${attempt} failed (${m}); retrying\n`)
        }
      }
    })()
  }

  return {
    port,
    url: `http://${host}:${port}/mcp`,
    tools,
    // server.close() alone waits for ALL active connections to drain — bot 的 MCP
    // streamable-http long session 不主动断 → close() callback 永远不 fire → teardown 卡死。
    // closeAllConnections() 强制 reset 所有 socket，再 close()。3s 硬顶兜底（极端情况）。
    close: () => new Promise<void>((resolve) => {
      probeAborted = true
      const done = () => resolve()
      const timer = setTimeout(done, 3000)
      try { (server as { closeAllConnections?: () => void }).closeAllConnections?.() } catch { /* not available pre-Node 18.2 */ }
      server.close(() => { clearTimeout(timer); done() })
    }),
  }
}

/** MCP handshake against upstream → tools/list → [{name, first-line desc}].
 *  Streamable-HTTP transport: initialize MAY return an mcp-session-id header
 *  (stateful mode) or MAY NOT (FastMCP stateless_http mode — current upstream).
 *  We accept either: if we get a sid we carry it on subsequent calls and DELETE
 *  it at the end; without one, we just send tools/list directly. Without this
 *  fallback, a stateless upstream silently makes the probe return [] and the
 *  daily prompt loses its tool catalog → bot hallucinates tool names by analogy
 *  (e.g. fund_index_valuation from fund_index_return). Body may be SSE
 *  (`event: …\ndata: …`) or pure JSON; we accept both. */
async function probeUpstreamTools(upstreamUrl: string): Promise<{ summaries: SimworldToolSummary[]; rawTools: Record<string, unknown>[]; stateless: boolean }> {
  const baseHeaders = { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream' }
  const initResp = await fetch(upstreamUrl, {
    method: 'POST',
    headers: baseHeaders,
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'world-simworld-proxy', version: '0.1' } } }),
  })
  if (!initResp.ok) throw new Error(`initialize HTTP ${initResp.status}`)
  const sid = initResp.headers.get('mcp-session-id')
  await initResp.text() // drain body to free the connection

  const sessionHeaders: Record<string, string> = { ...baseHeaders }
  if (sid) {
    sessionHeaders['mcp-session-id'] = sid
    // Only stateful upstreams need the post-initialize notification.
    await fetch(upstreamUrl, { method: 'POST', headers: sessionHeaders, body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }) }).then(r => r.text())
  }

  const listResp = await fetch(upstreamUrl, { method: 'POST', headers: sessionHeaders, body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} }) })
  if (!listResp.ok) throw new Error(`tools/list HTTP ${listResp.status}`)
  const text = await listResp.text()
  const payload = parseToolsListPayload(text)
  const tools = (payload && typeof payload === 'object' && 'result' in payload && isObject((payload as Record<string, unknown>).result))
    ? ((payload as { result: Record<string, unknown> }).result.tools)
    : null
  if (!Array.isArray(tools)) throw new Error('tools/list result missing tools array')

  // Stateful upstream: try graceful session teardown so it doesn't leak a
  // dangling session per run. Stateless upstream: no session to delete.
  // Failure is silent — server will GC eventually.
  if (sid) {
    fetch(upstreamUrl, { method: 'DELETE', headers: sessionHeaders }).catch(() => { /* best-effort */ })
  }

  const named = tools.filter((t): t is Record<string, unknown> => isObject(t) && typeof t.name === 'string')
  const summaries = named
    .filter(t => !HIDDEN_TOOLS.has(String(t.name)))   // 隐藏申赎原始接口，不进 bot 的工具目录
    .map(t => ({
      name: String(t.name),
      description: (typeof t.description === 'string' ? t.description : '').trim().split('\n')[0].trim(),
    }))
  return { summaries, rawTools: named, stateless: sid === null }
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
