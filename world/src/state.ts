import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { stateFile } from './paths.ts'

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
}

export function stateExists(worldRoot: string): boolean {
  return existsSync(stateFile(worldRoot))
}

export function writeState(worldRoot: string, state: WorldState): void {
  const path = stateFile(worldRoot)
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.tmp`
  writeFileSync(tmp, JSON.stringify(state, null, 2) + '\n')
  renameSync(tmp, path)
}

export function readState(worldRoot: string): WorldState {
  const path = stateFile(worldRoot)
  if (!existsSync(path)) throw new Error(`no state.json at ${path}`)
  return JSON.parse(readFileSync(path, 'utf8')) as WorldState
}

export function touchState(worldRoot: string, patch: Partial<WorldState>): WorldState {
  const cur = readState(worldRoot)
  const next: WorldState = { ...cur, ...patch, updated_at: new Date().toISOString() }
  writeState(worldRoot, next)
  return next
}
