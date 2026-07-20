// bot101 交互式对话引擎（market-reports.html 右下角对话框的后端）。
//
// 目标：让用户在看板上「直接和 bot101 对话」，且是 **真 agentic** —— bot101 可以
// 现场调用 MCP 工具拉实时数据（行情/估值/因子/宏观研报 + 自己的只读账户）再回答，
// 而不是只对着静态文本闲聊。
//
// 与日度回测的关系：复用 bot101 自己那套 LLM 端点（research-loop.yaml 里 bot 实跑用的
// base_url/model/api_key）和同样的两个 MCP 上游。但本引擎 **不经** world 的 per-run proxy
// （那是 ephemeral、随 run 起灭、还占端口），而是直连上游、在引擎内复刻 proxy 的
// 「抹时间字段/run_id + 调用时强制注入」语义：
//   - simworld-data(:18078)  → tools/list 抹掉 simulated_datetime；tools/call 注入
//     `simulated_datetime = <as-of> 15:00:00`（PIT，bot 看不到也改不了这个时点）。
//     其余日期字段(start_date/end_date/…)留给 LLM 当窗口旋钮，上游自身按 as-of 截断防穿越。
//   - fund-portfolio bot-only(:28172) → 抹 run_id；tools/call 注入 run_id=oos-bot101-daily，
//     且 **只暴露只读工具**（下单工具 portfolio_place_* 一律不进 tools 列表 → chat 不能下单）。
// 工具调用串行执行（一个 LLM 回合里的多个 tool_call 顺序跑），天然避开 simworld 单 worker
// 的并发超时坑，也就不需要 proxy 的串行闸门。
//
// 申赎原始接口(HIDDEN)与 proxy 一致整条隐藏。

import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { resolveLlmEndpointFromRlConfig } from '../history-window/compact.ts'

// ---------- 对外类型 ----------
export interface ChatMessage { role: 'user' | 'assistant'; content: string }
export interface ToolTraceEntry { server: string; name: string; args: Record<string, unknown>; ok: boolean; preview: string }
export interface ChatTurnInput {
  /** 浏览器侧维护的对话历史（user/assistant 文本，不含工具内部消息）。 */
  messages: ChatMessage[]
  /** 数据时点 = 用户在页面上选中的日期 YYYY-MM-DD。工具调用锁死在此日及之前。 */
  asOfDate: string
  /** server.ts 用 loadMarketReports/loadOosBot101 拼好的「当前页面上下文」markdown。 */
  pageContext: string
  /** 客户端断开（SSE 路由 req.close）时中止本回合：停掉后续 LLM/工具调用。 */
  signal?: AbortSignal
}
export interface ChatTurnResult { reply: string; trace: ToolTraceEntry[] }
/** 流式事件回调。event 取值：
 *  - 'step'        {iter}                  新一轮 LLM 调用开始（客户端清空当前在写的文本气泡）
 *  - 'token'       {text}                  最终答案的增量 token（也可能是工具轮的开场白，下一个 step 会清掉）
 *  - 'tool_call'   {server,name,args}      即将执行某个工具
 *  - 'tool_result' {name,ok,preview}       某工具返回
 *  done/error/meta 由调用方（server.ts）在外层补发。 */
export type ChatEmit = (event: string, data: Record<string, unknown>) => void
export interface Bot101ChatEngine {
  runTurn(input: ChatTurnInput): Promise<ChatTurnResult>
  runTurnStream(input: ChatTurnInput, emit: ChatEmit): Promise<ChatTurnResult>
}

export interface Bot101ChatEngineOptions {
  /** world/runtime 根（与 server.ts 的 worldRoot 一致）。 */
  worldRoot: string
  /** 固定 OOS run。 */
  runId?: string
  /** 上游 MCP；默认线上端口。 */
  simworldUrl?: string
  fundUrl?: string
  /** 读「市场研报库」某 report_type 在 as-of 当日及之前的最近一期（与 strategy-server.get_market_report
   *  同口径，PIT 防穿越）。由 server.ts 注入（直查 fund.db）。给定后，chat 的 bot101 会多出一个本地
   *  `get_market_report` 工具，能现场拉「当前/历史」市场主线、行情研判、轮动、宏观资讯等研报。
   *  缺省=不暴露该工具（向后兼容，单测 buildToolRegistry 不受影响）。 */
  loadMarketReportAsOf?: (reportType: string, asOf: string) => { as_of_date: string; content_md: string } | null
}

// ---------- 常量 ----------
const DEFAULT_RUN_ID = 'oos-bot101-daily'
const DEFAULT_SIMWORLD_URL = 'http://127.0.0.1:18078/mcp'
const DEFAULT_FUND_URL = 'http://127.0.0.1:28172/mcp'
const SIM_DATE_FIELD = 'simulated_datetime'
const FUND_RUNID_FIELD = 'run_id'
const TIME_OF_DAY = '15:00:00'
// 与 simworld-proxy/server.ts:HIDDEN_TOOLS 一致：申赎原始接口对 agent 彻底隐藏。
const HIDDEN_TOOLS = new Set<string>(['fund_subscription_redemption_summary', 'fund_index_subscription_redemption'])
// fund-portfolio 写工具：chat 严禁下单，整条不进 tools 列表。
const FUND_WRITE_TOOLS = new Set<string>(['portfolio_place_buy_order', 'portfolio_place_sell_order'])
const MAX_TOOL_ITERS = 6          // LLM↔工具往返上限，兜住成本/时延
const MAX_HISTORY = 16            // 注回的历史消息条数上限
const LLM_TIMEOUT_MS = 150_000
const TOOL_TIMEOUT_MS = 30_000
const METHODOLOGY_MAX_CHARS = 9000 // 方法论很长(47KB)，截断进 system prompt 控 token
// 本地合成工具：不走 MCP 上游，引擎内直接用 opts.loadMarketReportAsOf 查 fund.db 的 market_reports。
const LOCAL_MARKET_REPORT_TOOL = 'get_market_report'
// 与 strategy-server VALID_REPORT_TYPES + macro_news 一致。'all' 只取前三份日报（macro_news 单独读）。
const LOCAL_MARKET_REPORT_TYPES = ['market_context', 'market_mainline', 'mainline_rotation', 'macro_news'] as const
const LOCAL_DAILY_REPORT_TYPES = ['market_context', 'market_mainline', 'mainline_rotation'] as const
// 与 strategy-server.get_market_report 同口径（2026-07-02「替换」）：主线/rotation 优先取日度版
// （skill 日度纪律确定性引擎），缺失回退月度——对话里的 bot101 看到的与生产 run 注入一致。
const LOCAL_MAINLINE_DAILY_ALIAS: Record<string, string> = {
  market_mainline: 'market_mainline_daily',
  mainline_rotation: 'mainline_rotation_daily',
}

// ---------- 内部类型 ----------
interface LlmEndpoint { baseUrl: string; apiKey: string; model: string }
interface McpTool { name: string; description?: string; inputSchema?: Record<string, unknown> }
interface OpenAiTool { type: 'function'; function: { name: string; description: string; parameters: Record<string, unknown> } }
interface RegistryEntry { server: 'simworld' | 'fund' | 'local'; url: string; injectDate: boolean; injectRunId: boolean }

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x)
}

// ---------- 极简 streamable-HTTP MCP 客户端（每次一个全新 session）----------
// 兼容无状态(simworld，无 sid)与有状态(fund，有 sid + notifications/initialized + DELETE)。
function parseJsonOrSse(text: string): unknown {
  if (text.startsWith('event:') || text.includes('\ndata:')) {
    for (const ev of text.split(/\n\n/)) {
      const dataLine = ev.split('\n').find(l => l.startsWith('data:'))
      if (!dataLine) continue
      try { return JSON.parse(dataLine.slice(5).trim()) } catch { /* try next */ }
    }
    return null
  }
  try { return JSON.parse(text) } catch { return null }
}

async function mcpRoundtrip(url: string, op: Record<string, unknown>, timeoutMs: number): Promise<Record<string, unknown> | null> {
  const baseHeaders: Record<string, string> = { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream' }
  const signal = AbortSignal.timeout(timeoutMs)
  const initResp = await fetch(url, {
    method: 'POST', headers: baseHeaders, signal,
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'bot101-chat', version: '0.1' } } }),
  })
  if (!initResp.ok) throw new Error(`MCP init HTTP ${initResp.status} @ ${url}`)
  const sid = initResp.headers.get('mcp-session-id')
  await initResp.text()
  const headers: Record<string, string> = sid ? { ...baseHeaders, 'mcp-session-id': sid } : baseHeaders
  try {
    if (sid) {
      await fetch(url, { method: 'POST', headers, signal, body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }) }).then(r => r.text())
    }
    const resp = await fetch(url, { method: 'POST', headers, signal, body: JSON.stringify(op) })
    if (!resp.ok) throw new Error(`MCP ${String(op.method)} HTTP ${resp.status} @ ${url}`)
    const parsed = parseJsonOrSse(await resp.text())
    return isObject(parsed) ? parsed : null
  } finally {
    if (sid) fetch(url, { method: 'DELETE', headers }).catch(() => { /* best-effort */ })
  }
}

async function mcpListTools(url: string): Promise<McpTool[]> {
  const r = await mcpRoundtrip(url, { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} }, 25_000)
  const result = r && isObject(r.result) ? r.result : null
  const tools = result && Array.isArray((result as Record<string, unknown>).tools) ? (result as { tools: unknown[] }).tools : []
  return tools.filter((t): t is McpTool => isObject(t) && typeof (t as { name?: unknown }).name === 'string')
}

async function mcpCallToolText(url: string, name: string, args: Record<string, unknown>): Promise<{ ok: boolean; text: string }> {
  let r: Record<string, unknown> | null
  try {
    r = await mcpRoundtrip(url, { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name, arguments: args } }, TOOL_TIMEOUT_MS)
  } catch (e) {
    return { ok: false, text: `工具调用失败: ${e instanceof Error ? e.message : String(e)}` }
  }
  if (!r) return { ok: false, text: '工具无响应' }
  if (isObject(r.error)) {
    const msg = (r.error as { message?: unknown }).message
    return { ok: false, text: `工具错误: ${typeof msg === 'string' ? msg : JSON.stringify(r.error)}` }
  }
  const result = isObject(r.result) ? r.result : null
  if (!result) return { ok: false, text: '工具无返回' }
  const content = Array.isArray((result as { content?: unknown }).content) ? (result as { content: unknown[] }).content : []
  const texts = content
    .filter((c): c is { type: string; text: string } => isObject(c) && (c as { type?: unknown }).type === 'text' && typeof (c as { text?: unknown }).text === 'string')
    .map(c => c.text)
  const isErr = (result as { isError?: unknown }).isError === true
  const text = texts.join('\n').trim()
  return { ok: !isErr, text: text || (isErr ? '(工具返回错误，无文本)' : '(空结果)') }
}

// ---------- schema 抹除（克隆，不动上游对象）----------
function stripField(schema: unknown, field: string): { schema: Record<string, unknown>; had: boolean } {
  if (!isObject(schema)) return { schema: { type: 'object', properties: {} }, had: false }
  const clone = JSON.parse(JSON.stringify(schema)) as Record<string, unknown>
  let had = false
  if (isObject(clone.properties) && field in clone.properties) { delete (clone.properties as Record<string, unknown>)[field]; had = true }
  if (Array.isArray(clone.required)) {
    const i = (clone.required as unknown[]).indexOf(field)
    if (i >= 0) { (clone.required as unknown[]).splice(i, 1); had = true }
  }
  return { schema: clone, had }
}

// ---------- 工具发现：上游 tools/list → OpenAI tools + 注入注册表 ----------
interface DiscoveredTools { openai: OpenAiTool[]; registry: Map<string, RegistryEntry> }

export function buildToolRegistry(simTools: McpTool[], fundTools: McpTool[], simUrl: string, fundUrl: string): DiscoveredTools {
  const openai: OpenAiTool[] = []
  const registry = new Map<string, RegistryEntry>()
  for (const t of simTools) {
    if (HIDDEN_TOOLS.has(t.name)) continue
    const { schema, had } = stripField(t.inputSchema, SIM_DATE_FIELD)
    registry.set(t.name, { server: 'simworld', url: simUrl, injectDate: had, injectRunId: false })
    openai.push({ type: 'function', function: { name: t.name, description: t.description ?? '', parameters: schema } })
  }
  for (const t of fundTools) {
    if (FUND_WRITE_TOOLS.has(t.name)) continue // chat 禁止下单
    const { schema, had } = stripField(t.inputSchema, FUND_RUNID_FIELD)
    registry.set(t.name, { server: 'fund', url: fundUrl, injectDate: false, injectRunId: had })
    openai.push({ type: 'function', function: { name: t.name, description: t.description ?? '', parameters: schema } })
  }
  return { openai, registry }
}

// ---------- 本地合成工具：get_market_report（查当前/历史市场研报）----------
// strategy-server 的 get_market_report 只在 per-run proxy 后面、且只会返回「当前世界日的最近一期」
// （bot 没法指定历史日）。chat 引擎不连 strategy-server，于是这里在引擎内合成一个同名工具：
//   - report_type 同口径：market_context / market_mainline / mainline_rotation / macro_news / all
//   - 多一个 as_of_date：让 bot 能**回看历史某交易日**的研报（PIT 夹到 ≤ 当前数据时点，不会穿越）
// 这样用户在对话里问「最新/上周的市场主线」时，bot 有真工具可调，而不是只能对着注入的单日上下文。
export function buildLocalMarketReportTool(): OpenAiTool {
  return {
    type: 'function',
    function: {
      name: LOCAL_MARKET_REPORT_TOOL,
      description:
        '读取系统预生成的「市场研究报告」（全局共享、所有 bot 同源、PIT 防穿越）。report_type：' +
        'market_context(行情/regime/敢不敢上仓) | market_mainline(主线板块+可投基金池) | ' +
        'mainline_rotation(主线轮动组合骨架) | macro_news(宏观资讯要点) | all(一次取前三份日报，不含 macro_news)。' +
        '默认返回「当前数据时点当日及之前的最近一期」；想回看历史某交易日的研报，传 as_of_date=YYYY-MM-DD，' +
        '会取 ≤ 该日的最近一期（仍受 PIT 约束、不超过当前数据时点）。问「最新/历史的市场主线、当日决策依据」就用它。',
      parameters: {
        type: 'object',
        properties: {
          report_type: { type: 'string', description: 'market_context | market_mainline | mainline_rotation | macro_news | all' },
          as_of_date: { type: 'string', description: '可选 YYYY-MM-DD：回看历史某交易日的研报时传它；缺省=当前数据时点。' },
        },
        required: ['report_type'],
      },
    },
  }
}

/** 把 get_market_report 入参解析为「要取哪些 report_type + 夹进 PIT 的有效 as-of」。纯函数，便于单测。
 *  as_of_date 只允许往回看：给未来日（或缺省）一律夹到 turnAsOf，杜绝穿越。 */
export function planLocalMarketReport(args: Record<string, unknown>, turnAsOf: string): { wanted: string[]; effAsOf: string; error?: string } {
  const rt = typeof args.report_type === 'string' ? args.report_type.trim() : ''
  const wanted: string[] = rt === 'all'
    ? [...LOCAL_DAILY_REPORT_TYPES]
    : ((LOCAL_MARKET_REPORT_TYPES as readonly string[]).includes(rt) ? [rt] : [])
  if (wanted.length === 0) {
    return { wanted: [], effAsOf: turnAsOf, error: `report_type "${rt}" 非法：只能是 ${LOCAL_MARKET_REPORT_TYPES.join(' / ')} 或 all` }
  }
  let effAsOf = turnAsOf
  const req = typeof args.as_of_date === 'string' ? args.as_of_date.trim() : ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(req) && req < turnAsOf) effAsOf = req
  return { wanted, effAsOf }
}

// ---------- LLM 流式调用 ----------
// 一次 /chat/completions（stream:true）：边读边把 content 增量回调给 onContent；同时把
// 分片到达的 tool_calls 按 index 装配（id/name 取首个带值的片，arguments 逐片拼接）。
// 返回整轮装配好的 {content, toolCalls}。即便是非流式调用方（runTurn），内部也走流式累加。
interface AssembledCall { id: string; name: string; arguments: string }

async function llmChatStream(
  endpoint: LlmEndpoint, messages: unknown[], tools: OpenAiTool[],
  onContent: (delta: string) => void, signal?: AbortSignal,
): Promise<{ content: string; toolCalls: AssembledCall[] }> {
  const url = endpoint.baseUrl.replace(/\/+$/, '') + '/chat/completions'
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), LLM_TIMEOUT_MS)
  const onAbort = () => ac.abort()
  if (signal) { if (signal.aborted) ac.abort(); else signal.addEventListener('abort', onAbort, { once: true }) }
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'authorization': `Bearer ${endpoint.apiKey}`, 'accept': 'text/event-stream' },
      signal: ac.signal,
      body: JSON.stringify({ model: endpoint.model, messages, tools, tool_choice: 'auto', temperature: 0.4, stream: true }),
    })
    if (!res.ok) { const b = await res.text().catch(() => ''); throw new Error(`LLM HTTP ${res.status}: ${b.slice(0, 240)}`) }
    if (!res.body) throw new Error('LLM 无响应体')
    let content = ''
    const byIndex = new Map<number, AssembledCall>()
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    const drain = (chunk: string): void => {
      for (const line of chunk.split('\n')) {
        const t = line.trim()
        if (!t.startsWith('data:')) continue
        const payload = t.slice(5).trim()
        if (!payload || payload === '[DONE]') continue
        let d: Record<string, unknown>
        try { d = JSON.parse(payload) } catch { continue }
        const choices = Array.isArray(d.choices) ? d.choices : []
        const delta = isObject(choices[0]) ? (choices[0] as { delta?: unknown }).delta : null
        if (!isObject(delta)) continue
        if (typeof delta.content === 'string' && delta.content) { content += delta.content; onContent(delta.content) }
        const tcs = Array.isArray((delta as { tool_calls?: unknown }).tool_calls) ? (delta as { tool_calls: unknown[] }).tool_calls : []
        for (const tcU of tcs) {
          if (!isObject(tcU)) continue
          const index = typeof tcU.index === 'number' ? tcU.index : 0
          const fn = isObject(tcU.function) ? tcU.function : {}
          const cur = byIndex.get(index) ?? { id: '', name: '', arguments: '' }
          if (typeof tcU.id === 'string' && tcU.id) cur.id = tcU.id
          const nm = (fn as { name?: unknown }).name
          if (typeof nm === 'string' && nm) cur.name = nm
          const ar = (fn as { arguments?: unknown }).arguments
          if (typeof ar === 'string') cur.arguments += ar
          byIndex.set(index, cur)
        }
      }
    }
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buf.indexOf('\n\n')) >= 0) { drain(buf.slice(0, idx)); buf = buf.slice(idx + 2) }
    }
    if (buf.trim()) drain(buf)
    const toolCalls = [...byIndex.entries()].sort((a, b) => a[0] - b[0]).map(e => e[1]).filter(c => c.name)
    return { content, toolCalls }
  } finally {
    clearTimeout(timer)
    if (signal) signal.removeEventListener('abort', onAbort)
  }
}

// ---------- 持久化人格块（进程生命周期内不变，缓存）----------
function readIf(path: string, maxChars?: number): string {
  if (!existsSync(path)) return ''
  const s = readFileSync(path, 'utf8').trim()
  return maxChars && s.length > maxChars ? s.slice(0, maxChars) + '\n…（方法论后文略，需要细节可继续问）' : s
}

// ---------- 引擎工厂 ----------
export function createBot101ChatEngine(opts: Bot101ChatEngineOptions): Bot101ChatEngine {
  const runId = opts.runId ?? DEFAULT_RUN_ID
  const simUrl = opts.simworldUrl ?? DEFAULT_SIMWORLD_URL
  const fundUrl = opts.fundUrl ?? DEFAULT_FUND_URL
  const repoRoot = join(opts.worldRoot, '..', '..')
  const botDir = join(repoRoot, 'bots', 'bot101')
  const shadowMethodology = join(opts.worldRoot, 'runs', runId, 'workspaces', 'bot101', 'METHODOLOGY.md')
  const rlConfigPath = join(opts.worldRoot, 'runs', runId, 'workspaces', 'bot101', 'config', 'research-loop.yaml')

  // 人格块只读一次（dashboard 部署时重启，文件不会中途变）。
  const personaBlock = (() => {
    const soul = readIf(join(botDir, 'SOUL.md'))
    const identity = readIf(join(botDir, 'IDENTITY.md'))
    const user = readIf(join(botDir, 'USER.md'))
    const methodology = readIf(shadowMethodology, METHODOLOGY_MAX_CHARS) || readIf(join(botDir, 'METHODOLOGY.md'), METHODOLOGY_MAX_CHARS)
    const parts: string[] = []
    if (identity) parts.push('# 我是谁（IDENTITY）\n' + identity)
    if (soul) parts.push('# 我的性格与表达（SOUL）\n' + soul)
    if (user) parts.push('# 我服务的用户与风险偏好（USER）\n' + user)
    if (methodology) parts.push('# 我的投资方法论（当前生效版本，节选）\n' + methodology)
    return parts.join('\n\n')
  })()

  let discovery: Promise<DiscoveredTools> | null = null
  function discover(): Promise<DiscoveredTools> {
    if (!discovery) {
      discovery = (async () => {
        const [simTools, fundTools] = await Promise.all([
          mcpListTools(simUrl).catch(() => [] as McpTool[]),
          mcpListTools(fundUrl).catch(() => [] as McpTool[]),
        ])
        if (simTools.length === 0 && fundTools.length === 0) {
          discovery = null // 上游全挂 → 下次重试，别把空注册表缓存死
          throw new Error('无法连接任何 MCP 上游（simworld:18078 / fund:28172 都没返回工具）')
        }
        const built = buildToolRegistry(simTools, fundTools, simUrl, fundUrl)
        // 接入了研报库就追加本地 get_market_report（查当前/历史市场主线等）。不走 MCP，url 留空。
        if (opts.loadMarketReportAsOf) {
          built.openai.push(buildLocalMarketReportTool())
          built.registry.set(LOCAL_MARKET_REPORT_TOOL, { server: 'local', url: '', injectDate: false, injectRunId: false })
        }
        return built
      })()
    }
    return discovery
  }

  // 本地 get_market_report：直查研报库（opts.loadMarketReportAsOf），支持 as_of_date 回看历史，PIT 夹紧。
  function execLocalMarketReport(args: Record<string, unknown>, turnAsOf: string): { ok: boolean; text: string } {
    const lookup = opts.loadMarketReportAsOf
    if (!lookup) return { ok: false, text: '本实例未接入市场研报库，get_market_report 不可用。' }
    const plan = planLocalMarketReport(args, turnAsOf)
    if (plan.error) return { ok: false, text: plan.error }
    const parts: string[] = []
    for (const type of plan.wanted) {
      const tryTypes = LOCAL_MAINLINE_DAILY_ALIAS[type] ? [LOCAL_MAINLINE_DAILY_ALIAS[type], type] : [type]
      let row: { as_of_date: string; content_md: string } | null = null
      let used = type
      try {
        for (const tt of tryTypes) {
          row = lookup(tt, plan.effAsOf)
          if (row) { used = tt; break }
        }
      } catch (e) { return { ok: false, text: `读取 ${type} 研报失败：${e instanceof Error ? e.message : String(e)}` } }
      if (!row) parts.push(`# [${type}] 暂无报告（截至 ${plan.effAsOf} 库里无该类报告）`)
      else parts.push(`<!-- report_type=${used} as_of=${row.as_of_date} (PIT≤${plan.effAsOf}) -->\n${row.content_md}`)
    }
    return { ok: true, text: parts.join('\n\n---\n\n') }
  }

  function buildSystemPrompt(asOf: string, pageContext: string): string {
    const lines: string[] = [
      personaBlock,
      '',
      '# 你现在的处境（重要）',
      '你是 bot101，正在看板网页右下角的对话框里和用户**实时对话**。这**不是**日度交易决策环节：不要输出五份决策 MD、不要走「投资框架/市场/选基/持仓/巡检」那套流程、也不要尝试下单。就当面聊。',
      `数据时点（as-of）= ${asOf}（按 A股收盘 ${TIME_OF_DAY} 计），这是**截至当前最近一个已生成完整每日决策的交易日**——下方「当前页面上下文」里的研报与账户就是这一份最新决策。你调用的所有行情/估值/因子/研报工具都会被**自动锁定在该时点及之前**（PIT），你看不到也不要假设任何未来数据；用户口中的「最新/今天」就按这个 as-of 理解，不要说某天「缺失/查不到」。`,
      '你可以调用 simworld-data 的工具（市场指数行情/估值/温度、板块因子、宏观数据、research_search 宏观资讯研报等）和 fund-portfolio 的**只读**账户工具（你自己的持仓/历史业绩/成交/可买基金）来支撑回答；你**没有**下单权限。',
    ]
    if (opts.loadMarketReportAsOf) {
      lines.push('查「市场主线/行情研判/轮动组合/宏观资讯」用 **get_market_report**：report_type 取 market_mainline | market_context | mainline_rotation | macro_news | all；**要回看历史某交易日就传 as_of_date=YYYY-MM-DD**（会取 ≤ 该日的最近一期）。这就是你查最新或历史市场主线/决策依据的正路，别说查不到。')
    }
    lines.push(
      '用中文回答，简洁、有明确观点和依据。需要数据支撑时**先调工具拿到真实数字再下结论**，不要凭记忆编造行情或净值。每轮最多 ' + String(MAX_TOOL_ITERS) + ' 次工具往返，问题大就抓重点。',
      '',
      '# 当前页面上下文（用户正盯着这一天看）',
      pageContext || '（无）',
    )
    return lines.join('\n')
  }

  // 串行化对话回合：可变 as-of 注入 + 单 worker 上游，逐回合排队最稳。
  let queue: Promise<unknown> = Promise.resolve()
  function enqueue(input: ChatTurnInput, emit: ChatEmit): Promise<ChatTurnResult> {
    const task = queue.then(() => runTurnInner(input, emit), () => runTurnInner(input, emit))
    queue = task.then(() => undefined, () => undefined)
    return task
  }
  const noopEmit: ChatEmit = () => { /* 非流式调用方丢弃事件 */ }

  async function runTurnInner(input: ChatTurnInput, emit: ChatEmit): Promise<ChatTurnResult> {
    const asOf = /^\d{4}-\d{2}-\d{2}$/.test(input.asOfDate) ? input.asOfDate : ''
    if (!asOf) throw new Error('asOfDate 非法（需 YYYY-MM-DD）')
    const signal = input.signal
    const endpoint = resolveLlmEndpointFromRlConfig(rlConfigPath)
    const { openai, registry } = await discover()

    const history = (Array.isArray(input.messages) ? input.messages : [])
      .filter(m => m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string' && m.content.trim())
      .slice(-MAX_HISTORY)
      .map(m => ({ role: m.role, content: m.content }))
    if (history.length === 0) throw new Error('messages 为空')

    const convo: Record<string, unknown>[] = [{ role: 'system', content: buildSystemPrompt(asOf, input.pageContext) }, ...history]
    const trace: ToolTraceEntry[] = []

    async function execTool(name: string, args: Record<string, unknown>): Promise<{ ok: boolean; text: string; server: string }> {
      const entry = registry.get(name)
      if (!entry) return { ok: false, text: `未知工具 ${name}（不在可用列表里）`, server: '?' }
      if (entry.server === 'local') {
        if (name === LOCAL_MARKET_REPORT_TOOL) return { ...execLocalMarketReport(args, asOf), server: 'local' }
        return { ok: false, text: `未知本地工具 ${name}`, server: 'local' }
      }
      const callArgs: Record<string, unknown> = { ...args }
      if (entry.injectDate) callArgs[SIM_DATE_FIELD] = `${asOf} ${TIME_OF_DAY}`
      if (entry.injectRunId) callArgs[FUND_RUNID_FIELD] = runId
      const r = await mcpCallToolText(entry.url, name, callArgs)
      return { ...r, server: entry.server }
    }

    for (let iter = 0; iter < MAX_TOOL_ITERS; iter++) {
      if (signal?.aborted) throw new Error('客户端已断开，回合中止')
      emit('step', { iter })
      // 流式拿这一轮 LLM 输出：content 增量实时 emit 给前端；tool_calls 装配好再统一处理。
      const { content, toolCalls } = await llmChatStream(endpoint, convo, openai, (delta) => emit('token', { text: delta }), signal)

      if (toolCalls.length > 0) {
        // 把 assistant 的 tool_calls 原样回灌，再逐个串行执行、把结果作为 role:tool 追加。
        convo.push({ role: 'assistant', content, tool_calls: toolCalls.map(c => ({ id: c.id, type: 'function', function: { name: c.name, arguments: c.arguments } })) })
        for (const c of toolCalls) {
          let args: Record<string, unknown> = {}
          try { const p = JSON.parse(c.arguments || '{}'); if (isObject(p)) args = p } catch { /* 保持空 */ }
          const server = registry.get(c.name)?.server ?? '?'
          emit('tool_call', { server, name: c.name, args })
          const r = await execTool(c.name, args)
          trace.push({ server: r.server, name: c.name, args, ok: r.ok, preview: r.text.slice(0, 500) })
          emit('tool_result', { name: c.name, ok: r.ok, preview: r.text.slice(0, 500) })
          convo.push({ role: 'tool', tool_call_id: c.id, content: r.text.slice(0, 6000) })
        }
        continue
      }

      const reply = content.trim() || '(bot101 这一轮没有给出文本回复)'
      return { reply, trace }
    }
    return { reply: `(已连调 ${MAX_TOOL_ITERS} 轮工具仍未收敛到结论；可以把问题问得更聚焦一点，我再看。)`, trace }
  }

  return {
    runTurn: (input) => enqueue(input, noopEmit),
    runTurnStream: (input, emit) => enqueue(input, emit),
  }
}
