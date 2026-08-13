import { createHash } from 'node:crypto'
import { parse as parseYaml } from 'yaml'

export interface ParsedReply {
  session_id?: unknown
  reply?: unknown
  assistant_messages?: unknown
  messages?: unknown
}

export interface CandidateActionDraft {
  toolCallId: string
  toolName: string
  actionType: string
  instrument: string | null
  requestedAmount: number | null
  requestedShares: number | null
  reason: string | null
  observedOutcome: 'accepted' | 'rejected' | 'transport_error' | 'unknown'
  resultExcerpt: string
  sourceLocator: string
}

export interface EvidenceDraft {
  relation: 'SUPPORTS' | 'REFUTES' | 'UNCERTAIN'
  evidenceType: string
  evidenceRef: string | null
  verificationStatus: 'source_recorded' | 'agent_asserted' | 'unverified'
  sourceLocator: string
  rawExcerpt: string
}

export interface ClaimDraft {
  statement: string
  claimType: string
  status: 'active_at_decision' | 'accepted_for_execution' | 'overridden_for_execution' | 'unresolved'
  confidence: number | null
  confidenceLabel: string | null
  mechanisms: string[]
  falsifiers: string[]
  certainty: 'policy_defined' | 'estimated' | 'agent_asserted'
  sourceLocator: string
  rawExcerpt: string
  evidence: EvidenceDraft[]
}

export interface AgentMetricDraft {
  subject: string
  predicate: string
  value: number
  unit: string
  sourceLocator: string
  rawExcerpt: string
}

export interface ConflictDraft {
  conflictType: string
  leftStatement: string
  rightStatement: string
  resolution: string | null
  winner: string | null
  status: 'resolved' | 'unresolved' | 'data_quality_conflict'
  sourceLocator: string
  rawExcerpt: string
}

export function sha256(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex')
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, nested]) => [key, canonicalize(nested)]),
    )
  }
  return value
}

export function stableJson(value: unknown): string {
  return JSON.stringify(canonicalize(value))
}

export function asNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function parseJsonObject(text: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(text) as unknown
    return value && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

function replyText(parsed: ParsedReply): string {
  if (typeof parsed.reply === 'string' && parsed.reply.trim()) return parsed.reply
  if (!Array.isArray(parsed.assistant_messages)) return ''
  return parsed.assistant_messages
    .map(message => (message as { content?: unknown } | null)?.content)
    .filter((content): content is string => typeof content === 'string')
    .sort((left, right) => right.length - left.length)[0] ?? ''
}

export function pickFinalReply(parsed: ParsedReply): string {
  return replyText(parsed)
}

function classifyToolResult(content: string): CandidateActionDraft['observedOutcome'] {
  const parsed = parseJsonObject(content)
  if (parsed?.success === true || parsed?.order_id !== undefined) return 'accepted'
  if (parsed?.success === false) return 'rejected'
  if (/failed.*error sending request|transport|connection|ECONN|fetch failed/i.test(content)) return 'transport_error'
  if (/拒单|不允许|invalid|insufficient|超出|失败/.test(content)) return 'rejected'
  if (/订单已受理|success|order_id/i.test(content)) return 'accepted'
  return 'unknown'
}

export function extractCandidateActions(parsed: ParsedReply): CandidateActionDraft[] {
  const messages = Array.isArray(parsed.messages) ? parsed.messages : []
  const toolResults = new Map<string, string>()
  for (const message of messages) {
    const record = message && typeof message === 'object' ? message as Record<string, unknown> : {}
    if (record.role !== 'tool' || typeof record.tool_call_id !== 'string') continue
    toolResults.set(record.tool_call_id, typeof record.content === 'string' ? record.content : stableJson(record.content))
  }

  const drafts: CandidateActionDraft[] = []
  let assistantIndex = -1
  for (const message of messages) {
    const record = message && typeof message === 'object' ? message as Record<string, unknown> : {}
    if (record.role !== 'assistant') continue
    assistantIndex += 1
    const calls = Array.isArray(record.tool_calls) ? record.tool_calls : []
    for (let callIndex = 0; callIndex < calls.length; callIndex += 1) {
      const call = calls[callIndex] as Record<string, unknown>
      const fn = call.function && typeof call.function === 'object'
        ? call.function as Record<string, unknown>
        : {}
      const name = typeof fn.name === 'string' ? fn.name : ''
      const match = /portfolio_place_(buy|sell)_order$/i.exec(name)
      if (!match || typeof call.id !== 'string') continue
      const argumentText = typeof fn.arguments === 'string' ? fn.arguments : stableJson(fn.arguments ?? {})
      const args = parseJsonObject(argumentText) ?? {}
      const content = toolResults.get(call.id) ?? ''
      drafts.push({
        toolCallId: call.id,
        toolName: name,
        actionType: match[1].toUpperCase(),
        instrument: typeof args.fund_code === 'string' ? `fund:${args.fund_code}` : null,
        requestedAmount: asNumber(args.amount),
        requestedShares: asNumber(args.shares),
        reason: typeof args.reason === 'string' ? args.reason : null,
        observedOutcome: classifyToolResult(content),
        resultExcerpt: content.slice(0, 4_000),
        sourceLocator: `reply.json#/messages/assistant/${assistantIndex}/tool_calls/${callIndex}`,
      })
    }
  }
  return drafts
}

interface MarkdownSection {
  heading: string
  body: string
}

function markdownSections(markdown: string): MarkdownSection[] {
  const sections: MarkdownSection[] = []
  let current: { heading: string; lines: string[] } | null = null
  for (const line of markdown.split('\n')) {
    const heading = /^#{2,4}\s+(.+?)\s*$/.exec(line)
    if (heading) {
      if (current) sections.push({ heading: current.heading, body: current.lines.join('\n').trim() })
      current = { heading: heading[1], lines: [] }
    } else if (current) {
      current.lines.push(line)
    }
  }
  if (current) sections.push({ heading: current.heading, body: current.lines.join('\n').trim() })
  return sections
}

function cleanText(text: string): string {
  return text
    .replace(/^\s*(?:[-*+] |\d+[.)]\s+)/, '')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function sectionStatements(section: MarkdownSection): string[] {
  const statements: string[] = []
  const lines = section.body.split('\n')
  for (const line of lines) {
    if (/^\s*(?:[-*+] |\d+[.)]\s+)/.test(line)) {
      const text = cleanText(line)
      if (text.length >= 12) statements.push(text)
      continue
    }
    if (/^\s*\|/.test(line) && !/^\s*\|?\s*:?-+/.test(line)) {
      const cells = line.split('|').map(cleanText).filter(Boolean)
      if (cells.length >= 2 && !/维度|信号|结论|动作|来源|执行|持仓|方向|状态/.test(cells.join(''))) {
        statements.push(cells.join('；'))
      }
    }
  }
  if (!statements.length) {
    for (const paragraph of section.body.split(/\n\s*\n/)) {
      if (paragraph.includes('```') || paragraph.trim().startsWith('|')) continue
      const text = cleanText(paragraph)
      if (text.length >= 12) statements.push(text)
    }
  }
  return [...new Set(statements)].slice(0, 20)
}

function relationFromPolarity(value: unknown): EvidenceDraft['relation'] {
  const polarity = String(value ?? '').toLowerCase()
  if (polarity === '+' || /positive|support|支持|利多/.test(polarity)) return 'SUPPORTS'
  if (polarity === '-' || /negative|refute|反对|利空/.test(polarity)) return 'REFUTES'
  return 'UNCERTAIN'
}

function extractBeliefClaims(markdown: string): ClaimDraft[] {
  const blocks = [...markdown.matchAll(/```ya?ml\s*\n([\s\S]*?)```/gi)]
  const claims: ClaimDraft[] = []
  for (let blockIndex = 0; blockIndex < blocks.length; blockIndex += 1) {
    let document: unknown
    try { document = parseYaml(blocks[blockIndex][1]) as unknown } catch { continue }
    const root = document && typeof document === 'object' ? document as Record<string, unknown> : {}
    const belief = root.belief && typeof root.belief === 'object'
      ? root.belief as Record<string, unknown>
      : null
    if (!belief) continue
    const target = String(belief.target_index ?? belief.target ?? 'unspecified_target')
    const horizons = belief.horizons && typeof belief.horizons === 'object'
      ? belief.horizons as Record<string, unknown>
      : {}
    const evidenceRows = Array.isArray(belief.evidence) ? belief.evidence : []
    const evidence: EvidenceDraft[] = evidenceRows.map((item, evidenceIndex) => {
      const row = item && typeof item === 'object' ? item as Record<string, unknown> : {}
      const excerpt = stableJson(row)
      return {
        relation: relationFromPolarity(row.polarity),
        evidenceType: String(row.type ?? 'agent_evidence'),
        evidenceRef: row.ref === undefined ? null : String(row.ref),
        verificationStatus: 'agent_asserted',
        sourceLocator: `reply.json#/reply/belief/${blockIndex}/evidence/${evidenceIndex}`,
        rawExcerpt: excerpt,
      }
    })
    for (const [horizon, raw] of Object.entries(horizons)) {
      const values = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
      const pUp = asNumber(values.p_up)
      if (pUp === null) continue
      const prior = asNumber(values.prior_p_up)
      const delta = asNumber(values.delta)
      claims.push({
        statement: `${target} 在 ${horizon} 的上涨概率预测为 ${pUp}${prior === null ? '' : `（先验 ${prior}`}${delta === null ? (prior === null ? '' : '）') : `，变化 ${delta}）`}`,
        claimType: 'market_prediction',
        status: 'active_at_decision',
        confidence: pUp,
        confidenceLabel: null,
        mechanisms: [],
        falsifiers: [],
        certainty: 'estimated',
        sourceLocator: `reply.json#/reply/belief/${blockIndex}/horizons/${horizon}`,
        rawExcerpt: stableJson({ target, horizon, ...values }),
        evidence,
      })
    }
  }
  return claims
}

function inferMechanisms(statement: string): string[] {
  const pieces = statement.split(/(?:因为|由于|因此|机制[:：]|依据[:：]|→)/)
  if (pieces.length < 2) return []
  return pieces.slice(0, -1).map(cleanText).filter(text => text.length >= 8).slice(0, 4)
}

export function extractClaims(parsed: ParsedReply): ClaimDraft[] {
  const markdown = replyText(parsed)
  const claims = extractBeliefClaims(markdown)
  const selected = markdownSections(markdown).filter(section =>
    /市场环境判断|主线判断|关键观察|核心判断|决策逻辑|风险与观察/.test(section.heading),
  )
  for (const section of selected) {
    const statements = sectionStatements(section)
    for (let index = 0; index < statements.length; index += 1) {
      const statement = statements[index]
      const isPolicy = /方法论|硬纪律|宪章|规则|闸门|regime/i.test(statement)
      claims.push({
        statement,
        claimType: isPolicy ? 'policy_interpretation' : 'market_interpretation',
        status: /执行|严格/.test(statement) ? 'accepted_for_execution' : 'active_at_decision',
        confidence: null,
        confidenceLabel: null,
        mechanisms: inferMechanisms(statement),
        falsifiers: [],
        certainty: isPolicy ? 'policy_defined' : 'agent_asserted',
        sourceLocator: `reply.json#/reply/sections/${encodeURIComponent(section.heading)}/${index}`,
        rawExcerpt: statement,
        evidence: [{
          relation: 'UNCERTAIN',
          evidenceType: 'agent_narrative',
          evidenceRef: null,
          verificationStatus: 'unverified',
          sourceLocator: `reply.json#/reply/sections/${encodeURIComponent(section.heading)}/${index}`,
          rawExcerpt: statement,
        }],
      })
    }
  }
  const unique = new Map<string, ClaimDraft>()
  for (const claim of claims) unique.set(sha256(claim.statement), claim)
  return [...unique.values()]
}

export function extractAgentMetrics(parsed: ParsedReply): AgentMetricDraft[] {
  const markdown = replyText(parsed)
  const metrics: AgentMetricDraft[] = []
  const seen = new Set<string>()
  const patterns = [
    /(?:HS300|沪深300)[^。\n|]{0,36}?(?:vs\s*)?MA120\s*[=:：]?\s*(-?\d+(?:\.\d+)?)\s*%/gi,
    /(?:HS300|沪深300)\s*年线[^。\n|]{0,50}?(-?\d+(?:\.\d+)?)\s*%/gi,
  ]
  for (const pattern of patterns) {
    for (const match of markdown.matchAll(pattern)) {
      const value = Number(match[1])
      if (!Number.isFinite(value)) continue
      const excerpt = match[0].slice(0, 300)
      const key = `${value}|${excerpt}`
      if (seen.has(key)) continue
      seen.add(key)
      metrics.push({
        subject: 'index:CSI300',
        predicate: 'DISTANCE_TO_MA120',
        value,
        unit: 'percent',
        sourceLocator: `reply.json#/reply/text-offset/${match.index ?? 0}`,
        rawExcerpt: excerpt,
      })
    }
  }
  for (const [lineIndex, line] of markdown.split("\n").entries()) {
    if (!/(?:HS300|沪深300).*年线/.test(line) || !line.includes("|")) continue
    const cells = line.split("|").map(cleanText).filter(Boolean)
    const signal = cells[1] ?? line
    for (const match of signal.matchAll(/(-?\d+(?:\.\d+)?)\s*%/g)) {
      const value = Number(match[1])
      if (!Number.isFinite(value)) continue
      const key = String(value) + "|" + signal
      if (seen.has(key)) continue
      seen.add(key)
      metrics.push({
        subject: "index:CSI300", predicate: "DISTANCE_TO_MA120", value, unit: "percent",
        sourceLocator: "reply.json#/reply/line/" + lineIndex, rawExcerpt: signal.slice(0, 300),
      })
    }
  }
  return metrics
}

export function extractNarrativeConflicts(parsed: ParsedReply): ConflictDraft[] {
  const markdown = replyText(parsed)
  const conflicts: ConflictDraft[] = []
  for (const section of markdownSections(markdown)) {
    if (!/关键观察|冲突|决策逻辑|风险与观察/.test(section.heading)) continue
    for (const [index, statement] of sectionStatements(section).entries()) {
      if (!/冲突|\bvs\b|但(?:是)?/i.test(statement)) continue
      const split = statement.split(/冲突[:：]?|\bvs\b|但(?:是)?/i).map(cleanText).filter(Boolean)
      if (split.length < 2) continue
      conflicts.push({
        conflictType: 'agent_reported_decision_conflict',
        leftStatement: split[0],
        rightStatement: split.slice(1).join('；'),
        resolution: /执行|采用|因此|不触发/.test(statement) ? statement : null,
        winner: null,
        status: /执行|采用|因此|不触发/.test(statement) ? 'resolved' : 'unresolved',
        sourceLocator: `reply.json#/reply/sections/${encodeURIComponent(section.heading)}/${index}`,
        rawExcerpt: statement,
      })
    }
  }
  return conflicts.slice(0, 20)
}

export function summaryFromReply(parsed: ParsedReply): string {
  const markdown = replyText(parsed)
  const section = markdownSections(markdown).find(item => /一句话总结/.test(item.heading))
  if (section) return cleanText(section.body).slice(0, 1_500)
  const lines = markdown.split('\n').map(cleanText).filter(line => line && !line.startsWith('#') && !line.startsWith('|'))
  return (lines.find(line => !/^Now |^Let me /i.test(line)) ?? '未抽取到可靠摘要').slice(0, 1_500)
}
