import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, resolve } from 'node:path'
import type { WorldConfig } from './config.ts'
import { loadCalendar, computeTradingDates } from './calendar.ts'
import { mapWithConcurrency } from './concurrency.ts'
import { BotServer } from './botServer.ts'
import { buildShadowWorkspace } from './shadowWorkspace.ts'
import { renderDailyMessage } from './message.ts'
import { MemoryStore } from './memory-server/store.ts'
import { createMemoryServer, type MemoryServerHandle } from './memory-server/server.ts'
import { readState, writeState, type WorldState } from './state.ts'
import * as P from './paths.ts'

export type StartBotServer = (botId: string, argv: string[]) => Promise<BotServer>

export interface RunWorldOptions {
  worldRoot: string
  config: WorldConfig
  runId: string
  startBotServer?: StartBotServer
}

const SESSION_KEY = (runId: string): string => `trading-${runId}`
const JOURNAL_REL = 'memory/trading/journal.md'

export function botServerArgv(config: WorldConfig, botId: string, workspace: string, rlConfigPath: string): string[] {
  const serverEntry = join(config.researchLoopTs, 'server.ts')
  return [process.execPath, '--experimental-strip-types', serverEntry, '--bot-id', botId, '--workspace', workspace, '--config', rlConfigPath]
}

function log(worldRoot: string, runId: string, msg: string): void {
  const line = `${new Date().toISOString()} ${msg}\n`
  try { appendFileSync(P.runLogFile(worldRoot, runId), line) } catch { /* ignore */ }
  process.stdout.write(`[world ${runId}] ${msg}\n`)
}

function generateRlConfig(config: WorldConfig, worldRoot: string, runId: string, memoryUrl: string): void {
  let base: Record<string, unknown>
  try { base = JSON.parse(readFileSync(config.rlConfigBase, 'utf8')) as Record<string, unknown> }
  catch (err) { throw new Error(`cannot read rl_config_base ${config.rlConfigBase}: ${err instanceof Error ? err.message : String(err)}`) }
  const mcp = (typeof base.mcp === 'object' && base.mcp ? base.mcp : {}) as Record<string, unknown>
  mcp.mem0 = memoryUrl
  base.mcp = mcp
  base.openclaw_dir = config.rlOpenclawDir
  writeFileSync(P.runConfigFile(worldRoot, runId), JSON.stringify(base, null, 2) + '\n')
}

interface SetupResult {
  tradingDates: string[]
  memory: MemoryServerHandle
  bots: { botId: string; server: BotServer }[]
  currentDateRef: { value: string }
}

async function setup(opts: RunWorldOptions): Promise<SetupResult> {
  const { worldRoot, config, runId } = opts
  const startBotServer: StartBotServer = opts.startBotServer
    ?? ((botId, argv) => BotServer.start(botId, { argv, readyTimeoutMs: 60_000, onLog: (l) => process.stderr.write(l + '\n') }))

  // 交易日序列
  const cal = loadCalendar(config.calendar)
  const tradingDates = computeTradingDates(cal, config.replay.from, config.replay.to)

  // 校验数据齐全（fail-fast，在写 state 之前）
  const missing = tradingDates.filter(d => !existsSync(P.quotesFile(worldRoot, d)))
  if (missing.length) throw new Error(`missing quotes.json for ${missing.length} trading day(s): ${missing.slice(0, 5).join(', ')}${missing.length > 5 ? ', …' : ''}`)

  // 目录
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  mkdirSync(P.memoryDir(worldRoot, runId), { recursive: true })
  mkdirSync(P.workspacesDir(worldRoot, runId), { recursive: true })
  mkdirSync(config.rlOpenclawDir, { recursive: true })
  log(worldRoot, runId, `setup: ${tradingDates.length} trading days ${tradingDates[0]} .. ${tradingDates[tradingDates.length - 1]}, bots=[${config.bots.join(', ')}]`)

  // 记忆服务（进程内）
  const currentDateRef = { value: tradingDates[0] }
  const getCurrentDate = (): string => currentDateRef.value
  const store = new MemoryStore(P.memoryStoreFile(worldRoot, runId))
  const memory = await createMemoryServer({ store, getCurrentDate })
  writeFileSync(P.memoryRuntimeFile(worldRoot, runId), JSON.stringify({ port: memory.port, url: memory.url, collection: 'trading-memories' }, null, 2) + '\n')
  log(worldRoot, runId, `memory server at ${memory.url}`)

  // 生成 rl-config
  generateRlConfig(config, worldRoot, runId, memory.url)

  // 影子 workspace + bot server
  const bots: { botId: string; server: BotServer }[] = []
  try {
    for (const botId of config.bots) {
      const srcWs = isAbsolute(botId) ? botId : join(config.workspaceRoot, `workspace-${botId}`)
      if (!existsSync(srcWs)) throw new Error(`source workspace not found for ${botId}: ${srcWs}`)
      const shadow = P.shadowWorkspaceDir(worldRoot, runId, botId)
      buildShadowWorkspace({ sourceDir: srcWs, destDir: shadow, include: config.shadowInclude })
      const argv = botServerArgv(config, botId, shadow, P.runConfigFile(worldRoot, runId))
      const server = await startBotServer(botId, argv)
      bots.push({ botId, server })
      log(worldRoot, runId, `bot ${botId}: server ready`)
    }
  } catch (err) {
    // 启动阶段失败：关掉已起的 bot server + 记忆服务
    for (const b of bots) { try { await b.server.shutdown({ timeoutMs: 2000 }) } catch { /* ignore */ } }
    try { await memory.close() } catch { /* ignore */ }
    throw err
  }

  return { tradingDates, memory, bots, currentDateRef }
}

interface DayBotStatus { bot: string; status: 'ok' | 'error' | 'timeout' | 'dead'; iterations?: number; usage?: number; ms: number; error?: string }

async function chatOneBot(worldRoot: string, runId: string, date: string, message: string, perBotTimeoutMs: number, b: { botId: string; server: BotServer }): Promise<DayBotStatus> {
  const dir = P.botDayDir(worldRoot, runId, date, b.botId)
  mkdirSync(dir, { recursive: true })
  writeFileSync(P.sentFile(worldRoot, runId, date, b.botId), message)
  const startedAt = Date.now()
  const writeStatus = (s: DayBotStatus): void => writeFileSync(
    P.statusFile(worldRoot, runId, date, b.botId),
    JSON.stringify({ status: s.status, started_at: new Date(startedAt).toISOString(), finished_at: new Date().toISOString(), ...(typeof s.iterations === 'number' ? { iterations: s.iterations } : {}), ...(typeof s.usage === 'number' ? { usage: s.usage } : {}), ...(s.error ? { error: s.error } : {}) }, null, 2) + '\n',
  )
  if (!b.server.alive) { const s: DayBotStatus = { bot: b.botId, status: 'dead', ms: 0, error: 'server process not alive' }; writeStatus(s); return s }
  try {
    const r = await b.server.chat({ message, session_key: SESSION_KEY(runId), history: [] }, { timeoutMs: perBotTimeoutMs })
    writeFileSync(P.replyFile(worldRoot, runId, date, b.botId), JSON.stringify(r, null, 2) + '\n')
    const s: DayBotStatus = { bot: b.botId, status: 'ok', iterations: r.iterations, usage: r.usage, ms: Date.now() - startedAt }
    writeStatus(s)
    return s
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    const status: DayBotStatus['status'] = /timeout/i.test(msg) ? 'timeout' : !b.server.alive ? 'dead' : 'error'
    const s: DayBotStatus = { bot: b.botId, status, ms: Date.now() - startedAt, error: msg }
    writeStatus(s)
    return s
  }
}

/** Write artifacts for a bot whose chat we deliberately skipped (it was disabled by an earlier timeout/dead). */
function writeSkippedDeadBot(worldRoot: string, runId: string, date: string, message: string, b: { botId: string; server: BotServer }): DayBotStatus {
  const dir = P.botDayDir(worldRoot, runId, date, b.botId)
  mkdirSync(dir, { recursive: true })
  writeFileSync(P.sentFile(worldRoot, runId, date, b.botId), message)
  const now = new Date().toISOString()
  const s: DayBotStatus = { bot: b.botId, status: 'dead', ms: 0, error: 'disabled after a prior timeout/dead' }
  writeFileSync(P.statusFile(worldRoot, runId, date, b.botId), JSON.stringify({ status: s.status, started_at: now, finished_at: now, error: s.error }, null, 2) + '\n')
  return s
}

interface DaySummary { date: string; bots: DayBotStatus[] }

async function teardown(worldRoot: string, runId: string, setupRes: SetupResult, finalStatus: WorldState['status'], days: DaySummary[]): Promise<void> {
  for (const b of setupRes.bots) { try { await b.server.shutdown({ timeoutMs: 5000 }) } catch { /* ignore */ } }
  try { await setupRes.memory.close() } catch { /* ignore */ }
  writeFileSync(P.summaryFile(worldRoot, runId), JSON.stringify({ run_id: runId, status: finalStatus, days, finished_at: new Date().toISOString() }, null, 2) + '\n')
  let state: WorldState | undefined
  try { state = readState(worldRoot) } catch { /* state.json may not exist if setup never wrote it */ }
  if (state) {
    writeState(worldRoot, { ...state, status: finalStatus, updated_at: new Date().toISOString() })
    // 也把最终 state 复制进 run 目录存档
    writeFileSync(join(P.runDir(worldRoot, runId), 'state.json'), JSON.stringify({ ...state, status: finalStatus }, null, 2) + '\n')
  }
  log(worldRoot, runId, `teardown: status=${finalStatus}`)
}

export async function runWorld(opts: RunWorldOptions): Promise<void> {
  const { worldRoot, config, runId } = opts
  let setupRes: SetupResult
  try {
    setupRes = await setup(opts)
  } catch (err) {
    // 尽量记录 failed（state 可能还没建）
    try { if (existsSync(P.stateFile(worldRoot))) { const s = readState(worldRoot); writeState(worldRoot, { ...s, status: 'failed', updated_at: new Date().toISOString() }) } } catch { /* ignore */ }
    throw err
  }

  const initial: WorldState = {
    run_id: runId, status: 'running', current_date: setupRes.tradingDates[0],
    trading_dates: setupRes.tradingDates, cursor: 0, bots: config.bots,
    memory_port: setupRes.memory.port, started_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  }
  writeState(worldRoot, initial)

  await runLoop({ worldRoot, runId, config, setupRes, fromCursor: 0 })
}

interface RunLoopArgs { worldRoot: string; runId: string; config: WorldConfig; setupRes: SetupResult; fromCursor: number }

export async function runLoop(args: RunLoopArgs): Promise<void> {
  const { worldRoot, runId, config, setupRes, fromCursor } = args
  const dates = setupRes.tradingDates
  const days: DaySummary[] = []
  const perBotTimeoutMs = config.perBotTimeoutSeconds * 1000
  // 一旦某个 bot 在某天 timeout/dead，它的 server 可能还在处理上一天的请求（research-loop-ts 不串行化），
  // 之后的每一天都直接记 dead、不再向它发 chat。
  const brokenBots = new Set<string>()
  let aborted = false
  const onSigint = (): void => { aborted = true; log(worldRoot, runId, 'SIGINT received — will abort after current day') }
  process.on('SIGINT', onSigint)
  try {
    for (let cursor = fromCursor; cursor < dates.length; cursor++) {
      if (aborted || existsSync(P.stopFile(worldRoot, runId))) { log(worldRoot, runId, 'stop requested — aborting'); await teardown(worldRoot, runId, setupRes, 'aborted', days); return }
      const date = dates[cursor]
      setupRes.currentDateRef.value = date
      {
        const state = readState(worldRoot)
        writeState(worldRoot, { ...state, current_date: date, updated_at: new Date().toISOString() })
      }
      log(worldRoot, runId, `day ${cursor + 1}/${dates.length}: ${date} — sending to ${config.bots.length} bot(s)`)
      const isFirstDay = cursor === 0
      const quotesAbs = resolve(P.quotesFile(worldRoot, date))
      const statuses = await mapWithConcurrency(setupRes.bots, config.concurrency, async (b) => {
        const message = renderDailyMessage({ worldRoot, date, isFirstDay, quotesPath: quotesAbs, journalRelPath: JOURNAL_REL })
        if (brokenBots.has(b.botId)) return writeSkippedDeadBot(worldRoot, runId, date, message, b)
        return chatOneBot(worldRoot, runId, date, message, perBotTimeoutMs, b)
      })
      for (const s of statuses) { if (s.status === 'timeout' || s.status === 'dead') brokenBots.add(s.bot) }
      days.push({ date, bots: statuses })
      log(worldRoot, runId, `day ${date} done: ${statuses.map(s => `${s.bot}=${s.status}`).join(' ')}`)
      const st = readState(worldRoot)
      writeState(worldRoot, { ...st, cursor: cursor + 1, updated_at: new Date().toISOString() })
    }
    await teardown(worldRoot, runId, setupRes, 'done', days)
  } catch (err) {
    log(worldRoot, runId, `loop error: ${err instanceof Error ? err.message : String(err)}`)
    await teardown(worldRoot, runId, setupRes, 'failed', days)
    throw err
  } finally {
    process.off('SIGINT', onSigint)
  }
}

export interface ResumeWorldOptions {
  worldRoot: string
  config: WorldConfig
  startBotServer?: StartBotServer
}

export async function resumeWorld(opts: ResumeWorldOptions): Promise<void> {
  const { worldRoot, config } = opts
  const state = readState(worldRoot)
  if (state.status !== 'running') throw new Error(`cannot resume: state status is "${state.status}", nothing to resume`)
  // 清掉可能残留的 STOP 哨兵（否则 resume 会立刻被它中止）
  rmSync(P.stopFile(worldRoot, state.run_id), { force: true })
  // setup（重启记忆服务、重建/复用影子 workspace、重起 bot server），但 trading_dates 取自 state
  const setupRes = await setup({ worldRoot, config, runId: state.run_id, startBotServer: opts.startBotServer })
  // 若 calendar/replay 变了导致交易日序列对不上，拒绝
  if (setupRes.tradingDates.length !== state.trading_dates.length || setupRes.tradingDates[0] !== state.trading_dates[0] || setupRes.tradingDates[setupRes.tradingDates.length - 1] !== state.trading_dates[state.trading_dates.length - 1]) {
    for (const b of setupRes.bots) { try { await b.server.shutdown({ timeoutMs: 2000 }) } catch { /* ignore */ } }
    try { await setupRes.memory.close() } catch { /* ignore */ }
    throw new Error('resume: trading-date sequence changed since the run started; refuse to resume')
  }
  setupRes.currentDateRef.value = state.trading_dates[Math.min(state.cursor, state.trading_dates.length - 1)]
  await runLoop({ worldRoot, runId: state.run_id, config, setupRes, fromCursor: state.cursor })
}

/** 由独立的 `world stop` 进程调用：写一个 STOP 哨兵，正在跑的 runLoop 会在下一天开始前发现它。 */
export function requestStop(worldRoot: string): { ok: boolean; reason?: string } {
  if (!existsSync(P.stateFile(worldRoot))) return { ok: false, reason: 'no state.json' }
  const state = readState(worldRoot)
  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
  mkdirSync(P.runDir(worldRoot, state.run_id), { recursive: true })
  writeFileSync(P.stopFile(worldRoot, state.run_id), `requested at ${new Date().toISOString()}\n`)
  return { ok: true }
}
