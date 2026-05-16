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
  host?: string
  port?: number
}

export interface FundPortfolioProxyHandle {
  port: number
  url: string
  close(): Promise<void>
}

const INJECT_KEY = 'run_id'

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
      res.end(JSON.stringify({ status: 'ok', upstream: opts.upstreamUrl, runId: opts.runId }))
      return
    }
    if (req.method !== 'POST') {
      return forward(req, res, null)
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
        args[INJECT_KEY] = opts.runId
        parsed.params.arguments = args
      }
    }

    const outBody = parsed === null ? raw : JSON.stringify(parsed)
    await forward(req, res, outBody)
  }

  async function forward(req: IncomingMessage, res: ServerResponse, body: string | null): Promise<void> {
    const headers: Record<string, string> = {}
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
      if (['content-length', 'content-encoding', 'transfer-encoding', 'connection', 'keep-alive'].includes(lk)) return
      respHeaders[lk] = v
    })

    const ct = upstream.headers.get('content-type') ?? ''
    const text = await upstream.text()
    let outText = text

    if (ct.includes('text/event-stream') || ct.includes('application/json')) {
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
