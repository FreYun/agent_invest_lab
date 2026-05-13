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
  loop: 'research-loop' | 'openclaw-pi'
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
  const raw = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
  // 向后兼容：旧 state 没有 loop 字段，默认 research-loop
  if (raw.loop !== 'research-loop' && raw.loop !== 'openclaw-pi') raw.loop = 'research-loop'
  return raw as unknown as WorldState
}
