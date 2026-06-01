import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { runDir, runStateFile } from './paths.ts'

export type RunStatus = 'setup' | 'running' | 'paused' | 'done' | 'failed' | 'aborted'

export interface WorldState {
  run_id: string
  status: RunStatus
  current_date: string
  trading_dates: string[]
  cursor: number
  bots: string[]
  memory_port: number
  started_at: string
  updated_at: string
  loop: 'research-loop' | 'openclaw-pi'
  /** PID of the world orchestrator process that owns this run. Stale across restarts
   *  if the process died without teardown — that's the signal external readers (e.g.
   *  the dashboard) use to detect orphan runs and self-heal them to 'aborted'. */
  pid?: number
  /** Set by self-heal when an orphan run is reclaimed. Plain string, audit-only. */
  aborted_reason?: string
}

export function stateExists(worldRoot: string, runId: string): boolean {
  return existsSync(runStateFile(worldRoot, runId))
}

export function writeState(worldRoot: string, runId: string, state: WorldState): void {
  const path = runStateFile(worldRoot, runId)
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.tmp`
  writeFileSync(tmp, JSON.stringify(state, null, 2) + '\n')
  renameSync(tmp, path)
}

export function readState(worldRoot: string, runId: string): WorldState {
  const path = runStateFile(worldRoot, runId)
  if (!existsSync(path)) throw new Error(`no state.json at ${path}`)
  const raw = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
  if (raw.loop !== 'research-loop' && raw.loop !== 'openclaw-pi') raw.loop = 'research-loop'
  return raw as unknown as WorldState
}

/** Scan runtime/runs/* and return every readable state.json, newest started_at first.
 *  Corrupt or missing state files are skipped. */
function scanRunStates(worldRoot: string): WorldState[] {
  const runsBase = dirname(runDir(worldRoot, '_')) // runtime/runs
  if (!existsSync(runsBase)) return []
  let entries: string[]
  try { entries = readdirSync(runsBase) } catch { return [] }
  const out: WorldState[] = []
  for (const runId of entries) {
    try { out.push(readState(worldRoot, runId)) } catch { /* missing or corrupt — skip */ }
  }
  out.sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
  return out
}

/** Only the running runs, newest first. */
export function listActiveRuns(worldRoot: string): WorldState[] {
  return scanRunStates(worldRoot).filter(s => s.status === 'running')
}

/** Runs a control panel can act on: running (pausable/stoppable) or paused (stoppable),
 *  newest first. */
export function listControllableRuns(worldRoot: string): WorldState[] {
  return scanRunStates(worldRoot).filter(s => s.status === 'running' || s.status === 'paused')
}
