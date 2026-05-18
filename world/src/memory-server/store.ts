import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { randomUUID } from 'node:crypto'
import { tokenize } from './tokenize.ts'
import { STRATEGY_MEM0_PREFIX } from '../message.ts'

export interface MemoryRecord {
  id: string
  agent_id: string | null
  user_id: string | null
  text: string
  created_at: string
  metadata?: Record<string, unknown>
}

export interface AddInput {
  text: string
  agent_id?: string | null
  user_id?: string | null
  created_at: string
  metadata?: Record<string, unknown>
}

export interface SearchHit {
  memory: string
  agent_id: string | null
  score: number
  created_at: string
  id: string
}

function tokenCounts(text: string): Map<string, number> {
  const m = new Map<string, number>()
  for (const t of tokenize(text)) m.set(t, (m.get(t) ?? 0) + 1)
  return m
}

export class MemoryStore {
  private readonly file: string
  private readonly records: MemoryRecord[] = []

  constructor(file: string) {
    this.file = file
    if (existsSync(file)) {
      for (const line of readFileSync(file, 'utf8').split('\n')) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try { this.records.push(JSON.parse(trimmed) as MemoryRecord) } catch { /* skip corrupt line */ }
      }
    } else {
      mkdirSync(dirname(file), { recursive: true })
    }
  }

  add(input: AddInput): MemoryRecord {
    const rec: MemoryRecord = {
      id: randomUUID(),
      agent_id: input.agent_id ?? null,
      user_id: input.user_id ?? null,
      text: input.text,
      created_at: input.created_at,
      ...(input.metadata ? { metadata: input.metadata } : {}),
    }
    appendFileSync(this.file, JSON.stringify(rec) + '\n')
    this.records.push(rec)
    return rec
  }

  /** 找 agent_id 下 text 以 prefix 开头的最新一条记录（按 created_at 降序，并列时取最后写入）。
   *  用途：world 在 Day 1 结束后抽取 bot 写的策略文档（约定 prefix `# MY_STRATEGY`）。
   *  未找到返回 null。 */
  findLatestByPrefix(agent_id: string, prefix: string): MemoryRecord | null {
    let best: MemoryRecord | null = null
    for (const rec of this.records) {
      if (rec.agent_id !== agent_id) continue
      if (!rec.text.startsWith(prefix)) continue
      if (!best || rec.created_at > best.created_at) best = rec
      // created_at 相同时后写的覆盖前写的（自然遍历顺序），无需额外比较
    }
    return best
  }

  search(query: string, opts: { agent_id?: string; limit: number }): SearchHit[] {
    const q = tokenize(query)
    if (q.length === 0) return []
    const qSet = new Set(q)
    const limit = Math.max(1, Math.min(opts.limit, 50))
    const scored: SearchHit[] = []
    for (const rec of this.records) {
      if (opts.agent_id !== undefined && rec.agent_id !== opts.agent_id) continue
      // 策略文档每天被 world 直接注回 prompt（strategyBlock），不该再出现在 mem0_search
      // 的 hit list 里——否则把真正"昨天的实际判断"挤出 limit。前缀同 message.ts 的契约。
      if (rec.text.startsWith(STRATEGY_MEM0_PREFIX)) continue
      const counts = tokenCounts(rec.text)
      let score = 0
      for (const tok of qSet) score += counts.get(tok) ?? 0
      if (score <= 0) continue
      scored.push({ memory: rec.text, agent_id: rec.agent_id, score, created_at: rec.created_at, id: rec.id })
    }
    scored.sort((a, b) => b.score - a.score || (b.created_at < a.created_at ? -1 : b.created_at > a.created_at ? 1 : a.id < b.id ? -1 : 1))
    return scored.slice(0, limit)
  }
}
