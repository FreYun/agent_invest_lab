import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { extractDayDigestFromJsonl, renderDayDigest, type DayDigest } from './extract.ts'
import { compactHistory, resolveLlmEndpoint, resolveLlmEndpointFromRlConfig } from './compact.ts'

// History window 注入位置（在 daily prompt 顶部）。预算：20000 中文字符。
// 切割策略：
//   - 最近的（按时间倒序，累计字符 ≤ budget × 30%）→ 原样保留（"raw recent"）
//   - 余下更老的 → 若拼起来 ≤ budget × 60% 也原样保留；否则调主模型按 4 维度压缩到 ≤ budget × 60%
//   - 总长 = recent (≤30%) + (compact or older raw) (≤60%) + 标头/分隔 ≤10%
//
// 缓存（增量复用）：rl-openclaw/history-compact/<botId>/state.json，单一文件:
//   { compactText, compactedUpToDate }   ← 上一次 compact 的成品 + 它涵盖到哪一天
// 次日 older 段 = 已有 compactText + (compactedUpToDate 之后的新 older raw)。只要拼起来 ≤ compactBudget
// 就完全不调 LLM；溢出时才把"老 compact + 新增 raw"一次性丢给 LLM 做 recompact 并刷新 state。

export const HISTORY_WINDOW_BUDGET_CHARS = 20_000
const RECENT_FRACTION = 0.30
const COMPACT_FRACTION = 0.60

interface SessionIndexEntry {
  sessionFile?: string
  sessionId?: string
  updatedAt?: number
}

// sessions.json 路径。research-loop-rust2 把它写在 <rlOpenclawDir>/agents/<botId>/sessions/sessions.json。
function sessionsIndexPath(rlOpenclawDir: string, botId: string): string {
  return join(rlOpenclawDir, 'agents', botId, 'sessions', 'sessions.json')
}

function historyCompactDir(rlOpenclawDir: string, botId: string): string {
  return join(rlOpenclawDir, 'history-compact', botId)
}

// sessionKey 形如 `agent:bot1:trading-<runId>-<YYYY-MM-DD>`。最后 10 字符是 ISO 日期。
function parseDateFromSessionKey(key: string): string | null {
  if (key.length < 10) return null
  const tail = key.slice(-10)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(tail)) return null
  return tail
}

// 从 sessions.json 列出该 bot 所有 (date, sessionFile) 升序对，过滤 < beforeDate（不含当日）。
export function listBotSessionsBefore(rlOpenclawDir: string, botId: string, beforeDate: string): { date: string; sessionFile: string }[] {
  const p = sessionsIndexPath(rlOpenclawDir, botId)
  if (!existsSync(p)) return []
  let idx: Record<string, SessionIndexEntry>
  try { idx = JSON.parse(readFileSync(p, 'utf8')) as Record<string, SessionIndexEntry> }
  catch { return [] }
  const out: { date: string; sessionFile: string }[] = []
  for (const [key, entry] of Object.entries(idx)) {
    const date = parseDateFromSessionKey(key)
    if (!date || date >= beforeDate) continue
    if (!entry.sessionFile || !existsSync(entry.sessionFile)) continue
    out.push({ date, sessionFile: entry.sessionFile })
  }
  out.sort((a, b) => a.date.localeCompare(b.date))
  return out
}

export interface BuildHistoryWindowOptions {
  rlOpenclawDir: string
  botId: string
  beforeDate: string                  // 当前世界日，不含
  // 压缩 LLM 端点优先来源：本 bot 影子 workspace 的 research-loop.yaml（跟着 bot 当前 key 走）。
  rlConfigPath?: string
  openclawJsonPath: string            // 回退来源（pi loop / 测试 / rlConfigPath 解析失败）
  budgetChars?: number
  // 单元测试 / 离线场景：跳过真正的 LLM 调用，直接拼老 digest 当 compact（用于不依赖外部网络）。
  skipLlmCompact?: boolean
}

export interface HistoryWindowResult {
  markdown: string                    // 即将注入 prompt 的最终文本（含标头），空 history → ""
  dayCount: number                    // 纳入窗口的总 day 数（不含被裁掉的）
  recentDays: number                  // 原样保留的最近天数
  compactedDays: number               // 被压缩的天数
  totalChars: number
}

interface CompactState {
  compactText: string         // 最近一次 LLM compact 的成品
  compactedUpToDate: string   // 成品涵盖到的"最后一天 older digest"日期（含）
}

function compactStatePath(rlOpenclawDir: string, botId: string): string {
  return join(historyCompactDir(rlOpenclawDir, botId), 'state.json')
}

function readCompactState(rlOpenclawDir: string, botId: string): CompactState | null {
  const p = compactStatePath(rlOpenclawDir, botId)
  if (!existsSync(p)) return null
  try {
    const obj = JSON.parse(readFileSync(p, 'utf8')) as Partial<CompactState>
    if (typeof obj.compactText === 'string' && typeof obj.compactedUpToDate === 'string') {
      return { compactText: obj.compactText, compactedUpToDate: obj.compactedUpToDate }
    }
    return null
  } catch { return null }
}

function writeCompactState(rlOpenclawDir: string, botId: string, state: CompactState): void {
  const dir = historyCompactDir(rlOpenclawDir, botId)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  writeFileSync(compactStatePath(rlOpenclawDir, botId), JSON.stringify(state, null, 2))
}

// 压缩端点：优先 research-loop.yaml（rlConfigPath，跟 bot 当前 key 一致），
// 解析不到（文件缺失 / 字段不全 / pi loop 未提供）再回退 openclaw.json。
function resolveCompactEndpoint(opts: BuildHistoryWindowOptions) {
  if (opts.rlConfigPath) {
    try { return resolveLlmEndpointFromRlConfig(opts.rlConfigPath) } catch { /* fall through */ }
  }
  return resolveLlmEndpoint(opts.openclawJsonPath)
}

export async function buildHistoryWindow(opts: BuildHistoryWindowOptions): Promise<HistoryWindowResult> {
  const budget = opts.budgetChars ?? HISTORY_WINDOW_BUDGET_CHARS
  const recentBudget = Math.floor(budget * RECENT_FRACTION)
  const compactBudget = Math.floor(budget * COMPACT_FRACTION)

  const sessions = listBotSessionsBefore(opts.rlOpenclawDir, opts.botId, opts.beforeDate)
  if (!sessions.length) {
    return { markdown: '', dayCount: 0, recentDays: 0, compactedDays: 0, totalChars: 0 }
  }

  // 抽 digest（按时间升序），并配 day number（1-indexed）
  const digests: { dayNum: number; digest: DayDigest; rendered: string }[] = sessions.map((s, i) => {
    const digest = extractDayDigestFromJsonl(s.sessionFile, s.date)
    const rendered = renderDayDigest(digest, i + 1)
    return { dayNum: i + 1, digest, rendered }
  })

  // 从最新往前累加，直到达到 recentBudget。
  const recentBlocks: string[] = []
  let recentCharsUsed = 0
  let splitIdx = digests.length  // 即"前 splitIdx 个是 older，剩下 digests.length-splitIdx 个是 recent"
  for (let i = digests.length - 1; i >= 0; i--) {
    const len = digests[i].rendered.length + 2  // +2 for '\n\n' joiner
    if (recentBlocks.length > 0 && recentCharsUsed + len > recentBudget) break
    recentBlocks.unshift(digests[i].rendered)
    recentCharsUsed += len
    splitIdx = i
  }
  const olderDigests = digests.slice(0, splitIdx)
  const recentDays = digests.length - splitIdx

  // older 段处理（增量复用）：
  //   1. 没有 older → 直接跳过
  //   2. 有 state.json 且 compactedUpToDate 落在 older 范围内：
  //        olderBlock = state.compactText + (compactedUpToDate 之后的新 older 原文)
  //        - 拼起来 ≤ compactBudget → 直接用，无 LLM 调用 ✓ 这是绝大多数次日的常态
  //        - 否则 → LLM recompact(老 compact + 新增 raw)，刷新 state
  //   3. 冷启动 / state 失效（如人工删除）：
  //        - olderRaw ≤ compactBudget → 原样保留，不写 state（数据还没多到值得 compact）
  //        - 否则 → LLM 首次 compact，写 state
  let olderBlock = ''
  let compactedDays = 0
  if (olderDigests.length) {
    const lastOlderDate = olderDigests[olderDigests.length - 1].digest.date
    const firstOlderDate = olderDigests[0].digest.date
    const state = readCompactState(opts.rlOpenclawDir, opts.botId)
    // state 有效条件：cutoff 日期落在当前 older 段内（不能比第一天还早 → 中间会断档，不能比最后一天还晚 → 越界）
    const stateUsable = state
      && state.compactedUpToDate >= firstOlderDate
      && state.compactedUpToDate <= lastOlderDate
    if (stateUsable && state) {
      const cutoffIdx = olderDigests.findIndex(d => d.digest.date === state.compactedUpToDate)
      // 严格按日期匹配：找不到对应天（可能数据被回滚 / digest 重算变形）就走冷启动分支
      if (cutoffIdx >= 0) {
        const novelOlder = olderDigests.slice(cutoffIdx + 1)
        const novelRaw = novelOlder.map(d => d.rendered).join('\n\n')
        const candidate = novelRaw
          ? `${state.compactText}\n\n### 在此之后新增的 ${novelOlder.length} 个交易日（原样）\n\n${novelRaw}`
          : state.compactText
        compactedDays = cutoffIdx + 1  // state 涵盖的天数（不算新增的 raw 天）
        if (candidate.length <= compactBudget) {
          // 无需调 LLM —— 增量复用直接落地
          olderBlock = `> 以下为更早 ${olderDigests.length} 个交易日（${firstOlderDate} ～ ${lastOlderDate}）的历史笔记（${compactedDays} 天为 4 维度压缩，其余原样）：\n\n${candidate}`
        } else {
          // 老 compact + 新增 raw 超 budget → 再 compact 一次，把整段重新压扁
          let compactText: string
          if (opts.skipLlmCompact) {
            compactText = `（已 recompact ${olderDigests.length} 个交易日，截至 ${lastOlderDate}；LLM compact 已跳过）\n\n` + candidate.slice(0, compactBudget - 200)
          } else {
            const endpoint = resolveCompactEndpoint(opts)
            compactText = await compactHistory({
              endpoint,
              digestsMarkdown: candidate,
              targetChars: compactBudget - 200,
            })
          }
          writeCompactState(opts.rlOpenclawDir, opts.botId, { compactText, compactedUpToDate: lastOlderDate })
          compactedDays = olderDigests.length
          olderBlock = `> 以下为更早 ${olderDigests.length} 个交易日（${firstOlderDate} ～ ${lastOlderDate}）的 4 维度压缩笔记：\n\n${compactText}`
        }
      }
    }
    if (!olderBlock) {
      // 冷启动 / state 不可用
      const olderRaw = olderDigests.map(d => d.rendered).join('\n\n')
      if (olderRaw.length <= compactBudget) {
        olderBlock = olderRaw  // 还没多到需要 compact，原样
      } else {
        let compactText: string
        if (opts.skipLlmCompact) {
          compactText = `（已压缩 ${olderDigests.length} 个交易日，截至 ${lastOlderDate}；LLM compact 已跳过）\n\n` + olderRaw.slice(0, compactBudget - 200)
        } else {
          const endpoint = resolveCompactEndpoint(opts)
          compactText = await compactHistory({
            endpoint,
            digestsMarkdown: olderRaw,
            targetChars: compactBudget - 200,
          })
        }
        writeCompactState(opts.rlOpenclawDir, opts.botId, { compactText, compactedUpToDate: lastOlderDate })
        compactedDays = olderDigests.length
        olderBlock = `> 以下为更早 ${olderDigests.length} 个交易日（${firstOlderDate} ～ ${lastOlderDate}）的 4 维度压缩笔记：\n\n${compactText}`
      }
    }
  }

  const header = `【交易记忆窗口（最近 ${digests.length} 个交易日，已去除工具结果原文）】`
  const sections: string[] = [header]
  if (olderBlock) sections.push(olderBlock)
  if (recentBlocks.length) {
    sections.push(`### 最近 ${recentDays} 个交易日（原样保留）\n\n${recentBlocks.join('\n\n')}`)
  }
  const markdown = sections.join('\n\n')

  return {
    markdown,
    dayCount: digests.length,
    recentDays,
    compactedDays,
    totalChars: markdown.length,
  }
}
