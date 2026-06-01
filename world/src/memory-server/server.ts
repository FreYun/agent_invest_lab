import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import type { MemoryStore } from './store.ts'

export interface MemoryServerHandle {
  port: number
  url: string
  close(): Promise<void>
}

export interface CreateMemoryServerOpts {
  store: MemoryStore
  getCurrentDate: () => string
  port?: number
  host?: string
}

function send(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json' })
  res.end(text)
}

async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = []
  for await (const c of req) chunks.push(c as Buffer)
  const text = Buffer.concat(chunks).toString('utf8')
  if (!text.trim()) return {}
  return JSON.parse(text)
}

export async function createMemoryServer(opts: CreateMemoryServerOpts): Promise<MemoryServerHandle> {
  const host = opts.host ?? '127.0.0.1'
  const server = createServer((req, res) => {
    void (async () => {
      try {
        const url = req.url ?? '/'
        if (req.method === 'GET' && url === '/health') return send(res, 200, { status: 'ok' })

        if (req.method === 'POST' && url === '/memories') {
          const body = (await readJsonBody(req)) as Record<string, unknown>
          const messages = Array.isArray(body.messages) ? body.messages : []
          const text = messages.map(m => (m && typeof m === 'object' ? String((m as Record<string, unknown>).content ?? '') : '')).filter(Boolean).join('\n')
          const user_id = typeof body.user_id === 'string' ? body.user_id : null
          const agent_id = typeof body.agent_id === 'string' ? body.agent_id : null
          if (!user_id && !agent_id) return send(res, 400, { detail: 'at least one of user_id / agent_id required' })
          if (!text) return send(res, 400, { detail: 'messages[].content is empty' })
          const rec = opts.store.add({ text, user_id, agent_id, created_at: opts.getCurrentDate(), metadata: body.metadata && typeof body.metadata === 'object' ? (body.metadata as Record<string, unknown>) : undefined })
          return send(res, 200, { results: [{ id: rec.id }] })
        }

        if (req.method === 'POST' && url === '/search') {
          const body = (await readJsonBody(req)) as Record<string, unknown>
          const query = typeof body.query === 'string' ? body.query : ''
          if (!query) return send(res, 400, { detail: 'query required' })
          const agent_id = typeof body.agent_id === 'string' ? body.agent_id : undefined
          const limit = typeof body.limit === 'number' ? body.limit : 5
          const start_date = typeof body.start_date === 'string' ? body.start_date : undefined
          const end_date = typeof body.end_date === 'string' ? body.end_date : undefined
          const recency_tau_days = typeof body.recency_tau_days === 'number' ? body.recency_tau_days : undefined
          const hits = opts.store.search(query, {
            agent_id,
            limit,
            start_date,
            end_date,
            now: opts.getCurrentDate(),
            recency_tau_days,
          })
          return send(res, 200, { results: hits })
        }

        if (url === '/memories' || url === '/search') return send(res, 405, { detail: 'method not allowed' })
        return send(res, 404, { detail: 'not found' })
      } catch (err) {
        return send(res, 400, { detail: err instanceof Error ? err.message : String(err) })
      }
    })()
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(opts.port ?? 0, host, () => { server.off('error', reject); resolve() })
  })
  const port = (server.address() as AddressInfo).port
  return {
    port,
    url: `http://${host}:${port}`,
    close: () => new Promise<void>((resolve, reject) => server.close(err => (err ? reject(err) : resolve()))),
  }
}
