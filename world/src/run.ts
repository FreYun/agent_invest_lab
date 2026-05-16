import { spawn } from 'node:child_process'
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
import { createSimworldProxy, type SimworldProxyHandle } from './simworld-proxy/server.ts'
import { createFundPortfolioProxy, type FundPortfolioProxyHandle } from './fund-portfolio-proxy/server.ts'
import { readState, writeState, type WorldState } from './state.ts'
import * as P from './paths.ts'

export type StartBotServer = (botId: string, argv: string[]) => Promise<BotServer>

export interface RunWorldOptions {
  worldRoot: string
  config: WorldConfig
  runId: string
  startBotServer?: StartBotServer
}

// openclaw 解析 sessionKey → agentId 走 parseAgentSessionKey，要求格式 `agent:<id>:<rest>`
// (openclaw/src/sessions/session-key-utils.ts)。不带这个前缀，pi runner 内部 plugin tool
// context (mem0_search 等) 会 fallback 到 resolveDefaultAgentId(cfg)，把所有 bot 都当成
// 默认 agent (eg mag1)，导致 MEMORY.md / workspace 全部落到 mag1 那条线。
const SESSION_KEY = (runId: string, botId: string): string => `agent:${botId}:trading-${runId}`
const JOURNAL_REL = 'memory/trading/journal.md'

/** 选 openclaw.json 源路径。当前两种 loop 都用同一个 credentials 文件。 */
export function openclawJsonSource(config: WorldConfig): string {
  return config.openclawJson
}

/** 跑一次 fund-portfolio-mcp/cli_tools.py 子进程（绕过 MCP HTTP），返回 stdout 文本。
 *  用在 system 侧调 init_fund_account / close_my_day——这些 tool 在 BOT_ONLY 端口被隐藏，
 *  bot 看不见，只能由 world setup / 每日收盘自动触发。 */
export async function runFundCli(cliPath: string, cmd: string, args: string[], opts: { timeoutMs?: number } = {}): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolveP, reject) => {
    const child = spawn('python3', [cliPath, cmd, ...args], { stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    const timer = opts.timeoutMs ? setTimeout(() => { try { child.kill('SIGKILL') } catch { /* ignore */ } reject(new Error(`fund cli ${cmd} timeout (${opts.timeoutMs}ms)`)) }, opts.timeoutMs) : null
    child.stdout.on('data', d => { stdout += d.toString() })
    child.stderr.on('data', d => { stderr += d.toString() })
    child.on('error', err => { if (timer) clearTimeout(timer); reject(err) })
    child.on('close', code => { if (timer) clearTimeout(timer); resolveP({ stdout: stdout.trim(), stderr: stderr.trim(), code: code ?? -1 }) })
  })
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

/** Build env supplement so child Node fetch() honors the parent's HTTP(S)_PROXY.
 *
 * Two reasons we have to manage this in lab instead of relying on shell env:
 * 1. Node's built-in fetch (undici) DOES NOT read HTTPS_PROXY/HTTP_PROXY by default.
 *    Need `--use-env-proxy` (Node 22+ EnvHttpProxyAgent) to opt in. We inject it
 *    via NODE_OPTIONS so it survives spawn without changing argv plumbing.
 * 2. undici's EnvHttpProxyAgent NO_PROXY parser doesn't grok CIDR ranges
 *    (e.g. `192.168.0.0/16`). When parsing fails it tends to bypass the proxy
 *    entirely → all fetches go direct → external API timeouts. Strip CIDR
 *    entries from NO_PROXY before handing it down.
 *
 * Returns {} when no proxy is set in the parent — we don't disturb proxy-free
 * environments. */
export function proxyEnvSupplement(env: NodeJS.ProcessEnv = process.env): Record<string, string> {
  const hasProxy = env.HTTPS_PROXY || env.https_proxy || env.HTTP_PROXY || env.http_proxy || env.ALL_PROXY || env.all_proxy
  if (!hasProxy) return {}
  const out: Record<string, string> = {}
  const existing = env.NODE_OPTIONS ?? ''
  out.NODE_OPTIONS = existing.includes('--use-env-proxy') ? existing : `${existing} --use-env-proxy`.trim()
  // CIDR entries (a.b.c.d/N) crash EnvHttpProxyAgent's no-proxy matcher; drop them.
  // Always keep at least localhost/127.0.0.1/::1 so loopback (e.g. our memory-server) bypasses.
  const raw = env.NO_PROXY ?? env.no_proxy ?? ''
  const cleaned = raw.split(',').map(s => s.trim()).filter(s => s && !/\/\d+$/.test(s))
  const baseLoopback = ['localhost', '127.0.0.1', '::1']
  for (const lb of baseLoopback) if (!cleaned.includes(lb)) cleaned.push(lb)
  out.NO_PROXY = cleaned.join(',')
  return out
}

/** openclaw-pi loop：从真实 ~/.openclaw/agents/<botId>/agent/ 把 auth-profiles / auth-state / models.json
 *  种子拷贝到 piSessionsDir/agents/<botId>/agent/。已存在的不动（"seed once"），让 lab 的 agent state 后续
 *  与 openclaw 脱钩演化。配合 spawn 时 env OPENCLAW_STATE_DIR=<piSessionsDir>，pi 把 sessions.json /
 *  *.jsonl 也写进 <piSessionsDir>/agents/<botId>/sessions/，dashboard 可以按 world-root 扫描列出来。 */
export function seedPiAgentBot(piSessionsDir: string, sourceAgentsDir: string, botId: string): void {
  const dest = join(piSessionsDir, 'agents', botId, 'agent')
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
  simworldProxy: SimworldProxyHandle
  fundPortfolioProxy: FundPortfolioProxyHandle | null
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
  // pi sessions dir is per-run (not config-driven). Each run owns its own
  // session tree so the same bot can run in two parallel runs without colliding.
  const piSessionsDirForRun = config.loop === 'openclaw-pi' ? P.piSessionsDir(worldRoot, runId) : undefined
  // Lay pi state under <piSessionsDirForRun>/agents/<botId>/ so it mirrors openclaw's
  // own ~/.openclaw/agents/<id>/ layout. That lets dashboards/auditors point at
  // <piSessionsDirForRun> as a "world openclaw root" and scan sessions/sessions.json
  // exactly the same way they scan the real openclaw state tree.
  const piAgentDirFor = (botId: string): string | undefined =>
    piSessionsDirForRun ? join(piSessionsDirForRun, 'agents', botId, 'agent') : undefined
  const startBotServer: StartBotServer = opts.startBotServer
    ?? ((botId, argv) => {
      const agentDir = piAgentDirFor(botId)
      // - OPENCLAW_AGENT_DIR / PI_CODING_AGENT_DIR: per-agent dir (auth-profiles, models.json).
      // - OPENCLAW_STATE_DIR: pi computes session paths from <STATE_DIR>/agents/<id>/sessions/
      //   (see openclaw config/sessions/paths.ts). Point it at piSessionsDir so sessions.json +
      //   *.jsonl + *.state.json all land under <piSessionsDir>/agents/<botId>/sessions/, never
      //   touching ~/.openclaw/agents/<id>/sessions/. pi-stdio-server reads STATE_DIR to compute
      //   the sessionFile path so the index file matches the jsonl filename.
      const piEnv: Record<string, string> = agentDir && piSessionsDirForRun
        ? {
            OPENCLAW_AGENT_DIR: agentDir,
            PI_CODING_AGENT_DIR: agentDir,
            OPENCLAW_STATE_DIR: piSessionsDirForRun,
          }
        : {}
      const proxyEnv = proxyEnvSupplement()
      // research-loop reads this file each turn to pin its system-prompt date to the
      // world's replay day instead of the host wall-clock. World rewrites the file
      // at the top of each trading day. pi doesn't auto-inject the date in its
      // system prompt, so the env var is benign there (just unused).
      const dateOverrideEnv: Record<string, string> = { WORLD_DATE_OVERRIDE_FILE: P.worldDateOverrideFile(worldRoot, runId) }
      const env: Record<string, string> = { ...dateOverrideEnv, ...piEnv, ...proxyEnv }
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
  // 历史上这里 fail-fast 校验每个交易日有 days/<d>/quotes.json——那时 prompt 会把
  // quotes.json 渲染进 daily message。现在 prompt 改走 simworld-data MCP 实时查行情，
  // quotes.json 已经不再被任何活路径消费（overview.ts 也成了死代码）。删掉这个守门，
  // calendar 内任意区间都能跑。如果需要回来强制 quotes 数据齐全，重新加回 missing check。

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

  // simworld-data MCP 代理（必选，进程内）。模板变量 ${SIMWORLD_PROXY_URL} 在
  // buildShadowWorkspace 拷贝 config/mcporter.json 时替换为下面的 url。
  const simworldProxy = await createSimworldProxy({ upstreamUrl: config.simworldUpstreamUrl, getCurrentDate })
  writeFileSync(P.simworldProxyRuntimeFile(worldRoot, runId), JSON.stringify({ port: simworldProxy.port, url: simworldProxy.url, upstream: config.simworldUpstreamUrl }, null, 2) + '\n')
  log(worldRoot, runId, `simworld-data proxy at ${simworldProxy.url} (upstream ${config.simworldUpstreamUrl})`)
  const templateVars: Record<string, string> = { SIMWORLD_PROXY_URL: simworldProxy.url }

  // fund-portfolio-mcp 代理（仅当 fundMcpCli 配置时启用——基金 run 才需要）：
  //   - 强制注入 run_id 到所有 writer 工具的 arguments
  //   - 从 tools/list 的 inputSchema 删除 run_id（bot 永远看不见）
  // bot 的 mcporter.json 用 ${FUND_PORTFOLIO_PROXY_URL} 占位符引用。
  let fundPortfolioProxy: FundPortfolioProxyHandle | null = null
  if (config.fundMcpCli && config.fundPortfolioUpstreamUrl) {
    fundPortfolioProxy = await createFundPortfolioProxy({ upstreamUrl: config.fundPortfolioUpstreamUrl, runId })
    writeFileSync(P.fundPortfolioProxyRuntimeFile(worldRoot, runId), JSON.stringify({ port: fundPortfolioProxy.port, url: fundPortfolioProxy.url, upstream: config.fundPortfolioUpstreamUrl, runId }, null, 2) + '\n')
    log(worldRoot, runId, `fund-portfolio proxy at ${fundPortfolioProxy.url} (upstream ${config.fundPortfolioUpstreamUrl}, run_id=${runId})`)
    templateVars.FUND_PORTFOLIO_PROXY_URL = fundPortfolioProxy.url
  }

  // 按 loop 分支生成 server 配置：research-loop 写 trading-rl-config.json；pi 直接 patch 已经复制的 openclaw.json 的 mcp.mem0。
  if (config.loop === 'openclaw-pi') {
    patchPiOpenclawJsonMemory(rlOpenclawDir, memory.url)
    // Pi 的 agents dir 隔离：seed once 把每个 bot 的 auth-profiles / models 从真实 ~/.openclaw/agents/<bot>/agent
    // 拷到 lab 自己的 piSessionsDir/<bot>/agent。之后 lab agent 与 openclaw agent 完全脱钩演化。
    if (piSessionsDirForRun && config.openclawRoot) {
      mkdirSync(piSessionsDirForRun, { recursive: true })
      const sourceAgentsDir = join(config.openclawRoot, '..', 'agents')
      for (const botId of config.bots) {
        if (!isAbsolute(botId)) seedPiAgentBot(piSessionsDirForRun, sourceAgentsDir, botId)
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
      buildShadowWorkspace({ sourceDir: srcWs, destDir: shadow, include: config.shadowInclude, templateVars })
      const argv = botServerArgv(config, botId, shadow, loopConfigPath(config, worldRoot, runId))
      const server = await startBotServer(botId, argv)
      bots.push({ botId, server })
      log(worldRoot, runId, `bot ${botId}: server ready`)
    }
  } catch (err) {
    // 启动阶段失败：关掉已起的 bot server + 记忆服务 + 代理
    for (const b of bots) { try { await b.server.shutdown({ timeoutMs: 2000 }) } catch { /* ignore */ } }
    try { await memory.close() } catch { /* ignore */ }
    try { await simworldProxy.close() } catch { /* ignore */ }
    if (fundPortfolioProxy) try { await fundPortfolioProxy.close() } catch { /* ignore */ }
    throw err
  }

  // 系统侧 init：每个 bot 调一次 init_fund_account。bot 在 BOT_ONLY 端口看不到这个 tool，
  // 只能由 world 帮它建账户。默认 capital 100 万、全现金、reset=true（world replay 起点干净）。
  if (config.fundMcpCli) {
    // Per-run 可买基金白名单：写到 fund-portfolio-mcp 进程能读到的固定路径。
    // 路径必须跟 lab-fund-bot-only.service / lab-fund-readonly.service 的
    // FUND_BUYABLE_CODES_FILE env 一致（手工 sync；改一处记得改另一处）。
    if (config.buyableFundCodes) {
      try {
        writeFileSync(BUYABLE_CODES_FILE, JSON.stringify({ fund_codes: config.buyableFundCodes }) + '\n')
        log(worldRoot, runId, `fund buyable codes pinned (${config.buyableFundCodes.length}): ${config.buyableFundCodes.slice(0, 8).join(',')}${config.buyableFundCodes.length > 8 ? ',…' : ''}`)
      } catch (err) {
        log(worldRoot, runId, `fund buyable codes write FAILED: ${err instanceof Error ? err.message : String(err)}`)
      }
    }
    const capital = config.fundInitialCapital ?? 1_000_000
    // --run-id 把本轮 runId 透给 cli_tools.py，server.py 的 _require_run_id 才能放行 init。
    const initArgs = ['--initial-capital', String(capital), '--run-id', runId]
    if (config.fundInitReset !== false) initArgs.push('--reset')
    for (const { botId } of bots) {
      try {
        const r = await runFundCli(config.fundMcpCli, 'init_fund_account', ['--bot-id', botId, ...initArgs], { timeoutMs: 30_000 })
        log(worldRoot, runId, `fund init ${botId}: code=${r.code} ${r.stdout.slice(0, 200)}${r.stderr ? ` | stderr: ${r.stderr.slice(0, 200)}` : ''}`)
      } catch (err) {
        log(worldRoot, runId, `fund init ${botId} FAILED: ${err instanceof Error ? err.message : String(err)}`)
      }
    }
  }

  return { tradingDates, memory, simworldProxy, fundPortfolioProxy, bots, currentDateRef }
}

// 必须跟 lab-fund-bot-only.service / lab-fund-readonly.service 的 FUND_BUYABLE_CODES_FILE
// 完全一致——server.py 在 BOT_ONLY 模式下从这里读 curated 列表。
const BUYABLE_CODES_FILE = '/home/rooot/agent_invest_lab/data/lab-fund-buyable.json'

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
    const r = await b.server.chat({ message, session_key: SESSION_KEY(runId, b.botId), history: [] }, { timeoutMs: perBotTimeoutMs })
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
  // 1) 先把 state.json 翻成终态——dashboard 通过 status!=='running' 判断能否起新 run，
  //    任何 cleanup hang 都不能阻塞这一步。同时把 run 目录里的 state archive 也写下。
  let state: WorldState | undefined
  try { state = readState(worldRoot, runId) } catch { /* state.json 可能 setup 都没写出来 */ }
  if (state) {
    try { writeState(worldRoot, runId, { ...state, status: finalStatus, updated_at: new Date().toISOString() }) } catch { /* ignore */ }
  }
  // 2) 关 server / memory / proxy；用 Promise.race 给整体 cleanup 一个 10s 硬顶。
  //    任何单独 close 卡住都会被这个超时罩住，绝不让 teardown 永远挂在等待 socket drain。
  await Promise.race([
    Promise.allSettled([
      ...setupRes.bots.map(b => b.server.shutdown({ timeoutMs: 5000 })),
      setupRes.memory.close(),
      setupRes.simworldProxy.close(),
      ...(setupRes.fundPortfolioProxy ? [setupRes.fundPortfolioProxy.close()] : []),
    ]),
    new Promise<void>(resolve => setTimeout(resolve, 10_000)),
  ])
  // 3) summary 是 best-effort——cleanup 已经收尾，state 已落盘，summary 只是审阅辅助。
  try { writeFileSync(P.summaryFile(worldRoot, runId), JSON.stringify({ run_id: runId, status: finalStatus, days, finished_at: new Date().toISOString() }, null, 2) + '\n') } catch { /* ignore */ }
  log(worldRoot, runId, `teardown: status=${finalStatus}`)
}

export async function runWorld(opts: RunWorldOptions): Promise<void> {
  const { worldRoot, config, runId } = opts
  let setupRes: SetupResult
  try {
    setupRes = await setup(opts)
  } catch (err) {
    // 尽量记录 failed（state 可能还没建）
    try { if (existsSync(P.runStateFile(worldRoot, runId))) { const s = readState(worldRoot, runId); writeState(worldRoot, runId, { ...s, status: 'failed', updated_at: new Date().toISOString() }) } } catch { /* ignore */ }
    throw err
  }

  const initial: WorldState = {
    run_id: runId, status: 'running', current_date: setupRes.tradingDates[0],
    trading_dates: setupRes.tradingDates, cursor: 0, bots: config.bots,
    memory_port: setupRes.memory.port, started_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    loop: config.loop,
  }
  writeState(worldRoot, runId, initial)

  await runLoop({ worldRoot, runId, config, setupRes, fromCursor: 0 })
}

interface RunLoopArgs { worldRoot: string; runId: string; config: WorldConfig; setupRes: SetupResult; fromCursor: number }

/** 第 N / 2N / 3N … 个交易日（cursor 0-based）算研究日；researchDayEvery=0 关闭。 */
export function isResearchDay(cursor: number, researchDayEvery: number): boolean {
  return researchDayEvery > 0 && ((cursor + 1) % researchDayEvery === 0)
}

export async function runLoop(args: RunLoopArgs): Promise<void> {
  const { worldRoot, runId, config, setupRes, fromCursor } = args
  const dates = setupRes.tradingDates
  const days: DaySummary[] = []
  const perBotTimeoutMs = config.perBotTimeoutSeconds * 1000
  const researchDayTimeoutMs = config.researchDayTimeoutSeconds * 1000
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
      // Pin the loop processes' system-prompt date to today's world day before any
      // chat goes out. See WORLD_DATE_OVERRIDE_FILE in startBotServer above.
      writeFileSync(P.worldDateOverrideFile(worldRoot, runId), date)
      {
        const state = readState(worldRoot, runId)
        writeState(worldRoot, runId, { ...state, current_date: date, updated_at: new Date().toISOString() })
      }
      const isResearch = isResearchDay(cursor, config.researchDayEvery)
      const isFirstDay = cursor === 0
      // First day shares research-day's wider budget: full rules + cold-start onboarding
      // (discover_tools, read SOUL/IDENTITY/journal, query holdings/cooldown, web_fetch
      // sanity check, write first journal entry) consistently spills past a 60s budget.
      // Research-day budget is the right ceiling for that workload too, so reuse it
      // instead of adding another knob.
      const useExtendedBudget = isFirstDay || isResearch
      const timeoutMs = useExtendedBudget ? researchDayTimeoutMs : perBotTimeoutMs
      const tagBits = [isFirstDay ? '[first day]' : '', isResearch ? '[research day]' : ''].filter(Boolean).join(' ')
      log(worldRoot, runId, `day ${cursor + 1}/${dates.length}: ${date}${tagBits ? ' ' + tagBits : ''} — sending to ${config.bots.length} bot(s) (timeout=${Math.floor(timeoutMs / 1000)}s)`)
      // 系统侧 settle：T+1 收口。每天 chat **之前** 把所有 order_date < today 的 pending 单按
      // reference_nav 结算（BUY → 持仓增加 + 释放 cash_in_transit；SELL → 现金回流 + 释放 pending_sell）。
      // close_my_day 不做这件事，所以必须独立调一次。bot 在 BOT_ONLY 端口看不到 settle。
      // Day 1 (cursor=0) 也调，no-op 安全（没有更早的 pending 单）。
      if (config.fundMcpCli) {
        for (const { botId } of setupRes.bots) {
          try {
            const r = await runFundCli(config.fundMcpCli, 'settle_pending_orders', ['--bot-id', botId, '--as-of-date', date, '--run-id', runId], { timeoutMs: 30_000 })
            log(worldRoot, runId, `fund settle ${botId} ${date}: code=${r.code} ${r.stdout.slice(0, 200)}`)
          } catch (err) {
            log(worldRoot, runId, `fund settle ${botId} ${date} FAILED: ${err instanceof Error ? err.message : String(err)}`)
          }
        }
      }
      const quotesAbs = resolve(P.quotesFile(worldRoot, date))
      const statuses = await mapWithConcurrency(setupRes.bots, config.concurrency, async (b) => {
        const message = renderDailyMessage({ worldRoot, date, isFirstDay, quotesPath: quotesAbs, journalRelPath: JOURNAL_REL, buyableFundCodes: config.buyableFundCodes })
        if (brokenBots.has(b.botId)) return writeSkippedDeadBot(worldRoot, runId, date, message, b)
        return chatOneBot(worldRoot, runId, date, message, timeoutMs, b)
      })
      for (const s of statuses) { if (s.status === 'timeout' || s.status === 'dead') brokenBots.add(s.bot) }
      // 系统侧 close：每个 bot（不论 chat 状态如何）跑一次 close_my_day 落收盘快照。
      // bot 在 BOT_ONLY 端口看不到 close_my_day，只能 world 触发；这是"每天收盘核算"的硬契约。
      // snapshot 文本写到 <botDayDir>/close_my_day.json，方便后续审阅。
      if (config.fundMcpCli) {
        for (const { botId } of setupRes.bots) {
          try {
            const r = await runFundCli(config.fundMcpCli, 'close_my_day', ['--bot-id', botId, '--trade-date', date, '--run-id', runId], { timeoutMs: 30_000 })
            const dir = P.botDayDir(worldRoot, runId, date, botId)
            mkdirSync(dir, { recursive: true })
            writeFileSync(join(dir, 'close_my_day.json'), r.stdout + '\n')
            log(worldRoot, runId, `fund close ${botId} ${date}: code=${r.code} (snapshot saved)`)
          } catch (err) {
            log(worldRoot, runId, `fund close ${botId} ${date} FAILED: ${err instanceof Error ? err.message : String(err)}`)
          }
        }
      }
      days.push({ date, bots: statuses })
      log(worldRoot, runId, `day ${date} done: ${statuses.map(s => `${s.bot}=${s.status}`).join(' ')}`)
      const st = readState(worldRoot, runId)
      writeState(worldRoot, runId, { ...st, cursor: cursor + 1, updated_at: new Date().toISOString() })
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
  runId: string
  startBotServer?: StartBotServer
}

export async function resumeWorld(opts: ResumeWorldOptions): Promise<void> {
  const { worldRoot, config, runId } = opts
  const state = readState(worldRoot, runId)
  if (state.status !== 'running') throw new Error(`cannot resume: state status is "${state.status}", nothing to resume`)
  if (state.loop !== config.loop) {
    throw new Error(`resume: state loop="${state.loop}" but world.yaml loop="${config.loop}" — refuse to resume across loop change`)
  }
  // 清掉可能残留的 STOP 哨兵（否则 resume 会立刻被它中止）
  rmSync(P.stopFile(worldRoot, runId), { force: true })
  // setup（重启记忆服务、重建/复用影子 workspace、重起 bot server），但 trading_dates 取自 state
  const setupRes = await setup({ worldRoot, config, runId, startBotServer: opts.startBotServer })
  // 若 calendar/replay 变了导致交易日序列对不上，拒绝
  if (setupRes.tradingDates.length !== state.trading_dates.length || setupRes.tradingDates[0] !== state.trading_dates[0] || setupRes.tradingDates[setupRes.tradingDates.length - 1] !== state.trading_dates[state.trading_dates.length - 1]) {
    for (const b of setupRes.bots) { try { await b.server.shutdown({ timeoutMs: 2000 }) } catch { /* ignore */ } }
    try { await setupRes.memory.close() } catch { /* ignore */ }
    throw new Error('resume: trading-date sequence changed since the run started; refuse to resume')
  }
  setupRes.currentDateRef.value = state.trading_dates[Math.min(state.cursor, state.trading_dates.length - 1)]
  await runLoop({ worldRoot, runId, config, setupRes, fromCursor: state.cursor })
}

/** 由独立的 `world stop` 进程调用：写一个 STOP 哨兵，正在跑的 runLoop 会在下一天开始前发现它。 */
export function requestStop(worldRoot: string, runId: string): { ok: boolean; reason?: string } {
  if (!existsSync(P.runStateFile(worldRoot, runId))) return { ok: false, reason: `no state.json for run ${runId}` }
  const state = readState(worldRoot, runId)
  if (state.status !== 'running') return { ok: false, reason: `state status is "${state.status}"` }
  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  writeFileSync(P.stopFile(worldRoot, runId), `requested at ${new Date().toISOString()}\n`)
  return { ok: true }
}
