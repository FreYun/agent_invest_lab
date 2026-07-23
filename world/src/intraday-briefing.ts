// 盘中「当日研究室简报」装配：按 run 的 strategy_id 路由到 res 研究室，取每室规范主报最新一份，
// 拼成一段 markdown 注入 decide 前的 daily message。数据源是 .openclaw/workspace-resN/memory 下的 .md
// 文件（板块室与 res14 未进 fund.db，只能读文件）。live 是对真实最新交易日决策，喂昨日盘面不构成未来函数。
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

interface RoomDef { name: string; dir: string; pattern: 'date' | 'market_env' | 'us_transmit' }
interface Routing {
  always: string[]
  staleDays: number
  rooms: Record<string, RoomDef>
  sectors: Record<string, string>
  broad: string[]
}

const SECTION_CHAR_CAP = 8000

const PATTERNS: Record<RoomDef['pattern'], RegExp> = {
  date:        /^(\d{4}-\d{2}-\d{2})\.md$/,
  market_env:  /^(\d{4}-\d{2}-\d{2})-市场环境\.md$/,
  us_transmit: /^(\d{4}-\d{2}-\d{2})-隔夜美股A股传导\.md$/,
}

function daysBetween(from: string, to: string): number {
  const a = Date.parse(from + 'T00:00:00Z'), b = Date.parse(to + 'T00:00:00Z')
  if (Number.isNaN(a) || Number.isNaN(b)) return 0
  return Math.round((b - a) / 86400000)
}

// 只在「规范主报」里取日期最大那份：严格匹配室的 pattern，排除主题分片(YYYY-MM-DD-xxx)、
// _raw_*.json、MEMORY.md、子目录。返回 null = 该室无可用主报。
function latestMainReport(roomDir: string, pattern: RoomDef['pattern']): { date: string; content: string } | null {
  const re = PATTERNS[pattern]
  let best: { date: string; file: string } | null = null
  let names: string[]
  try { names = readdirSync(roomDir) } catch { return null }
  for (const name of names) {
    const m = re.exec(name)
    if (!m) continue
    if (!best || m[1] > best.date) best = { date: m[1], file: name }
  }
  if (!best) return null
  try {
    return { date: best.date, content: readFileSync(join(roomDir, best.file), 'utf8').trim() }
  } catch {
    return null
  }
}

function resolveRooms(strategyId: string | undefined, routing: Routing): string[] {
  const rooms = [...routing.always]
  const sector = strategyId ? routing.sectors[strategyId] : undefined
  if (sector && !rooms.includes(sector)) rooms.push(sector)
  return rooms
}

// 该 strategy_id 是否已知（对口板块 / 宽基 / 常驻均算已覆盖）；供路由覆盖验证脚本用。
export function isStrategyRouted(strategyId: string, routing: Routing): boolean {
  return strategyId in routing.sectors || routing.broad.includes(strategyId)
}

export function loadRouting(routingPath: string): Routing {
  return JSON.parse(readFileSync(routingPath, 'utf8')) as Routing
}

// 返回简报正文 markdown（不含外层标题，由 message.ts 的 briefingBlock 包裹）。无任何可读内容时返回 ''。
export function assembleBriefing(opts: {
  strategyId?: string
  resRoot: string
  routingPath: string
  asOfDate: string
}): string {
  let routing: Routing
  try { routing = loadRouting(opts.routingPath) } catch { return '' }
  const roomIds = resolveRooms(opts.strategyId, routing)
  const sections: string[] = []
  let anyContent = false
  for (const id of roomIds) {
    const def = routing.rooms[id]
    if (!def) continue
    const rpt = latestMainReport(join(opts.resRoot, def.dir), def.pattern)
    if (!rpt) {
      sections.push(`【${id} · ${def.name} · 无可用主报，已跳过】`)
      continue
    }
    const ago = daysBetween(rpt.date, opts.asOfDate)
    const stale = ago >= routing.staleDays ? ' · 已过期' : ''
    let body = rpt.content
    if (body.length > SECTION_CHAR_CAP) body = body.slice(0, SECTION_CHAR_CAP) + '\n…（已截断）'
    sections.push(`【${id} · ${def.name} · 报告日 ${rpt.date} · 距今 ${ago} 天${stale}】\n${body}`)
    anyContent = true
  }
  return anyContent ? sections.join('\n\n---\n\n') : ''
}
