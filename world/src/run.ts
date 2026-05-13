import { appendFileSync, copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
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

/** 选 openclaw.json 源路径。当前两种 loop 都用同一个 credentials 文件。 */
export function openclawJsonSource(config: WorldConfig): string {
  return config.openclawJson
}

/** 选 loop server 的配置文件路径：research-loop 用生成的 trading-rl-config.json；pi 直接用 rl-openclaw/openclaw.json 副本（带 mcp.mem0 patch）。 */
export function loopConfigPath(config: WorldConfig, worldRoot: string, runId: string): string {
  if (config.loop === 'openclaw-pi') return join(P.rlOpenclawDir(worldRoot, runId), 'openclaw.json')
  return P.runConfigFile(worldRoot, runId)
}

/** openclaw-pi loop：如果 rl-openclaw/openclaw.json 已经有 mcp.mem0 字段，把它改写为本 run 的 memory URL；
 *  否则不动（openclaw 的 config 校验对未知 mcp 子键会拒，所以不能凭空塞入）。 */
export function patchPiOpenclawJsonMemory(rlOpenclawDir: string, memoryUrl: string): void {
  const p = join(rlOpenclawDir, 'openclaw.json')
  let cfg: Record<string, unknown>
  try { cfg = JSON.parse(readFileSync(p, 'utf8')) as Record<string, unknown> }
  catch (err) { throw new Error(`cannot read ${p}: ${err instanceof Error ? err.message : String(err)}`) }
  if (typeof cfg.mcp !== 'object' || !cfg.mcp) return
  const mcp = cfg.mcp as Record<string, unknown>
  if (!('mem0' in mcp)) return
  mcp.mem0 = memoryUrl
  writeFileSync(p, JSON.stringify(cfg, null, 2) + '\n')
}

export function botServerArgv(config: WorldConfig, botId: string, workspace: string, loopConfigPath: string): string[] {
  if (config.loop === 'openclaw-pi') {
    if (!config.piServerEntry) throw new Error('botServerArgv: piServerEntry required for openclaw-pi loop')
    if (!config.openclawRoot) throw new Error('botServerArgv: openclawRoot required for openclaw-pi loop')
    // pi-server imports openclaw internals with .js specifiers pointing at .ts files;
    // --experimental-strip-types doesn't rewrite .js → .ts, so use tsx loader via absolute path
    // (avoids depending on the spawn cwd having tsx in node_modules).
    const tsxLoader = join(config.openclawRoot, 'node_modules/tsx/dist/loader.mjs')
    return [process.execPath, '--import', `file://${tsxLoader}`, config.piServerEntry, '--bot-id', botId, '--workspace', workspace, '--openclaw-json', loopConfigPath]
  }
  const serverEntry = join(config.researchLoop, 'server.ts')
  return [process.execPath, '--experimental-strip-types', serverEntry, '--bot-id', botId, '--workspace', workspace, '--config', loopConfigPath]
}

/** openclaw-pi loop：从真实 ~/.openclaw/agents/<botId>/agent/ 把 auth-profiles / auth-state / models.json
 *  种子拷贝到 piSessionsDir/<botId>/agent/。已存在的不动（"seed once"），让 lab 的 agent state 后续与 openclaw
 *  脱钩演化。配合 spawn 时 env OPENCLAW_AGENTS_DIR=<piSessionsDir>，pi 把所有读写都改道到 lab 的隔离树里，
 *  不污染真实 .openclaw/agents/。 */
export function seedPiAgentBot(piSessionsDir: string, sourceAgentsDir: string, botId: string): void {
  const dest = join(piSessionsDir, botId, 'agent')
  if (existsSync(join(dest, 'auth-profiles.json'))) return
  mkdirSync(dest, { recursive: true })
  const src = join(sourceAgentsDir, botId, 'agent')
  for (const f of ['auth-profiles.json', 'auth-state.json', 'models.json']) {
    const s = join(src, f)
    if (existsSync(s)) {
      try { copyFileSync(s, join(dest, f)) } catch { /* best-effort */ }
    }
  }
}

function log(worldRoot: string, runId: string, msg: string): void {
  const line = `${new Date().toISOString()} ${msg}\n`
  try { appendFileSync(P.runLogFile(worldRoot, runId), line) } catch { /* ignore */ }
  process.stdout.write(`[world ${runId}] ${msg}\n`)
}

function generateRlConfig(config: WorldConfig, worldRoot: string, runId: string, memoryUrl: string, openclawDir: string): void {
  let base: Record<string, unknown>
  try { base = JSON.parse(readFileSync(config.rlConfigBase, 'utf8')) as Record<string, unknown> }
  catch (err) { throw new Error(`cannot read rl_config_base ${config.rlConfigBase}: ${err instanceof Error ? err.message : String(err)}`) }
  const mcp = (typeof base.mcp === 'object' && base.mcp ? base.mcp : {}) as Record<string, unknown>
  mcp.mem0 = memoryUrl
  base.mcp = mcp
  base.openclaw_dir = openclawDir
  base.extra_roots = [config.skillsRoot]
  // 让 rl-ts 从 run 专属 rl-openclaw/openclaw.json 里读模型 API key，
  // 而不是它默认写死的 /home/rooot/.openclaw/openclaw.json
  base.openclaw_json_path = join(openclawDir, 'openclaw.json')
  writeFileSync(P.runConfigFile(worldRoot, runId), JSON.stringify(base, null, 2) + '\n')
}

interface SetupResult {
  tradingDates: string[]
  memory: MemoryServerHandle
  bots: { botId: string; server: BotServer }[]
  currentDateRef: { value: string }
}

function formatBotNotification(botId: string, method: string, params: Record<string, unknown>): string | null {
  if (method === 'research.started') {
    const topic = typeof params.topic === 'string' ? params.topic.slice(0, 48) : ''
    return `bot ${botId}: research started${topic ? ` (${topic}${topic.length === 48 ? '…' : ''})` : ''}`
  }
  if (method === 'research.progress') {
    const event = typeof params.event === 'string' ? params.event : ''
    if (event === 'phase') {
      const phase = typeof params.phase === 'string' ? params.phase : 'unknown'
      return `bot ${botId}: phase=${phase}`
    }
    if (event === 'nudge') {
      const text = typeof params.text === 'string' ? params.text.slice(0, 80) : ''
      return `bot ${botId}: ${text || 'progress update'}`
    }
    if (event === 'done') return `bot ${botId}: research completed`
  }
  if (method === 'tool.call') {
    const name = typeof params.name === 'string' ? params.name : 'unknown'
    return `bot ${botId}: tool ${name}`
  }
  if (method === 'tool.result') {
    const name = typeof params.name === 'string' ? params.name : 'unknown'
    const isError = Boolean(params.is_error)
    return `bot ${botId}: tool ${name} ${isError ? 'error' : 'ok'}`
  }
  if (method === 'message.done') {
    const text = typeof params.text === 'string' ? params.text.slice(0, 80) : ''
    return `bot ${botId}: reply ready${text ? ` (${text}${text.length === 80 ? '…' : ''})` : ''}`
  }
  if (method === 'log' && params.level === 'error') {
    const message = typeof params.message === 'string' ? params.message : 'unknown error'
    return `bot ${botId}: error ${message}`
  }
  return null
}

async function setup(opts: RunWorldOptions): Promise<SetupResult> {
  const { worldRoot, config, runId } = opts
  // pi-server needs to be spawned with cwd=openclawRoot so tsx's tsconfig.json lookup picks up
  // openclaw's path aliases (openclaw/plugin-sdk/*). research-loop doesn't need this.
  const spawnCwd = config.loop === 'openclaw-pi' ? config.openclawRoot : undefined
  // pi reads/writes the agent dir from $OPENCLAW_AGENT_DIR (auth-profiles, models.json, sessions).
  // Point it at the per-bot lab-isolated dir so reads use seeded auth and writes (new session jsonl) land
  // under piSessionsDir, never touching the real ~/.openclaw/agents/. PI_CODING_AGENT_DIR is the equivalent
  // env that pi-coding-agent SDK reads — set both for belt-and-suspenders.
  const piAgentDirFor = (botId: string): string | undefined =>
    config.loop === 'openclaw-pi' && config.piSessionsDir
      ? join(config.piSessionsDir, botId, 'agent')
      : undefined
  const startBotServer: StartBotServer = opts.startBotServer
    ?? ((botId, argv) => {
      const agentDir = piAgentDirFor(botId)
      // OPENCLAW_AGENT_DIR / PI_CODING_AGENT_DIR redirect auth + per-agent files.
      // OPENCLAW_STATE_DIR redirects openclaw's state tree (where session jsonl + sessions.json land):
      // pi writes to <STATE_DIR>/agents/<defaultAgentId>/sessions/<UUID>.jsonl. Point it at piSessionsDir
      // and arrange the structure so the lab tree mirrors openclaw's expected layout
      // (<piSessionsDir>/agents/<id>/sessions/...) — but the user-facing layout the user wants is
      // <piSessionsDir>/<botId>/sessions/... so we pass piSessionsDir as STATE_DIR and let openclaw
      // create its agents/<id>/sessions/ tree inside; net effect: sessions land at
      // <piSessionsDir>/agents/<resolved>/sessions/<UUID>.jsonl. Auth files stay seeded at
      // <piSessionsDir>/<botId>/agent (compatible with the AGENT_DIR override).
      // Pi reads per-agent auth/models from $OPENCLAW_AGENT_DIR (and the equivalent
      // SDK var PI_CODING_AGENT_DIR). Point both at the lab's seeded per-bot dir so
      // pi finds API keys without falling back to ~/.openclaw/agents/main/.
      // WORLD_PI_SESSIONS_DEST tells pi-server where to copy the session jsonl (and
      // then unlink the source from ~/.openclaw/agents/<resolved>/sessions/) so the
      // real openclaw tree stays clean. The pi-server passes the sessionId as a UUID
      // so the source filename is predictable.
      const env = agentDir
        ? {
            OPENCLAW_AGENT_DIR: agentDir,
            PI_CODING_AGENT_DIR: agentDir,
            WORLD_PI_SESSIONS_DEST: join(config.piSessionsDir!, botId, 'sessions'),
          }
        : undefined
      return BotServer.start(botId, {
        argv,
        cwd: spawnCwd,
        env,
        readyTimeoutMs: 60_000,
        onLog: (l) => process.stderr.write(l + '\n'),
        onNotification: (method, params) => {
          const line = formatBotNotification(botId, method, params)
          if (line) log(worldRoot, runId, line)
        },
      })
    })

  // 交易日序列
  const cal = loadCalendar(config.calendar)
  const tradingDates = computeTradingDates(cal, config.replay.from, config.replay.to)

  // 校验数据齐全（fail-fast，在写 state 之前）
  const missing = tradingDates.filter(d => !existsSync(P.quotesFile(worldRoot, d)))
  if (missing.length) throw new Error(`missing quotes.json for ${missing.length} trading day(s): ${missing.slice(0, 5).join(', ')}${missing.length > 5 ? ', …' : ''}`)

  const rlOpenclawDir = config.rlOpenclawDir ?? P.rlOpenclawDir(worldRoot, runId)

  // 目录
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  mkdirSync(P.memoryDir(worldRoot, runId), { recursive: true })
  mkdirSync(P.workspacesDir(worldRoot, runId), { recursive: true })
  mkdirSync(rlOpenclawDir, { recursive: true })
  // 把 openclaw.json 复制进 run 专属 openclaw 目录（research-loop 的 openclaw_dir → session/事件存档落这里，与真实 .openclaw 隔离）
  const srcOpenclawJson = openclawJsonSource(config)
  const dstOpenclawJson = join(rlOpenclawDir, 'openclaw.json')
  if (existsSync(srcOpenclawJson) && !existsSync(dstOpenclawJson)) {
    try { copyFileSync(srcOpenclawJson, dstOpenclawJson) } catch { /* 非致命 */ }
  }
  log(worldRoot, runId, `setup: ${tradingDates.length} trading days ${tradingDates[0]} .. ${tradingDates[tradingDates.length - 1]}, bots=[${config.bots.join(', ')}]`)

  // 记忆服务（进程内）
  const currentDateRef = { value: tradingDates[0] }
  const getCurrentDate = (): string => currentDateRef.value
  const store = new MemoryStore(P.memoryStoreFile(worldRoot, runId))
  const memory = await createMemoryServer({ store, getCurrentDate })
  writeFileSync(P.memoryRuntimeFile(worldRoot, runId), JSON.stringify({ port: memory.port, url: memory.url, collection: 'trading-memories' }, null, 2) + '\n')
  log(worldRoot, runId, `memory server at ${memory.url}`)

  // 按 loop 分支生成 server 配置：research-loop 写 trading-rl-config.json；pi 直接 patch 已经复制的 openclaw.json 的 mcp.mem0。
  if (config.loop === 'openclaw-pi') {
    patchPiOpenclawJsonMemory(rlOpenclawDir, memory.url)
    // Pi 的 agents dir 隔离：seed once 把每个 bot 的 auth-profiles / models 从真实 ~/.openclaw/agents/<bot>/agent
    // 拷到 lab 自己的 piSessionsDir/<bot>/agent。之后 lab agent 与 openclaw agent 完全脱钩演化。
    if (config.piSessionsDir && config.openclawRoot) {
      mkdirSync(config.piSessionsDir, { recursive: true })
      const sourceAgentsDir = join(config.openclawRoot, '..', 'agents')
      for (const botId of config.bots) {
        if (!isAbsolute(botId)) seedPiAgentBot(config.piSessionsDir, sourceAgentsDir, botId)
      }
    }
  } else {
    generateRlConfig(config, worldRoot, runId, memory.url, rlOpenclawDir)
  }

  // 影子 workspace + bot server
  const bots: { botId: string; server: BotServer }[] = []
  try {
    for (const botId of config.bots) {
      const srcWs = isAbsolute(botId) ? botId : join(config.botsRoot, botId)
      if (!existsSync(srcWs)) throw new Error(`source workspace not found for ${botId}: ${srcWs}`)
      const shadow = P.shadowWorkspaceDir(worldRoot, runId, botId)
      buildShadowWorkspace({ sourceDir: srcWs, destDir: shadow, include: config.shadowInclude })
      const argv = botServerArgv(config, botId, shadow, loopConfigPath(config, worldRoot, runId))
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
  const writeStatus = (status: DayBotStatus['status'] | 'running', extras: { iterations?: number; usage?: number; error?: string; finishedAt?: string } = {}): void => {
    const payload: Record<string, unknown> = {
      status,
      started_at: new Date(startedAt).toISOString(),
      ...(extras.finishedAt ? { finished_at: extras.finishedAt } : {}),
      ...(typeof extras.iterations === 'number' ? { iterations: extras.iterations } : {}),
      ...(typeof extras.usage === 'number' ? { usage: extras.usage } : {}),
      ...(extras.error ? { error: extras.error } : {}),
    }
    writeFileSync(P.statusFile(worldRoot, runId, date, b.botId), JSON.stringify(payload, null, 2) + '\n')
  }
  if (!b.server.alive) {
    const s: DayBotStatus = { bot: b.botId, status: 'dead', ms: 0, error: 'server process not alive' }
    writeStatus(s.status, { error: s.error, finishedAt: new Date().toISOString() })
    return s
  }
  writeStatus('running')
  log(worldRoot, runId, `bot ${b.botId}: chat request sent for ${date} (timeout=${Math.floor(perBotTimeoutMs / 1000)}s)`)
  try {
    const r = await b.server.chat({ message, session_key: SESSION_KEY(runId), history: [] }, { timeoutMs: perBotTimeoutMs })
    writeFileSync(P.replyFile(worldRoot, runId, date, b.botId), JSON.stringify(r, null, 2) + '\n')
    const s: DayBotStatus = { bot: b.botId, status: 'ok', iterations: r.iterations, usage: r.usage, ms: Date.now() - startedAt }
    writeStatus(s.status, { iterations: s.iterations, usage: s.usage, finishedAt: new Date().toISOString() })
    return s
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    const status: DayBotStatus['status'] = /timeout/i.test(msg) ? 'timeout' : !b.server.alive ? 'dead' : 'error'
    const s: DayBotStatus = { bot: b.botId, status, ms: Date.now() - startedAt, error: msg }
    writeStatus(s.status, { error: s.error, finishedAt: new Date().toISOString() })
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
    loop: config.loop,
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
  if (state.loop !== config.loop) {
    throw new Error(`resume: state loop="${state.loop}" but world.yaml loop="${config.loop}" — refuse to resume across loop change`)
  }
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
