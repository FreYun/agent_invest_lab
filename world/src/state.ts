import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { runDir, runStateFile } from './paths.ts'
import type { DeepResearchCommitmentsByBot } from './deep-research-commitment.ts'

export type RunStatus = 'setup' | 'running' | 'paused' | 'done' | 'failed' | 'aborted'

export interface WorldState {
  run_id: string
  /** live run 的源回测 run。它既是 dashboard 血缘，也是历史窗口/workspace 续跑的真相源。 */
  source_run_id?: string
  status: RunStatus
  current_date: string
  trading_dates: string[]
  cursor: number
  bots: string[]
  memory_port: number
  started_at: string
  updated_at: string
  loop: 'research-loop' | 'openclaw-pi'
  /** 决策节奏：bot 在哪些交易日被真正唤起。'trading_days'=每个交易日（或按 chatStepDays 取模）；
   *  'weekly'=每周首个交易日；'monthly'=每月首个交易日。dashboard 据此在进度旁标注「日度/周度/月度」，
   *  避免把「逐日推进的结算游标 cursor/total」误读成「天天交易」。可选：老 run 的 state.json 无此字段。 */
  chat_step_mode?: 'trading_days' | 'weekly' | 'monthly'
  /** trading_days 模式的交易日步长（每 N 个交易日决策一次，1=日度）。 */
  chat_step_days?: number
  /** weekly 模式的目标周几（1=周一…5=周五）；缺省=周首交易日。 */
  chat_weekday?: number
  /** monthly 模式的目标交易日序号（正=从月初、负=从月末倒数，如 -1=月末）；缺省=1（月初）。 */
  chat_monthly_nth?: number
  /** 最近一次 bot 实际发起 start_research 的日期（ISO yyyy-mm-dd）。仅 agent-triggered
   *  模式使用：run.ts 用它计算 gapDays，判定今日是 forced/authorized；chat 后若 tool_trace
   *  含 start_research，则次日 writeState 时更新为当日日期。null/undefined = 从未深研。 */
  last_deep_research_date?: string
  /** 每个 bot 最近一次实际发起深研的日期。老 run 缺失时回退 last_deep_research_date。 */
  last_deep_research_dates?: Record<string, string>
  /** 账户当前回撤是否已处于阈值内；用于把持续回撤电平转换成一次性的阈值穿越事件。 */
  deep_research_drawdown_active_by_bot?: Record<string, boolean>
  /** 深研日成功买入形成的跨日承诺；普通日卖单由 proxy 按此状态拦截。 */
  deep_research_commitments_by_bot?: DeepResearchCommitmentsByBot
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
