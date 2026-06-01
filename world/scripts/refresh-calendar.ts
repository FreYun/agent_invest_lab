#!/usr/bin/env -S node --experimental-strip-types
// Refresh runtime/calendar.json to the latest trading day SimWorld actually has
// data for. The world's calendar is the hard ceiling for any replay run
// (computeTradingDates only returns days that exist in it) AND the source the
// dashboard's 新建回测 modal reads for its 结束日期 default. Rather than freezing a
// date by hand, this pulls the authoritative trading days from the SimWorld
// upstream (the same data the world replays) and appends any new ones.
//
// Idempotent: re-running when already current is a no-op. Safe to schedule
// (see world-calendar-refresh.{service,timer}); a backend hiccup logs a warning
// and exits 0 without touching the calendar, so the timer never corrupts it.
//
//   node --experimental-strip-types scripts/refresh-calendar.ts [--dry-run]
//
// "latest available" = newest day SimWorld returns a HS300 quote for (quotes are
// "15:00 收盘后可见"), so the calendar tracks data availability, not tushare's
// forward-looking calendar.

import { readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse as parseYaml } from 'yaml'

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const DEFAULT_UPSTREAM = 'http://127.0.0.1:18078/mcp'

const worldRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const configPath = join(worldRoot, 'config', 'world.yaml')
const dryRun = process.argv.includes('--dry-run')

function log(msg: string): void { process.stdout.write(`[refresh-calendar] ${msg}\n`) }
function warn(msg: string): void { process.stderr.write(`[refresh-calendar] ${msg}\n`) }

/** Read simworld upstream URL + calendar path from world.yaml (with sane defaults). */
function readWorldConfig(): { upstreamUrl: string; calendarPath: string } {
  let raw: Record<string, unknown> = {}
  try { raw = (parseYaml(readFileSync(configPath, 'utf8')) ?? {}) as Record<string, unknown> }
  catch (e) { warn(`could not read ${configPath} (${(e as Error).message}); using defaults`) }
  const upstreamUrl = typeof raw.simworld_upstream_url === 'string' ? raw.simworld_upstream_url : DEFAULT_UPSTREAM
  const calRel = typeof raw.calendar === 'string' ? raw.calendar : '../runtime/calendar.json'
  // world.yaml's `calendar` is relative to the config dir (matches loadWorldConfig).
  const calendarPath = isAbsolute(calRel) ? calRel : resolve(dirname(configPath), calRel)
  return { upstreamUrl, calendarPath }
}

function pad2(n: number): string { return String(n).padStart(2, '0') }
function isoDay(d: Date): string { return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}` }

/** One MCP tools/call over streamable-http; tolerates SSE (`data:` lines) or plain JSON. */
async function mcpCall(upstreamUrl: string, name: string, args: Record<string, unknown>): Promise<unknown> {
  const res = await fetch(upstreamUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }),
    signal: AbortSignal.timeout(30_000),
  })
  if (!res.ok) throw new Error(`upstream HTTP ${res.status}`)
  const text = await res.text()
  const dataLines = text.split(/\r?\n/).filter(l => l.startsWith('data:'))
  const payload = dataLines.length ? dataLines[dataLines.length - 1].slice(5).trim() : text.trim()
  const env = JSON.parse(payload) as { result?: { structuredContent?: unknown; content?: Array<{ text?: string }> }; error?: { message?: string } }
  if (env.error) throw new Error(`MCP error: ${env.error.message ?? 'unknown'}`)
  const result = env.result ?? {}
  if (result.structuredContent !== undefined) return result.structuredContent
  const inner = result.content?.[0]?.text
  if (typeof inner === 'string') return JSON.parse(inner)
  throw new Error('MCP result missing structuredContent/content')
}

/** Trading days SimWorld has HS300 quotes for in [from, to] (PIT, T set past `to`). */
async function fetchTradingDays(upstreamUrl: string, from: string, to: string): Promise<string[]> {
  const sc = await mcpCall(upstreamUrl, 'market_index_quote', {
    market: 'cn',
    symbols: ['沪深300'],
    simulated_datetime: `${to} 23:00:00`,
    start_date: from,
    end_date: to,
  }) as { success?: boolean; items?: Array<{ 是否可用?: boolean; 行情记录?: Array<{ 日期?: string }> }> }
  const item = sc.items?.[0]
  if (!item?.行情记录) return []
  const days = item.行情记录
    .map(r => (typeof r.日期 === 'string' ? r.日期.slice(0, 10) : ''))
    .filter(d => ISO_DATE.test(d))
  return [...new Set(days)].sort()
}

function atomicWriteJson(path: string, value: unknown): void {
  const tmp = `${path}.tmp`
  writeFileSync(tmp, JSON.stringify(value, null, 2) + '\n')
  renameSync(tmp, path)
}

async function main(): Promise<number> {
  const { upstreamUrl, calendarPath } = readWorldConfig()

  let cal: { trading_days?: unknown }
  try { cal = JSON.parse(readFileSync(calendarPath, 'utf8')) }
  catch (e) { warn(`cannot read calendar ${calendarPath}: ${(e as Error).message}`); return 1 }
  const days = Array.isArray(cal.trading_days) ? (cal.trading_days as unknown[]).filter((d): d is string => typeof d === 'string' && ISO_DATE.test(d)) : []
  if (!days.length) { warn(`calendar ${calendarPath} has no valid trading_days; refusing to touch it`); return 1 }
  const lastDay = days[days.length - 1]

  // Query from the current last day through a week past today — wide enough to
  // capture every new day regardless of when this runs; the backend only ever
  // returns days it actually has, so a future end_date just clamps to reality.
  const horizon = new Date(); horizon.setDate(horizon.getDate() + 7)
  const to = isoDay(horizon)

  let fetched: string[]
  try { fetched = await fetchTradingDays(upstreamUrl, lastDay, to) }
  catch (e) { warn(`SimWorld query failed (${(e as Error).message}); calendar left unchanged`); return 0 }

  const existing = new Set(days)
  const toAppend = fetched.filter(d => d > lastDay && !existing.has(d))
  if (!toAppend.length) { log(`already current — last trading day ${lastDay} (SimWorld latest matches)`); return 0 }

  const merged = [...days, ...toAppend]
  for (let i = 1; i < merged.length; i++) {
    if (merged[i] <= merged[i - 1]) { warn(`merge produced non-ascending dates near ${merged[i]}; aborting`); return 1 }
  }

  if (dryRun) { log(`[dry-run] would append ${toAppend.length}: ${toAppend.join(', ')} (→ ${merged[merged.length - 1]})`); return 0 }
  atomicWriteJson(calendarPath, { ...cal, trading_days: merged })
  log(`appended ${toAppend.length}: ${toAppend.join(', ')} | calendar now ${merged.length} days, last = ${merged[merged.length - 1]}`)
  return 0
}

main().then(code => { process.exitCode = code }).catch(err => { warn(err instanceof Error ? err.stack ?? err.message : String(err)); process.exitCode = 1 })
