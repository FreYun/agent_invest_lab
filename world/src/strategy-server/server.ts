import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { existsSync, readFileSync, writeFileSync, appendFileSync, mkdirSync } from 'node:fs'
import type { AddressInfo } from 'node:net'
import { strategiesDir, strategyFile, strategyRevisionsFile } from '../paths.ts'

// 进程内 MCP 服务，承载两个工具让 bot 自己管理"投资策略文档"：
//   - update_my_strategy(bot_id, strategy, reason): 完整替换当前策略文档，追加修订审计
//   - get_my_strategy(bot_id):                     读出当前策略文档
//
// 策略文件位于 <runDir>/strategies/<botId>.md（与 extractStrategies 写入的位置一致）。
// 修订日志 <runDir>/strategies/<botId>.revisions.jsonl —— 每次 update 追加一行
// {ts, reason, new_size, prior_size}，用于事后审计 bot 何时为何改了策略。
//
// 信任模型：bot_id 由调用方在参数中传入（无认证），同 fund-portfolio-mcp 的"按参数声明身份"做法。
// 多 bot 共用同一进程的此服务也没问题——目录是按 bot_id 分文件的，写入互不污染。
//
// 协议：MCP streamable-http 的最小子集（POST JSON-RPC + 单次 JSON 响应，不用 SSE）。
// 支持的 method：initialize / tools/list / tools/call / ping / resources/list / prompts/list；
// notifications/* 接受后直接 202。其它 method 返回标准 method-not-found 错误。

export interface StrategyServerOptions {
  worldRoot: string
  runId: string
  /** 写修订审计时的"世界日期"——用世界时间而不是 wall clock，方便审计日志和回放日对齐。 */
  getCurrentDate: () => string
  host?: string
  port?: number
}

export interface StrategyServerHandle {
  port: number
  url: string
  close(): Promise<void>
}

interface JsonRpcRequest {
  jsonrpc?: string
  id?: number | string | null
  method?: string
  params?: Record<string, unknown>
}

interface JsonRpcResponse {
  jsonrpc: '2.0'
  id: number | string | null
  result?: unknown
  error?: { code: number; message: string }
}

const PROTOCOL_VERSION = '2024-11-05'
const STRATEGY_MEM0_PREFIX = '# MY_STRATEGY'

// Tool descriptions 里要带几个被 discover_tools 常用 query 命中的关键词
// （strategy / revise / investment / portfolio），不然 bot 用宽泛 query
// 时会捞不到。每个 tool 单独一段说明，写法、风格、长度的强约束都不在这里——
// 那部分由 Day 1 prompt 给。
const TOOLS = [
  {
    name: 'update_my_strategy',
    description:
      '更新（完整替换）你当前的投资策略文档（investment strategy revise update revision）。' +
      '传入的 strategy 必须是完整的 markdown（不是 diff），会覆盖旧版本；下一交易日的 prompt ' +
      '自动注入这一新版本。reason 一句话说清楚为什么调整——会写进审计日志（revisions.jsonl）' +
      '供事后回看。每次调用都视作一次正式 portfolio strategy revision。',
    inputSchema: {
      type: 'object',
      properties: {
        bot_id: {
          type: 'string',
          description: '你的 bot id（比如 bot7）。world 用它确定写哪个 strategy 文件。',
        },
        strategy: {
          type: 'string',
          description:
            '完整的新策略 markdown 文本。必须以 `# MY_STRATEGY` 作为第一行（与 Day 1 写法一致）。' +
            '会完整覆盖当前策略，所以一定带上所有你想保留的内容。',
        },
        reason: {
          type: 'string',
          description:
            '一句话说明这次调整的原因——观察到了什么、上一版哪里失效、新版本要解决什么。' +
            '只用于审计日志，不会被注入 prompt。',
        },
      },
      required: ['bot_id', 'strategy', 'reason'],
    },
  },
  {
    name: 'get_my_strategy',
    description:
      '读取你当前的投资策略文档（与每日 prompt 注入的内容一致，investment strategy review）。' +
      '日常不必显式调用——策略每天会被自动注入到 prompt——但如果你想中途重新审视、' +
      '或者想确认刚刚 update_my_strategy 的写入是否生效，可以调一次。',
    inputSchema: {
      type: 'object',
      properties: {
        bot_id: { type: 'string', description: '你的 bot id（比如 bot7）。' },
      },
      required: ['bot_id'],
    },
  },
] as const

interface ToolResult { content: Array<{ type: 'text'; text: string }>; isError?: boolean }
function ok(text: string): ToolResult { return { content: [{ type: 'text', text }] } }
function err(text: string): ToolResult { return { content: [{ type: 'text', text }], isError: true } }

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = []
  for await (const c of req) chunks.push(c as Buffer)
  return Buffer.concat(chunks).toString('utf8')
}

function rpcError(id: number | string | null | undefined, code: number, message: string): JsonRpcResponse {
  return { jsonrpc: '2.0', id: id ?? null, error: { code, message } }
}

function rpcResult(id: number | string | null | undefined, result: unknown): JsonRpcResponse {
  return { jsonrpc: '2.0', id: id ?? null, result }
}

export async function createStrategyServer(opts: StrategyServerOptions): Promise<StrategyServerHandle> {
  const host = opts.host ?? '127.0.0.1'
  const { worldRoot, runId, getCurrentDate } = opts
  // 提前建好目录（一次性，工具调用就不需要再 mkdir）。
  mkdirSync(strategiesDir(worldRoot, runId), { recursive: true })

  function handleToolCall(name: string, args: Record<string, unknown>): ToolResult {
    const botId = typeof args.bot_id === 'string' ? args.bot_id.trim() : ''
    if (!botId) return err('bot_id 必填且非空')
    // 仅允许 alphanumeric/_-，防止路径穿越（../../../...）。
    if (!/^[A-Za-z0-9_-]+$/.test(botId)) return err(`bot_id "${botId}" 含非法字符：只允许字母/数字/_/-`)

    if (name === 'update_my_strategy') {
      const strategy = typeof args.strategy === 'string' ? args.strategy : ''
      const reason = typeof args.reason === 'string' ? args.reason.trim() : ''
      if (!strategy.trim()) return err('strategy 必填（完整 markdown 文本，不是 diff）')
      if (!strategy.trimStart().startsWith(STRATEGY_MEM0_PREFIX)) {
        return err(`strategy 必须以 \`${STRATEGY_MEM0_PREFIX}\` 作为第一行——和 Day 1 写法一致，便于审计`)
      }
      if (!reason) return err('reason 必填——一句话说明为什么改这一版（用于审计日志）')

      const stratPath = strategyFile(worldRoot, runId, botId)
      let priorSize: number | null = null
      try { priorSize = readFileSync(stratPath, 'utf8').length } catch { /* first write */ }

      const content = strategy.endsWith('\n') ? strategy : strategy + '\n'
      writeFileSync(stratPath, content)

      const revEntry = { ts: getCurrentDate(), reason, new_size: strategy.length, prior_size: priorSize }
      appendFileSync(strategyRevisionsFile(worldRoot, runId, botId), JSON.stringify(revEntry) + '\n')

      return ok(
        `策略已更新（新版 ${strategy.length} chars`
        + (priorSize === null ? '；首次写入' : `；上一版 ${priorSize} chars`)
        + `）。理由已写入审计日志：${reason}\n下一交易日的 prompt 会注入这一新版本。`
      )
    }

    if (name === 'get_my_strategy') {
      const stratPath = strategyFile(worldRoot, runId, botId)
      if (!existsSync(stratPath)) {
        return err(`找不到 ${botId} 的策略文件——Day 1 应该用 mem0_add 写过一份 \`${STRATEGY_MEM0_PREFIX}\` 起头的策略。如果你现在是 Day 1 且还没写，请先用 mem0_add 写一份。`)
      }
      try { return ok(readFileSync(stratPath, 'utf8')) }
      catch (e) { return err(`读取策略失败：${e instanceof Error ? e.message : String(e)}`) }
    }

    return err(`unknown tool: ${name}`)
  }

  function handleRpc(msg: JsonRpcRequest): JsonRpcResponse | null {
    const id = msg.id
    const method = msg.method ?? ''
    if (method === 'initialize') {
      return rpcResult(id, {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: { tools: {} },
        serverInfo: { name: 'strategy-server', version: '0.1.0' },
      })
    }
    if (method === 'ping') return rpcResult(id, {})
    if (method === 'tools/list') return rpcResult(id, { tools: TOOLS })
    if (method === 'tools/call') {
      const params = msg.params ?? {}
      const toolName = typeof params.name === 'string' ? params.name : ''
      const argsRaw = params.arguments
      const args = typeof argsRaw === 'object' && argsRaw !== null && !Array.isArray(argsRaw) ? argsRaw as Record<string, unknown> : {}
      try { return rpcResult(id, handleToolCall(toolName, args)) }
      catch (e) { return rpcError(id, -32603, `tool execution error: ${e instanceof Error ? e.message : String(e)}`) }
    }
    // We have nothing to offer in resources/prompts, but answering with empty lists
    // beats method-not-found—some clients call these on init and fail loudly otherwise.
    if (method === 'resources/list') return rpcResult(id, { resources: [] })
    if (method === 'prompts/list') return rpcResult(id, { prompts: [] })
    // Notifications: no response, just acknowledge.
    if (method.startsWith('notifications/')) return null
    return rpcError(id, -32601, `method not found: ${method}`)
  }

  const server = createServer((req, res) => {
    void handle(req, res).catch(e => {
      res.writeHead(500, { 'content-type': 'application/json' })
      res.end(JSON.stringify(rpcError(null, -32603, `strategy-server internal error: ${e instanceof Error ? e.message : String(e)}`)))
    })
  })

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    // 健康检查（不参与 MCP 协议；dashboard / smoke test 可以用）。
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ status: 'ok', tools: TOOLS.map(t => t.name) }))
      return
    }
    if (req.method !== 'POST') {
      res.writeHead(405, { 'content-type': 'application/json' })
      res.end(JSON.stringify(rpcError(null, -32600, 'only POST is supported for MCP requests')))
      return
    }
    const raw = await readBody(req)
    let msg: JsonRpcRequest | null = null
    try { msg = raw ? JSON.parse(raw) as JsonRpcRequest : null }
    catch { msg = null }
    if (!msg) {
      res.writeHead(400, { 'content-type': 'application/json' })
      res.end(JSON.stringify(rpcError(null, -32700, 'invalid JSON-RPC body')))
      return
    }

    const out = handleRpc(msg)
    if (out === null) {
      // notification — accept with 202, no body
      res.writeHead(202)
      res.end()
      return
    }
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify(out))
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
      const done = (): void => resolve()
      const timer = setTimeout(done, 3000)
      try { (server as { closeAllConnections?: () => void }).closeAllConnections?.() } catch { /* not available pre-Node 18.2 */ }
      server.close(() => { clearTimeout(timer); done() })
    }),
  }
}
