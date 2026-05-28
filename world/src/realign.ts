import { existsSync, readdirSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import * as P from './paths.ts'
import { readState, writeState, type RunStatus } from './state.ts'

// Realign a run's world-side per-day dirs with its openclaw session jsonls.
// World side is authoritative: each <runId>/YYYY-MM-DD/<bot>/ dir is the world
// orchestrator's "this day actually ran" receipt. The openclaw session jsonls
// under <runId>/rl-openclaw/agents/<bot>/sessions/ are owned by the bot's
// research-loop server and only happen to share the same lifecycle.
//
// The two can drift when:
//   - someone manually rm's per-day dirs to drop garbage days (the case that
//     prompted this — bot timed out for 50+ days and left tiny error stubs);
//   - the orchestrator crashes mid-write (the ENOENT-on-status.json story);
//   - state.json gets hand-edited to a different cursor than the disk reality.
//
// After drift, the dashboard's per-bot row still claims "N 天" based on the
// openclaw jsonl count, and the right-pane Day list shows world days that no
// longer have any world-side artifacts. realignRunDates trims the openclaw
// side to match the world side and pulls state.json's cursor/current_date back
// into agreement so the dashboard, summary, and resume all see the same shape.

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

export interface RealignOptions {
  /** Report what would change but touch nothing on disk. */
  dryRun?: boolean
  /** Leave state.json and summary.json alone; only sync the openclaw side. */
  preserveState?: boolean
}

export interface RealignBotReport {
  botId: string
  keptSessionKeys: string[]
  removedSessionKeys: string[]
  removedJsonlPaths: string[]
}

export interface RealignReport {
  runId: string
  worldDates: string[]
  perBot: RealignBotReport[]
  stateBefore: { status: RunStatus; cursor: number; current_date: string; aborted_reason?: string }
  stateAfter:  { status: RunStatus; cursor: number; current_date: string; aborted_reason?: string }
  summaryRemoved: boolean
}

interface SessionEntry { sessionId?: unknown; sessionFile?: unknown; [k: string]: unknown }

/** session_key shape: `agent:<bot>:trading-<runId>-<YYYY-MM-DD>`. Returns the date or null
 *  (the key is for some other run, a non-trading chat, or otherwise unrelated). */
function trailingTradingDate(key: string, runId: string, botId: string): string | null {
  const prefix = `agent:${botId}:trading-${runId}-`
  if (!key.startsWith(prefix)) return null
  const tail = key.slice(prefix.length)
  return DATE_RE.test(tail) ? tail : null
}

/** True iff `name` is a YYYY-MM-DD subdir under `runDir`. */
function isDateDir(runDir: string, name: string): boolean {
  if (!DATE_RE.test(name)) return false
  try { return statSync(join(runDir, name)).isDirectory() }
  catch { return false }
}

function atomicWriteJson(path: string, value: unknown): void {
  const tmp = `${path}.tmp`
  writeFileSync(tmp, JSON.stringify(value, null, 2) + '\n')
  renameSync(tmp, path)
}

export function realignRunDates(worldRoot: string, runId: string, opts: RealignOptions = {}): RealignReport {
  const runDir = P.runDir(worldRoot, runId)
  if (!existsSync(runDir)) throw new Error(`no run dir at ${runDir}`)
  const state = readState(worldRoot, runId)

  const worldDates = readdirSync(runDir).filter(n => isDateDir(runDir, n)).sort()
  const worldDateSet = new Set(worldDates)

  const perBot: RealignBotReport[] = []
  const agentsDir = join(P.rlOpenclawDir(worldRoot, runId), 'agents')
  if (existsSync(agentsDir)) {
    const botDirs = readdirSync(agentsDir).filter(name => {
      try { return statSync(join(agentsDir, name)).isDirectory() }
      catch { return false }
    })
    for (const botId of botDirs) {
      const sessFile = join(agentsDir, botId, 'sessions', 'sessions.json')
      if (!existsSync(sessFile)) continue
      let parsed: unknown
      try { parsed = JSON.parse(readFileSync(sessFile, 'utf8')) }
      catch { continue } // corrupt — leave it alone, manual fix
      // sessions.json can be {key: entry} or [entry] in different openclaw versions
      // (dashboard handles both at server.js:1301). We only rewrite the object form,
      // since that's what research-loop writes; array form is left untouched.
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) continue
      const map = parsed as Record<string, SessionEntry>

      const kept: string[] = []
      const removedKeys: string[] = []
      const removedJsonl: string[] = []
      const next: Record<string, SessionEntry> = {}
      for (const [key, value] of Object.entries(map)) {
        const date = trailingTradingDate(key, runId, botId)
        if (date === null || worldDateSet.has(date)) {
          next[key] = value
          kept.push(key)
          continue
        }
        // Orphan trading-day session: file deletions + drop from sessions.json.
        removedKeys.push(key)
        const sid = typeof value.sessionId === 'string' ? value.sessionId : ''
        const jsonl = typeof value.sessionFile === 'string' && value.sessionFile
          ? value.sessionFile
          : (sid ? join(dirname(sessFile), `${sid}.jsonl`) : '')
        if (jsonl) removedJsonl.push(jsonl)
        if (!opts.dryRun) {
          if (jsonl && existsSync(jsonl)) { try { unlinkSync(jsonl) } catch { /* best-effort */ } }
          const sideState = sid ? join(dirname(sessFile), `${sid}.state.json`) : ''
          if (sideState && existsSync(sideState)) { try { unlinkSync(sideState) } catch { /* best-effort */ } }
        }
      }
      if (!opts.dryRun && removedKeys.length) atomicWriteJson(sessFile, next)
      perBot.push({ botId, keptSessionKeys: kept, removedSessionKeys: removedKeys, removedJsonlPaths: removedJsonl })
    }
  }

  // state.json: cursor = first trading_dates index whose world per-day dir is gone.
  // current_date follows cursor; status flips to 'paused' so `world resume` re-runs
  // that day. Already-terminal runs (done/aborted/failed) keep their status —
  // realigning a finished run shouldn't accidentally revive it.
  let newCursor = 0
  for (; newCursor < state.trading_dates.length; newCursor++) {
    if (!worldDateSet.has(state.trading_dates[newCursor])) break
  }
  const beforeStateSlice = { status: state.status, cursor: state.cursor, current_date: state.current_date, aborted_reason: state.aborted_reason }
  let afterStateSlice = { ...beforeStateSlice }
  let summaryRemoved = false
  if (!opts.preserveState) {
    const lastIdx = state.trading_dates.length - 1
    const newCurrentDate = state.trading_dates[Math.min(newCursor, lastIdx)] ?? state.current_date
    // Terminal statuses don't flip back; an in-flight run (running/paused/setup) becomes paused.
    const resumable: RunStatus[] = ['running', 'paused', 'setup']
    const newStatus: RunStatus = resumable.includes(state.status) ? 'paused' : state.status
    afterStateSlice = { status: newStatus, cursor: newCursor, current_date: newCurrentDate, aborted_reason: undefined }
    if (!opts.dryRun) {
      const { aborted_reason: _drop, ...rest } = state
      writeState(worldRoot, runId, { ...rest, status: newStatus, cursor: newCursor, current_date: newCurrentDate, updated_at: new Date().toISOString() })
    }
    const summaryPath = P.summaryFile(worldRoot, runId)
    if (existsSync(summaryPath)) {
      summaryRemoved = true
      if (!opts.dryRun) { try { unlinkSync(summaryPath) } catch { /* best-effort */ } }
    }
  }

  return {
    runId,
    worldDates,
    perBot,
    stateBefore: beforeStateSlice as RealignReport['stateBefore'],
    stateAfter: afterStateSlice as RealignReport['stateAfter'],
    summaryRemoved,
  }
}

/** Human-readable single-screen summary. The CLI prints this; programmatic
 *  callers can ignore it and inspect the structured report directly. */
export function formatRealignReport(r: RealignReport, opts: { dryRun: boolean }): string {
  const tag = opts.dryRun ? '[dry-run] ' : ''
  const lines: string[] = []
  lines.push(`${tag}realign ${r.runId}`)
  lines.push(`world per-day dirs: ${r.worldDates.length}${r.worldDates.length ? ` (${r.worldDates[0]} … ${r.worldDates[r.worldDates.length - 1]})` : ''}`)
  let totalRemoved = 0
  for (const b of r.perBot) {
    totalRemoved += b.removedSessionKeys.length
    if (b.removedSessionKeys.length === 0) {
      lines.push(`  ${b.botId}: ${b.keptSessionKeys.length} sessions kept (in sync)`)
    } else {
      lines.push(`  ${b.botId}: kept ${b.keptSessionKeys.length}, removed ${b.removedSessionKeys.length} orphan session(s)`)
      const sample = b.removedSessionKeys.slice(0, 5).map(k => `      - ${k}`).join('\n')
      lines.push(sample)
      if (b.removedSessionKeys.length > 5) lines.push(`      … ${b.removedSessionKeys.length - 5} more`)
    }
  }
  const sB = r.stateBefore, sA = r.stateAfter
  const changed = sB.status !== sA.status || sB.cursor !== sA.cursor || sB.current_date !== sA.current_date || sB.aborted_reason !== sA.aborted_reason
  if (!changed) {
    lines.push(`state.json: unchanged (status=${sA.status}, cursor=${sA.cursor}/${sB.cursor}, date=${sA.current_date})`)
  } else {
    lines.push(`state.json: status ${sB.status} → ${sA.status}, cursor ${sB.cursor} → ${sA.cursor}, current_date ${sB.current_date} → ${sA.current_date}${sB.aborted_reason ? ` (dropped aborted_reason: ${sB.aborted_reason})` : ''}`)
  }
  if (r.summaryRemoved) lines.push(`summary.json: removed (was stale after realign)`)
  lines.push(`total orphan sessions ${opts.dryRun ? 'to remove' : 'removed'}: ${totalRemoved}`)
  return lines.join('\n') + '\n'
}

