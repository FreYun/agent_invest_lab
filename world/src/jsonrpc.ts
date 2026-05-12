import { createInterface } from 'node:readline'
import type { Readable, Writable } from 'node:stream'

export interface JsonRpcNotification {
  method: string
  params: Record<string, unknown>
}

interface Pending {
  resolve: (v: unknown) => void
  reject: (e: Error) => void
  timer: NodeJS.Timeout | null
}

export class JsonRpcStdioClient {
  private readonly stdin: Writable
  private nextId = 1
  private readonly pending = new Map<number, Pending>()
  private notificationHandler: ((n: JsonRpcNotification) => void) | null = null
  private readonly queuedNotifications: JsonRpcNotification[] = []
  private readonly waiters: { predicate: (n: JsonRpcNotification) => boolean; resolve: (n: JsonRpcNotification) => void; reject: (e: Error) => void; timer: NodeJS.Timeout }[] = []
  private closed = false

  constructor(stdin: Writable, stdout: Readable) {
    this.stdin = stdin
    const rl = createInterface({ input: stdout })
    rl.on('line', (line) => this.handleLine(line))
    stdout.on('close', () => this.failAll(new Error('stdout closed')))
  }

  private handleLine(line: string): void {
    const t = line.trim()
    if (!t) return
    let msg: { id?: unknown; result?: unknown; error?: { code?: number; message?: string }; method?: string; params?: Record<string, unknown> }
    try { msg = JSON.parse(t) } catch { return }
    if (typeof msg.id === 'number' && (('result' in msg) || ('error' in msg))) {
      const p = this.pending.get(msg.id)
      if (!p) return // 晚到的、已超时的回复 → 丢弃
      this.pending.delete(msg.id)
      if (p.timer) clearTimeout(p.timer)
      if (msg.error) p.reject(new Error(`JSON-RPC error ${msg.error.code ?? ''}: ${msg.error.message ?? 'unknown'}`))
      else p.resolve(msg.result)
      return
    }
    if (typeof msg.method === 'string' && msg.id === undefined) {
      const n: JsonRpcNotification = { method: msg.method, params: msg.params ?? {} }
      // 满足某个 waiter？
      for (let i = this.waiters.length - 1; i >= 0; i--) {
        if (this.waiters[i].predicate(n)) {
          const w = this.waiters.splice(i, 1)[0]
          clearTimeout(w.timer)
          w.resolve(n)
          return
        }
      }
      if (this.notificationHandler) this.notificationHandler(n)
      else this.queuedNotifications.push(n)
    }
  }

  onNotification(handler: (n: JsonRpcNotification) => void): void {
    this.notificationHandler = handler
    while (this.queuedNotifications.length) handler(this.queuedNotifications.shift()!)
  }

  waitFor(predicate: (n: JsonRpcNotification) => boolean, timeoutMs: number): Promise<JsonRpcNotification> {
    // 先看已经排队的
    for (let i = 0; i < this.queuedNotifications.length; i++) {
      if (predicate(this.queuedNotifications[i])) return Promise.resolve(this.queuedNotifications.splice(i, 1)[0])
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const idx = this.waiters.findIndex(w => w.timer === timer)
        if (idx >= 0) this.waiters.splice(idx, 1)
        reject(new Error(`timeout waiting for notification (${timeoutMs}ms)`))
      }, timeoutMs)
      this.waiters.push({ predicate, resolve, reject, timer })
    })
  }

  request<T = unknown>(method: string, params: Record<string, unknown>, opts?: { timeoutMs?: number }): Promise<T> {
    if (this.closed) return Promise.reject(new Error('client closed'))
    const id = this.nextId++
    return new Promise<T>((resolve, reject) => {
      const timer = opts?.timeoutMs
        ? setTimeout(() => { this.pending.delete(id); reject(new Error(`request timeout: ${method} (${opts.timeoutMs}ms)`)) }, opts.timeoutMs)
        : null
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer })
      this.stdin.write(JSON.stringify({ id, method, params }) + '\n')
    })
  }

  private failAll(err: Error): void {
    this.closed = true
    for (const [, p] of this.pending) { if (p.timer) clearTimeout(p.timer); p.reject(err) }
    this.pending.clear()
    for (const w of this.waiters) { clearTimeout(w.timer); w.reject(err) }
    this.waiters.length = 0
  }
}
