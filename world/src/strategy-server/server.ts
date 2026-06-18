import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { existsSync, readFileSync, writeFileSync, appendFileSync, mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { join, resolve } from 'node:path'
import type { AddressInfo } from 'node:net'
import { shadowWorkspaceDir, strategiesDir, strategyRevisionsFile, fundDbFile } from '../paths.ts'
import { loadStrategyLibrary } from '../strategy-library.ts'

// 进程内 MCP 服务，承载两个工具让 bot 自己管理"投资策略文档"：
//   - update_my_strategy(bot_id, strategy, reason): 完整替换 shadow METHODOLOGY.md，追加修订审计
//   - get_my_strategy(bot_id):                     读出 shadow METHODOLOGY.md
//
// 策略 = bot 的 methodology。文件位于 <runDir>/workspaces/<botId>/METHODOLOGY.md（research-loop
// 每次 chat 把它 splice 进 system prompt 的 ## METHODOLOGY.md section）。
// 修订日志 <runDir>/strategies/<botId>.revisions.jsonl —— 每次 update 追加一行
// {ts, reason, new_size, prior_size}。
//
// 信任模型：bot_id 由调用方在参数中传入（无认证），同 fund-portfolio-mcp 的做法。
//
// ── 协议实现：MCP streamable-http，对齐项目里 FastMCP 实际发的格式 ──────────────
// 关键点（与最初版的修正）：
//   1. 响应格式 = text/event-stream（`event: message\ndata: <jsonrpc>\n\n`）。
//      MCP spec 允许 application/json，但项目里实际跑的 FastMCP 全发 SSE，按它来更稳。
//      （客户端只接受 application/json 时退回 JSON——不破坏 spec。）
//   2. initialize 时签发 mcp-session-id（UUID），写到响应 header；后续请求会带它，
//      我们不校验（无认证），只是按 spec 接受 + 回显。
//   3. capabilities 展开成 {experimental, prompts, resources, tools} 而非 {tools:{}}
//      —— 部分客户端会扫这几个字段做能力门控。
//   4. 工具 schema 用 FastMCP 风格：properties.<field>.title + inputSchema.title
//      "<tool>Arguments"，外加 outputSchema 描述返回结构。description 同时保留，给
//      LLM 看到更丰富的语义（FastMCP 也支持 description；只是默认 type-hint 模式不生成）。

export interface StrategyServerOptions {
  worldRoot: string
  runId: string
  /** 写修订审计时的"世界日期"——用世界时间而不是 wall clock，方便审计对齐回放日。
   *  也用作 market_reports 读写的 PIT 游标（get 只返回 as_of<=此日的最近一期；submit 以此日为 as_of）。 */
  getCurrentDate: () => string
  /** market_reports 所在的 SQLite 库路径。缺省 = fundDbFile(worldRoot)（<repo>/data/fund.db）。
   *  测试用临时库覆盖；生产由 run.ts 显式传真实 fund.db。 */
  fundDbPath?: string
  host?: string
  port?: number
}

export interface StrategyServerHandle {
  port: number
  url: string
  close(): Promise<void>
}

interface JsonRpcRequest {
  jsonrpc?: string
  id?: number | string | null
  method?: string
  params?: Record<string, unknown>
}

interface JsonRpcResponse {
  jsonrpc: '2.0'
  id: number | string | null
  result?: unknown
  error?: { code: number; message: string }
}

const PROTOCOL_VERSION = '2024-11-05'
const SERVER_NAME = 'strategy-server'
const SERVER_VERSION = '0.1.0'
const SERVER_INSTRUCTIONS =
  '策略文档服务。每个 bot 当前 active methodology = shadow workspace 下的 METHODOLOGY.md（research-loop ' +
  '每次 chat 都把它 splice 进 system prompt 的 ## METHODOLOGY.md section）。' +
  '共享策略库在 shadow workspace 的 strategies/index-products 下，可通过 list_strategies/get_strategy 查看；' +
  'update_my_strategy 完整重写当前 active methodology，下一交易日 system prompt 自动注入新版。' +
  '修订原因强制传入，全程审计可回放。'

function methodologyPathOf(worldRoot: string, runId: string, botId: string): string {
  return join(shadowWorkspaceDir(worldRoot, runId, botId), 'METHODOLOGY.md')
}

function strategyLibraryRootOf(worldRoot: string, runId: string, botId: string): string {
  return join(shadowWorkspaceDir(worldRoot, runId, botId), 'strategies', 'index-products')
}

// MCP 工具声明：对齐 FastMCP 实际输出（properties.title + inputSchema.title + outputSchema）。
// description 同时给上，让 LLM 看到字段语义；FastMCP 默认不写但 spec 完全允许。
const TOOLS = [
  {
    name: 'update_my_strategy',
    description:
      '更新（完整替换）你当前 active methodology 文档 METHODOLOGY.md（investment strategy revise update revision）。' +
      '传入的 strategy 必须是完整的 markdown（不是 diff），会覆盖旧版本；下一交易日的 system prompt ' +
      '自动注入这一新版本（## METHODOLOGY.md section）。reason 一句话说清楚为什么调整——会写进审计日志' +
      '（revisions.jsonl）供事后回看。每次调用都视作一次正式 portfolio strategy revision。',
    inputSchema: {
      type: 'object',
      title: 'update_my_strategyArguments',
      properties: {
        bot_id: {
          type: 'string',
          title: 'Bot Id',
          description: '你的 bot id（比如 bot7）。world 用它确定写哪个 METHODOLOGY.md。',
        },
        strategy: {
          type: 'string',
          title: 'Strategy',
          description:
            '完整的新 methodology markdown 文本（按 methodology 原本的结构写即可，无前缀要求）。' +
            '会完整覆盖当前 methodology，所以一定带上所有你想保留的内容。',
        },
        reason: {
          type: 'string',
          title: 'Reason',
          description:
            '一句话说明这次调整的原因——观察到了什么、上一版哪里失效、新版本要解决什么。' +
            '只用于审计日志，不会被注入 prompt。',
        },
      },
      required: ['bot_id', 'strategy', 'reason'],
    },
    outputSchema: {
      type: 'object',
      title: 'update_my_strategyOutput',
      properties: { result: { type: 'string', title: 'Result' } },
      required: ['result'],
    },
  },
  {
    name: 'list_strategies',
    description:
      '列出本 run 对该 bot 可见的共享指数产品策略 catalog（只列 strategy_id/title/target_index/default fund codes，不注入全文）。',
    inputSchema: {
      type: 'object',
      title: 'list_strategiesArguments',
      properties: {
        bot_id: { type: 'string', title: 'Bot Id', description: '你的 bot id（比如 bot7）。' },
      },
      required: ['bot_id'],
    },
    outputSchema: {
      type: 'object',
      title: 'list_strategiesOutput',
      properties: { result: { type: 'string', title: 'Result' } },
      required: ['result'],
    },
  },
  {
    name: 'get_strategy',
    description:
      '读取某个共享策略库策略全文。注意：这只是查看其它产品策略；交易执行仍应遵循当前 active METHODOLOGY.md。',
    inputSchema: {
      type: 'object',
      title: 'get_strategyArguments',
      properties: {
        bot_id: { type: 'string', title: 'Bot Id', description: '你的 bot id（比如 bot7）。' },
        strategy_id: { type: 'string', title: 'Strategy Id', description: '共享策略库里的 strategy_id，例如 hs300。' },
      },
      required: ['bot_id', 'strategy_id'],
    },
    outputSchema: {
      type: 'object',
      title: 'get_strategyOutput',
      properties: { result: { type: 'string', title: 'Result' } },
      required: ['result'],
    },
  },
  {
    name: 'get_active_strategy',
    description: '读取当前 active METHODOLOGY.md；等价于 get_my_strategy，但名称更明确。',
    inputSchema: {
      type: 'object',
      title: 'get_active_strategyArguments',
      properties: {
        bot_id: { type: 'string', title: 'Bot Id', description: '你的 bot id（比如 bot7）。' },
      },
      required: ['bot_id'],
    },
    outputSchema: {
      type: 'object',
      title: 'get_active_strategyOutput',
      properties: { result: { type: 'string', title: 'Result' } },
      required: ['result'],
    },
  },
  {
    name: 'get_my_strategy',
    description:
      '读取你当前 active methodology 文档 METHODOLOGY.md（与每日 system prompt 注入的内容一致，investment strategy review）。' +
      '日常不必显式调用——methodology 每天会被自动 splice 进 system prompt——但如果你想中途重新审视、' +
      '或者想确认刚刚 update_my_strategy 的写入是否生效，可以调一次。',
    inputSchema: {
      type: 'object',
      title: 'get_my_strategyArguments',
      properties: {
        bot_id: {
          type: 'string',
          title: 'Bot Id',
          description: '你的 bot id（比如 bot7）。',
        },
      },
      required: ['bot_id'],
    },
    outputSchema: {
      type: 'object',
      title: 'get_my_strategyOutput',
      properties: { result: { type: 'string', title: 'Result' } },
      required: ['result'],
    },
  },
  {
    name: 'get_v5_mainline_plan',
    description:
      '读取 v5 主线回测的权威确定性数据源（scout board_trend_daily + coarse_themes + HS300/MA120）。' +
      '这是 bot101 / reporter-mainline 复刻「主线回测_v5」时的唯一主线真值入口：' +
      '用它返回的 regime、top15、v4_holdings、portfolio、fund_matches 写 market_mainline / mainline_rotation；' +
      '不得再用 simworld-data 的 sector_search/sector_factor/sector_market 重新选择主线。' +
      '返回 PIT：按当前世界日向前取最近一个 v5 月初决策日；fund_matches 由 scout board_fund_match.py 计算，是 v5 回测基金映射真值。',
    inputSchema: {
      type: 'object',
      title: 'get_v5_mainline_planArguments',
      properties: {
        bot_id: { type: 'string', title: 'Bot Id', description: '你的 bot id（比如 bot101 或 reporter-mainline）。' },
      },
      required: ['bot_id'],
    },
    outputSchema: {
      type: 'object',
      title: 'get_v5_mainline_planOutput',
      properties: { result: { type: 'string', title: 'Result' } },
      required: ['result'],
    },
  },
  {
    name: 'get_market_report',
    description:
      '读取全局共享的"市场研究报告"——由系统在每月初预生成、所有 bot 共享。三类报告：' +
      'market_context(行情/regime/risk_state 敢不敢上仓) · market_mainline(主线板块+可投基金池) · ' +
      'mainline_rotation(主线轮动组合骨架:regime开关/核心卫星/计数器/今日动作)。' +
      '每日决策前先读这三份(report_type=all 一次取全)，再结合你自己的账户与人设做操作——' +
      '不要自己从头重算主线/regime，那是这些报告已经做完的事。' +
      '返回的是"当前世界日及之前"最近一期报告(PIT，绝不含未来信息)；月初生成、月内各日读到的是同一份。',
    inputSchema: {
      type: 'object',
      title: 'get_market_reportArguments',
      properties: {
        bot_id: { type: 'string', title: 'Bot Id', description: '你的 bot id（比如 bot101）。' },
        report_type: {
          type: 'string',
          title: 'Report Type',
          description:
            "要读哪份：'market_context' | 'market_mainline' | 'mainline_rotation'，或 'all' 一次取全部三份。",
        },
      },
      required: ['bot_id', 'report_type'],
    },
    outputSchema: {
      type: 'object',
      title: 'get_market_reportOutput',
      properties: { result: { type: 'string', title: 'Result' } },
      required: ['result'],
    },
  },
  {
    name: 'submit_market_report',
    description:
      '【仅供系统 reporter agent 调用】把刚生成的市场研究报告写入全局共享库。' +
      'as_of_date 由系统按"世界当前日"自动确定，你无需也无法指定（PIT）。' +
      '同一 (report_type, as_of_date) 重复提交会覆盖旧版本（幂等续跑）。',
    inputSchema: {
      type: 'object',
      title: 'submit_market_reportArguments',
      properties: {
        bot_id: { type: 'string', title: 'Bot Id', description: 'reporter agent id（必须以 reporter- 开头）。' },
        report_type: {
          type: 'string',
          title: 'Report Type',
          description: "'market_context' | 'market_mainline' | 'mainline_rotation'。",
        },
        content_md: { type: 'string', title: 'Content Md', description: '研报正文（完整 markdown）。' },
        structured_json: {
          type: 'string',
          title: 'Structured Json',
          description: '可选：categorical 结构化字段的 JSON 字符串（regime/risk_state/主线/基金池/组合/计数器…）。',
        },
      },
      required: ['bot_id', 'report_type', 'content_md'],
    },
    outputSchema: {
      type: 'object',
      title: 'submit_market_reportOutput',
      properties: { result: { type: 'string', title: 'Result' } },
      required: ['result'],
    },
  },
] as const

interface ToolResult { content: Array<{ type: 'text'; text: string }>; isError?: boolean }
function ok(text: string): ToolResult { return { content: [{ type: 'text', text }] } }
function err(text: string): ToolResult { return { content: [{ type: 'text', text }], isError: true } }

// ── market_reports（全局共享市场研究报告）SQLite 读写 ───────────────────────────
// 与 backtest-dashboard 一致走 sqlite3 CLI（fund.db 是 WAL、被 live run 并发写，必须带 busy
// timeout）。SQL 经 stdin 传入（execFileSync 不经 shell；大段 content_md 走 stdin 不受 argv 长度限制）。
const VALID_REPORT_TYPES = ['market_context', 'market_mainline', 'mainline_rotation'] as const
type ReportType = (typeof VALID_REPORT_TYPES)[number]

const MARKET_REPORTS_DDL = `
CREATE TABLE IF NOT EXISTS market_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_type TEXT NOT NULL,
  as_of_date TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'global',
  content_md TEXT NOT NULL,
  structured_json TEXT,
  agent_run_id TEXT,
  generated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(report_type, as_of_date, scope)
);
CREATE INDEX IF NOT EXISTS idx_market_reports_lookup
  ON market_reports (report_type, scope, as_of_date);`

export function sqlStr(s: string): string { return `'${s.replace(/'/g, "''")}'` }

/** 跑一段 SQL，返回 stdout（SELECT 用 -json，其它为空串）。失败抛错（execFileSync 非零退出即 throw）。 */
export function runSqlite(dbPath: string, sql: string): string {
  return execFileSync('sqlite3', ['-json', '-cmd', '.timeout 5000', dbPath], {
    input: sql,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  })
}

export function ensureMarketReportsTable(dbPath: string): void {
  runSqlite(dbPath, MARKET_REPORTS_DDL)
}

export type V5MainlinePlanForReport = {
  as_of_date?: string
  decision_trade_date?: string
  regime?: {
    name?: string
    hs300_vs_ma120?: number
    concentration?: { dominant_group?: string; dominant_count?: number; counts?: Record<string, number> }
  }
  v4_holdings?: Array<Record<string, unknown>>
  portfolio?: Array<Record<string, unknown>>
  fund_matches?: Array<{
    board_code?: string
    board_name?: string
    role?: string
    selected?: Record<string, unknown>
    candidates?: Array<Record<string, unknown>>
  }>
}

function strField(v: unknown): string { return typeof v === "string" ? v : "" }
function numField(v: unknown): number | null { return typeof v === "number" && Number.isFinite(v) ? v : null }
function pctField(v: unknown): string {
  const n = numField(v)
  return n === null ? "" : (n * 100).toFixed(1) + "%"
}

// v5_mainline_plan.py 跑一次要 ~100s（CPU 近乎不占，几乎全在等 simworld/上游网络）。
// reporter-mainline / bot101 一个决策日内会调 get_v5_mainline_plan 几十次，submit 时还要再算
// 一遍 → 旧实现每次都重跑脚本，execFileSync 同步阻塞 event loop，agent 单次工具调用先超时再
// 重试、重试又触发新一轮 100s 执行 → 雪崩，mainline 报告永远产不出（2026-06-15 实测 06-12
// 连续 6 次失败的根因）。按世界日缓存 raw stdout：同一 asOf 第一次算、之后秒返回（PIT 当日
// plan 是确定值）。加 5min timeout 防上游真卡死时 execFileSync 无限阻塞。
const _v5PlanRawCache = new Map<string, string>()
function runV5MainlinePlanRaw(worldRoot: string, asOf: string): string {
  const cached = _v5PlanRawCache.get(asOf)
  if (cached !== undefined) return cached
  const script = resolve(worldRoot, "..", "..", "scripts", "v5_mainline_plan.py")
  if (!existsSync(script)) throw new Error("找不到 v5 主线脚本：" + script)
  const out = execFileSync("python3", [script, "--date", asOf, "--compact", "--include-funds"], {
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
    timeout: 300_000,
  })
  _v5PlanRawCache.set(asOf, out)
  return out
}

export function loadV5MainlinePlanForReport(worldRoot: string, asOf: string): V5MainlinePlanForReport {
  return JSON.parse(runV5MainlinePlanRaw(worldRoot, asOf)) as V5MainlinePlanForReport
}

// preloadedPlan：调用方（如 backfill-deterministic）已拿到同日 plan 时传入，省一次 v5 脚本子进程。
export function normalizeMarketMainlineSubmission(worldRoot: string, asOf: string, content: string, preloadedPlan?: V5MainlinePlanForReport): { content: string; structured: string } {
  const plan = preloadedPlan ?? loadV5MainlinePlanForReport(worldRoot, asOf)
  const holdings = (plan.portfolio?.length ? plan.portfolio : plan.v4_holdings) ?? []
  const mainlineSectors = holdings.map(h => ({
    code: strField(h.code),
    name: strField(h.name),
    role: strField(h.role),
    rank: numField(h.rank),
    above_ma60: typeof h.above_ma60 === "boolean" ? h.above_ma60 : null,
  }))
  const fundPool = (plan.fund_matches ?? []).map(m => {
    const selected = m.selected ?? {}
    return {
      board_code: m.board_code ?? "",
      board_name: m.board_name ?? "",
      role: m.role ?? "",
      code: strField(selected.code),
      name: strField(selected.name),
      track_index: strField(selected.index_name),
      index_code: strField(selected.index_code),
      tier: strField(selected.verdict),
      overlap: numField(selected.overlap),
      overlap_weight: numField(selected.overlap_weight),
      corr: numField(selected.corr),
      beta: numField(selected.beta),
      r2: numField(selected.r2),
      scale_yi: numField(selected.scale_yi),
      pool_tier: strField(selected.pool_tier),
    }
  })
  const structured = {
    mainline_theme: plan.regime?.concentration?.dominant_group ?? "无主线",
    confidence: plan.regime?.name === "抱主线·v4" ? "中" : "弱",
    regime: plan.regime?.name ?? "",
    dominant_group: plan.regime?.concentration?.dominant_group ?? "",
    dominant_count: plan.regime?.concentration?.dominant_count ?? null,
    hs300_vs_ma120: plan.regime?.hs300_vs_ma120 ?? null,
    mainline_sectors: mainlineSectors,
    v4_holdings: mainlineSectors,
    fund_pool: fundPool,
    bottom_line_product: fundPool[0] ?? null,
    fund_matches: plan.fund_matches ?? [],
    source: "v5_mainline_plan",
    decision_trade_date: plan.decision_trade_date ?? plan.as_of_date ?? "",
  }
  const holdingText = mainlineSectors.map(h => h.code + " " + h.name + " [" + h.role + "]").join(" | ")
  const fundText = fundPool.map(f => f.board_code + "->" + f.code + " " + f.name + "(" + f.tier + ", overlap=" + pctField(f.overlap) + ", corr=" + (f.corr ?? "") + ")").join(" | ")
  const header = [
    "<!-- v5_mainline_verified=1 -->",
    "# v5 主线校验摘要",
    "",
    "| 字段 | 结果 |",
    "|---|---|",
    "| regime | " + (plan.regime?.name ?? "") + " |",
    "| 主导组 | " + (plan.regime?.concentration?.dominant_group ?? "") + " |",
    "| 主线板块 | " + holdingText + " |",
    "| 基金映射 | " + fundText + " |",
    "",
    "以下正文由 reporter 生成；上方摘要与 structured_json 由 strategy-server 按 v5 确定性输出覆盖。",
  ].join("\n")
  return { content: header + "\n\n---\n\n" + content, structured: JSON.stringify(structured) }
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = []
  for await (const c of req) chunks.push(c as Buffer)
  return Buffer.concat(chunks).toString('utf8')
}

function rpcError(id: number | string | null | undefined, code: number, message: string): JsonRpcResponse {
  return { jsonrpc: '2.0', id: id ?? null, error: { code, message } }
}

function rpcResult(id: number | string | null | undefined, result: unknown): JsonRpcResponse {
  return { jsonrpc: '2.0', id: id ?? null, result }
}

/** 把单条 JSON-RPC 响应序列化为 SSE event 文本（FastMCP 同款格式）。 */
function sseEvent(payload: unknown): string {
  return `event: message\ndata: ${JSON.stringify(payload)}\n\n`
}

export async function createStrategyServer(opts: StrategyServerOptions): Promise<StrategyServerHandle> {
  const host = opts.host ?? '127.0.0.1'
  const { worldRoot, runId, getCurrentDate } = opts
  const dbPath = opts.fundDbPath ?? fundDbFile(worldRoot)
  // 提前建好目录（一次性，工具调用就不需要再 mkdir）。
  mkdirSync(strategiesDir(worldRoot, runId), { recursive: true })
  // market_reports 表幂等 ensure（fresh DB / 新环境也能直接跑 pre-pass）。fund.db 缺失时
  // 不致命——report 工具会在调用时报错，但 strategy/methodology 工具照常工作。
  try { ensureMarketReportsTable(dbPath) }
  catch (e) { console.warn(`[strategy-server] ensure market_reports table failed: ${e instanceof Error ? e.message : String(e)}`) }

  // mcp-session-id：initialize 时签发，后续请求可带可不带（我们不校验，只是按 spec 暴露）。
  // 多客户端共享时各自有自己的 session id 也没关系——server 是无状态的。
  // 为了 dashboard / log 调试方便，仍把 issued ids 留个内存集合，但不用做权限决策。
  const issuedSessions = new Set<string>()

  function handleToolCall(name: string, args: Record<string, unknown>): ToolResult {
    const botId = typeof args.bot_id === 'string' ? args.bot_id.trim() : ''
    if (!botId) return err('bot_id 必填且非空')
    // 仅允许 alphanumeric/_-，防止路径穿越（../../../...）。
    if (!/^[A-Za-z0-9_-]+$/.test(botId)) return err(`bot_id "${botId}" 含非法字符：只允许字母/数字/_/-`)

    const targetPath = methodologyPathOf(worldRoot, runId, botId)

    if (name === 'update_my_strategy') {
      const strategy = typeof args.strategy === 'string' ? args.strategy : ''
      const reason = typeof args.reason === 'string' ? args.reason.trim() : ''
      if (!strategy.trim()) return err('strategy 必填（完整 markdown 文本，不是 diff）')
      if (!reason) return err('reason 必填——一句话说明为什么改这一版（用于审计日志）')

      let priorSize: number | null = null
      try { priorSize = readFileSync(targetPath, 'utf8').length } catch { /* first write */ }

      const content = strategy.endsWith('\n') ? strategy : strategy + '\n'
      writeFileSync(targetPath, content)

      // 审计日志：strategies/ 目录在 setup 时已 mkdir；这里直接 append。
      const revEntry = { ts: getCurrentDate(), reason, new_size: strategy.length, prior_size: priorSize }
      appendFileSync(strategyRevisionsFile(worldRoot, runId, botId), JSON.stringify(revEntry) + '\n')

      return ok(
        `METHODOLOGY.md 已更新（新版 ${strategy.length} chars`
        + (priorSize === null ? '；首次写入' : `；上一版 ${priorSize} chars`)
        + `）。理由已写入审计日志：${reason}\n下一交易日的 system prompt（## METHODOLOGY.md section）会注入这一新版本。`
      )
    }

    if (name === 'list_strategies') {
      try {
        const lib = loadStrategyLibrary(strategyLibraryRootOf(worldRoot, runId, botId))
        const strategies = [...lib.strategies.values()].sort((a, b) => a.id.localeCompare(b.id)).map(strategy => ({
          strategy_id: strategy.id,
          title: strategy.title,
          target_index: strategy.targetIndex,
          default_buyable_fund_codes: strategy.defaultBuyableFundCodes,
        }))
        return ok(JSON.stringify({ success: true, strategies }, null, 2))
      } catch (e) {
        return err(`读取共享策略库失败：${e instanceof Error ? e.message : String(e)}`)
      }
    }

    if (name === 'get_strategy') {
      const strategyId = typeof args.strategy_id === 'string' ? args.strategy_id.trim() : ''
      if (!strategyId) return err('strategy_id 必填且非空')
      if (!/^[A-Za-z0-9_-]+$/.test(strategyId)) return err(`strategy_id "${strategyId}" 含非法字符：只允许字母/数字/_/-`)
      try {
        const lib = loadStrategyLibrary(strategyLibraryRootOf(worldRoot, runId, botId))
        const strategy = lib.strategies.get(strategyId)
        if (!strategy) return err(`strategy_id "${strategyId}" 不存在`)
        return ok(readFileSync(strategy.methodologyPath, 'utf8'))
      } catch (e) {
        return err(`读取共享策略失败：${e instanceof Error ? e.message : String(e)}`)
      }
    }

    if (name === 'get_my_strategy' || name === 'get_active_strategy') {
      if (!existsSync(targetPath)) {
        return err(`找不到 ${botId} 的 METHODOLOGY.md——这通常意味着 shadow workspace 没拷贝成功，请联系 world 维护者。`)
      }
      try { return ok(readFileSync(targetPath, 'utf8')) }
      catch (e) { return err(`读取 active methodology 失败：${e instanceof Error ? e.message : String(e)}`) }
    }

    if (name === 'get_v5_mainline_plan') {
      const asOf = getCurrentDate()
      try {
        return ok(runV5MainlinePlanRaw(worldRoot, asOf).trim())
      } catch (e) {
        return err('读取 v5_mainline_plan 失败：' + (e instanceof Error ? e.message : String(e)))
      }
    }

    if (name === 'get_market_report') {
      const rt = typeof args.report_type === 'string' ? args.report_type.trim() : ''
      if (!rt) return err("report_type 必填：'market_context' | 'market_mainline' | 'mainline_rotation' | 'all'")
      const wanted: ReportType[] = rt === 'all'
        ? [...VALID_REPORT_TYPES]
        : (VALID_REPORT_TYPES.includes(rt as ReportType) ? [rt as ReportType] : [])
      if (wanted.length === 0) return err(`report_type "${rt}" 非法：只能是 ${VALID_REPORT_TYPES.join(' / ')} 或 all`)
      const asOf = getCurrentDate()  // PIT 游标：bot 拿不到、改不了
      try {
        const parts: string[] = []
        for (const type of wanted) {
          const sql = `SELECT as_of_date, content_md FROM market_reports `
            + `WHERE report_type=${sqlStr(type)} AND scope='global' AND as_of_date<=${sqlStr(asOf)} `
            + `ORDER BY as_of_date DESC LIMIT 1;`
          const out = runSqlite(dbPath, sql).trim()
          const rows = out ? (JSON.parse(out) as { as_of_date: string; content_md: string }[]) : []
          if (rows.length === 0) {
            parts.push(`# [${type}] 暂无报告\n（截至当前世界日尚无该类报告——可能 pre-pass 未生成到此区间。请按各自方法论自行判断或保守处理。）`)
          } else {
            parts.push(`<!-- report_type=${type} as_of=${rows[0].as_of_date} (PIT≤${asOf}) -->\n${rows[0].content_md}`)
          }
        }
        return ok(parts.join('\n\n---\n\n'))
      } catch (e) {
        return err(`读取 market_report 失败：${e instanceof Error ? e.message : String(e)}`)
      }
    }

    if (name === 'submit_market_report') {
      if (!botId.startsWith('reporter-')) {
        return err(`submit_market_report 仅供系统 reporter agent 调用（bot_id 需以 reporter- 开头），当前 "${botId}" 无权写入。`)
      }
      const rt = typeof args.report_type === 'string' ? args.report_type.trim() : ''
      if (!VALID_REPORT_TYPES.includes(rt as ReportType)) {
        return err(`report_type "${rt}" 非法：只能是 ${VALID_REPORT_TYPES.join(' / ')}`)
      }
      let content = typeof args.content_md === "string" ? args.content_md : ""
      if (!content.trim()) return err("content_md 必填（完整研报 markdown）")
      let structured = typeof args.structured_json === "string" && args.structured_json.trim() ? args.structured_json : null
      if (structured !== null) {
        try { JSON.parse(structured) } catch { return err("structured_json 不是合法 JSON 字符串") }
      }
      const asOf = getCurrentDate()  // PIT：as_of_date 由世界当前日决定，agent 无法指定
      if (rt === "market_mainline") {
        try {
          const normalized = normalizeMarketMainlineSubmission(worldRoot, asOf, content)
          content = normalized.content
          structured = normalized.structured
        } catch (e) {
          return err("market_mainline v5 规范化失败：" + (e instanceof Error ? e.message : String(e)))
        }
      }
      try {
        const sql = `INSERT INTO market_reports (report_type, as_of_date, scope, content_md, structured_json, agent_run_id) `
          + `VALUES (${sqlStr(rt)}, ${sqlStr(asOf)}, 'global', ${sqlStr(content)}, ${structured === null ? 'NULL' : sqlStr(structured)}, ${sqlStr(runId)}) `
          + `ON CONFLICT(report_type, as_of_date, scope) DO UPDATE SET `
          + `content_md=excluded.content_md, structured_json=excluded.structured_json, `
          + `agent_run_id=excluded.agent_run_id, generated_at=datetime('now');`
        runSqlite(dbPath, sql)
        return ok(`market_report 已写入：${rt} @ ${asOf}（${content.length} chars${structured === null ? '' : '，含 structured_json'}）。`)
      } catch (e) {
        return err(`写入 market_report 失败：${e instanceof Error ? e.message : String(e)}`)
      }
    }

    return err(`unknown tool: ${name}`)
  }

  /** 处理一条 JSON-RPC 请求。返回 null = notification（不需要响应）；否则返回 response 对象。 */
  function handleRpc(msg: JsonRpcRequest): JsonRpcResponse | null {
    const id = msg.id
    const method = msg.method ?? ''
    if (method === 'initialize') {
      return rpcResult(id, {
        // 按 spec：如果客户端的 protocolVersion 我们支持，echo 回去；否则给我们支持的版本。
        // 我们目前只声明支持 2024-11-05；客户端发其它版本我们仍然回 2024-11-05，客户端自行决定降级。
        protocolVersion: PROTOCOL_VERSION,
        // 与 FastMCP 对齐：四类 capability 都列出，subtle 字段填默认值，便于严格客户端能力门控。
        capabilities: {
          experimental: {},
          prompts: { listChanged: false },
          resources: { subscribe: false, listChanged: false },
          tools: { listChanged: false },
        },
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        instructions: SERVER_INSTRUCTIONS,
      })
    }
    if (method === 'ping') return rpcResult(id, {})
    if (method === 'tools/list') return rpcResult(id, { tools: TOOLS })
    if (method === 'tools/call') {
      const params = msg.params ?? {}
      const toolName = typeof params.name === 'string' ? params.name : ''
      const argsRaw = params.arguments
      const args = typeof argsRaw === 'object' && argsRaw !== null && !Array.isArray(argsRaw) ? argsRaw as Record<string, unknown> : {}
      try { return rpcResult(id, handleToolCall(toolName, args)) }
      catch (e) { return rpcError(id, -32603, `tool execution error: ${e instanceof Error ? e.message : String(e)}`) }
    }
    // We have nothing to offer in resources/prompts; answer with empty lists so
    // clients calling them on init don't fail noisily.
    if (method === 'resources/list') return rpcResult(id, { resources: [] })
    if (method === 'prompts/list') return rpcResult(id, { prompts: [] })
    // Notifications: no response, just acknowledge.
    if (method.startsWith('notifications/')) return null
    return rpcError(id, -32601, `method not found: ${method}`)
  }

  const server = createServer((req, res) => {
    void handle(req, res).catch(e => {
      // Best-effort error reporting; if headers already sent we just end the response.
      try {
        res.writeHead(500, { 'content-type': 'application/json' })
        res.end(JSON.stringify(rpcError(null, -32603, `strategy-server internal error: ${e instanceof Error ? e.message : String(e)}`)))
      } catch { try { res.end() } catch { /* ignore */ } }
    })
  })

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    // 健康检查（不参与 MCP 协议；dashboard / smoke test 可以用）。
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ status: 'ok', tools: TOOLS.map(t => t.name) }))
      return
    }
    // MCP 允许客户端在 streamable-http transport 上 GET /mcp 建立 SSE listener
    // 接收 server-side 通知。我们没有 server push 内容，但要回 200 + 空 SSE 流，
    // 否则严格客户端 GET 失败可能拒绝继续。简单做法：405（spec 允许），客户端
    // 该忽略并继续走 POST。fund-portfolio-mcp（FastMCP）实测也是 405。
    if (req.method !== 'POST') {
      res.writeHead(405, { 'content-type': 'application/json', allow: 'POST' })
      res.end(JSON.stringify(rpcError(null, -32600, 'only POST is supported on /mcp')))
      return
    }
    const raw = await readBody(req)
    let msg: JsonRpcRequest | null = null
    try { msg = raw ? JSON.parse(raw) as JsonRpcRequest : null }
    catch { msg = null }
    if (!msg) {
      res.writeHead(400, { 'content-type': 'application/json' })
      res.end(JSON.stringify(rpcError(null, -32700, 'invalid JSON-RPC body')))
      return
    }

    const out = handleRpc(msg)
    if (out === null) {
      // notification — accept with 202, no body
      res.writeHead(202)
      res.end()
      return
    }

    // 初始化时签发 mcp-session-id。后续请求若带 header 我们接受但不校验。
    const headers: Record<string, string> = {}
    if (msg.method === 'initialize') {
      const sid = randomUUID().replace(/-/g, '')
      issuedSessions.add(sid)
      headers['mcp-session-id'] = sid
    } else {
      const incoming = req.headers['mcp-session-id']
      const sid = Array.isArray(incoming) ? incoming[0] : incoming
      if (sid) headers['mcp-session-id'] = sid
    }

    // 响应格式：客户端 Accept 含 text/event-stream → SSE（FastMCP 同款，mcporter 实测走这条）。
    // 否则退回到 application/json（spec 允许，单元测试 / 简单 curl 更好对付）。
    const accept = (req.headers.accept ?? '').toString()
    const wantsSse = accept.includes('text/event-stream')
    if (wantsSse) {
      headers['content-type'] = 'text/event-stream'
      headers['cache-control'] = 'no-cache, no-transform'
      headers['connection'] = 'keep-alive'
      res.writeHead(200, headers)
      res.write(sseEvent(out))
      res.end()
    } else {
      headers['content-type'] = 'application/json'
      res.writeHead(200, headers)
      res.end(JSON.stringify(out))
    }
  }

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(opts.port ?? 0, host, () => { server.off('error', reject); resolve() })
  })
  const port = (server.address() as AddressInfo).port

  return {
    port,
    url: `http://${host}:${port}/mcp`,
    close: () => new Promise<void>((resolve) => {
      const done = (): void => resolve()
      const timer = setTimeout(done, 3000)
      try { (server as { closeAllConnections?: () => void }).closeAllConnections?.() } catch { /* not available pre-Node 18.2 */ }
      server.close(() => { clearTimeout(timer); done() })
    }),
  }
}
