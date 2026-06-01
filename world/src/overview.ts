import { existsSync, readFileSync } from 'node:fs'
import { overviewFile, quotesFile, eventsFile } from './paths.ts'

const FALLBACK = '（当日概览不可用：未提供 overview.md，且无法从 quotes.json 自动生成概览。请直接读取上面给出的 quotes.json 文件了解今日行情。）'

function readJson(path: string): unknown | undefined {
  if (!existsSync(path)) return undefined
  try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return undefined }
}

function eventsDigest(worldRoot: string, date: string): string {
  const raw = readJson(eventsFile(worldRoot, date))
  let list: unknown[] = []
  if (Array.isArray(raw)) list = raw
  else if (raw && typeof raw === 'object' && Array.isArray((raw as { events?: unknown[] }).events)) list = (raw as { events: unknown[] }).events
  const titles = list
    .map(e => (typeof e === 'string' ? e : e && typeof e === 'object' ? String((e as Record<string, unknown>).title ?? (e as Record<string, unknown>).name ?? '') : ''))
    .filter(Boolean)
    .slice(0, 8)
  return titles.length ? `\n\n今日要点/事件:\n${titles.map(t => `- ${t}`).join('\n')}` : ''
}

export function resolveOverview(worldRoot: string, date: string): string {
  const md = overviewFile(worldRoot, date)
  if (existsSync(md)) {
    const text = readFileSync(md, 'utf8').trim()
    if (text) return text
  }
  const quotes = readJson(quotesFile(worldRoot, date))
  let body = FALLBACK
  if (quotes && typeof quotes === 'object' && !Array.isArray(quotes)) {
    const q = quotes as Record<string, unknown>
    if (typeof q.summary === 'string' && q.summary.trim()) {
      body = q.summary.trim()
    } else if (Array.isArray(q.indices)) {
      const lines = (q.indices as unknown[])
        .map(it => (it && typeof it === 'object' ? `${String((it as Record<string, unknown>).name ?? '?')}: ${String((it as Record<string, unknown>).change_pct ?? (it as Record<string, unknown>).pct ?? '?')}` : ''))
        .filter(Boolean)
      if (lines.length) body = `主要指数:\n${lines.map(l => `- ${l}`).join('\n')}`
    }
  }
  return body + eventsDigest(worldRoot, date)
}
