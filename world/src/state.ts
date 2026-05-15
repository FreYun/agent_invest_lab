import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { runDir, runStateFile } from './paths.ts'

export type RunStatus = 'setup' | 'running' | 'done' | 'failed' | 'aborted'

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

/** Scan runtime/runs/* for state.json files and return only the running ones,
 *  sorted by started_at descending. Corrupt or missing state files are skipped. */
export function listActiveRuns(worldRoot: string): WorldState[] {
  const runsBase = dirname(runDir(worldRoot, '_')) // runtime/runs
  if (!existsSync(runsBase)) return []
  let entries: string[]
  try { entries = readdirSync(runsBase) } catch { return [] }
  const out: WorldState[] = []
  for (const runId of entries) {
    try {
      const st = readState(worldRoot, runId)
      if (st.status === 'running') out.push(st)
    } catch { /* missing or corrupt — skip */ }
  }
  out.sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
  return out
}
