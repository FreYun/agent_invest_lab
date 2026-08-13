import { createHash } from 'node:crypto'

export interface MarkdownSection {
  level: number
  heading: string
  body: string
}

export type CandidateCardType = 'risk_warning' | 'market_hypothesis'
export type CandidateDirection = 'increase' | 'reduce' | 'hold' | 'mixed' | 'inspect'

export interface ExperienceCandidate {
  sourceSection: string
  sourceText: string
  cardType: CandidateCardType
  direction: CandidateDirection
  topics: string[]
  conflictSignature: string | null
}


export interface CanonicalCandidateText {
  title: string
  claim: string
  appliesIfText: string
}

/**
 * 将来源 Agent 的原话包装成有明确归属、时间和适用边界的第三人称表述。
 * 原话本身不改写，继续作为引文与 experience_card_sources 的审计证据保存。
 */
export function renderThirdPersonCandidate(candidate: ExperienceCandidate, decisionDate: string): CanonicalCandidateText {
  const kind = candidate.cardType === 'risk_warning' ? '候选风险规则' : '候选市场假设'
  const attribution = candidate.cardType === 'risk_warning'
    ? `在 ${decisionDate} 的来源案例中，来源 Agent 基于其当时的人设、策略与风险预算提出${kind}`
    : `在 ${decisionDate} 的来源案例中，来源 Agent 基于当时可得信息提出${kind}`
  return {
    title: `${kind}（来源 Agent）：${candidate.sourceText.slice(0, 52)}`,
    claim: `${attribution}：“${candidate.sourceText}”。`,
    appliesIfText: `仅当当前市场条件、投资期限、策略类型与风险预算均与 ${decisionDate} 的来源案例相匹配时，才可将该候选判断纳入研究。`,
  }
}

const DECISION_STUB_MAXLEN = 200
const DECISION_LONG_FALLBACK_ABS = 800
const DECISION_LONG_FALLBACK_RATIO = 2

export function sha256(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex')
}

export function stripBeliefBlocks(md: string): string {
  return String(md ?? '')
    .replace(/```[^\n]*\n[\s\S]*?```/g, block => /belief\s*:/.test(block) ? '' : block)
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

export function pickDecisionText(parsed: { reply?: unknown; assistant_messages?: unknown }): string {
  const reply = typeof parsed.reply === 'string' ? parsed.reply : ''
  const messages = Array.isArray(parsed.assistant_messages) ? parsed.assistant_messages : []
  let best = ''
  let bestMeat = -1
  for (const message of messages) {
    const content = (message as { content?: unknown } | null)?.content
    if (typeof content !== 'string') continue
    const meat = stripBeliefBlocks(content).length
    if (meat > bestMeat) {
      best = content
      bestMeat = meat
    }
  }
  const replyMeat = stripBeliefBlocks(reply).length
  if (replyMeat < DECISION_STUB_MAXLEN && bestMeat > replyMeat) return best
  if (bestMeat >= replyMeat * DECISION_LONG_FALLBACK_RATIO && bestMeat >= DECISION_LONG_FALLBACK_ABS) return best
  return reply
}

export function parseMarkdownSections(markdown: string): MarkdownSection[] {
  const sections: MarkdownSection[] = []
  let current: { level: number; heading: string; lines: string[] } | null = null
  for (const line of markdown.split('\n')) {
    const match = /^(#{2,4})\s+(.+?)\s*$/.exec(line)
    if (match) {
      if (current) sections.push({ level: current.level, heading: current.heading, body: current.lines.join('\n').trim() })
      current = { level: match[1].length, heading: match[2].trim(), lines: [] }
      continue
    }
    if (current) current.lines.push(line)
  }
  if (current) sections.push({ level: current.level, heading: current.heading, body: current.lines.join('\n').trim() })
  return sections
}

function cleanMarkdown(text: string): string {
  return text
    .replace(/^\s*(?:[-*+] |\d+[.)]\s+)/, '')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function sectionItems(body: string): string[] {
  const items: string[] = []
  let current = ''
  for (const line of body.split('\n')) {
    if (/^\s*(?:[-*+] |\d+[.)]\s+)/.test(line)) {
      if (current.trim()) items.push(cleanMarkdown(current))
      current = line
    } else if (current && line.trim() && !line.trim().startsWith('|')) {
      current += ' ' + line.trim()
    }
  }
  if (current.trim()) items.push(cleanMarkdown(current))
  if (items.length) return items.filter(Boolean)
  return body
    .split(/\n\s*\n/)
    .map(cleanMarkdown)
    .filter(text => text && !text.startsWith('|'))
}

function inferDirection(text: string): CandidateDirection {
  const blocksIncrease = /不是加仓|不(?:宜|应|能|可)?加仓|禁止加仓|不可加仓|不追|不增/.test(text)
  const blocksReduce = /不(?:宜|应|能|可)?减仓|不降仓|不清仓|不杀|禁止减仓/.test(text)
  const increase = !blocksIncrease && /加仓|建仓|增仓|提高仓位|进场|新进/.test(text)
  const reduce = !blocksReduce && /减仓|降仓|清仓|卖出|止损|剔除|降低仓位|降至|压至|清核心|清科技|降卫星/.test(text)
  const hold = blocksIncrease || blocksReduce || /持有|维持|不动|观望/.test(text)
  if (increase && reduce) return 'mixed'
  if (increase) return 'increase'
  if (reduce) return 'reduce'
  if (hold) return 'hold'
  return 'inspect'
}

const TOPIC_RULES: Array<[string, RegExp]> = [
  ['liquidity', /流动性|资金面|资金流|ETF净流入/],
  ['trend', /趋势|破位|MA\d+|年线|donchian/i],
  ['valuation', /估值|PE|ERP|股债性价比/i],
  ['crowding', /拥挤|情绪过热|情绪冰点/],
  ['drawdown', /回撤|止损|本金亏损/],
  ['geopolitics', /地缘|战争|停火|霍尔木兹|油价/],
  ['fundamentals', /基本面|产业景气|盈利|订单|业绩/],
  ['rotation', /主线|卫星|核心|轮动|top\d+/i],
]

function inferTopics(text: string): string[] {
  const topics = TOPIC_RULES.filter(([, re]) => re.test(text)).map(([topic]) => topic)
  return topics.length ? topics : ['unclassified']
}

function isCandidateSection(heading: string): CandidateCardType | null {
  if (/闸门|证伪触发|证伪条件/.test(heading)) return 'risk_warning'
  if (/深研.*(?:核心结论|要点消化)|核心结论/.test(heading)) return 'market_hypothesis'
  // These are source materials, not reusable experiences. They are retained
  // for later retrospective synthesis only.
  if (/仓位(?:合成|判定)|决策逻辑|风险与观察/.test(heading)) return 'market_hypothesis'
  return null
}

export function extractSummary(sections: MarkdownSection[], decisionText: string): string {
  const summary = sections.find(section => /一句话总结/.test(section.heading))
  if (summary) {
    const text = cleanMarkdown(summary.body)
    if (text) return text.slice(0, 800)
  }
  const fallback = stripBeliefBlocks(decisionText)
    .split('\n')
    .map(cleanMarkdown)
    .find(line => line && !line.startsWith('#') && !line.startsWith('|'))
  return (fallback ?? '').slice(0, 800)
}

export function extractExperienceCandidates(sections: MarkdownSection[]): ExperienceCandidate[] {
  const candidates: ExperienceCandidate[] = []
  for (const section of sections) {
    const cardType = isCandidateSection(section.heading)
    if (!cardType) continue
    for (const text of sectionItems(section.body).slice(0, 8)) {
      if (text.length < 12) continue
      const direction = inferDirection(text)
      const topics = inferTopics(text)
      const primaryTopic = topics[0] === 'unclassified' ? null : topics[0]
      candidates.push({
        sourceSection: section.heading,
        sourceText: text.slice(0, 2_000),
        cardType,
        direction,
        topics,
        conflictSignature: primaryTopic ? `portfolio|unspecified|${primaryTopic}` : null,
      })
    }
  }
  return candidates
}

