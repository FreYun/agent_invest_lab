import { existsSync, readFileSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { parse as parseYaml } from 'yaml'
import { loadStrategyLibrary } from './strategy-library.ts'

export const DEFAULT_SHADOW_INCLUDE = [
  'IDENTITY.md', 'SOUL.md', 'AGENTS.md', 'USER.md',
  'METHODOLOGY.md', 'RESEARCH.md', 'MEMORY.md',
  'EQUIPPED_SKILLS.md', 'TOOLS.md', 'skills',
  // bot 自带的 MCP 清单 — rl-ts applyWorkspaceMcporter 会读。
  // 不复制整个 config/，避免 config/research-loop.* 里的 workspace/mcp 覆盖 world 传入的 shadow workspace/run config。
  'config/mcporter.json',
]

export interface WorldConfig {
  researchLoop: string
  // 可选。指向 research-loop 的 Rust binary（rust/target/release/research-loop-rust2）。
  // 设了 → world 按 `<bin> server --bot-id ... --workspace ... --config ...` 起 server，
  // 走 rust 实现；不设 → 沿用 researchLoop/ts/server.ts。两端 JSON-RPC 协议同集（ping / chat /
  // shutdown），bot 那侧无感。RESEARCH_LOOP_RUST_BIN 环境变量也可覆写本字段（环境优先）。
  // 切到 rust 主要为了让 chat error event 不再被 ts server.ts 静默吃掉（ts server.ts
  // 的 for-await 没分支处理 `event.type === 'error'`，chat_llm 超时会伪装成 ok 返回）。
  researchLoopRustBin?: string
  botsRoot: string
  openclawJson: string
  skillsRoot: string
  bots: string[]
  replay: { from: string; to: string }
  calendar: string
  concurrency: number
  perBotTimeoutSeconds: number
  rlConfigBase: string
  rlOpenclawDir?: string
  shadowInclude: string[]
  // 每隔 N 个交易日给一次更长的 chat 预算（"研究日"）。0 = 关闭，所有日都用 perBotTimeoutSeconds。
  // 触发条件：(cursor + 1) % researchDayEvery === 0，即第 N / 2N / 3N 个交易日。
  researchDayEvery: number
  researchDayTimeoutSeconds: number
  // 每隔 N 个「决策日」（bot 实际被唤起的 chat 日，非交易日）标记一次「深度研究日」：
  // 第 N / 2N / 3N … 个决策日的 daily message 注入【深度研究日】块，允许 bot 调 start_research
  // （前提：rl_config_base 的 deny 里已放开研究工具）。0 = 关闭（默认，历史行为）。
  // 与 researchDayEvery（只加预算、按交易日计数）互相独立。可选（老配置/测试 fixture 不带 = 关闭）。
  deepResearchEvery?: number
  // 深度研究日的 per-bot chat 超时（外层 kill）。要覆盖引擎内研究预算（rl config limits.max_time_minutes）
  // + 常规决策时间。缺省 = max(researchDayTimeoutSeconds, 2400)。
  deepResearchTimeoutSeconds?: number
  // 深度研究调度模式。
  //   ordinal（默认，向后兼容）：按 deepResearchEvery 的第 N/2N/3N 个决策日强制深研。
  //   agent-triggered：bot 自主决定哪天深研，硬上限 deepResearchMaxGapDays 交易日。
  //   run.ts 侧据此计算 { authorized, forced, gapDays } 三态；message.ts 侧据此渲染
  //   触发信号块 vs 强制块。
  deepResearchMode?: 'ordinal' | 'agent-triggered'
  // agent-triggered 模式下距上次深研的最大交易日 gap，达到即 forced（系统在 message
  // 里下强制指令）。仅 agent-triggered 生效；缺省=4。
  deepResearchMaxGapDays?: number
  // 崩盘阈值强制深研（默认关闭，开则不依赖 agent 自主判断）。基准指数出现
  //   |单日涨跌%| >= crashTriggerDailyMovePct 或 距近高回撤 >= crashTriggerDrawdownPct
  // 时，当日 forced 深研。基准默认沪深300，单指数 bot 应在 config 里改成自己的目标指数。
  crashTriggerEnabled?: boolean
  crashTriggerDailyMovePct?: number
  crashTriggerDrawdownPct?: number
  crashTriggerBenchmark?: { code: string; name: string }
  // bot chat 频率（交易日步长）。1 = 每个交易日都唤起 bot（默认）。N > 1 = 每 N 个交易日唤起
  // 一次 bot chat（cursor 0, N, 2N, …），中间天系统侧 settle_pending_orders + close_my_day 仍
  // 按日推进，simulated_datetime 和 currentDateRef 同样每天更新——只是 bot 不被叫起。用于
  // 模拟"周频"或更长周期的调仓节奏。
  chatStepDays: number
  // 决策日的对齐口径。'trading_days'（默认）：按交易日序号取模（cursor % chatStepDays），
  // 历史行为不变。'weekly'：每个自然周（按周一起算）的第一个交易日唤起一次。'monthly'：每个
  // 自然月的第一个交易日唤起一次。weekly/monthly 下 chatStepDays 被忽略，决策日由日历边界派生，
  // 不会因假期漂移。中间交易日同样只做系统侧 settle/close，simulated_datetime 照常推进。
  chatStepMode: 'trading_days' | 'weekly' | 'monthly'
  // weekly 模式下决策落在每周的第几天。ISO 周几：1=周一 … 5=周五；遇假就近顺延到「≥该周几」的首个
  // 交易日，整周都在该周几之前则落到本周最后一个交易日。缺省=1（≈周首交易日，历史行为）。
  chatWeekday?: number
  // monthly 模式下决策落在每月第几个交易日。正数=从月初数（1=月初）；负数=从月末倒数（-1=月末）。
  // 越界自动夹到当月首/末交易日。缺省=1（月初，历史行为）。
  chatMonthlyNth?: number
  // 单指数 daily briefing 中研究报告注入开关。缺省=总开关开，market_reports 全量、res1/2/4/5 全开，保持历史行为。
  singleFundBriefingRes?: SingleFundBriefingResConfig
  // reporter 模式（market-reports pre-pass 专用）：bot 不是投资者而是「市场研究员」，每个决策日
  // 只产出一份研报并 submit_market_report，不交易、无账户。开启后 daily message 换成精简的
  // 「产出本期研报」提示（不注入持仓/buyable/交易规则/belief schema），其余 plumbing（PIT 日期注入、
  // simworld 工具预激活、影子工作区、mem0）全部复用。配合不设 fund_mcp_cli（不交易）使用。
  reporterMode?: boolean
  loop: 'research-loop' | 'openclaw-pi'
  openclawRoot?: string
  piServerEntry?: string
  // 系统侧基金账户管理（不让 bot 自己 init / close）：world setup 阶段调一次 init_fund_account
  // 创建初始现金账户；每天 chat 完后调一次 close_my_day 落收盘快照。
  // 走 fund-portfolio-mcp/cli_tools.py 子进程，绕过 MCP HTTP（bot 看不到这两个 tool）。
  // 不填则跳过——非基金 bot / 不需要这套生命周期的 run 默认安全。
  fundMcpCli?: string
  fundInitialCapital?: number
  // world replay 语义 = 每次 run 起点干净。fundInitReset=true 时 init 之前先清空该 bot
  // 在 fund.db 里的所有行（accounts/holdings/orders/actions/snapshots/runs）。默认 true
  // 当 fundMcpCli 已配置——非 reset 的延续场景请显式传 false。
  fundInitReset?: boolean
  // 是否允许 bot 自己改 USER.md（风险偏好真相源）—— 透给 strategy-server，开了才暴露
  // update_my_user/get_my_user。默认 false（生产 bot101/102/103 run 不开）。仅「按 USER.md
  // 风险偏好建档」的测试/分身 run 设 true（yaml: enable_user_self_edit）。
  enableUserSelfEdit?: boolean
  // Optional shared strategy library. When botAssignments[botId] is present, world
  // generates that bot's shadow METHODOLOGY.md from the referenced shared strategy.
  strategyLibraryRoot?: string
  botAssignments?: Record<string, BotAssignment>
  // 本 run 显式给 bot 播报的可买基金白名单（day-1 prompt 注入）。
  // user 每轮回测自己挑（典型 3-10 只代表性 ETF / 主题基金）。fund.db 里有 300+
  // 个 fund_code，全播会污染上下文；这里收窄成 user 关心的小集合。
  // fundMcpCli 启用时必填、非空。bot 仍可调 portfolio_get_buyable_funds 看全集。
  buyableFundCodes?: string[]
  // 必选。world 进程内起一个 streamable-http MCP 代理包住这个上游：
  //   - tools/list 把每个工具的 simulated_datetime 从 inputSchema 里删掉
  //   - tools/call 强制注入 simulated_datetime = <world_date> 15:00:00
  // bot 的 mcporter.json 用 ${SIMWORLD_PROXY_URL} 占位符引用代理监听 URL，
  // buildShadowWorkspace 拷贝时替换。
  simworldUpstreamUrl: string
  // 可选。手动指定 daily prompt 里 simworld-data 工具清单（白名单）。
  // 不设 → 用 probe 从 upstream 抓到的全集（现状，会全量灌进 prompt）。
  // 设了 → 只列这些 name；description 缺省时按 name 从 probe 结果里 fallback，
  // probe 里也找不到就只渲染裸 name。
  simworldTools?: { name: string; description?: string }[]
  // 可选。fund-portfolio-mcp 的 streamable-http 上游 URL（默认 http://localhost:28172/mcp）。
  // 提供时 world 会起一个 fund-portfolio-proxy 包住它：
  //   - tools/list 把每个 writer 工具的 run_id 从 inputSchema 里删掉
  //   - tools/call 强制注入 run_id = <本轮 runId>，让 bot 写入永远带审计标签
  // bot 的 mcporter.json 用 ${FUND_PORTFOLIO_PROXY_URL} 占位符引用，buildShadowWorkspace 替换。
  fundPortfolioUpstreamUrl?: string
  // 可选。per-bot 模型覆盖（来自 18888 新建回测 modal 的「本次」选择）。botId → research-loop 的
  // model.primary 片段 { provider, base_url, model, api_key_from_openclaw }。优先级最高：
  // 盖过 bots/<bot>/config/model.yaml 和全局 rlConfigBase。缺省时各 bot 用自带 model.yaml / 全局兜底。
  botModels?: Record<string, Record<string, unknown>>
  /** Phase 1（decide）：跳过 close_my_day（当日收盘 NAV 尚未披露）。 */
  skipClose?: boolean
  /** Phase 2（settle）：不唤醒 bot，只跑系统侧 settle + close。 */
  skipChat?: boolean
}

export interface BotAssignment {
  strategyId: string
  buyableFundCodes?: string[]
}

export const SINGLE_FUND_BRIEFING_RES_REPORTS = [
  'market_context',
  'macro_news',
  'market_mainline',
  'mainline_rotation',
  'market_strategy',
  'policy_analysis',
  'intl_relations',
  'cross_market_linkage',
] as const
export type SingleFundBriefingResReport = typeof SINGLE_FUND_BRIEFING_RES_REPORTS[number]
export interface SingleFundBriefingResConfig {
  enabled: boolean
  reports: Record<SingleFundBriefingResReport, boolean>
}

const DEFAULT_SINGLE_FUND_BRIEFING_RES_REPORTS: Record<SingleFundBriefingResReport, boolean> = {
  market_context: true,
  macro_news: true,
  market_mainline: true,
  mainline_rotation: true,
  market_strategy: true,
  policy_analysis: true,
  intl_relations: true,
  cross_market_linkage: true,
}

function parseSingleFundBriefingRes(raw: unknown): SingleFundBriefingResConfig | undefined {
  if (raw === undefined) return undefined
  const reports: Record<SingleFundBriefingResReport, boolean> = { ...DEFAULT_SINGLE_FUND_BRIEFING_RES_REPORTS }
  if (typeof raw === 'boolean') return { enabled: raw, reports }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('world config: "single_fund_briefing_res" must be a boolean or object')
  }
  const obj = raw as Record<string, unknown>
  let enabled = true
  if (obj.enabled !== undefined) {
    if (typeof obj.enabled !== 'boolean') throw new Error('world config: "single_fund_briefing_res.enabled" must be boolean')
    enabled = obj.enabled
  }
  const reportRaw = obj.reports !== undefined ? obj.reports : obj
  if (!reportRaw || typeof reportRaw !== 'object' || Array.isArray(reportRaw)) {
    throw new Error('world config: "single_fund_briefing_res.reports" must be an object')
  }
  const valid = new Set<string>(SINGLE_FUND_BRIEFING_RES_REPORTS)
  const marketReportKeys: SingleFundBriefingResReport[] = ['market_context', 'macro_news', 'market_mainline', 'mainline_rotation']
  const reportObj = reportRaw as Record<string, unknown>
  if (reportObj.market_reports !== undefined) {
    if (typeof reportObj.market_reports !== 'boolean') throw new Error('world config: single_fund_briefing_res.market_reports must be boolean')
    for (const k of marketReportKeys) reports[k] = reportObj.market_reports
  }
  for (const [key, value] of Object.entries(reportObj)) {
    if (key === 'enabled' || key === 'reports' || key === 'market_reports') continue
    if (!valid.has(key)) throw new Error('world config: unknown single_fund_briefing_res report "' + key + '"')
    if (typeof value !== 'boolean') throw new Error('world config: single_fund_briefing_res.' + key + ' must be boolean')
    reports[key as SingleFundBriefingResReport] = value
  }
  return { enabled, reports }
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

function reqString(obj: Record<string, unknown>, key: string): string {
  const v = obj[key]
  if (typeof v !== 'string' || v.trim() === '') throw new Error(`world config: "${key}" is required and must be a non-empty string`)
  return v
}

function validIsoDate(s: string, label: string): string {
  if (!ISO_DATE.test(s)) throw new Error(`world config: "${label}" must be YYYY-MM-DD (got ${JSON.stringify(s)})`)
  const d = new Date(s + 'T00:00:00Z')
  if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== s) throw new Error(`world config: "${label}" is not a real date (${s})`)
  return s
}

function resolveMaybe(base: string, p: string): string {
  return isAbsolute(p) ? p : resolve(base, p)
}

function parseFundCodeList(raw: unknown, label: string): string[] {
  if (!Array.isArray(raw) || !raw.every(c => typeof c === 'string' && /^\d{6}$/.test(c))) {
    throw new Error('world config: "' + label + '" must be an array of 6-digit fund code strings')
  }
  const codes = [...new Set(raw as string[])].sort()
  if (codes.length === 0) throw new Error('world config: "' + label + '" cannot be empty when set')
  return codes
}

function parseBotAssignments(raw: unknown, bots: string[]): Record<string, BotAssignment> | undefined {
  if (raw === undefined) return undefined
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('world config: "bot_assignments" must be an object keyed by bot id')
  }
  const botSet = new Set(bots)
  const out: Record<string, BotAssignment> = {}
  for (const [botId, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!botSet.has(botId)) throw new Error('world config: "bot_assignments.' + botId + '" references a bot not listed in "bots"')
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('world config: "bot_assignments.' + botId + '" must be an object')
    }
    const entry = value as Record<string, unknown>
    if (typeof entry.strategy_id !== 'string' || !entry.strategy_id.trim()) {
      throw new Error('world config: "bot_assignments.' + botId + '.strategy_id" must be a non-empty string')
    }
    const assignment: BotAssignment = { strategyId: entry.strategy_id.trim() }
    if (entry.buyable_fund_codes !== undefined) {
      assignment.buyableFundCodes = parseFundCodeList(entry.buyable_fund_codes, 'bot_assignments.' + botId + '.buyable_fund_codes')
    }
    out[botId] = assignment
  }
  return Object.keys(out).length ? out : undefined
}

/** 从 raw YAML 对象解析 WorldConfig（供测试直接调用，无需 yaml 文件）。
 * baseDir 用于相对路径解析；测试时可省略（缺省当前工作目录）。
 */
export function parseWorldConfig(raw: Record<string, unknown>, baseDir?: string): WorldConfig {
  const resolvedBaseDir = baseDir ?? process.cwd()

  const researchLoop = reqString(raw, 'research_loop')
  // 优先级：env RESEARCH_LOOP_RUST_BIN > world.yaml research_loop_rust_bin > 未设（走 ts）。
  // env 优先方便临时切换（`RESEARCH_LOOP_RUST_BIN=... world run --config ...`）而不动 yaml。
  const researchLoopRustBin = (process.env.RESEARCH_LOOP_RUST_BIN && process.env.RESEARCH_LOOP_RUST_BIN.trim())
    || (typeof raw.research_loop_rust_bin === 'string' && raw.research_loop_rust_bin.trim()
      ? resolveMaybe(resolvedBaseDir, raw.research_loop_rust_bin)
      : undefined)

  // world.yaml lives at <world>/config/world.yaml. Defaults:
  //   bots_root      → ../../bots         (i.e. <repo>/bots/<botId>/, sibling of <world>)
  //   skills_root    → ../../skills       (i.e. <repo>/skills/, injected as extra_roots)
  //   openclaw_json  → ./openclaw.json    (i.e. <world>/config/openclaw.json, credentials)
  const botsRoot = typeof raw.bots_root === 'string' && raw.bots_root.trim()
    ? resolveMaybe(resolvedBaseDir, raw.bots_root)
    : resolveMaybe(resolvedBaseDir, '../../bots')
  const skillsRoot = typeof raw.skills_root === 'string' && raw.skills_root.trim()
    ? resolveMaybe(resolvedBaseDir, raw.skills_root)
    : resolveMaybe(resolvedBaseDir, '../../skills')
  const openclawJson = typeof raw.openclaw_json === 'string' && raw.openclaw_json.trim()
    ? resolveMaybe(resolvedBaseDir, raw.openclaw_json)
    : resolveMaybe(resolvedBaseDir, 'openclaw.json')

  const botsRaw = raw.bots
  if (!Array.isArray(botsRaw) || botsRaw.length === 0 || !botsRaw.every(b => typeof b === 'string' && b.trim())) {
    throw new Error('world config: "bots" must be a non-empty array of bot ids (e.g. [bot1, bot7])')
  }
  const bots = botsRaw as string[]

  const strategyLibraryRoot = typeof raw.strategy_library_root === 'string' && raw.strategy_library_root.trim()
    ? resolveMaybe(resolvedBaseDir, raw.strategy_library_root)
    : undefined
  const botAssignments = parseBotAssignments(raw.bot_assignments, bots)
  // per-bot 模型覆盖（18888 modal 本次选择）：{ botId: { provider, base_url, model, api_key_from_openclaw } }。
  // 只收非空对象；botId 不限定在 bots 内（多写的键无害，writeResearchLoopYaml 按 botId 取）。
  let botModels: Record<string, Record<string, unknown>> | undefined
  if (raw.bot_models && typeof raw.bot_models === 'object') {
    const out: Record<string, Record<string, unknown>> = {}
    for (const [botId, v] of Object.entries(raw.bot_models as Record<string, unknown>)) {
      if (v && typeof v === 'object') out[botId] = v as Record<string, unknown>
    }
    if (Object.keys(out).length) botModels = out
  }

  const replayRaw = (raw.replay ?? {}) as Record<string, unknown>
  const from = validIsoDate(reqString(replayRaw, 'from'), 'replay.from')
  const to = validIsoDate(reqString(replayRaw, 'to'), 'replay.to')
  if (from > to) throw new Error(`world config: replay.from (${from}) is after replay.to (${to})`)

  const calendar = typeof raw.calendar === 'string' && raw.calendar.trim()
    ? resolveMaybe(resolvedBaseDir, raw.calendar)
    : resolveMaybe(resolvedBaseDir, 'calendar.json')
  const concurrency = typeof raw.concurrency === 'number' && raw.concurrency >= 1 ? Math.floor(raw.concurrency) : 4
  const perBotTimeoutSeconds = typeof raw.per_bot_timeout_seconds === 'number' && raw.per_bot_timeout_seconds > 0 ? Math.floor(raw.per_bot_timeout_seconds) : 1200
  const researchDayEvery = typeof raw.research_day_every === 'number' && raw.research_day_every >= 0 ? Math.floor(raw.research_day_every) : 0
  const researchDayTimeoutSeconds = typeof raw.research_day_timeout_seconds === 'number' && raw.research_day_timeout_seconds > 0 ? Math.floor(raw.research_day_timeout_seconds) : Math.max(perBotTimeoutSeconds, 300)
  const deepResearchEvery = typeof raw.deep_research_every === 'number' && raw.deep_research_every >= 0 ? Math.floor(raw.deep_research_every) : 0
  const deepResearchTimeoutSeconds = typeof raw.deep_research_timeout_seconds === 'number' && raw.deep_research_timeout_seconds > 0 ? Math.floor(raw.deep_research_timeout_seconds) : Math.max(researchDayTimeoutSeconds, 2400)
  const rawDrMode = typeof raw.deep_research_mode === 'string' ? raw.deep_research_mode.trim() : ''
  const deepResearchMode: 'ordinal' | 'agent-triggered' = rawDrMode === 'agent-triggered' ? 'agent-triggered' : 'ordinal'
  const deepResearchMaxGapDays = typeof raw.deep_research_max_gap_days === 'number' && raw.deep_research_max_gap_days >= 1 ? Math.floor(raw.deep_research_max_gap_days) : 4
  const crashTriggerEnabled = raw.crash_trigger_enabled === true
  const crashTriggerDailyMovePct = typeof raw.crash_trigger_daily_move_pct === 'number' && raw.crash_trigger_daily_move_pct > 0 ? raw.crash_trigger_daily_move_pct : 3
  const crashTriggerDrawdownPct = typeof raw.crash_trigger_drawdown_pct === 'number' && raw.crash_trigger_drawdown_pct > 0 ? raw.crash_trigger_drawdown_pct : 8
  const ctb = raw.crash_trigger_benchmark as { code?: unknown; name?: unknown } | undefined
  const crashTriggerBenchmark = (ctb && typeof ctb === 'object' && typeof ctb.code === 'string' && typeof ctb.name === 'string')
    ? { code: ctb.code, name: ctb.name }
    : { code: '000300.SH', name: '沪深300' }
  const chatStepDays = typeof raw.chat_step_days === 'number' && raw.chat_step_days >= 1 ? Math.floor(raw.chat_step_days) : 1
  const rawStepMode = typeof raw.chat_step_mode === 'string' ? raw.chat_step_mode.trim() : ''
  const chatStepMode: 'trading_days' | 'weekly' | 'monthly' = rawStepMode === 'weekly' || rawStepMode === 'monthly' ? rawStepMode : 'trading_days'
  // weekly：周几（1=周一…5=周五）；非法/缺省 → undefined（run.ts 回落到周首交易日）。
  const chatWeekday = typeof raw.chat_weekday === 'number' && raw.chat_weekday >= 1 && raw.chat_weekday <= 5 ? Math.floor(raw.chat_weekday) : undefined
  // monthly：第 N 个交易日（正=从月初、负=从月末倒数，不能为 0）；非法/缺省 → undefined（回落到月初）。
  const chatMonthlyNth = typeof raw.chat_monthly_nth === 'number' && Number.isFinite(raw.chat_monthly_nth) && Math.trunc(raw.chat_monthly_nth) !== 0 ? Math.trunc(raw.chat_monthly_nth) : undefined
  const singleFundBriefingRes = parseSingleFundBriefingRes(raw.single_fund_briefing_res)
  const reporterMode = raw.reporter_mode === true
  const rlConfigBase = typeof raw.rl_config_base === 'string' && raw.rl_config_base.trim()
    ? resolveMaybe(resolvedBaseDir, raw.rl_config_base)
    : resolveMaybe(resolvedBaseDir, '../config/trading-rl-config.base.json')
  const rlOpenclawDir = typeof raw.rl_openclaw_dir === 'string' && raw.rl_openclaw_dir.trim() ? raw.rl_openclaw_dir : undefined
  const shadowInclude = Array.isArray(raw.shadow_include) && raw.shadow_include.every(x => typeof x === 'string')
    ? (raw.shadow_include as string[])
    : DEFAULT_SHADOW_INCLUDE

  const loopRaw = raw.loop
  let loop: 'research-loop' | 'openclaw-pi' = 'research-loop'
  if (loopRaw !== undefined) {
    if (loopRaw !== 'research-loop' && loopRaw !== 'openclaw-pi') {
      throw new Error(`world config: "loop" must be "research-loop" or "openclaw-pi" (got ${JSON.stringify(loopRaw)})`)
    }
    loop = loopRaw
  }

  let openclawRoot: string | undefined
  let piServerEntry: string | undefined
  if (loop === 'openclaw-pi') {
    if (!existsSync(openclawJson)) {
      throw new Error(`world config: openclaw_json not found (required for openclaw-pi loop): ${openclawJson}`)
    }
    openclawRoot = typeof raw.openclaw_root === 'string' && raw.openclaw_root.trim()
      ? resolveMaybe(resolvedBaseDir, raw.openclaw_root)
      // 缺省走 repo 根的 .openclaw 符号链接（baseDir=<world>/config → ../../.openclaw），不硬编码旧 HOME。
      : resolveMaybe(resolvedBaseDir, '../../.openclaw/openclaw')
    piServerEntry = typeof raw.pi_server_entry === 'string' && raw.pi_server_entry.trim()
      ? resolveMaybe(resolvedBaseDir, raw.pi_server_entry)
      : join(openclawRoot, 'src/agents/agent_invest_pi_stdio_server.ts')
    if (!existsSync(piServerEntry)) {
      throw new Error(`world config: pi_server_entry not found: ${piServerEntry}`)
    }
    // pi_sessions_dir is now per-run (computed in run.ts as <runDir>/pi-sessions).
    // Old world.yaml entries are parsed-but-ignored to keep configs portable.
    if (typeof raw.pi_sessions_dir === 'string' && raw.pi_sessions_dir.trim()) {
      process.stderr.write(`world config WARNING: "pi_sessions_dir" is deprecated and ignored (now auto = <runDir>/pi-sessions per run).\n`)
    }
  }

  // 可选；默认指向本机 ttjj-data-pit 默认端口（18078），即 simworld-proxy 的上游。
  // host 通过 config 注入：配置里显式设 simworld_upstream_url 即用该值（本机的 world.yaml 注入远程数据机地址），
  // 缺省则回落 127.0.0.1 本机，方便其他自起本地 MCP 的环境。
  const simworldUpstreamUrl = typeof raw.simworld_upstream_url === 'string' && raw.simworld_upstream_url.trim()
    ? raw.simworld_upstream_url.trim()
    : 'http://127.0.0.1:18078/mcp'

  let simworldTools: { name: string; description?: string }[] | undefined
  if (raw.simworld_tools !== undefined) {
    if (!Array.isArray(raw.simworld_tools)) {
      throw new Error('world config: "simworld_tools" must be an array of { name, description? } objects (or a list of name strings)')
    }
    const seen = new Set<string>()
    simworldTools = raw.simworld_tools.map((entry, i) => {
      let name: string
      let description: string | undefined
      if (typeof entry === 'string') {
        name = entry.trim()
      } else if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
        const e = entry as Record<string, unknown>
        if (typeof e.name !== 'string' || !e.name.trim()) {
          throw new Error(`world config: "simworld_tools[${i}].name" must be a non-empty string`)
        }
        name = e.name.trim()
        if (e.description !== undefined) {
          if (typeof e.description !== 'string') {
            throw new Error(`world config: "simworld_tools[${i}].description" must be a string`)
          }
          description = e.description
        }
      } else {
        throw new Error(`world config: "simworld_tools[${i}]" must be a string or { name, description? } object`)
      }
      if (!name) throw new Error(`world config: "simworld_tools[${i}]" name cannot be empty`)
      if (seen.has(name)) throw new Error(`world config: "simworld_tools" contains duplicate name "${name}"`)
      seen.add(name)
      return description !== undefined ? { name, description } : { name }
    })
    if (simworldTools.length === 0) {
      throw new Error('world config: "simworld_tools" cannot be empty when set — omit the key to fall back to probe-all')
    }
  }

  // 可选；默认指向本机 fund-portfolio-mcp 默认端口（28172）。基金 run 才会被实际使用。
  // 同 simworld：配置里显式设 fund_portfolio_upstream_url 即用该值，缺省回落 127.0.0.1 本机。
  const fundPortfolioUpstreamUrl = typeof raw.fund_portfolio_upstream_url === 'string' && raw.fund_portfolio_upstream_url.trim()
    ? raw.fund_portfolio_upstream_url.trim()
    : 'http://127.0.0.1:28172/mcp'

  const fundMcpCli = typeof raw.fund_mcp_cli === 'string' && raw.fund_mcp_cli.trim()
    ? resolveMaybe(resolvedBaseDir, raw.fund_mcp_cli)
    : undefined
  const fundInitialCapital = typeof raw.fund_initial_capital === 'number' && raw.fund_initial_capital > 0
    ? raw.fund_initial_capital
    : (fundMcpCli ? 1_000_000 : undefined)
  const fundInitReset = typeof raw.fund_init_reset === 'boolean'
    ? raw.fund_init_reset
    : (fundMcpCli ? true : undefined)
  const enableUserSelfEdit = raw.enable_user_self_edit === true
  let buyableFundCodes: string[] | undefined
  if (raw.buyable_fund_codes !== undefined) {
    buyableFundCodes = parseFundCodeList(raw.buyable_fund_codes, 'buyable_fund_codes')
  }

  const strategyDefaults: Record<string, string[]> = {}
  if (botAssignments) {
    if (!strategyLibraryRoot) throw new Error('world config: "strategy_library_root" is required when "bot_assignments" is set')
    const lib = loadStrategyLibrary(strategyLibraryRoot)
    for (const [botId, assignment] of Object.entries(botAssignments)) {
      const strategy = lib.strategies.get(assignment.strategyId)
      if (!strategy) throw new Error('world config: bot_assignments.' + botId + '.strategy_id "' + assignment.strategyId + '" not found in strategy library')
      strategyDefaults[botId] = strategy.defaultBuyableFundCodes
    }
  }
  if (fundMcpCli && !buyableFundCodes) {
    for (const botId of bots) {
      const assignment = botAssignments?.[botId]
      const resolved = assignment?.buyableFundCodes ?? strategyDefaults[botId]
      if (!resolved || resolved.length === 0) {
        throw new Error('world config: "buyable_fund_codes" is required when "fund_mcp_cli" is set unless every bot assignment resolves a non-empty buyable pool')
      }
    }
  }

  const skipClose = typeof raw.skip_close === 'boolean' ? raw.skip_close : undefined
  const skipChat = typeof raw.skip_chat === 'boolean' ? raw.skip_chat : undefined

  return { researchLoop, researchLoopRustBin, botsRoot, openclawJson, skillsRoot, bots, replay: { from, to }, calendar, concurrency, perBotTimeoutSeconds, researchDayEvery, researchDayTimeoutSeconds, deepResearchEvery, deepResearchTimeoutSeconds, deepResearchMode, deepResearchMaxGapDays, crashTriggerEnabled, crashTriggerDailyMovePct, crashTriggerDrawdownPct, crashTriggerBenchmark, chatStepDays, chatStepMode, chatWeekday, chatMonthlyNth, singleFundBriefingRes, reporterMode, rlConfigBase, rlOpenclawDir, shadowInclude, loop, openclawRoot, piServerEntry, fundMcpCli, fundInitialCapital, fundInitReset, enableUserSelfEdit, strategyLibraryRoot, botAssignments, buyableFundCodes, simworldUpstreamUrl, simworldTools, fundPortfolioUpstreamUrl, botModels, skipClose, skipChat }
}

export function loadWorldConfig(path: string): WorldConfig {
  const text = readFileSync(path, 'utf8')
  const raw = (parseYaml(text) ?? {}) as Record<string, unknown>
  const baseDir = dirname(resolve(path))
  return parseWorldConfig(raw, baseDir)
}
