import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { readState, writeState } from './state.ts'
import * as P from './paths.ts'

// Run lifecycle control: write the STOP / PAUSE sentinels (or flip terminal state) that a
// running runLoop reacts to. Kept in its own module — depending only on state + paths — so
// out-of-process callers (the CLI, the dashboard server) can trigger stop/pause without
// pulling in run.ts's heavy graph (bot servers, memory server, proxies). run.ts re-exports
// these for backwards-compatible imports.

/** 由独立的 `world stop` 进程调用。
 *  - running：写 STOP 哨兵，正在跑的 runLoop 会在下一天开始前发现它 → teardown 'aborted'。
 *  - paused：没有进程在跑、没人消费哨兵，直接把 state 翻成终态 'aborted'(取消一个暂停的 run)。 */
export function requestStop(worldRoot: string, runId: string): { ok: boolean; reason?: string } {
  if (!existsSync(P.runStateFile(worldRoot, runId))) return { ok: false, reason: `no state.json for run ${runId}` }
  const state = readState(worldRoot, runId)
  if (state.status === 'paused') {
    writeState(worldRoot, runId, { ...state, status: 'aborted', updated_at: new Date().toISOString() })
    return { ok: true }
  }
  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  writeFileSync(P.stopFile(worldRoot, runId), `requested at ${new Date().toISOString()}\n`)
  return { ok: true }
}

/** 由独立的 `world pause` 进程调用：写一个 PAUSE 哨兵，正在跑的 runLoop 的 poller 发现后会立即
 *  kill 当天 bot、停在 'paused'(可 resume，从该日重跑)。只接受 running——已 done/aborted/paused 的没有意义。 */
export function requestPause(worldRoot: string, runId: string): { ok: boolean; reason?: string } {
  if (!existsSync(P.runStateFile(worldRoot, runId))) return { ok: false, reason: `no state.json for run ${runId}` }
  const state = readState(worldRoot, runId)
  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  writeFileSync(P.pauseFile(worldRoot, runId), `requested at ${new Date().toISOString()}\n`)
  return { ok: true }
}
