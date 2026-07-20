import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'

// fund-portfolio-mcp (streamable-http) wrapper:
// - Strip `run_id` from every tool's inputSchema in tools/list (bot can never set it,
//   never sees it as a knob).
// - On tools/call, force-inject `run_id = <opts.runId>` so every write made by bot
//   is tagged with the run that produced it. Audit-critical for same-day re-runs.
// Mirrors simworld-proxy structure 1:1; only difference is the value is a const
// per proxy instance (one pi-loop = one run_id) instead of a date-changing callback.

export interface FundPortfolioProxyOptions {
  upstreamUrl: string
  /** Constant run_id for the lifetime of this proxy (one pi-loop = one run). */
  runId: string
  /** Current world trade date. When set, buy/sell order trade_date is hidden from bots and forced here. */
  getTradeDate?: () => string
  host?: string
  port?: number
}

export interface FundPortfolioProxyHandle {
  port: number
  url: string
  close(): Promise<void>
}

const RUN_ID_KEY = 'run_id'
const TRADE_DATE_KEY = 'trade_date'
const TRADE_DATE_TOOLS = new Set(['portfolio_place_buy_order', 'portfolio_place_sell_order'])

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

function stripFromToolSchema(tool: Record<string, unknown>, hideTradeDate: boolean): boolean {
  const schema = tool.inputSchema
  if (!isObject(schema)) return false
  let hadRunId = false
  const name = typeof tool.name === 'string' ? tool.name : ''
  const keysToStrip = [RUN_ID_KEY]
  if (hideTradeDate && TRADE_DATE_TOOLS.has(name)) keysToStrip.push(TRADE_DATE_KEY)
  const props = schema.properties
  if (isObject(props)) {
    for (const k of keysToStrip) {
      if (k in props) {
        delete props[k]
        if (k === RUN_ID_KEY) hadRunId = true
      }
    }
  }
  const required = schema.required
  if (Array.isArray(required)) {
    for (const k of keysToStrip) {
      const idx = required.indexOf(k)
      if (idx >= 0) {
        required.splice(idx, 1)
        if (k === RUN_ID_KEY) hadRunId = true
      }
    }
  }
  return hadRunId
}

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
          newLines.push(line)
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

export async function createFundPortfolioProxy(opts: FundPortfolioProxyOptions): Promise<FundPortfolioProxyHandle> {
  const host = opts.host ?? '127.0.0.1'
  if (!opts.runId || typeof opts.runId !== 'string') {
    throw new Error('createFundPortfolioProxy: opts.runId is required (constant string)')
  }
  // Tools whose upstream schema contains run_id. Empty until we see a tools/list
  // response. Before that, tools/call requests get injected unconditionally —
  // fund-portfolio-mcp's writer tools all accept run_id; the read-only tools
  // (get_*) ignore unknown kwargs gracefully (fastmcp/python).
  const needInjection = new Set<string>()
  let schemaSeen = false

  // Session-id ownership: decouple bot's view from upstream's. Bot sees botFacingSessionId
  // for the lifetime of the run; upstreamSessionId is what we put on the wire to upstream and
  // can rotate when upstream restarts. Why we own this layer instead of pass-through: the
  // research-loop MCP client caches the session-id from the first initialize and never
  // reconnects. When systemctl restart lab-fund-* lands mid-run (real incident 2026-05-19
  // for bots 3/11/16), FastMCP forgets all sessions, every subsequent tool call returns
  // HTTP 404 + "Session not found", and the bot is stuck for the rest of the run. This proxy
  // catches the 404, replays the bot's initialize against upstream to get a fresh sid, and
  // retries the original request transparently — bot never sees the rotation.
  let botFacingSessionId: string | undefined
  let upstreamSessionId: string | undefined
  let cachedInitBody: string | undefined
  // Single-flight reinit guard: when several in-flight requests all see 404 after a restart,
  // only one of them runs initialize and the rest await its result.
  let reinitInFlight: Promise<void> | null = null

  function looksLikeSessionNotFound(status: number, body: string): boolean {
    if (status !== 404) return false
    // FastMCP exact: {"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Session not found"}}
    // Be liberal in matching — any 404 mentioning "Session not found" should trigger reinit.
    return body.includes('Session not found')
  }

  async function reinitUpstream(): Promise<void> {
    if (reinitInFlight) { await reinitInFlight; return }
    if (!cachedInitBody) {
      // No initialize ever observed — we can't replay one without knowing the bot's
      // desired protocolVersion / clientInfo. Bail; bot will get the 404.
      return
    }
    reinitInFlight = (async () => {
      try {
        const r = await fetch(opts.upstreamUrl, {
          method: 'POST',
          headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
          body: cachedInitBody,
        })
        if (!r.ok) return  // give up; next attempt will retry
        const newSid = r.headers.get('mcp-session-id')
        // Drain body so the connection can be reused; we don't need to parse it.
        await r.text()
        if (newSid && newSid.length > 0) upstreamSessionId = newSid
      } finally {
        reinitInFlight = null
      }
    })()
    await reinitInFlight
  }

  const server = createServer((req, res) => {
    void handle(req, res).catch(err => {
      const msg = err instanceof Error ? err.message : String(err)
      res.writeHead(502, { 'content-type': 'text/plain' })
      res.end(`fund-portfolio-proxy upstream error: ${msg}`)
    })
  })

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ status: 'ok', upstream: opts.upstreamUrl, runId: opts.runId, tradeDate: opts.getTradeDate?.() ?? null }))
      return
    }
    if (req.method !== 'POST') {
      return forward(req, res, null, null)
    }

    const raw = await readBody(req)
    let parsed: JsonRpcMessage | null = null
    try { parsed = raw ? (JSON.parse(raw) as JsonRpcMessage) : null }
    catch { parsed = null }

    if (parsed && parsed.method === 'tools/call' && isObject(parsed.params)) {
      const name = typeof parsed.params.name === 'string' ? parsed.params.name : ''
      const shouldInject = schemaSeen ? needInjection.has(name) : true
      if (shouldInject) {
        const args = isObject(parsed.params.arguments) ? parsed.params.arguments : {}
        // Force-overwrite even if bot supplied a value — bot must not be able
        // to fake the run_id (which would break audit).
        args[RUN_ID_KEY] = opts.runId
        parsed.params.arguments = args
      }
      if (opts.getTradeDate && TRADE_DATE_TOOLS.has(name)) {
        const args = isObject(parsed.params.arguments) ? parsed.params.arguments : {}
        // Force world-date order placement. Bots cannot backdate to yesterday's NAV
        // to work around missing same-day NAV; upstream accepts awaiting_nav instead.
        args[TRADE_DATE_KEY] = opts.getTradeDate()
        parsed.params.arguments = args
      }
    }

    const outBody = parsed === null ? raw : JSON.stringify(parsed)
    const method = parsed?.method
    if (method === 'initialize') cachedInitBody = outBody
    await forward(req, res, outBody, method ?? null)
  }

  /** One upstream POST + response read. No retry, no body rewriting — just the raw send.
   *  Pulled out so the 404-Session-not-found path can retry without re-walking handle(). */
  async function sendUpstream(req: IncomingMessage, body: string | null, methodHint: string | null): Promise<{ status: number; headers: Headers; text: string }> {
    const headers: Record<string, string> = {}
    for (const [k, v] of Object.entries(req.headers)) {
      if (v === undefined) continue
      const lk = k.toLowerCase()
      if (['host', 'connection', 'keep-alive', 'content-length', 'transfer-encoding'].includes(lk)) continue
      headers[lk] = Array.isArray(v) ? v.join(', ') : v
    }
    if (!headers['accept']) headers['accept'] = 'application/json, text/event-stream'
    if (body !== null && !headers['content-type']) headers['content-type'] = 'application/json'
    // Substitute bot-facing sid with our tracked upstream sid. initialize must NOT carry
    // an old sid — FastMCP would reuse the dead session otherwise (and refuse to mint a new one).
    if (methodHint === 'initialize') {
      delete headers['mcp-session-id']
    } else if (upstreamSessionId) {
      headers['mcp-session-id'] = upstreamSessionId
    }
    const init: RequestInit = { method: req.method ?? 'GET', headers }
    if (body !== null) init.body = body
    const upstream = await fetch(opts.upstreamUrl, init)
    const text = await upstream.text()
    return { status: upstream.status, headers: upstream.headers, text }
  }

  async function forward(req: IncomingMessage, res: ServerResponse, body: string | null, methodHint: string | null): Promise<void> {
    let upstream = await sendUpstream(req, body, methodHint)

    // Transparent reconnect: upstream restart kills sid → 404 + "Session not found".
    // Reinit and retry exactly once. If retry also fails, surface the latest response.
    if (looksLikeSessionNotFound(upstream.status, upstream.text) && methodHint !== 'initialize' && cachedInitBody) {
      await reinitUpstream()
      if (upstreamSessionId) upstream = await sendUpstream(req, body, methodHint)
    }

    const respHeaders: Record<string, string> = {}
    upstream.headers.forEach((v, k) => {
      const lk = k.toLowerCase()
      if (['content-length', 'content-encoding', 'transfer-encoding', 'connection', 'keep-alive'].includes(lk)) return
      respHeaders[lk] = v
    })

    // initialize response: capture upstream sid (rotates on reconnect) and pin botFacingSessionId
    // on the FIRST initialize so the bot's MCP client never sees its sid change. For
    // subsequent initialize calls (e.g. re-init triggered by us, though those go through the
    // separate reinitUpstream path), we still don't expose the new sid to the bot.
    const upstreamSidHeader = upstream.headers.get('mcp-session-id')
    if (methodHint === 'initialize' && upstreamSidHeader) {
      upstreamSessionId = upstreamSidHeader
      if (!botFacingSessionId) botFacingSessionId = upstreamSidHeader
    }
    // Always rewrite the response's mcp-session-id back to botFacingSessionId so the bot's
    // stored sid stays valid for its full session.
    if (botFacingSessionId && respHeaders['mcp-session-id']) {
      respHeaders['mcp-session-id'] = botFacingSessionId
    }

    const ct = upstream.headers.get('content-type') ?? ''
    let outText = upstream.text

    if (ct.includes('text/event-stream') || ct.includes('application/json')) {
      const rewrite = (msg: JsonRpcMessage): void => {
        if (!isObject(msg.result)) return
        const tools = (msg.result as Record<string, unknown>).tools
        if (!Array.isArray(tools)) return
        let sawSchema = false
        for (const t of tools) {
          if (!isObject(t)) continue
          const had = stripFromToolSchema(t, Boolean(opts.getTradeDate))
          const name = typeof t.name === 'string' ? t.name : ''
          if (name) {
            sawSchema = true
            if (had) needInjection.add(name); else needInjection.delete(name)
          }
        }
        if (sawSchema) schemaSeen = true
      }
      if (ct.includes('text/event-stream')) {
        outText = rewriteSseBody(upstream.text, rewrite)
      } else {
        try {
          const parsed = JSON.parse(upstream.text) as JsonRpcMessage
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
  return {
    port,
    url: `http://${host}:${port}/mcp`,
    close: () => new Promise<void>((resolve) => {
      const done = () => resolve()
      const timer = setTimeout(done, 3000)
      try { (server as { closeAllConnections?: () => void }).closeAllConnections?.() } catch { /* not available pre-Node 18.2 */ }
      server.close(() => { clearTimeout(timer); done() })
    }),
  }
}
