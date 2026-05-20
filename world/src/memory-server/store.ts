import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { randomUUID } from 'node:crypto'
import { tokenize } from './tokenize.ts'

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

export interface SearchOptions {
  agent_id?: string
  limit: number
  // Inclusive YYYY-MM-DD bounds on created_at. Records lacking a parseable
  // created_at are dropped when either bound is set.
  start_date?: string
  end_date?: string
  // Reference "today" (YYYY-MM-DD) used to compute recency decay. Pass the
  // simulated world date so retrieval is PIT-consistent.
  now?: string
  // Decay τ in days for score *= exp(-Δdays/τ). 0 disables decay; default 30
  // gently favors recent records without erasing the long tail (e.g. 30-day
  // record retains ~0.37 of its raw TF score).
  recency_tau_days?: number
}

const DEFAULT_RECENCY_TAU_DAYS = 30

function tokenCounts(text: string): Map<string, number> {
  const m = new Map<string, number>()
  for (const t of tokenize(text)) m.set(t, (m.get(t) ?? 0) + 1)
  return m
}

function parseDayMs(s: string | undefined | null): number | null {
  if (!s) return null
  const t = Date.parse(s + 'T00:00:00Z')
  return Number.isFinite(t) ? t : null
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

  search(query: string, opts: SearchOptions): SearchHit[] {
    const q = tokenize(query)
    if (q.length === 0) return []
    const qSet = new Set(q)
    const limit = Math.max(1, Math.min(opts.limit, 50))
    const startMs = parseDayMs(opts.start_date)
    const endMs = parseDayMs(opts.end_date)
    const nowMs = parseDayMs(opts.now)
    const tau = opts.recency_tau_days ?? DEFAULT_RECENCY_TAU_DAYS
    const decayEnabled = tau > 0 && nowMs !== null
    const scored: SearchHit[] = []
    for (const rec of this.records) {
      if (opts.agent_id !== undefined && rec.agent_id !== opts.agent_id) continue
      const createdMs = parseDayMs(rec.created_at)
      if (startMs !== null && (createdMs === null || createdMs < startMs)) continue
      if (endMs !== null && (createdMs === null || createdMs > endMs)) continue
      const counts = tokenCounts(rec.text)
      let score = 0
      for (const tok of qSet) score += counts.get(tok) ?? 0
      if (score <= 0) continue
      if (decayEnabled && createdMs !== null) {
        const dDays = Math.max(0, Math.round((nowMs! - createdMs) / 86_400_000))
        score *= Math.exp(-dDays / tau)
      }
      scored.push({ memory: rec.text, agent_id: rec.agent_id, score, created_at: rec.created_at, id: rec.id })
    }
    scored.sort((a, b) => b.score - a.score || (b.created_at < a.created_at ? -1 : b.created_at > a.created_at ? 1 : a.id < b.id ? -1 : 1))
    return scored.slice(0, limit)
  }
}
