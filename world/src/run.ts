import { spawn } from 'node:child_process'
import { appendFileSync, copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { parse as parseYaml } from 'yaml'
import type { WorldConfig } from './config.ts'
import { copyStrategyLibraryToWorkspace, loadStrategyLibrary, renderActiveMethodology, renderStrategyCatalog, renderTaskHeader, type StrategyLibrary } from './strategy-library.ts'
import { loadCalendar, computeTradingDates } from './calendar.ts'
import { mapWithConcurrency } from './concurrency.ts'
import { BotServer } from './botServer.ts'
import { buildShadowWorkspace } from './shadowWorkspace.ts'
import { renderDailyMessage, botKindOf } from './message.ts'
import { fetchDailyContext } from './daily-context.ts'
import { buildHistoryWindow } from './history-window/index.ts'
import { MemoryStore } from './memory-server/store.ts'
import { createMemoryServer, type MemoryServerHandle } from './memory-server/server.ts'
import { createSimworldProxy, HIDDEN_TOOLS, type SimworldProxyHandle } from './simworld-proxy/server.ts'
import { createFundPortfolioProxy, type FundPortfolioProxyHandle } from './fund-portfolio-proxy/server.ts'
import { createStrategyServer, type StrategyServerHandle, runSqlite, sqlStr } from './strategy-server/server.ts'
import { fetchIntradayQuoteBlock } from './intraday-market.ts'
import { assembleBriefing } from './intraday-briefing.ts'
import { readState, writeState, type WorldState } from './state.ts'
import { buildBeliefContext, validateBeliefMd } from './belief-context/index.ts'
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
//
// 含 date 后缀：每个世界日是独立 chat session（history:[] 也保证 history 不串），但
// loop server 这边的 sessionKey→UUID 映射也按日切分，sessions.jsonl 不再是 326 天连成
// 一个超大文件、dashboard 也能按日查阅。run.ts:chatOneBot 调用这个函数时传当天 date。
const SESSION_KEY = (runId: string, botId: string, date: string): string =>
  `agent:${botId}:trading-${runId}-${date}`

/** 选 openclaw.json 源路径。当前两种 loop 都用同一个 credentials 文件。 */
export function openclawJsonSource(config: WorldConfig): string {
  return config.openclawJson
}

/** 跑一次 fund-portfolio-mcp/cli_tools.py 子进程（绕过 MCP HTTP），返回 stdout 文本。
 *  用在 system 侧调 init_fund_account / close_my_day——这些 tool 在 BOT_ONLY 端口被隐藏，
 *  bot 看不见，只能由 world setup / 每日收盘自动触发。 */
export async function runFundCli(cliPath: string, cmd: string, args: string[], opts: { timeoutMs?: number } = {}): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolveP, reject) => {
    // 裸 `python3` 在本机会落到缺 `mcp` 包的解释器 → cli_tools.py import 即崩。
    // 优先 FUND_MCP_PYTHON，其次 cli 同级的 .venv/bin/python（fund-portfolio-mcp/.venv），
    // 最后兜底 python3。
    const cliAbs = resolve(cliPath)
    const venvPython = join(dirname(cliAbs), '.venv', 'bin', 'python')
    const python = process.env.FUND_MCP_PYTHON || (existsSync(venvPython) ? venvPython : 'python3')
    // cli_tools.py → db.py 缺省把 DB_PATH 落到 /home/rooot/...（另一套 .openclaw 库，本机无权限）。
    // 从 cli 路径推导仓库根（<repo>/fund-portfolio-mcp/cli_tools.py → <repo>），把 OPENCLAW_ROOT /
    // FUND_DB_PATH 指向本仓库的 data/fund.db；已显式设置则尊重不覆盖。与 restart-fund-mcp*.sh 一致。
    const repoRoot = dirname(dirname(cliAbs))
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      OPENCLAW_ROOT: process.env.OPENCLAW_ROOT || repoRoot,
      FUND_DB_PATH: process.env.FUND_DB_PATH || join(repoRoot, 'data', 'fund.db'),
    }
    const child = spawn(python, [cliAbs, cmd, ...args], { stdio: ['ignore', 'pipe', 'pipe'], env })
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
  // research-loop 分支：当 researchLoopRustBin 配置时走 rust binary；否则 fallback 到
  // ts/server.ts。两端 JSON-RPC 协议同集（ping / chat / shutdown），BotServer 无感。
  // 切到 rust 主要为了让 ChatEvent::Error 不再被 ts server 静默吃掉——rs server.rs 把
  // chat_error 透出到 RPC 返回值，run.ts 据此把当天标 'error' 而不是 'ok'。
  if (config.researchLoopRustBin) {
    return [config.researchLoopRustBin, 'server', '--bot-id', botId, '--workspace', workspace, '--config', loopConfigPath]
  }
  const serverEntry = join(config.researchLoop, 'server.ts')
  return [process.execPath, '--experimental-strip-types', serverEntry, '--bot-id', botId, '--workspace', workspace, '--config', loopConfigPath]
}

// 哪些 bot 要把"判断管线 skill"整篇直接注入 daily prompt（不靠 load_skill）。
// 值 = 该 bot 要注入的 skill 目录名，按数组顺序就是判断流程顺序。
// 2026-06-11：bot101/102/103 改为「系统预读注入三份市场研报」(见 REPORT_CONSUMING_BOTS)，
// 不再注入 skill 让其自跑主线识别流水线（遵循度低、退化成只查持仓板块）。此表清空，机制保留备用。
const INJECT_PIPELINE_SKILLS: Record<string, string[]> = {}

/** 系统侧从 fund.db 预读「4 份市场研报 + 四研判室(res1/2/4/5)观点」的 content_md
 *  （PIT：as_of_date<=世界日，各取最新一期）。market_reports 与 res_reports 同库；
 *  与 strategy-server.get_market_report / res_query.get_prof_views 同口径，但走系统注入而非 bot 工具调用。
 *  8 份全缺 → 返回 undefined（message.ts 不渲染该块）。
 *  2026-07-02（用户拍板「替换」）：主线/rotation 优先取日度版（market_mainline_daily /
 *  mainline_rotation_daily，skill 日度纪律确定性引擎，backfill-mainline-daily.ts 生成）；
 *  日度缺失（世界日早于日度回补起点 2025-01，或日度管线故障）回退月度版，注入永不缺块。
 *  月度版的生成管线（prepass / v5）不动，只换消费端。 */
function readMarketReportsForInjection(fundDbPath: string, worldDate: string):
  { context: string; mainline: string; rotation: string; macroNews: string;
    res: { market_strategy: string; policy_analysis: string; intl_relations: string; cross_market_linkage: string } } | undefined {
  // res_reports 同 (report_type, as_of_date) 可能有多行（历史回填）→ 必须 id DESC 取最新一行，
  // 对齐 res_query.get_prof_views；market_reports 有 UNIQUE 约束无多行，加 id DESC 无害，统一一条 helper。
  const readLatest = (table: string, type: string): string => {
    const sql = `SELECT content_md FROM ${table} WHERE report_type=${sqlStr(type)} AND scope='global' `
      + `AND as_of_date<=${sqlStr(worldDate)} ORDER BY as_of_date DESC, id DESC LIMIT 1;`
    try {
      const out = runSqlite(fundDbPath, sql).trim()
      const rows = out ? (JSON.parse(out) as { content_md: string }[]) : []
      return rows[0]?.content_md ?? ''
    } catch { return '' }
  }
  const context = readLatest('market_reports', 'market_context')
  const mainline = readLatest('market_reports', 'market_mainline_daily') || readLatest('market_reports', 'market_mainline')
  const rotation = readLatest('market_reports', 'mainline_rotation_daily') || readLatest('market_reports', 'mainline_rotation')
  const macroNews = readLatest('market_reports', 'macro_news')
  const res = {
    market_strategy: readLatest('res_reports', 'market_strategy'),
    policy_analysis: readLatest('res_reports', 'policy_analysis'),
    intl_relations: readLatest('res_reports', 'intl_relations'),
    cross_market_linkage: readLatest('res_reports', 'cross_market_linkage'),
  }
  const anyRes = res.market_strategy || res.policy_analysis || res.intl_relations || res.cross_market_linkage
  if (!context && !mainline && !rotation && !macroNews && !anyRes) return undefined
  return { context, mainline, rotation, macroNews, res }
}

/** 从 daily 主线/rotation 报告 markdown 里正则抽出提到的 6 位数基金代码。
 *  用于收窄「daily prompt 费率块」的入参（省 ~30k tokens/day）——bot 只需要看得到
 *  持仓 + 报告推荐载体的费率，全 buyable 池 759 只 99% 用不上。
 *  匹配 `**\d{6}` / ` \d{6} ` / `载体：**\d{6}` 都覆盖，一律去重返回。 */
function extractFundCodesFromReport(md: string): string[] {
  if (!md) return []
  const codes = new Set<string>()
  const re = /(?:^|[^0-9])(\d{6})(?![0-9])/g
  let m: RegExpExecArray | null
  while ((m = re.exec(md)) !== null) codes.add(m[1])
  return [...codes]
}

/** 【多基金 bot 的费率块入参】= 当前持仓 codes ∪ 报告推荐载体 codes。
 *  持仓：从 fund_bot_position_snapshots 拿 (bot_id, run_id) 上距 worldDate 最近一日的快照 fund_code。
 *  报告：从 mainline_daily / rotation_daily（缺失回退月度版）里正则抽 6 位数。
 *  两者并集通常 ≤10 只，比全 buyable 池 759 只节省大量 daily prompt token。
 *  日度报告 + 持仓都缺失（Day 1 或极端情况）→ 空数组，caller 侧回退到全 buyable 池（老行为）。 */
export function computeRelevantFundCodesForBot(
  fundDbPath: string, botId: string, runId: string, worldDate: string
): string[] {
  const readLatestReport = (type: string): string => {
    const sql = `SELECT content_md FROM market_reports WHERE report_type=${sqlStr(type)} AND scope='global' `
      + `AND as_of_date<=${sqlStr(worldDate)} ORDER BY as_of_date DESC, id DESC LIMIT 1;`
    try {
      const out = runSqlite(fundDbPath, sql).trim()
      const rows = out ? (JSON.parse(out) as { content_md: string }[]) : []
      return rows[0]?.content_md ?? ''
    } catch { return '' }
  }
  const mainline = readLatestReport('market_mainline_daily') || readLatestReport('market_mainline')
  const rotation = readLatestReport('mainline_rotation_daily') || readLatestReport('mainline_rotation')
  const merged = new Set<string>([...extractFundCodesFromReport(mainline), ...extractFundCodesFromReport(rotation)])
  // 叠上持仓 codes（同 bot、同 run 上距 worldDate 最近一日的持仓快照）。
  try {
    const holdingsSql = `SELECT fund_code FROM fund_bot_position_snapshots WHERE bot_id=${sqlStr(botId)} `
      + `AND run_id=${sqlStr(runId)} AND trade_date=(SELECT MAX(trade_date) FROM fund_bot_position_snapshots `
      + `WHERE bot_id=${sqlStr(botId)} AND run_id=${sqlStr(runId)} AND trade_date<=${sqlStr(worldDate)});`
    const out = runSqlite(fundDbPath, holdingsSql).trim()
    const rows = out ? (JSON.parse(out) as { fund_code: string }[]) : []
    for (const r of rows) if (r.fund_code) merged.add(r.fund_code)
  } catch { /* 持仓拉不到不阻塞 */ }
  return [...merged]
}

/** 读取某 bot 要直接注入的判断管线 skill 全文（从其 shadow workspace 的 skills/<id>/SKILL.md）。
 *  没配置注入 / 文件缺失 → 返回 undefined / 跳过，message.ts 自然不渲染该块（行为不变）。 */
function readInjectedPipelineSkills(worldRoot: string, runId: string, botId: string): { id: string; content: string }[] | undefined {
  const ids = INJECT_PIPELINE_SKILLS[botId]
  if (!ids || !ids.length) return undefined
  const shadow = P.shadowWorkspaceDir(worldRoot, runId, botId)
  const out: { id: string; content: string }[] = []
  for (const id of ids) {
    const p = join(shadow, 'skills', id, 'SKILL.md')
    try { out.push({ id, content: readFileSync(p, 'utf8') }) }
    catch { /* skill 未装则跳过，不阻断当日决策 */ }
  }
  return out.length ? out : undefined
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

interface StrategyAssignmentAuditEntry {
  strategy_id: string
  strategy_title: string
  target_index: string
  buyable_fund_codes: string[]
}

type BuyableCodesByBot = Record<string, string[]>

function buyableCodesForBot(config: WorldConfig, lib: StrategyLibrary | null, botId: string): string[] | undefined {
  const assignment = config.botAssignments?.[botId]
  if (assignment?.buyableFundCodes?.length) return assignment.buyableFundCodes
  if (assignment && lib) return lib.strategies.get(assignment.strategyId)?.defaultBuyableFundCodes
  return config.buyableFundCodes
}

function buildBuyableCodesByBot(config: WorldConfig, lib: StrategyLibrary | null): BuyableCodesByBot {
  const out: BuyableCodesByBot = {}
  for (const botId of config.bots) {
    const codes = buyableCodesForBot(config, lib, botId)
    if (codes && codes.length) out[botId] = [...new Set(codes)].sort()
  }
  return out
}

function installStrategyLibraryInShadow(opts: {
  config: WorldConfig
  lib: StrategyLibrary | null
  shadow: string
  botId: string
  buyableCodes: string[] | undefined
}): StrategyAssignmentAuditEntry | null {
  const { config, lib, shadow, botId, buyableCodes } = opts
  if (!lib) return null
  copyStrategyLibraryToWorkspace(lib, shadow)
  writeFileSync(join(shadow, 'STRATEGY_LIBRARY.md'), renderStrategyCatalog(lib))
  const assignment = config.botAssignments?.[botId]
  if (!assignment) return null
  const strategy = lib.strategies.get(assignment.strategyId)
  if (!strategy) throw new Error('bot ' + botId + ' strategy "' + assignment.strategyId + '" not found in strategy library')
  const codes = buyableCodes ?? strategy.defaultBuyableFundCodes
  if (!codes.length) throw new Error('bot ' + botId + ' strategy "' + assignment.strategyId + '" resolved empty buyable fund pool')
  writeFileSync(join(shadow, 'METHODOLOGY.md'), renderActiveMethodology({ botId, strategy, buyableFundCodes: codes }))
  // 持久化任务头 pin：update_my_strategy 每次重写方法论时会读这份，把 target_index / buyable_fund_codes
  // 重新锚回去，防止 bot 自进化时把标的代码丢了漂到错误指数。写在 shadow 根下、随 shadow 重建而刷新。
  writeFileSync(join(shadow, '.methodology-header.md'), renderTaskHeader({ botId, strategy, buyableFundCodes: codes }))
  return {
    strategy_id: strategy.id,
    strategy_title: strategy.title,
    target_index: strategy.targetIndex,
    buyable_fund_codes: codes,
  }
}

export function generateRlConfig(config: WorldConfig, worldRoot: string, runId: string, memoryUrl: string, openclawDir: string): void {
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

/** 新版 research-loop-rust2 只从 `<workspace>/config/research-loop.yaml` 读全部配置
 *  （model / api_key / mem0 / ...），不再认 `--config trading-rl-config.json` 里的
 *  `api_key_from_openclaw`（rust config.rs::merge_from_data 已移除该支持）。world 之前只生成
 *  被忽略的 trading-rl-config.json，导致 rust 端 primary_api_key 为空 → "LLM API not configured"。
 *
 *  这里在每个 bot 的影子 workspace 里生成 research-loop.yaml：以 rl_config_base 为模板，把
 *  api_key 解析成明文（从 run 专属 rl-openclaw/openclaw.json 的同名 provider 取），注入本 run 的
 *  mem0 URL / workspace / skills，并关掉 dashboard（多 bot 并发时 rust 默认监听 18890 会互撞）。
 *  JSON 是合法 YAML，直接按 .yaml 落盘即可被 serde_yaml 解析。buildShadowWorkspace 把
 *  config/research-loop.yaml 当 runtime-override 跳过拷贝，所以这份每次 setup 重写、不会被覆盖。 */
/** 读 bots/<botId>/config/model.yaml（真 YAML）的模型片段——bot 自带的常驻默认模型。
 *  字段同 research-loop 的 model.primary：provider / base_url / model / api_key_from_openclaw（或 api_key）。
 *  缺文件 / 解析失败 / 绝对路径 bot → 返回 null，由调用方回退全局 base。 */
function readBotModelOverride(botsRoot: string, botId: string): Record<string, unknown> | null {
  if (isAbsolute(botId)) return null
  const f = join(botsRoot, botId, 'config', 'model.yaml')
  if (!existsSync(f)) return null
  try {
    const parsed = parseYaml(readFileSync(f, 'utf8'))
    if (parsed && typeof parsed === 'object') return parsed as Record<string, unknown>
  } catch { /* 解析失败：回退全局 base */ }
  return null
}

export function writeResearchLoopYaml(config: WorldConfig, botId: string, shadow: string, memoryUrl: string, openclawDir: string): void {
  let base: Record<string, unknown>
  try { base = JSON.parse(readFileSync(config.rlConfigBase, 'utf8')) as Record<string, unknown> }
  catch (err) { throw new Error(`cannot read rl_config_base ${config.rlConfigBase}: ${err instanceof Error ? err.message : String(err)}`) }
  const model = (typeof base.model === 'object' && base.model ? base.model : {}) as Record<string, unknown>
  const basePrimary = (typeof model.primary === 'object' && model.primary ? model.primary : {}) as Record<string, unknown>
  // per-bot 模型优先级：config.botModels[botId]（modal 本次覆盖）> bots/<bot>/config/model.yaml（bot 默认）
  // > 全局 rlConfigBase。语义是字段级合并——override 提供的 provider/base_url/model/api_key* 盖过 base，
  // 没提供的继续从 base.primary 继承（LLM 调用参数等都在 base 里，不必每个 bot 重复写）。
  const override = config.botModels?.[botId] ?? readBotModelOverride(config.botsRoot, botId) ?? {}
  const primary = { ...basePrimary, ...override } as Record<string, unknown>
  // 单一真相源（SSOT）：provider（api_key_from_openclaw）唯一决定 base_url + key，二者都取自
  // openclaw.json 的同一 provider 条目 → base_url 与 key 不可能脱钩。model.yaml 里手写的 base_url
  // 一律忽略（历史上它和 key 引用各自维护、git revert 一翻就错配：火山网关收到 eastmoney key → 401）。
  // 同名模型 id 可能挂多个 provider（kimi-k2.6 同时在 zai-coding-plan/kimi-volc），故判别器必须是
  // provider 名而非模型名。两个 key 消费者都要喂：rust chat（config.rs::merge_from_data）只认
  // api_key_from_openclaw（查 providers.<name>.apiKey）、完全不读明文 api_key；TS 端 history-window
  // 压缩（compact.ts::resolveLlmEndpointFromRlConfig）只读明文 api_key + base_url。
  const provName = typeof primary.api_key_from_openclaw === 'string' ? primary.api_key_from_openclaw : ''
  if (provName) {
    let prov: { baseUrl?: string; apiKey?: string } | undefined
    try {
      const oc = JSON.parse(readFileSync(join(openclawDir, 'openclaw.json'), 'utf8')) as Record<string, unknown>
      const providers = (((oc.models as Record<string, unknown> | undefined)?.providers) ?? {}) as Record<string, { baseUrl?: string; apiKey?: string }>
      prov = providers[provName]
    } catch (err) {
      throw new Error(`writeResearchLoopYaml(${botId}): 读不到 ${openclawDir}/openclaw.json — ${err instanceof Error ? err.message : String(err)}`)
    }
    if (!prov || typeof prov.baseUrl !== 'string' || !prov.baseUrl || typeof prov.apiKey !== 'string' || !prov.apiKey) {
      throw new Error(`writeResearchLoopYaml(${botId}): provider "${provName}" 在 openclaw.json 查无 baseUrl/apiKey —— model.yaml 的 api_key_from_openclaw 写错了？`)
    }
    primary.base_url = prov.baseUrl   // base_url 由 provider 派生，无视手写值（SSOT）
    primary.api_key = prov.apiKey     // 明文给压缩端；api_key_from_openclaw 原样保留给 rust
  }
  model.primary = primary
  base.model = model
  const mcp = (typeof base.mcp === 'object' && base.mcp ? base.mcp : {}) as Record<string, unknown>
  mcp.mem0 = memoryUrl
  base.mcp = mcp
  base.extra_roots = [config.skillsRoot]
  base.openclaw_dir = openclawDir
  base.workspace = shadow
  // 回测多 bot 并发，rust 默认 dashboard 监听 18890 会互撞端口 → 显式关掉。
  const dash = (typeof base.dashboard === 'object' && base.dashboard ? base.dashboard : {}) as Record<string, unknown>
  dash.enabled = false
  base.dashboard = dash
  // 预激活 simworld 数据工具：rust 的 deferred-MCP 默认要先 discover_tools 才能调，回测里每个
  // 决策日都触发 tool→未激活→discover_tools→重试 的试错风暴（实测单个 run discover_tools 被调
  // 98 次、试错全部命中 mcp__simworld_data__*）。把白名单工具写进 tools.always_load，rust 在会话
  // 首轮即由 activate_chat_pinned_tools 预激活，彻底免去 discover_tools。名字用全前缀
  // mcp__simworld_data__<name>，精确匹配 rust registry 的 deferred-tool 键（见 tools.rs get_active_tools）。
  // HIDDEN_TOOLS 兜底：proxy 端已经把申赎原始接口从 tools/list 和 call 里都滤掉了，这里再滤一次，
  // 防 dashboard 临时配置显式列了隐藏工具时 always_load 去预激活一个 proxy 不暴露的名字。
  if (config.simworldTools && config.simworldTools.length) {
    const toolsCfg = (typeof base.tools === 'object' && base.tools ? base.tools : {}) as Record<string, unknown>
    toolsCfg.always_load = config.simworldTools.filter(t => !HIDDEN_TOOLS.has(t.name)).map(t => `mcp__simworld_data__${t.name}`)
    base.tools = toolsCfg
  }
  const dst = join(shadow, 'config', 'research-loop.yaml')
  mkdirSync(dirname(dst), { recursive: true })
  writeFileSync(dst, JSON.stringify(base, null, 2) + '\n')
}

interface SetupResult {
  tradingDates: string[]
  memory: MemoryServerHandle
  simworldProxy: SimworldProxyHandle
  fundPortfolioProxy: FundPortfolioProxyHandle | null
  strategyServer: StrategyServerHandle
  bots: { botId: string; server: BotServer }[]
  currentDateRef: { value: string }
  buyableCodesByBot: BuyableCodesByBot
  // 进程内共享的 MemoryStore 实例；与 memory-server 是同一份。
  memoryStore: MemoryStore
  // Kill the named bot's server and spawn a fresh one in its slot. Used by runLoop
  // after a per-day chat timeout so the next day doesn't race with the still-in-flight
  // request on the server side (research-loop-ts doesn't serialize same-session chats
  // and has no cancel — without restart, the dropped chat keeps issuing tool calls
  // against the next day's currentDateRef, mem0 timestamps, and session jsonl).
  restartBot: (botId: string) => Promise<void>
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
  const simworldProxy = await createSimworldProxy({ upstreamUrl: config.simworldUpstreamUrl, getCurrentDate, clientId: `run-${runId}` })
  writeFileSync(P.simworldProxyRuntimeFile(worldRoot, runId), JSON.stringify({ port: simworldProxy.port, url: simworldProxy.url, upstream: config.simworldUpstreamUrl }, null, 2) + '\n')
  log(worldRoot, runId, `simworld-data proxy at ${simworldProxy.url} (upstream ${config.simworldUpstreamUrl}); ${simworldProxy.tools.length} tools probed`)
  if (config.simworldTools) {
    const probedNames = new Set(simworldProxy.tools.map(t => t.name))
    const missing = config.simworldTools.filter(t => !probedNames.has(t.name)).map(t => t.name)
    log(worldRoot, runId, `simworld_tools whitelist active (${config.simworldTools.length} entries) → tools.always_load 预激活${missing.length ? ` — 上游 probe 里不存在（bot 将调不到）: ${missing.join(', ')}` : ''}`)
  }
  const templateVars: Record<string, string> = { SIMWORLD_PROXY_URL: simworldProxy.url }
  const strategyLibrary = config.strategyLibraryRoot ? loadStrategyLibrary(config.strategyLibraryRoot) : null
  const buyableCodesByBot = buildBuyableCodesByBot(config, strategyLibrary)

  // fund-portfolio-mcp 代理（仅当 fundMcpCli 配置时启用——基金 run 才需要）：
  //   - 强制注入 run_id 到所有 writer 工具的 arguments
  //   - 从 tools/list 的 inputSchema 删除 run_id（bot 永远看不见）
  // bot 的 mcporter.json 用 ${FUND_PORTFOLIO_PROXY_URL} 占位符引用。
  let fundPortfolioProxy: FundPortfolioProxyHandle | null = null
  if (config.fundMcpCli && config.fundPortfolioUpstreamUrl) {
    fundPortfolioProxy = await createFundPortfolioProxy({ upstreamUrl: config.fundPortfolioUpstreamUrl, runId, getTradeDate: getCurrentDate })
    writeFileSync(P.fundPortfolioProxyRuntimeFile(worldRoot, runId), JSON.stringify({ port: fundPortfolioProxy.port, url: fundPortfolioProxy.url, upstream: config.fundPortfolioUpstreamUrl, runId }, null, 2) + '\n')
    log(worldRoot, runId, `fund-portfolio proxy at ${fundPortfolioProxy.url} (upstream ${config.fundPortfolioUpstreamUrl}, run_id=${runId})`)
    templateVars.FUND_PORTFOLIO_PROXY_URL = fundPortfolioProxy.url
  }

  // strategy-server（始终启用，进程内）：bot 通过 update_my_strategy / get_my_strategy 工具
  // 管理自己的 METHODOLOGY.md（shadow workspace 下；research-loop 每次 chat 都把它 splice 进
  // system prompt 的 ## METHODOLOGY.md section）。修订审计落 runDir/strategies/<bot>.revisions.jsonl。
  // bot 的 mcporter.json 用 ${STRATEGY_SERVER_URL} 引用。
  const strategyServer = await createStrategyServer({ worldRoot, runId, getCurrentDate, enableUserSelfEdit: config.enableUserSelfEdit })
  writeFileSync(P.strategyServerRuntimeFile(worldRoot, runId), JSON.stringify({ port: strategyServer.port, url: strategyServer.url }, null, 2) + '\n')
  log(worldRoot, runId, `strategy-server at ${strategyServer.url}`)
  templateVars.STRATEGY_SERVER_URL = strategyServer.url

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
  const strategyAssignmentAudit: Record<string, StrategyAssignmentAuditEntry> = {}
  const spawnBotServer = async (botId: string): Promise<BotServer> => {
    const srcWs = isAbsolute(botId) ? botId : join(config.botsRoot, botId)
    if (!existsSync(srcWs)) throw new Error(`source workspace not found for ${botId}: ${srcWs}`)
    const shadow = P.shadowWorkspaceDir(worldRoot, runId, botId)
    buildShadowWorkspace({ sourceDir: srcWs, destDir: shadow, include: config.shadowInclude, templateVars })
    if (config.loop !== 'openclaw-pi') writeResearchLoopYaml(config, botId, shadow, memory.url, rlOpenclawDir)
    const audit = installStrategyLibraryInShadow({
      config,
      lib: strategyLibrary,
      shadow,
      botId,
      buyableCodes: buyableCodesByBot[botId],
    })
    if (audit) strategyAssignmentAudit[botId] = audit
    const argv = botServerArgv(config, botId, shadow, loopConfigPath(config, worldRoot, runId))
    return startBotServer(botId, argv)
  }
  const restartBot: SetupResult['restartBot'] = async (botId) => {
    const idx = bots.findIndex(b => b.botId === botId)
    if (idx < 0) throw new Error(`restartBot: unknown botId ${botId}`)
    try { await bots[idx].server.shutdown({ timeoutMs: 5000 }) } catch { /* ignore — we're replacing it */ }
    const server = await spawnBotServer(botId)
    bots[idx] = { botId, server }
  }
  try {
    for (const botId of config.bots) {
      const server = await spawnBotServer(botId)
      bots.push({ botId, server })
      log(worldRoot, runId, `bot ${botId}: server ready`)
    }
    if (Object.keys(strategyAssignmentAudit).length > 0) {
      writeFileSync(
        join(P.runDir(worldRoot, runId), 'strategy-assignments.json'),
        JSON.stringify({ run_id: runId, bots: strategyAssignmentAudit }, null, 2) + '\n',
      )
      log(worldRoot, runId, `strategy assignments written for ${Object.keys(strategyAssignmentAudit).length} bot(s)`)
    }
  } catch (err) {
    // 启动阶段失败：关掉已起的 bot server + 记忆服务 + 代理 + strategy-server
    for (const b of bots) { try { await b.server.shutdown({ timeoutMs: 2000 }) } catch { /* ignore */ } }
    try { await memory.close() } catch { /* ignore */ }
    try { await simworldProxy.close() } catch { /* ignore */ }
    if (fundPortfolioProxy) try { await fundPortfolioProxy.close() } catch { /* ignore */ }
    try { await strategyServer.close() } catch { /* ignore */ }
    throw err
  }

  // 系统侧 init：每个 bot 调一次 init_fund_account。bot 在 BOT_ONLY 端口看不到这个 tool，
  // 只能由 world 帮它建账户。默认 capital 100 万、全现金、reset=true（world replay 起点干净）。
  if (config.fundMcpCli) {
    // Per-run 可买基金白名单：写 <buyableCodesDir>/<runId>.json。fund-portfolio-mcp 服务通过
    // FUND_BUYABLE_CODES_DIR env 读这个目录，按调用方传入的 run_id 选文件。
    // 路径相对关系由 paths.ts 单点维护，必须和 lab-fund-{bot-only,readonly}.service 的
    // FUND_BUYABLE_CODES_DIR env 保持一致。
    if (Object.keys(buyableCodesByBot).length > 0) {
      try {
        const writePayload = config.botAssignments ? buyableCodesByBot : (config.buyableFundCodes ?? buyableCodesByBot)
        const written = writeBuyableCodesFile(worldRoot, runId, writePayload)
        const union = [...new Set(Object.values(buyableCodesByBot).flat())].sort()
        log(worldRoot, runId, `fund buyable codes pinned (${union.length} union): ${union.slice(0, 8).join(',')}${union.length > 8 ? ',…' : ''} -> ${written}`)
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

  return { tradingDates, memory, simworldProxy, fundPortfolioProxy, strategyServer, bots, currentDateRef, buyableCodesByBot, memoryStore: store, restartBot }
}

/** 写当前 run 的可买基金白名单文件。落点 = paths.buyableCodesFile(worldRoot, runId)；
 *  fund-portfolio-mcp 通过 FUND_BUYABLE_CODES_DIR 读同一目录。
 *  抽出来 named export 是为了 concurrent-runs.test.ts 能直接测"两 run 各自写、互不覆盖"，
 *  不用跑整个 setup()。返回写入的绝对路径，便于调用方 log 或测试 assert。 */
export function writeBuyableCodesFile(worldRoot: string, runId: string, codes: string[] | BuyableCodesByBot): string {
  const p = P.buyableCodesFile(worldRoot, runId)
  mkdirSync(dirname(p), { recursive: true })
  if (Array.isArray(codes)) {
    writeFileSync(p, JSON.stringify({ fund_codes: [...new Set(codes)].sort() }) + '\n')
    return p
  }
  const byBot = Object.fromEntries(
    Object.entries(codes).map(([botId, botCodes]) => [botId, [...new Set(botCodes)].sort()]),
  )
  const union = [...new Set(Object.values(byBot).flat())].sort()
  writeFileSync(p, JSON.stringify({ fund_codes: union, by_bot: byBot }) + '\n')
  return p
}

interface DayBotStatus { bot: string; status: 'ok' | 'error' | 'timeout' | 'dead'; iterations?: number; usage?: number; ms: number; error?: string; toolCalls?: number; deepResearchFired?: boolean }

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
  // 记录 chat 开始前 server 端累计 tool call 数，结束后取差。catch 分支（timeout/dead/RPC error）
  // 也要拿这个值——chat 没正常 return，没法看 result.tool_trace，但 server 端的 tool.call 通知
  // 已经流过 BotServer 的 listener。用这个 delta 实现"放松"判定。
  const toolCallStart = b.server.toolCallCount
  try {
    const r = await b.server.chat({ message, session_key: SESSION_KEY(runId, b.botId, date), history: [] }, { timeoutMs: perBotTimeoutMs })
    writeFileSync(P.replyFile(worldRoot, runId, date, b.botId), JSON.stringify(r, null, 2) + '\n')
    // belief 校验（非阻塞）：两路源——多基金 bot 在 MD frontmatter, 单基金 bot 在 reply.json fence。
    // 结果落 belief_validation.json，便于审阅与下回合 buildBeliefContext。
    // 任何异常都 swallow——绝不阻塞 chat 主流程；用 date 作 stamp 避免引入系统时间依赖。
    try {
      const mdPath = join(P.shadowWorkspaceDir(worldRoot, runId, b.botId), 'memory/portfolio/fund/市场环境判断.md')
      const replyPath = P.replyFile(worldRoot, runId, date, b.botId)
      const result = await validateBeliefMd(mdPath, replyPath)
      if (result !== null) {
        const validationPath = join(P.botDayDir(worldRoot, runId, date, b.botId), 'belief_validation.json')
        writeFileSync(validationPath, JSON.stringify({ checked_at_date: date, ...result }, null, 2))
        if (!result.ok) {
          log(worldRoot, runId, `[belief-validate] bot=${b.botId} date=${date} issues=${result.issues.join(';')}`)
        }
      }
    } catch (e) {
      log(worldRoot, runId, `[belief-validate] bot ${b.botId} ${date} failed: ${e instanceof Error ? e.message : String(e)}`)
    }
    // r.chat_error 由 rs server.rs 在 chat.send() 因 chat_llm 超时 / LLM 错误 mid-flow
    // 终止时填写。chat 本身 graceful return（带 lastReply），不带这字段就没法区分"正常
    // 完成"还是"被静默截断"。有则当天记 'error'，避免下一天还踩同一个 60s 坑。
    // "放松"判定：当日做了至少一个 tool call 就算推进——chat_error 中途挂掉、reply 为空都不要紧，
    // bot 已经走通了 LLM→tool 那一段，明天可以继续。真正要拦的是「LLM 连一次都没接通」造成的
    // 0s 垃圾日：connection refused 时 chat 1.5s 就 chat_error 返回，tool_trace 空——这种当天 pause。
    // 优先用 result.tool_trace 长度（rs/ts server 都填）；server 端通知计数器作为兜底交叉验证。
    const toolCalls = Math.max(
      Array.isArray(r.tool_trace) ? r.tool_trace.length : 0,
      b.server.toolCallCount - toolCallStart,
    )
    const deepResearchFired = Array.isArray(r.tool_trace) && r.tool_trace.some((ev: unknown) => {
      if (!ev || typeof ev !== 'object') return false
      const o = ev as Record<string, unknown>
      return o.type === 'tool_start' && o.name === 'start_research'
    })
    if (toolCalls === 0) {
      const reason = r.chat_error ? `no tool calls; chat_error: ${r.chat_error}` : 'no tool calls (chat returned without invoking any tool)'
      const s: DayBotStatus = { bot: b.botId, status: 'error', iterations: r.iterations, usage: r.usage, ms: Date.now() - startedAt, error: reason, toolCalls, deepResearchFired }
      writeStatus(s.status, { iterations: s.iterations, usage: s.usage, error: s.error, finishedAt: new Date().toISOString() })
      log(worldRoot, runId, `bot ${b.botId}: chat made no tool calls — failing the day${r.chat_error ? ` (chat_error: ${r.chat_error})` : ''}`)
      return s
    }
    if (r.chat_error) log(worldRoot, runId, `bot ${b.botId}: chat ended with chat_error but ${toolCalls} tool call(s) made — advancing (${r.chat_error})`)
    const s: DayBotStatus = { bot: b.botId, status: 'ok', iterations: r.iterations, usage: r.usage, ms: Date.now() - startedAt, toolCalls, deepResearchFired }
    writeStatus(s.status, { iterations: s.iterations, usage: s.usage, finishedAt: new Date().toISOString() })
    return s
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    const status: DayBotStatus['status'] = /timeout/i.test(msg) ? 'timeout' : !b.server.alive ? 'dead' : 'error'
    const toolCalls = b.server.toolCallCount - toolCallStart
    const s: DayBotStatus = { bot: b.botId, status, ms: Date.now() - startedAt, error: msg, toolCalls }
    writeStatus(s.status, { error: s.error, finishedAt: new Date().toISOString() })
    if (toolCalls > 0) log(worldRoot, runId, `bot ${b.botId}: chat ${status} but ${toolCalls} tool call(s) made before — will advance via 放松判定`)
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
      setupRes.strategyServer.close(),
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
    chat_step_mode: config.chatStepMode,
    chat_step_days: config.chatStepDays,
    chat_weekday: config.chatWeekday,
    chat_monthly_nth: config.chatMonthlyNth,
    pid: process.pid,
  }
  writeState(worldRoot, runId, initial)

  await runLoop({ worldRoot, runId, config, setupRes, fromCursor: 0 })
}

interface RunLoopArgs { worldRoot: string; runId: string; config: WorldConfig; setupRes: SetupResult; fromCursor: number }

/** 第 N / 2N / 3N … 个交易日（cursor 0-based）算研究日；researchDayEvery=0 关闭。 */
export function isResearchDay(cursor: number, researchDayEvery: number): boolean {
  return researchDayEvery > 0 && ((cursor + 1) % researchDayEvery === 0)
}

/** cursor 是 run 内第几个决策日（1-based）。确定性推导（扫 [0..cursor] 数 chat day），
 *  不用运行时计数器——resume（fromCursor>0）时序数不漂移。372 天 × O(n) 可忽略。 */
export function chatDayOrdinal(cursor: number, dates: string[], mode: 'trading_days' | 'weekly' | 'monthly', chatStepDays: number, opts?: ChatDayOpts): number {
  let n = 0
  for (let c = 0; c <= cursor; c++) { if (isChatDayAt(c, dates, mode, chatStepDays, opts)) n++ }
  return n
}

/** 第 N / 2N / 3N … 个决策日（ordinal 1-based）为深度研究日；deepResearchEvery=0 关闭。
 *  按「决策日序数」而非交易日计——weekly run 里 every=4 ≈ 每 4 周一次深研。 */
export function isDeepResearchDay(ordinal: number, deepResearchEvery: number): boolean {
  return deepResearchEvery > 0 && ordinal % deepResearchEvery === 0
}

/** 交易日 gap：dates[] 里 fromDate → toDate 的间隔（不含 fromDate 当日）。fromDate 不在 dates
 *  中或 toDate 更早 → 返回 Number.MAX_SAFE_INTEGER，触发 forced（相当于"从未深研过"）。 */
export function tradingDaysBetween(dates: string[], fromDate: string | undefined, toDate: string): number {
  if (!fromDate) return Number.MAX_SAFE_INTEGER
  const i = dates.indexOf(fromDate)
  const j = dates.indexOf(toDate)
  if (i < 0 || j < 0 || j < i) return Number.MAX_SAFE_INTEGER
  return j - i
}

export interface DeepResearchStateInput {
  mode: 'ordinal' | 'agent-triggered'
  ordinal: number
  every: number
  maxGapDays: number
  todayDate: string
  lastDeepDate?: string
  tradingDates: string[]
}
export interface DeepResearchStateOut {
  /** 是否允许 bot 今日调 start_research（message.ts 是否渲染触发块或强制块）。 */
  authorized: boolean
  /** 是否强制（forced 日不做 = 违反调度纪律；authorized-not-forced 是可选）。 */
  forced: boolean
  /** 距上次深研的交易日 gap（含今日的偏移）；从未深研过 = MAX_SAFE_INTEGER。 */
  gapDays: number
}

/** 三分支判定：
 *   ordinal：authorized = forced = isDeepResearchDay(ordinal, every)；等价老行为。
 *   agent-triggered：authorized 恒 true；forced = (gapDays >= maxGapDays) —— 达上限系统强制。 */
export function computeDeepResearchState(inp: DeepResearchStateInput): DeepResearchStateOut {
  const gapDays = tradingDaysBetween(inp.tradingDates, inp.lastDeepDate, inp.todayDate)
  if (inp.mode === 'agent-triggered') {
    return { authorized: true, forced: gapDays >= inp.maxGapDays, gapDays }
  }
  const isDR = isDeepResearchDay(inp.ordinal, inp.every)
  return { authorized: isDR, forced: isDR, gapDays }
}


/** ISO 周锚：返回该日期所在自然周的周一（YYYY-MM-DD）。同一周的任意一天得到同一字符串。 */
export function isoWeekKey(isoDate: string): string {
  const d = new Date(isoDate + 'T00:00:00Z')
  const dow = (d.getUTCDay() + 6) % 7 // 周一=0 … 周日=6
  d.setUTCDate(d.getUTCDate() - dow)
  return d.toISOString().slice(0, 10)
}

/** 自然月锚：YYYY-MM。 */
function monthKey(isoDate: string): string { return isoDate.slice(0, 7) }

/** ISO 周几：1=周一 … 7=周日。 */
function isoDow(isoDate: string): number {
  const d = new Date(isoDate + 'T00:00:00Z')
  return ((d.getUTCDay() + 6) % 7) + 1
}

/** weekly：可调决策日（周几 / 第 N 个交易日）的灵活参数。缺省即历史行为。 */
export interface ChatDayOpts { weekday?: number; monthlyNth?: number }

/** weekly：cursor 所在自然周里被选中的决策交易日 cursor。
 *  目标周几 wd(1..5)：取该周内首个「周几 ≥ wd」的交易日；整周都在 wd 之前(罕见,如该周几及其后全是假期)
 *  则取该周最后一个交易日。wd 缺省=1 → 周内首个交易日（历史行为）。 */
function weeklyTargetCursor(cursor: number, dates: string[], wd: number): number {
  const wk = isoWeekKey(dates[cursor])
  let s = cursor; while (s > 0 && isoWeekKey(dates[s - 1]) === wk) s--
  let e = cursor; while (e < dates.length - 1 && isoWeekKey(dates[e + 1]) === wk) e++
  for (let c = s; c <= e; c++) if (isoDow(dates[c]) >= wd) return c
  return e
}

/** monthly：cursor 所在自然月里被选中的决策交易日 cursor。
 *  nth>0：从月初数第 nth 个交易日(1=月初)；nth<0：从月末倒数(-1=月末)。越界夹到首/末。
 *  nth 缺省=1 → 月初（历史行为）。 */
function monthlyTargetCursor(cursor: number, dates: string[], nth: number): number {
  const mk = monthKey(dates[cursor])
  let s = cursor; while (s > 0 && monthKey(dates[s - 1]) === mk) s--
  let e = cursor; while (e < dates.length - 1 && monthKey(dates[e + 1]) === mk) e++
  const len = e - s + 1
  let idx = nth > 0 ? nth - 1 : len + nth // nth<0: -1 → len-1(末)
  if (idx < 0) idx = 0
  if (idx > len - 1) idx = len - 1
  return s + idx
}

/**
 * 本 cursor 是否决策日（唤起 bot chat）。cursor 0 永远是（run 首日建仓）。
 * - trading_days：cursor % chatStepDays === 0（每 N 个交易日，锚定 run 起点；历史行为）。
 * - weekly：落在每个自然周的目标交易日（opts.weekday，缺省周首）。
 * - monthly：落在每个自然月的目标交易日（opts.monthlyNth，缺省月初；负数从月末倒数）。
 * weekly/monthly 由日历边界派生、不随假期漂移；chatStepDays 在这两种模式下被忽略。
 * 首个（可能不完整的）周/月已由 cursor 0 覆盖，故同周/同月不再二次决策——保证「每周期一次决策」。
 */
export function isChatDayAt(cursor: number, dates: string[], mode: 'trading_days' | 'weekly' | 'monthly', chatStepDays: number, opts?: ChatDayOpts): boolean {
  if (cursor === 0) return true
  if (mode === 'weekly') {
    if (isoWeekKey(dates[cursor]) === isoWeekKey(dates[0])) return false // 首周已由 cursor 0 覆盖
    return cursor === weeklyTargetCursor(cursor, dates, opts?.weekday ?? 1)
  }
  if (mode === 'monthly') {
    if (monthKey(dates[cursor]) === monthKey(dates[0])) return false // 首月已由 cursor 0 覆盖
    return cursor === monthlyTargetCursor(cursor, dates, opts?.monthlyNth ?? 1)
  }
  return cursor % chatStepDays === 0
}

/** 上一个决策日的 cursor（严格 < 当前）。首日（无更早决策日）返回自身。 */
export function previousChatCursor(cursor: number, dates: string[], mode: 'trading_days' | 'weekly' | 'monthly', chatStepDays: number, opts?: ChatDayOpts): number {
  for (let c = cursor - 1; c >= 0; c--) { if (isChatDayAt(c, dates, mode, chatStepDays, opts)) return c }
  return cursor
}

export async function runLoop(args: RunLoopArgs): Promise<void> {
  const { worldRoot, runId, config, setupRes, fromCursor } = args
  const dates = setupRes.tradingDates
  const days: DaySummary[] = []
  const perBotTimeoutMs = config.perBotTimeoutSeconds * 1000
  const researchDayTimeoutMs = config.researchDayTimeoutSeconds * 1000
  const deepResearchEvery = config.deepResearchEvery ?? 0
  const deepResearchTimeoutMs = (config.deepResearchTimeoutSeconds ?? Math.max(config.researchDayTimeoutSeconds, 2400)) * 1000
  const deepResearchMode: 'ordinal' | 'agent-triggered' = config.deepResearchMode ?? 'ordinal'
  const deepResearchMaxGapDays = config.deepResearchMaxGapDays ?? 4
  // dead = bot server 进程已经退出，无可挽救 → 加入 brokenBots，剩余日子直接 writeSkippedDeadBot
  // 跳过。timeout 不进这个集合：当天记 timeout，但下一天循环顶部会 restartBot 重启该 bot 的
  // server（kill 老的、spawn 新的），这样后续日子能恢复正常 chat。重启的必要性：research-loop-ts
  // 不串行化、也没有 chat cancel —— 客户端 timeout 后老 chat 还在 server 端跑，会继续发 tool 调用，
  // 这些调用会读到「下一天」的 currentDateRef / mem0 当前日期 / fund-portfolio 模拟时间，并跟新一天
  // 的 chat 并发写同一个 session jsonl。
  const brokenBots = new Set<string>()
  const needsRestart = new Set<string>()
  let aborted = false
  const onSigint = (): void => { aborted = true; log(worldRoot, runId, 'SIGINT received — will abort after current day') }
  process.on('SIGINT', onSigint)
  // Immediate stop / pause: `world stop` / `world pause` write a STOP / PAUSE sentinel. Both kill
  // the *current* day right now via the same mechanism — shut bot servers down, which makes
  // their pending chat requests reject (stdout closes) → mapWithConcurrency resolves → the loop
  // bails BEFORE close_my_day and BEFORE advancing the cursor. The only difference is teardown
  // status: stop → 'aborted' (terminal, non-resumable, cursor abandoned); pause → 'paused'
  // (resumable, cursor untouched so resume re-runs this whole day from scratch — each world day
  // is an isolated session, so a fresh re-run is safe).
  //
  // Previously STOP was only checked at the day-boundary (top of the for-loop), forcing the user
  // to wait for all bots' chats to drain — with 18+ bots × multi-minute budgets that meant 5-15+
  // minutes from click to actual stop, and the dashboard's "正在跑 12/291" indicator hid the wait.
  // The mid-day kill brings STOP in line with PAUSE's responsiveness.
  let stopRequested = false
  let pauseRequested = false
  const killBotsOnce = (label: string): void => {
    log(worldRoot, runId, `${label} requested — killing current day`)
    for (const b of setupRes.bots) { void b.server.shutdown({ timeoutMs: 2000 }).catch(() => { /* ignore — we're tearing down */ }) }
  }
  const controlPoll = setInterval(() => {
    // STOP takes precedence over PAUSE — if both sentinels exist (or the user double-clicks),
    // terminal beats resumable. We only need to kill bots once; subsequent ticks are no-ops.
    if (!stopRequested && existsSync(P.stopFile(worldRoot, runId))) {
      stopRequested = true
      if (!pauseRequested) killBotsOnce('stop')
      return
    }
    if (!pauseRequested && existsSync(P.pauseFile(worldRoot, runId))) {
      pauseRequested = true
      if (!stopRequested) killBotsOnce('pause')
    }
  }, 1000)
  try {
    for (let cursor = fromCursor; cursor < dates.length; cursor++) {
      if (aborted || stopRequested || existsSync(P.stopFile(worldRoot, runId))) { log(worldRoot, runId, 'stop requested — aborting'); await teardown(worldRoot, runId, setupRes, 'aborted', days); return }
      if (pauseRequested || existsSync(P.pauseFile(worldRoot, runId))) { log(worldRoot, runId, 'pause requested — pausing (resumable from this day)'); await teardown(worldRoot, runId, setupRes, 'paused', days); return }
      // Drain pending restarts BEFORE today's chat goes out. Each timeout from yesterday
      // owns a still-in-flight chat on its bot's server; we kill+spawn so today's chat
      // hits a clean server with no leftover tool-call stream.
      if (needsRestart.size > 0) {
        for (const botId of needsRestart) {
          try {
            await setupRes.restartBot(botId)
            log(worldRoot, runId, `bot ${botId}: server restarted after prior timeout`)
          } catch (err) {
            log(worldRoot, runId, `bot ${botId}: server restart FAILED (${err instanceof Error ? err.message : String(err)}) — banning for rest of run`)
            brokenBots.add(botId)
          }
        }
        needsRestart.clear()
      }
      const date = dates[cursor]
      setupRes.currentDateRef.value = date
      // Pin the loop processes' system-prompt date to today's world day before any
      // chat goes out. See WORLD_DATE_OVERRIDE_FILE in startBotServer above.
      writeFileSync(P.worldDateOverrideFile(worldRoot, runId), date)
      {
        const state = readState(worldRoot, runId)
        writeState(worldRoot, runId, { ...state, current_date: date, updated_at: new Date().toISOString() })
      }
      // 决策日判定：trading_days 取模 / weekly / monthly 日历对齐（详见 isChatDayAt）。中间天系统侧
      // settle + close 仍按日推进，simulated_datetime 同样每天更新——只是 bot 不被叫起。cursor 0 永远
      // 是 chat day（与 isFirstDay 对齐）。
      const chatDayOpts = { weekday: config.chatWeekday, monthlyNth: config.chatMonthlyNth }
      const isChatDay = config.skipChat ? false : isChatDayAt(cursor, dates, config.chatStepMode, config.chatStepDays, chatDayOpts)
      // 距上次决策已过几个交易日 + 上次决策日，用于周期感知 prompt（让低频 bot 知道这是周/月度再平衡，
      // 下方数据块覆盖的是整段区间而非单日）。首日/日度 = 1，不渲染周期块。
      const prevChatCursor = previousChatCursor(cursor, dates, config.chatStepMode, config.chatStepDays, chatDayOpts)
      const periodTradingDays = cursor - prevChatCursor
      const periodSinceDate = periodTradingDays > 0 ? dates[prevChatCursor] : null
      // 系统侧 settle：T+1 收口。每天 chat **之前** 把所有 order_date < today 的 pending 单按
      // reference_nav 结算（BUY → 持仓增加 + 释放 cash_in_transit；SELL → 现金回流 + 释放 pending_sell）。
      // close_my_day 不做这件事，所以必须独立调一次。bot 在 BOT_ONLY 端口看不到 settle。
      // Day 1 (cursor=0) 也调，no-op 安全（没有更早的 pending 单）。
      // chatStepDays > 1 时中间天也调——系统视角的"每日开盘"不能跳。
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
      if (!isChatDay) {
        log(worldRoot, runId, `day ${cursor + 1}/${dates.length}: ${date} [skip-chat step=${config.chatStepDays}] — system-side settle/close only`)
      }
      let statuses: DayBotStatus[] = []
      if (isChatDay) {
      const isResearch = isResearchDay(cursor, config.researchDayEvery)
      const isFirstDay = cursor === 0
      // First day shares research-day's wider budget: full rules + cold-start onboarding
      // (discover_tools, read SOUL/IDENTITY, query holdings/cooldown, web_fetch
      // sanity check) consistently spills past a 60s budget.
      // Research-day budget is the right ceiling for that workload too, so reuse it
      // instead of adding another knob.
      // 周/月度决策日本身就是一次重再平衡（覆盖整段区间），等同研究日，给更宽预算——无需再单独配
      // research_day_every 去命中它们。
      const useExtendedBudget = isFirstDay || isResearch || periodTradingDays > 1
      // 深度研究日（第 N/2N/3N 个决策日）：给最宽预算（覆盖引擎内研究 budget + 常规决策），
      // 并让 message.ts 注入【深度研究日】授权块。deepResearchEvery=0 时恒 false（历史行为）。
      const chatOrdinal = chatDayOrdinal(cursor, dates, config.chatStepMode, config.chatStepDays, chatDayOpts)
      const stateNow = readState(worldRoot, runId)
      const drState = computeDeepResearchState({
        mode: deepResearchMode,
        ordinal: chatOrdinal,
        every: deepResearchEvery,
        maxGapDays: deepResearchMaxGapDays,
        todayDate: date,
        lastDeepDate: stateNow.last_deep_research_date,
        tradingDates: dates,
      })
      const isDeepResearch = drState.forced
      const isDeepAuthorized = drState.authorized
      // agent-triggered 下 authorized-not-forced 也给中间档预算：允许 bot 若真选择研究不被 900s 卡死。
      // 常规日 timeout 保持 perBotTimeoutMs（用户明确不加压）。
      const timeoutMs = drState.forced
        ? deepResearchTimeoutMs
        : (isDeepAuthorized && deepResearchMode === 'agent-triggered'
            ? Math.max(Math.floor(deepResearchTimeoutMs / 2), perBotTimeoutMs * 2)
            : (useExtendedBudget ? researchDayTimeoutMs : perBotTimeoutMs))
      const stepTag = config.chatStepMode === 'weekly' ? `[weekly@dow${config.chatWeekday ?? 1}]`
        : config.chatStepMode === 'monthly' ? `[monthly#${config.chatMonthlyNth ?? 1}]`
        : (config.chatStepDays > 1 ? `[step=${config.chatStepDays}d]` : '')
      const tagBits = [isFirstDay ? '[first day]' : '', isResearch ? '[research day]' : '', drState.forced ? `[deep-research#forced gap=${drState.gapDays}]` : (isDeepAuthorized && deepResearchMode === 'agent-triggered' ? `[deep-research#authorized gap=${drState.gapDays}]` : ''), periodTradingDays > 1 ? `[+${periodTradingDays}td]` : '', stepTag].filter(Boolean).join(' ')
      log(worldRoot, runId, `day ${cursor + 1}/${dates.length}: ${date}${tagBits ? ' ' + tagBits : ''} — sending to ${config.bots.length} bot(s) (timeout=${Math.floor(timeoutMs / 1000)}s)`)
      const quotesAbs = resolve(P.quotesFile(worldRoot, date))
      statuses = await mapWithConcurrency(setupRes.bots, config.concurrency, async (b) => {
        const botBuyableFundCodes = setupRes.buyableCodesByBot[b.botId] ?? config.buyableFundCodes
        // Prefetch the per-bot daily context (account snapshot, recent PnL,
        // held-fund NAV, major indices) so the bot doesn't have to spend
        // round-trips re-discovering routine inputs every morning. Talks to
        // simworld via the upstream URL (proxy adds simulated_datetime only
        // on the bot path — we don't need that overhead from world's side).
        // Best-effort: each fetcher returns null on error, renderer just
        // skips the corresponding block. A simworld blip won't break the day.
        // 多基金 bot（bot101/102/103）的费率块按"持仓 + 日度报告推荐载体"子集塞，
        // 单基金 / 多资产 bot 保持传全 buyable（池子小）。目的：把 daily prompt 里
        // 全池 759 只的费率大表（~30k tokens）砍到 ~10 只（~500 tokens），减少 LLM prefill 耗时。
        const relevantFundCodes = botKindOf(b.botId) === 'multi-fund'
          ? computeRelevantFundCodesForBot(P.fundDbFile(worldRoot), b.botId, runId, date)
          : undefined
        const dailyContext = await fetchDailyContext({
          runId, botId: b.botId, asOfDate: date,
          fundMcpCli: config.fundMcpCli,
          simworldUrl: config.simworldUpstreamUrl,
          // dates[0] anchors the benchmark cumulative %. Without it the
          // benchmark fetcher can't decide "since when"; with it the bot sees
          // alpha-since-run-start in the PnL trend block.
          runStartDate: dates[0],
          // 单标的择时基准：用本轮买池的 NAV B&H 当对照（单只 → 该基金 B&H；多只 → 等权篮子）。
          buyableFundCodes: botBuyableFundCodes,
          // 费率块专用：多基金 bot 传子集，其它 bot 未传 → 回退全 buyable（daily-context.ts 内部处理）。
          relevantFundCodes,
        })
        // 滚动 history window：从前几个交易日的 session jsonl 抽 digest（去掉工具结果原文），
        // 按 20000 字符预算切割。超 budget 时用主模型（openclaw.json 的 default route）按 4 维度
        // 压缩老的 60%。Day 1 时 sessions.json 还没有任何记录，返回空字符串。
        // best-effort：抽取/压缩失败不阻塞 chat。
        let historyWindow = ''
        try {
          const hw = await buildHistoryWindow({
            rlOpenclawDir: P.rlOpenclawDir(worldRoot, runId),
            botId: b.botId,
            beforeDate: date,
            // 压缩端点首选本 bot 的 research-loop.yaml（model.primary，跟 bot 当前 key 一致）；
            // 解析不到再回退 openclaw.json。修复点：旧版只读 openclaw.json 的 zai-coding-plan
            // provider，key 与回测端点脱节 → 401 → 整窗清空。
            rlConfigPath: join(P.shadowWorkspaceDir(worldRoot, runId, b.botId), 'config', 'research-loop.yaml'),
            openclawJsonPath: openclawJsonSource(config),
          })
          historyWindow = hw.markdown
          if (hw.dayCount > 0) {
            log(worldRoot, runId, `bot ${b.botId} history window ${date}: ${hw.dayCount} days (${hw.recentDays} recent + ${hw.compactedDays} compacted), ${hw.totalChars} chars`)
          }
        } catch (err) {
          log(worldRoot, runId, `bot ${b.botId} history window ${date} FAILED (continuing without): ${err instanceof Error ? err.message : String(err)}`)
        }
        // Bot 的 methodology 由 research-loop 每次 chat splice 进 system prompt 的
        // ## METHODOLOGY.md section，daily message 只附短提示（METHODOLOGY_DAY1_HINT /
        // METHODOLOGY_DAYN_HINT），不重复注入正文。
        // belief-context（市场环境判断的滚动摘要）：best-effort 注入到 daily message。
        // build 失败不阻塞 chat 主流程——空串等于不渲染对应 section。
        // reporter 模式不注入 belief-context（那是投资 bot 的市场环境判断滚动摘要 + 输出 schema，
        // 对"市场研究员"是噪声/误导）。
        const built = config.reporterMode
          ? { block: '', latest: null }
          : await buildBeliefContext(b.botId, runId, date).catch((e: unknown) => {
              log(worldRoot, runId, `[belief-context] bot ${b.botId} ${date} build failed: ${e instanceof Error ? e.message : String(e)}`)
              return { block: '', latest: null }
            })
        const beliefBlock = built.block
        // 末条 standing belief 的 t+5/t+20 p_up，喂给 message.ts 的 belief↔仓位 言行一致核对块。
        const latestBelief = built.latest
        // 周期块：仅当真的跳过了交易日（periodTradingDays > 1）才注入。区间涨跌直接复用
        // dailyContext.benchmark 的「自 run 起点累计 %」相减得到，无需另拉行情。
        let periodInfo: { tradingDays: number; sinceDate: string; benchMovePct: number | null } | undefined
        if (periodTradingDays > 1 && periodSinceDate) {
          let benchMovePct: number | null = null
          const bench = dailyContext.benchmark
          if (bench) {
            const from = bench.pointsByDate[periodSinceDate]
            const to = bench.latestCumulativePct
            if (typeof from === 'number' && typeof to === 'number') benchMovePct = to - from
          }
          periodInfo = { tradingDays: periodTradingDays, sinceDate: periodSinceDate, benchMovePct }
        }
        const kind = botKindOf(b.botId)
        const marketReports = kind === "multi-fund"
          ? readMarketReportsForInjection(P.fundDbFile(worldRoot), date)
          : undefined
        // 当日研究室简报：仅单指数 run（非 reporter、非多基金），按本 run 的 strategy_id 路由到 res 研究室，
        // 取每室规范主报最新一份拼成参考信号。res 根 = <repoRoot>/.openclaw（worldRoot=<world>/runtime，
        // 故 ../../.openclaw = /home/rooot/.openclaw），路由表在 config/res-routing.json。
        let briefing = ""
        if (!config.reporterMode && kind === "single-fund") {
          const strategyId = config.botAssignments?.[b.botId]?.strategyId
          try {
            briefing = assembleBriefing({
              strategyId,
              resRoot: resolve(worldRoot, "..", "..", ".openclaw"),
              routingPath: join(worldRoot, "..", "config", "res-routing.json"),
              asOfDate: date,
            })
            if (briefing) log(worldRoot, runId, "[briefing] bot " + b.botId + " " + date + ": injected (strategy=" + (strategyId ?? "?") + ", " + briefing.length + " chars)")
          } catch (e) {
            log(worldRoot, runId, "[briefing] bot " + b.botId + " " + date + ": skipped: " + (e as Error).message)
          }
        }
        let intradayMarketBlock = ""
        if (!config.reporterMode && kind === "multi-fund" && marketReports) {
          const rt = await fetchIntradayQuoteBlock({ repoRoot: resolve(worldRoot, "..", ".."), date, reports: marketReports })
          if (rt.block) {
            intradayMarketBlock = rt.block
            log(worldRoot, runId, "[intraday-market] bot " + b.botId + " " + date + ": injected " + rt.instruments.length + " instruments")
          } else if (rt.error) {
            log(worldRoot, runId, "[intraday-market] bot " + b.botId + " " + date + ": skipped after fetch error: " + rt.error)
          }
        }
        // reporter 模式：daily message 极简——研究员的任务（产出哪份研报、读哪些上游、怎么 submit）
        // 已全在其 AGENTS.md/METHODOLOGY.md（splice 进 system prompt）里写死。不注入持仓/buyable/
        // 交易规则/行情预取，避免把研究员当交易员。**不含日期**（PIT：reporter 不该知道世界日）。
        const message = config.reporterMode
          ? '新的一期市场研究。请严格按你的 AGENTS.md 与 METHODOLOGY.md：先读取所需上游报告（若有），用 simworld-data 工具端到端完成本期分析，产出研报正文与结构化字段，最后调用 submit_market_report 提交。提交成功即结束本期，不要做交易类操作。'
          : renderDailyMessage({
          worldRoot, date, isFirstDay,
          botId: b.botId,
          quotesPath: quotesAbs,
          buyableFundCodes: botBuyableFundCodes,
          injectedSkills: readInjectedPipelineSkills(worldRoot, runId, b.botId),
          // 系统预读注入三份市场研报：仅多基金权益 bot（multi-fund，bot101/102/103）——与 message.ts
          // 的注入分流口径一致；single-fund / multi-asset 不读、不注入，省一次 DB 查询。
          marketReports,
          // 当日研究室简报：仅单指数 run 非空（message.ts 也对 multi-fund 门控，双保险）。
          briefing,
          intradayMarketBlock,
          dailyContext,
          historyWindow,
          beliefBlock,
          latestBelief,
          periodInfo,
          // 仅 Day 1 fullRules 用到——message.ts 自己门控；这里无脑传即可，Day N 会丢弃。
          tradingDaysTotal: setupRes.tradingDates.length,
          // 深度研究实验：enabled = run 级（措辞从"研究模式禁用"换成"仅限深研日"）；
          // deepResearchDay = 本决策日注入【深度研究日】授权块。every=0 时两者恒 false/undefined。
          deepResearchEnabled: deepResearchEvery > 0 || deepResearchMode === 'agent-triggered',
          deepResearchDay: isDeepResearch,
          deepResearchMode,
          deepResearchForced: drState.forced,
          deepResearchAuthorized: drState.authorized,
          deepResearchGapDays: drState.gapDays,
          deepResearchMaxGapDays,
          deepResearchLastDate: stateNow.last_deep_research_date,
        })
        if (brokenBots.has(b.botId)) return writeSkippedDeadBot(worldRoot, runId, date, message, b)
        return chatOneBot(worldRoot, runId, date, message, timeoutMs, b)
      })
      for (const s of statuses) {
        if (s.status === 'dead') brokenBots.add(s.bot)
        else if (s.status === 'timeout') needsRestart.add(s.bot)
      }
      // The poller killed the bot servers mid-chat → bail BEFORE close_my_day and BEFORE
      // advancing the cursor. STOP wins over PAUSE if both fired (or were double-clicked).
      // cursor stays on this day so paused-resume re-runs it from scratch; for aborted the
      // cursor is moot (terminal status, no resume).
      if (stopRequested) { log(worldRoot, runId, `stop requested — dropping day ${date} (aborting)`); await teardown(worldRoot, runId, setupRes, 'aborted', days); return }
      if (pauseRequested) { log(worldRoot, runId, `pause requested — dropping day ${date} (resume will re-run it)`); await teardown(worldRoot, runId, setupRes, 'paused', days); return }
      // "放松"判定：当日只要任一 bot 至少做了一次 tool call 就算推进（即便最终被 timeout / dead /
      // chat_error 收尾）——bot14 在 1-06 的真实案例：chat 在 200000ms 客户端 timeout 触发后 8ms
      // 才吐出 reply，明明已经跑了 18 轮工具，只是擦边没赶上。失败只针对 toolCalls===0 的情况：
      // 0s 垃圾日（connection refused → chat 秒挂 → 一个工具都没调）；那种 pause 在当天，resume
      // 重跑。
      const failing = statuses.filter(s => s.status !== 'ok' && (s.toolCalls ?? 0) === 0)
      if (failing.length > 0) {
        const reasons = failing.map(s => `${s.bot}=${s.status}${s.error ? `(${s.error.slice(0, 80)})` : ''}`).join(' ')
        log(worldRoot, runId, `day ${date} failure — pausing run (resume will re-run this day): ${reasons}`)
        days.push({ date, bots: statuses })
        await teardown(worldRoot, runId, setupRes, 'paused', days)
        return
      }
      } // end if (isChatDay)
      // 系统侧 close：每个 bot（不论 chat 状态如何）跑一次 close_my_day 落收盘快照。
      // bot 在 BOT_ONLY 端口看不到 close_my_day，只能 world 触发；这是"每天收盘核算"的硬契约。
      // snapshot 文本写到 <botDayDir>/close_my_day.json，方便后续审阅。
      if (config.fundMcpCli && !config.skipClose) {
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
      if (isChatDay) {
        days.push({ date, bots: statuses })
        log(worldRoot, runId, `day ${date} done: ${statuses.map(s => `${s.bot}=${s.status}`).join(' ')}`)
      } else {
        log(worldRoot, runId, `day ${date} done: [skip-chat] settle/close only`)
      }
      const st = readState(worldRoot, runId)
      // agent-triggered 下：若任一 bot 当日调了 start_research → 更新 last_deep_research_date。
      // ordinal 模式：仍按 forced 日无条件更新（bot 不调也算走过一个深研窗口，避免下一日 gap 累加）。
      const fired = statuses.some(s => s.deepResearchFired === true)
      const nextLastDeep = fired ? date : st.last_deep_research_date
      writeState(worldRoot, runId, { ...st, cursor: cursor + 1, updated_at: new Date().toISOString(), last_deep_research_date: nextLastDeep })
      if (fired) log(worldRoot, runId, `day ${date}: start_research fired — last_deep_research_date <- ${date}`)
    }
    await teardown(worldRoot, runId, setupRes, 'done', days)
  } catch (err) {
    log(worldRoot, runId, `loop error: ${err instanceof Error ? err.message : String(err)}`)
    await teardown(worldRoot, runId, setupRes, 'failed', days)
    throw err
  } finally {
    clearInterval(controlPoll)
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
  // running = 崩溃残留(teardown 没跑、status 没翻终态)；paused = world pause 优雅停。两者都可续。
  // done/aborted/failed/setup 不可续。
  if (state.status !== 'running' && state.status !== 'paused') throw new Error(`cannot resume: state status is "${state.status}", nothing to resume`)
  if (state.loop !== config.loop) {
    throw new Error(`resume: state loop="${state.loop}" but world.yaml loop="${config.loop}" — refuse to resume across loop change`)
  }
  // 清掉可能残留的 STOP / PAUSE 哨兵（否则 resume 会立刻被它中止/暂停）
  rmSync(P.stopFile(worldRoot, runId), { force: true })
  rmSync(P.pauseFile(worldRoot, runId), { force: true })
  // resume = 续跑既有账户，setup 的 init_fund_account 绝不能带 --reset：force=true 会清掉
  // 本 run 的 holdings/actions/snapshots 并把现金重置成初始资金，等于把续跑变成"从断点重开"。
  // 强制 fundInitReset=false（无视 yaml），让 init 对已存在账户走 no-op，账本原样保留。
  const setupRes = await setup({ worldRoot, config: { ...config, fundInitReset: false }, runId, startBotServer: opts.startBotServer })
  // 若 calendar/replay 变了导致交易日序列对不上，拒绝
  if (setupRes.tradingDates.length !== state.trading_dates.length || setupRes.tradingDates[0] !== state.trading_dates[0] || setupRes.tradingDates[setupRes.tradingDates.length - 1] !== state.trading_dates[state.trading_dates.length - 1]) {
    for (const b of setupRes.bots) { try { await b.server.shutdown({ timeoutMs: 2000 }) } catch { /* ignore */ } }
    try { await setupRes.memory.close() } catch { /* ignore */ }
    throw new Error('resume: trading-date sequence changed since the run started; refuse to resume')
  }
  setupRes.currentDateRef.value = state.trading_dates[Math.min(state.cursor, state.trading_dates.length - 1)]
  // Adopt the run for THIS process so orphan-detection sees a fresh PID; the prior
  // PID may have died (that's how we got here) or, worse, been recycled to an
  // unrelated process — leaving the stale one would mis-direct future health checks.
  // Also flip status back to 'running': a resumed run is running, not paused. Keeping
  // status='paused' on disk forces every reader (dashboard, orphan-monitor) to do a
  // liveness-based effective-status translation, which goes stale during the setup
  // window and lies to the UI. Drop aborted_reason if some prior orphan-reaper stamped
  // it — we just resurrected the run, that audit note is no longer true.
  const { aborted_reason: _drop, ...rest } = state
  writeState(worldRoot, runId, { ...rest, status: 'running', pid: process.pid, updated_at: new Date().toISOString() })
  await runLoop({ worldRoot, runId, config, setupRes, fromCursor: state.cursor })
}

// Lifecycle control (stop/pause sentinels) lives in run-control.ts so out-of-process
// callers can trigger it without run.ts's heavy module graph. Re-exported here for
// backwards-compatible imports (cli.ts, tests).
export { requestStop, requestPause } from './run-control.ts'
