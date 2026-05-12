import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { createInterface } from 'node:readline'
import { JsonRpcStdioClient } from './jsonrpc.ts'

export interface BotChatParams {
  message: string
  session_key?: string
  history?: unknown[]
  channel?: string
  metadata?: Record<string, unknown>
}

export interface BotChatResult {
  reply: string
  session_id: string
  assistant_messages: unknown[]
  tool_trace: unknown[]
  usage: number
  iterations: number
  truncated_by_iterations: boolean
}

export interface BotServerOptions {
  argv: string[]               // argv[0] = 可执行文件（通常 process.execPath），其余是参数
  cwd?: string
  env?: Record<string, string>
  readyTimeoutMs?: number      // 等 server.ready 的超时，默认 30000
  onLog?: (line: string) => void   // 接收子进程 stderr 行
  onNotification?: (method: string, params: Record<string, unknown>) => void
}

export class BotServer {
  readonly botId: string
  private readonly child: ChildProcessWithoutNullStreams
  private readonly client: JsonRpcStdioClient
  private _alive = true
  private exitHandlers: ((code: number | null) => void)[] = []

  private constructor(botId: string, child: ChildProcessWithoutNullStreams, client: JsonRpcStdioClient, onLog?: (l: string) => void) {
    this.botId = botId
    this.child = child
    this.client = client
    const errRl = createInterface({ input: child.stderr })
    errRl.on('line', (l) => onLog?.(`[${botId}] ${l}`))
    // stdout close fires before the child 'exit' event; mark alive=false early so
    // that callers see it flipped when any pending request rejects due to stream close.
    child.stdout.on('close', () => { this._alive = false })
    child.on('exit', (code) => { this._alive = false; for (const h of this.exitHandlers) h(code) })
  }

  static async start(botId: string, opts: BotServerOptions): Promise<BotServer> {
    const [cmd, ...rest] = opts.argv
    const child = spawn(cmd, rest, { stdio: ['pipe', 'pipe', 'pipe'], cwd: opts.cwd, env: { ...process.env, ...opts.env } }) as ChildProcessWithoutNullStreams
    const client = new JsonRpcStdioClient(child.stdin, child.stdout)
    const bs = new BotServer(botId, child, client, opts.onLog)
    if (opts.onNotification) client.onNotification(n => opts.onNotification!(n.method, n.params))
    try {
      await client.waitFor(n => n.method === 'server.ready', opts.readyTimeoutMs ?? 30_000)
    } catch (err) {
      try { child.kill('SIGKILL') } catch { /* ignore */ }
      throw new Error(`bot ${botId}: server did not become ready: ${err instanceof Error ? err.message : String(err)}`)
    }
    return bs
  }

  get alive(): boolean { return this._alive }

  onExit(handler: (code: number | null) => void): void { this.exitHandlers.push(handler) }

  async ping(): Promise<{ pong: boolean; bot_id: string; workspace: string; model: string }> {
    return this.client.request('ping', {})
  }

  async chat(params: BotChatParams, opts?: { timeoutMs?: number }): Promise<BotChatResult> {
    if (!this._alive) throw new Error(`bot ${this.botId}: server process is not alive`)
    return this.client.request<BotChatResult>('chat', { ...params }, { timeoutMs: opts?.timeoutMs })
  }

  async shutdown(opts?: { timeoutMs?: number }): Promise<void> {
    if (!this._alive) return
    const timeoutMs = opts?.timeoutMs ?? 5000
    const exited = new Promise<void>((resolve) => { if (!this._alive) return resolve(); this.child.once('exit', () => resolve()) })
    try { await this.client.request('shutdown', {}, { timeoutMs: Math.min(timeoutMs, 2000) }) } catch { /* ignore */ }
    const killTimer = setTimeout(() => { try { this.child.kill('SIGKILL') } catch { /* ignore */ } }, timeoutMs)
    await exited
    clearTimeout(killTimer)
  }
}
