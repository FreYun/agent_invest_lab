import { readFileSync } from 'node:fs'

// 从一个 session jsonl 抽出当日 digest：保留 user prompt 头部 + assistant text + tool_use（带关键 args），
// 完全不收纳 tool_result 的 content（dailyContext / 行情 / NAV 这些占字数最多但价值最低）。
// jsonl 行结构（research-loop-rust2）：
//   - 第一行：{ type: "session", cwd, id, timestamp, version }
//   - { type: "custom", subtype: "research-loop-init", data: { source, topic } }  ← 完整 user prompt 在 topic
//   - { type: "message", message: { role, content } }                              ← content 是 anthropic block 列表
//   - { type: "custom", subtype: "research-loop-meta", ... }                       ← 收尾元数据
//
// content 里的 anthropic block：
//   - { type: "text", text }
//   - { type: "tool_use", id, name, input }
//   - { type: "tool_result", tool_use_id, content }   ← 跳过
//
// 我们生成的 markdown 形如：
//   ## 2026-01-05 (Day N)
//   - mem0_search "红利策略"
//   - market_temperature
//   - portfolio_place_buy_order fund=090010 amount=700000
//   ...
//   > 决策：估值 + 温度双低，建底仓 70%（最终 assistant text 前 400 字）

const TOOL_ARG_KEEP_KEYS = new Set([
  'fund_code', 'index_code', 'topic', 'query', 'symbol', 'code', 'ts_code',
  'amount', 'percent', 'percentage', 'shares', 'order_type', 'side',
  'reason', 'thesis', 'date', 'start_date', 'end_date', 'period',
  'name', 'category', 'industry',
])

const TOOL_NAME_PREFIX_STRIP = ['mcp__simworld_data__', 'mcp__fund_portfolio_mcp__', 'mcp__strategy_mcp__']

function stripToolPrefix(name: string): string {
  for (const p of TOOL_NAME_PREFIX_STRIP) {
    if (name.startsWith(p)) return name.slice(p.length)
  }
  return name
}

function fmtArgs(input: unknown): string {
  if (!input || typeof input !== 'object') return ''
  const obj = input as Record<string, unknown>
  const parts: string[] = []
  for (const k of Object.keys(obj)) {
    if (!TOOL_ARG_KEEP_KEYS.has(k)) continue
    const v = obj[k]
    if (v == null) continue
    if (typeof v === 'string') {
      const s = v.length > 60 ? v.slice(0, 60) + '…' : v
      parts.push(`${k}="${s}"`)
    } else if (typeof v === 'number' || typeof v === 'boolean') {
      parts.push(`${k}=${v}`)
    } else if (Array.isArray(v)) {
      parts.push(`${k}=[${v.length}]`)
    }
  }
  return parts.length ? ' ' + parts.join(' ') : ''
}

export interface DayDigest {
  date: string
  toolCalls: { name: string; args: string }[]
  finalText: string  // 最后一条 assistant text block，截到 400 字
  totalTextChars: number  // 全部 assistant text 字数（debug 用）
}

// 从单个 jsonl 抽 digest。tool_result 不收集；user prompt 不收集（已经在 dailyContext 当下灌过）。
export function extractDayDigestFromJsonl(jsonlPath: string, date: string): DayDigest {
  const lines = readFileSync(jsonlPath, 'utf8').split('\n').filter(l => l.trim())
  const toolCalls: { name: string; args: string }[] = []
  let lastText = ''
  let totalTextChars = 0
  for (const line of lines) {
    let obj: Record<string, unknown>
    try { obj = JSON.parse(line) as Record<string, unknown> }
    catch { continue }
    if (obj.type !== 'message') continue
    const msg = obj.message as { role?: string; content?: unknown } | undefined
    if (!msg || msg.role !== 'assistant') continue
    const content = msg.content
    if (!Array.isArray(content)) continue
    for (const block of content) {
      if (!block || typeof block !== 'object') continue
      const b = block as Record<string, unknown>
      if (b.type === 'tool_use' && typeof b.name === 'string') {
        toolCalls.push({ name: stripToolPrefix(b.name), args: fmtArgs(b.input) })
      } else if (b.type === 'text' && typeof b.text === 'string') {
        totalTextChars += b.text.length
        lastText = b.text  // 覆盖：保留最后一条（通常是收尾决策总结）
      }
    }
  }
  const finalText = lastText.length > 400 ? safeSlice(lastText, 400) + '…' : lastText
  return { date, toolCalls, finalText, totalTextChars }
}

// JS String.slice 按 UTF-16 code unit 切，可能把一个 surrogate pair（emoji 等）拦腰切断，
// 留下一个孤立的高代理 \uD8XX。downstream 用 JSON.stringify 时这个孤立代理会被原样输出为 \uXXXX，
// rust serde_json 的严格解析器看到没有配对的低代理就报 "unexpected end of hex escape" 抛 -32700。
// 退一位避开这个边界。
function safeSlice(s: string, n: number): string {
  if (n <= 0 || n >= s.length) return s.slice(0, n)
  const code = s.charCodeAt(n - 1)
  if (code >= 0xd800 && code <= 0xdbff) return s.slice(0, n - 1)
  return s.slice(0, n)
}

export function renderDayDigest(d: DayDigest, dayNum: number | undefined): string {
  const header = dayNum !== undefined ? `## ${d.date} (Day ${dayNum})` : `## ${d.date}`
  const toolLines = d.toolCalls.length
    ? d.toolCalls.map(t => `- ${t.name}${t.args}`).join('\n')
    : '- （无工具调用）'
  const reflection = d.finalText.trim() ? `\n> ${d.finalText.trim().replace(/\n+/g, ' ')}` : ''
  return `${header}\n${toolLines}${reflection}`
}
