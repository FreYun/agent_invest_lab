import { existsSync, readFileSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { parse as parseYaml } from 'yaml'

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
  loop: 'research-loop' | 'openclaw-pi'
  openclawRoot?: string
  piServerEntry?: string
  piSessionsDir?: string
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
  // 可选。fund-portfolio-mcp 的 streamable-http 上游 URL（默认 http://localhost:28172/mcp）。
  // 提供时 world 会起一个 fund-portfolio-proxy 包住它：
  //   - tools/list 把每个 writer 工具的 run_id 从 inputSchema 里删掉
  //   - tools/call 强制注入 run_id = <本轮 runId>，让 bot 写入永远带审计标签
  // bot 的 mcporter.json 用 ${FUND_PORTFOLIO_PROXY_URL} 占位符引用，buildShadowWorkspace 替换。
  fundPortfolioUpstreamUrl?: string
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

export function loadWorldConfig(path: string): WorldConfig {
  const text = readFileSync(path, 'utf8')
  const raw = (parseYaml(text) ?? {}) as Record<string, unknown>
  const baseDir = dirname(resolve(path))

  const researchLoop = reqString(raw, 'research_loop')

  // world.yaml lives at <world>/config/world.yaml. Defaults:
  //   bots_root      → ../../bots         (i.e. <repo>/bots/<botId>/, sibling of <world>)
  //   skills_root    → ../../skills       (i.e. <repo>/skills/, injected as extra_roots)
  //   openclaw_json  → ./openclaw.json    (i.e. <world>/config/openclaw.json, credentials)
  const botsRoot = typeof raw.bots_root === 'string' && raw.bots_root.trim()
    ? resolveMaybe(baseDir, raw.bots_root)
    : resolveMaybe(baseDir, '../../bots')
  const skillsRoot = typeof raw.skills_root === 'string' && raw.skills_root.trim()
    ? resolveMaybe(baseDir, raw.skills_root)
    : resolveMaybe(baseDir, '../../skills')
  const openclawJson = typeof raw.openclaw_json === 'string' && raw.openclaw_json.trim()
    ? resolveMaybe(baseDir, raw.openclaw_json)
    : resolveMaybe(baseDir, 'openclaw.json')

  const botsRaw = raw.bots
  if (!Array.isArray(botsRaw) || botsRaw.length === 0 || !botsRaw.every(b => typeof b === 'string' && b.trim())) {
    throw new Error('world config: "bots" must be a non-empty array of bot ids (e.g. [bot1, bot7])')
  }
  const bots = botsRaw as string[]

  const replayRaw = (raw.replay ?? {}) as Record<string, unknown>
  const from = validIsoDate(reqString(replayRaw, 'from'), 'replay.from')
  const to = validIsoDate(reqString(replayRaw, 'to'), 'replay.to')
  if (from > to) throw new Error(`world config: replay.from (${from}) is after replay.to (${to})`)

  const calendar = typeof raw.calendar === 'string' && raw.calendar.trim()
    ? resolveMaybe(baseDir, raw.calendar)
    : resolveMaybe(baseDir, 'calendar.json')
  const concurrency = typeof raw.concurrency === 'number' && raw.concurrency >= 1 ? Math.floor(raw.concurrency) : 4
  const perBotTimeoutSeconds = typeof raw.per_bot_timeout_seconds === 'number' && raw.per_bot_timeout_seconds > 0 ? Math.floor(raw.per_bot_timeout_seconds) : 1200
  const researchDayEvery = typeof raw.research_day_every === 'number' && raw.research_day_every >= 0 ? Math.floor(raw.research_day_every) : 0
  const researchDayTimeoutSeconds = typeof raw.research_day_timeout_seconds === 'number' && raw.research_day_timeout_seconds > 0 ? Math.floor(raw.research_day_timeout_seconds) : Math.max(perBotTimeoutSeconds, 300)
  const rlConfigBase = typeof raw.rl_config_base === 'string' && raw.rl_config_base.trim()
    ? resolveMaybe(baseDir, raw.rl_config_base)
    : resolveMaybe(baseDir, '../config/trading-rl-config.base.json')
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
  let piSessionsDir: string | undefined
  if (loop === 'openclaw-pi') {
    if (!existsSync(openclawJson)) {
      throw new Error(`world config: openclaw_json not found (required for openclaw-pi loop): ${openclawJson}`)
    }
    openclawRoot = typeof raw.openclaw_root === 'string' && raw.openclaw_root.trim()
      ? resolveMaybe(baseDir, raw.openclaw_root)
      : '/home/rooot/.openclaw/openclaw'
    piServerEntry = typeof raw.pi_server_entry === 'string' && raw.pi_server_entry.trim()
      ? resolveMaybe(baseDir, raw.pi_server_entry)
      : join(openclawRoot, 'src/agents/agent_invest_pi_stdio_server.ts')
    if (!existsSync(piServerEntry)) {
      throw new Error(`world config: pi_server_entry not found: ${piServerEntry}`)
    }
    // pi session 持久化目录。pi-server 会把每次 chat 的 jsonl 落到 <piSessionsDir>/<botId>/<sessionId>.jsonl，
    // 不再像默认的临时文件那样 chat 结束即删。默认相对 world.yaml → ../../session（即 <repo>/session）。
    piSessionsDir = typeof raw.pi_sessions_dir === 'string' && raw.pi_sessions_dir.trim()
      ? resolveMaybe(baseDir, raw.pi_sessions_dir)
      : resolveMaybe(baseDir, '../../session')
  }

  const simworldUpstreamUrl = reqString(raw, 'simworld_upstream_url').trim()

  // 可选；默认指向本机 fund-portfolio-mcp 默认端口（28172）。基金 run 才会被实际使用。
  const fundPortfolioUpstreamUrl = typeof raw.fund_portfolio_upstream_url === 'string' && raw.fund_portfolio_upstream_url.trim()
    ? raw.fund_portfolio_upstream_url.trim()
    : 'http://localhost:28172/mcp'

  const fundMcpCli = typeof raw.fund_mcp_cli === 'string' && raw.fund_mcp_cli.trim()
    ? resolveMaybe(baseDir, raw.fund_mcp_cli)
    : undefined
  const fundInitialCapital = typeof raw.fund_initial_capital === 'number' && raw.fund_initial_capital > 0
    ? raw.fund_initial_capital
    : (fundMcpCli ? 1_000_000 : undefined)
  const fundInitReset = typeof raw.fund_init_reset === 'boolean'
    ? raw.fund_init_reset
    : (fundMcpCli ? true : undefined)
  let buyableFundCodes: string[] | undefined
  if (raw.buyable_fund_codes !== undefined) {
    if (!Array.isArray(raw.buyable_fund_codes) || !raw.buyable_fund_codes.every(c => typeof c === 'string' && /^\d{6}$/.test(c))) {
      throw new Error('world config: "buyable_fund_codes" must be an array of 6-digit fund code strings')
    }
    buyableFundCodes = [...new Set(raw.buyable_fund_codes as string[])].sort()
    if (buyableFundCodes.length === 0) {
      throw new Error('world config: "buyable_fund_codes" cannot be empty when set — pick the funds bot is allowed to buy this run')
    }
  }
  if (fundMcpCli && !buyableFundCodes) {
    throw new Error('world config: "buyable_fund_codes" is required when "fund_mcp_cli" is set — list the fund codes bot can buy this run (e.g. [510300, 159915, 002611])')
  }

  return { researchLoop, botsRoot, openclawJson, skillsRoot, bots, replay: { from, to }, calendar, concurrency, perBotTimeoutSeconds, researchDayEvery, researchDayTimeoutSeconds, rlConfigBase, rlOpenclawDir, shadowInclude, loop, openclawRoot, piServerEntry, piSessionsDir, fundMcpCli, fundInitialCapital, fundInitReset, buyableFundCodes, simworldUpstreamUrl, fundPortfolioUpstreamUrl }
}
