// market-reports pre-pass —— 独立编排驱动（不使用 world 的 runWorld/runLoop 引擎）。
//
// 目标：预生成全局共享的三类市场研究报告（market_context / market_mainline / mainline_rotation），
// 由三个 reporter agent 在每个决策日各产出一份并经 strategy-server.submit_market_report 落
// fund.db 的 market_reports 表。回测的各投资 bot 之后经 get_market_report（PIT）读取。
//
// 与 world 回测系统**物理解耦**：
//   - 不调 runWorld/runLoop（那套含交易/结算/belief/每日消息的日度引擎）；本文件自己编排月度循环。
//   - run 目录写到独立的 --out-dir（默认 world/runtime-prepass），**不进 world/runtime/runs**，
//     因此不出现在 18888 看板的 run 列表、不与真回测共用 runtime。
//   - 只复用组件级模块（simworld-proxy / strategy-server / memory-server / BotServer / 影子工作区
//     / research-loop.yaml 写入）当库——它们是基础设施，不是"引擎"。
//   - 全程端口都是临时随机端口（memory/proxy/strategy 各自 listen :0），不占用任何固定端口。
//
// 相对 runWorld 的额外好处：**逐 reporter 落库校验 + 失败重试**——研报 submit 后立即查 DB 确认落库，
// 没落（LLM 瞬时错误等）就重试，消除"chat_error 后静默跳过留洞"的问题；**DB 幂等按 (日期,类型) 跳过**，
// 中断后直接重跑即续，无需 state.json。
//
// 用法（在 world/ 下）：
//   npm run prepass                                   # 默认 config + 默认 out-dir，DB 已有则跳过
//   node --experimental-strip-types src/market-reports/prepass-driver.ts --config config/world-market-reports.yaml
//   可选：--run-id <id> --out-dir <dir> --retries <n> --from YYYY-MM-DD --to YYYY-MM-DD

import { join, resolve, isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdirSync, writeFileSync, copyFileSync, existsSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { loadWorldConfig, type WorldConfig } from '../config.ts'
import { loadCalendar, computeTradingDates } from '../calendar.ts'
import { MemoryStore } from '../memory-server/store.ts'
import { createMemoryServer } from '../memory-server/server.ts'
import { createSimworldProxy } from '../simworld-proxy/server.ts'
import { createStrategyServer } from '../strategy-server/server.ts'
import { buildShadowWorkspace } from '../shadowWorkspace.ts'
import { BotServer } from '../botServer.ts'
import {
  botServerArgv, loopConfigPath, proxyEnvSupplement, openclawJsonSource,
  generateRlConfig, writeResearchLoopYaml,
} from '../run.ts'
import * as P from '../paths.ts'

// reporter bot → 它产出的 report_type
const REPORT_TYPE_BY_BOT: Record<string, string> = {
  'reporter-context': 'market_context',
  'reporter-mainline': 'market_mainline',
  'reporter-rotation': 'mainline_rotation',
}

// 每期发给 reporter 的 message（任务全在其 AGENTS.md/METHODOLOGY.md，这里只给触发，不含日期=PIT）。
const REPORTER_MESSAGE =
  '新的一期市场研究。请严格按你的 AGENTS.md 与 METHODOLOGY.md：先读取所需上游报告（若有），' +
  '按权威数据源完成本期分析（主线先用 strategy-mcp.get_v5_mainline_plan），产出研报正文与结构化字段，最后调用 submit_market_report 提交。' +
  '提交成功即结束本期，不要做交易类操作。'

function argVal(argv: string[], flag: string): string | undefined {
  const i = argv.indexOf(flag)
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined
}

/** 取窗口内每个自然月的第一个交易日。 */
function monthFirstTradingDays(dates: string[]): string[] {
  const out: string[] = []
  let lastKey = ''
  for (const d of dates) {
    const key = d.slice(0, 7) // YYYY-MM
    if (key !== lastKey) { out.push(d); lastKey = key }
  }
  return out
}

/** 日期所在 ISO 周的周一（YYYY-MM-DD），作为周分组 key。 */
function isoWeekMonday(d: string): string {
  const dt = new Date(`${d}T00:00:00Z`)
  dt.setUTCDate(dt.getUTCDate() - (dt.getUTCDay() + 6) % 7)  // Mon=0..Sun=6 回退到周一
  return dt.toISOString().slice(0, 10)
}

/** 取窗口内每个 ISO 周（周一起算）的第一个交易日。 */
function weekFirstTradingDays(dates: string[]): string[] {
  const out: string[] = []
  let lastKey = ''
  for (const d of dates) {
    const key = isoWeekMonday(d)
    if (key !== lastKey) { out.push(d); lastKey = key }
  }
  return out
}

/** 该 (report_type, as_of_date) 是否已落库（幂等跳过 / 重试后校验）。 */
function reportExists(dbPath: string, reportType: string, asOf: string): boolean {
  const sql = `SELECT 1 FROM market_reports WHERE report_type='${reportType}' AND scope='global' AND as_of_date='${asOf}' LIMIT 1;`
  try {
    const out = execFileSync('sqlite3', ['-cmd', '.timeout 5000', dbPath, sql], { encoding: 'utf8' }).trim()
    return out === '1'
  } catch { return false }
}

function log(msg: string): void {
  process.stdout.write(`[prepass] ${msg}\n`)
}

function sleep(ms: number): Promise<void> {
  return new Promise(res => setTimeout(res, Math.max(0, ms)))
}

// ── 单个 reporter 单日落库的「重试 + 硬重启兜底」编排 ────────────────────────────
//
// 两层防御，针对两类不同的瞬时故障：
//   阶段一 同进程内重试：处理纯 LLM 抖动（chat_llm 超时、偶发空回复）。廉价，复用已 warm 的
//          子进程与其 MCP 连接。
//   阶段二 硬重启兜底：处理**进程级 MCP 初始化瞬时失败**——研究循环 rust 子进程在启动窗口里
//          没连上上游 MCP（"simworld-data 0 tools / strategy-mcp init_failed / submit_market_report
//          不可用"），这个失败被缓存在进程内，同进程再 chat 也救不回来（2026-06-23 market_context
//          缺失即此故障）。唯一解是 shutdown 子进程后重新 BotServer.start，逼出一次全新的 MCP init，
//          并换一个干净的 session_key 让 agent 重新发现工具、从头研究。
//
// 全部 IO（落库校验 / chat / 重启 / sleep / 日志）依赖注入，便于单测（见 test/prepass-fallback.test.ts）。
export interface ReporterFallbackDeps {
  /** 该 reportType@date 是否已落库（每次调用都实时查，作为唯一成功判据）。 */
  reportExists: () => boolean
  /** 在当前子进程上跑一次 chat（传入本次使用的 session_key）。抛错即视为本次失败。 */
  chat: (sessionKey: string) => Promise<{ chatErr?: string }>
  /** 硬重启该 reporter 的子进程（全新 MCP 连接）。返回 false 表示重启失败、兜底无法继续。 */
  restart: () => Promise<boolean>
  sleep: (ms: number) => Promise<void>
  log: (msg: string) => void
  /** 同进程内重试次数（阶段一），不含首次。 */
  retries: number
  /** 硬重启兜底次数（阶段二）。 */
  hardRestarts: number
  retryBackoffMs?: number
  restartBackoffMs?: number
}

/** 返回 true = 报告已落库（含幂等命中）；false = 重试与硬重启兜底均未落库。 */
export async function generateReportWithFallback(
  botId: string,
  sessionKeyBase: string,
  deps: ReporterFallbackDeps,
): Promise<boolean> {
  const retryBackoffMs = deps.retryBackoffMs ?? 5000
  const restartBackoffMs = deps.restartBackoffMs ?? 8000
  if (deps.reportExists()) return true   // 幂等：已落库直接成功

  const runOnce = async (sessionKey: string): Promise<boolean> => {
    let chatErr: string | undefined
    try { chatErr = (await deps.chat(sessionKey)).chatErr }
    catch (e) { chatErr = e instanceof Error ? e.message : String(e) }
    if (deps.reportExists()) return true   // 落库校验才算成功
    if (chatErr) deps.log(`  ${botId} 本次未落库（chat_error: ${chatErr.slice(0, 80)}）`)
    return false
  }

  // 阶段一：同进程内重试
  for (let attempt = 0; attempt <= deps.retries; attempt++) {
    if (attempt > 0) { deps.log(`  ${botId} 重试 ${attempt}/${deps.retries}`); await deps.sleep(retryBackoffMs) }
    if (await runOnce(sessionKeyBase)) return true
  }

  // 阶段二：硬重启兜底（全新 MCP 连接 + 干净 session_key）
  for (let r = 1; r <= deps.hardRestarts; r++) {
    deps.log(`  ${botId} 同进程重试耗尽仍未落库 → 硬重启 server 第 ${r}/${deps.hardRestarts} 次（全新 MCP 连接）`)
    if (!(await deps.restart())) { deps.log(`  ${botId} server 重启失败，放弃兜底`); break }
    await deps.sleep(restartBackoffMs)
    if (await runOnce(`${sessionKeyBase}-rs${r}`)) return true
  }
  return false
}

async function main(argv = process.argv.slice(2)): Promise<number> {
  if (argv.includes('-h') || argv.includes('--help')) {
    process.stdout.write(
      'market-reports pre-pass（独立驱动，不走 world 引擎）\n' +
      '  --config <yaml>   缺省 config/world-market-reports.yaml（取 calendar/replay/simworld/model/bots）\n' +
      '  --out-dir <dir>   run 目录根，缺省 <cwd>/runtime-prepass（独立于 world/runtime）\n' +
      '  --run-id <id>     缺省 prepass-<ts>\n' +
      '  --from / --to     覆盖 config 的 replay 窗口（YYYY-MM-DD）\n' +
      '  --freq <f>        决策日频率 monthly（缺省，每月第一个交易日）| weekly（每 ISO 周第一个交易日）| daily（每个交易日）\n' +
      '  --retries <n>     每个 reporter 同进程内落库失败重试次数，缺省 2\n' +
      '  --hard-restarts <n> 同进程重试耗尽后，硬重启子进程（全新 MCP 连接）兜底次数，缺省 1\n')
    return 0
  }

  const configPath = argVal(argv, '--config') ?? 'config/world-market-reports.yaml'
  const config: WorldConfig = loadWorldConfig(configPath)
  if (!config.reporterMode) { process.stderr.write(`refuse: ${configPath} 未开启 reporter_mode。\n`); return 2 }
  if (config.loop === 'openclaw-pi') { process.stderr.write('refuse: pre-pass 只支持 research-loop。\n'); return 2 }

  const outRoot = resolve(argVal(argv, '--out-dir') ?? join(process.cwd(), 'runtime-prepass'))
  const runId = argVal(argv, '--run-id') ?? `prepass-${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}`
  const retries = Math.max(0, parseInt(argVal(argv, '--retries') ?? '2', 10) || 0)
  const hardRestarts = Math.max(0, parseInt(argVal(argv, '--hard-restarts') ?? '1', 10) || 0)
  const fundDbPath = resolve(join(process.cwd(), '..', 'data', 'fund.db'))
  const from = argVal(argv, '--from') ?? config.replay.from
  const to = argVal(argv, '--to') ?? config.replay.to
  const freq = argVal(argv, '--freq') ?? 'monthly'
  if (freq !== 'monthly' && freq !== 'weekly' && freq !== 'daily') { process.stderr.write(`refuse: --freq 只支持 monthly|weekly|daily，得到 ${freq}。\n`); return 2 }

  const reporters = config.bots.filter(b => b in REPORT_TYPE_BY_BOT)
  if (reporters.length === 0) { process.stderr.write('refuse: config.bots 里没有任何 reporter-*。\n'); return 2 }

  // ── 决策日 ──────────────────────────────────────────────────────────────
  const cal = loadCalendar(config.calendar)
  const tradingDates = computeTradingDates(cal, from, to)
  const decisionDays = freq === 'daily' ? tradingDates : freq === 'weekly' ? weekFirstTradingDays(tradingDates) : monthFirstTradingDays(tradingDates)
  log(`run=${runId} out=${outRoot} db=${fundDbPath}`)
  log(`窗口 ${tradingDates[0]}..${tradingDates[tradingDates.length - 1]} → ${decisionDays.length} 个${freq === 'daily' ? '日度' : freq === 'weekly' ? '周度' : '月度'}决策日 × ${reporters.length} reporter`)

  // ── 目录 + openclaw 凭据副本 ──────────────────────────────────────────────
  const rlOpenclawDir = P.rlOpenclawDir(outRoot, runId)
  mkdirSync(P.runDir(outRoot, runId), { recursive: true })
  mkdirSync(P.memoryDir(outRoot, runId), { recursive: true })
  mkdirSync(P.workspacesDir(outRoot, runId), { recursive: true })
  mkdirSync(rlOpenclawDir, { recursive: true })
  const srcOc = openclawJsonSource(config), dstOc = join(rlOpenclawDir, 'openclaw.json')
  if (existsSync(srcOc) && !existsSync(dstOc)) { try { copyFileSync(srcOc, dstOc) } catch { /* 非致命 */ } }

  // ── 进程内服务（全部随机端口）────────────────────────────────────────────
  const currentDateRef = { value: decisionDays[0] ?? tradingDates[0] }
  const getCurrentDate = (): string => currentDateRef.value
  const store = new MemoryStore(P.memoryStoreFile(outRoot, runId))
  const memory = await createMemoryServer({ store, getCurrentDate })
  const simworldProxy = await createSimworldProxy({ upstreamUrl: config.simworldUpstreamUrl, getCurrentDate, clientId: `prepass-${runId}` })
  const strategyServer = await createStrategyServer({ worldRoot: outRoot, runId, getCurrentDate, fundDbPath })
  log(`servers: memory=${memory.url} simworld=${simworldProxy.url} strategy=${strategyServer.url}`)
  const templateVars: Record<string, string> = { SIMWORLD_PROXY_URL: simworldProxy.url, STRATEGY_SERVER_URL: strategyServer.url }

  // research-loop 端共享 config（rust 实际读各 bot shadow 的 research-loop.yaml；这份是 --config 入参占位）
  generateRlConfig(config, outRoot, runId, memory.url, rlOpenclawDir)

  // ── spawn 三个 reporter（顺序：context→mainline→rotation 由 config.bots 顺序保证）────────
  const overrideFile = P.worldDateOverrideFile(outRoot, runId)
  const onBotLog = (l: string): void => { process.stderr.write(l + '\n') }
  const onBotNotification = (method: string): void => { if (method === 'tool.call') process.stdout.write('.') }
  // server 可被硬重启替换 → 用可变字段；同时保留 argv/env 以便原样重启。
  const bots: { botId: string; reportType: string; server: BotServer; argv: string[]; env: Record<string, string> }[] = []
  for (const botId of reporters) {
    const srcWs = isAbsolute(botId) ? botId : join(config.botsRoot, botId)
    if (!existsSync(srcWs)) { process.stderr.write(`source workspace not found: ${srcWs}\n`); return 2 }
    const shadow = P.shadowWorkspaceDir(outRoot, runId, botId)
    buildShadowWorkspace({ sourceDir: srcWs, destDir: shadow, include: config.shadowInclude, templateVars })
    writeResearchLoopYaml(config, botId, shadow, memory.url, rlOpenclawDir)
    const argvBot = botServerArgv(config, botId, shadow, loopConfigPath(config, outRoot, runId))
    const env: Record<string, string> = { WORLD_DATE_OVERRIDE_FILE: overrideFile, ...proxyEnvSupplement() }
    const server = await BotServer.start(botId, {
      argv: argvBot, env, readyTimeoutMs: 60_000, onLog: onBotLog, onNotification: onBotNotification,
    })
    bots.push({ botId, reportType: REPORT_TYPE_BY_BOT[botId], server, argv: argvBot, env })
    log(`bot ${botId} ready`)
  }

  const perBotTimeoutMs = (config.researchDayTimeoutSeconds ?? config.perBotTimeoutSeconds ?? 900) * 1000

  // 硬重启某 reporter 的子进程：shutdown 旧进程 → 重新 BotServer.start（全新 MCP init）。
  // 成功后原地替换 b.server，后续 chat / 收尾 shutdown 都作用在新进程上。
  const restartBot = async (b: typeof bots[number]): Promise<boolean> => {
    try { await b.server.shutdown({ timeoutMs: 5000 }) } catch { /* 旧进程可能已死，忽略 */ }
    try {
      b.server = await BotServer.start(b.botId, {
        argv: b.argv, env: b.env, readyTimeoutMs: 60_000, onLog: onBotLog, onNotification: onBotNotification,
      })
      return true
    } catch (e) { log(`  ${b.botId} server 重启异常：${e instanceof Error ? e.message : String(e)}`); return false }
  }

  // ── 月度循环 ──────────────────────────────────────────────────────────────
  let generated = 0, skipped = 0, failed = 0
  try {
    for (let di = 0; di < decisionDays.length; di++) {
      const date = decisionDays[di]
      currentDateRef.value = date                 // PIT 游标（proxy/strategy/memory 都读它）
      writeFileSync(overrideFile, date)           // research-loop 系统提示日期
      log(`=== 决策日 ${di + 1}/${decisionDays.length}: ${date} ===`)
      for (const b of bots) {
        if (reportExists(fundDbPath, b.reportType, date)) { log(`  ${b.botId} 已有 ${b.reportType}@${date}，跳过`); skipped++; continue }
        const ok = await generateReportWithFallback(b.botId, `agent:${b.botId}:prepass-${runId}-${date}`, {
          reportExists: () => reportExists(fundDbPath, b.reportType, date),
          chat: (sessionKey) => b.server.chat({ message: REPORTER_MESSAGE, session_key: sessionKey, history: [] }, { timeoutMs: perBotTimeoutMs }).then(r => ({ chatErr: r.chat_error })),
          restart: () => restartBot(b),
          sleep, log, retries, hardRestarts,
        })
        if (ok) { log(`  ${b.botId} ✓ ${b.reportType}@${date}`); generated++ }
        else { log(`  ${b.botId} ✗ ${b.reportType}@${date} —— 重试+硬重启兜底均未落库，跳过`); failed++ }
      }
    }
  } finally {
    log(`收尾：生成 ${generated} / 跳过 ${skipped} / 失败 ${failed}`)
    await Promise.race([
      Promise.allSettled([
        ...bots.map(b => b.server.shutdown({ timeoutMs: 5000 })),
        memory.close(), simworldProxy.close(), strategyServer.close(),
      ]),
      new Promise(res => setTimeout(res, 10_000)),
    ])
  }
  return failed > 0 ? 1 : 0
}

// 仅在作为脚本直接运行时启动 main()；被 import（如单测）时不自动执行。
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().then(code => { if (code) process.exitCode = code }).catch(err => {
    process.stderr.write(`[prepass] fatal: ${err instanceof Error ? err.stack ?? err.message : String(err)}\n`)
    process.exitCode = 1
  })
}
