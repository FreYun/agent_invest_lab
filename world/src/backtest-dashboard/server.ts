import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { dirname, join, resolve } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { fileURLToPath } from 'node:url'
import { botDayDir, hiddenRecordsFile, replyFile, runDir, runVerdictsFile, sentFile, shadowWorkspaceDir, strategyRevisionsFile, universeContaminationFile } from '../paths.ts'
import { loadStrategyLibrary, renderActiveMethodology } from '../strategy-library.ts'
import { readRunModel, type RunModelInfo } from '../run-model.ts'
import { listControllableRuns, type WorldState } from '../state.ts'
import { requestPause, requestStop } from '../run-control.ts'
import { buildHoldingsByDate, computeActionWeights } from './positions.ts'
import { createBot101ChatEngine, type Bot101ChatEngine, type ChatMessage } from './bot101-chat.ts'
import { fetchIntradayBoards } from '../intraday-boards.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const DEFAULT_DB = join(HERE, '../../../data/fund.db')
const REPO_ROOT = resolve(HERE, '../../..')
// 默认 worldRoot = <repo>/world/runtime；和 paths.ts 里其它 per-run helper 的约定一致。
// 仅用来定位每 run 的 universe-contamination.json marker 文件，不影响 DB 查询。
const DEFAULT_WORLD_ROOT = join(HERE, '../../runtime')
const DEFAULT_HTML = join(HERE, 'index.html')
const DEFAULT_MARKET_REPORTS_HTML = join(HERE, 'market-reports.html')
const DEFAULT_OOS_BOT101_HTML = join(HERE, 'oos-bot101.html')
const DEFAULT_RUNS_HTML = join(HERE, 'runs.html')
const DEFAULT_AGENTS_HTML = join(HERE, 'agents.html')

interface DailyRow {
  trade_date: string
  initial_capital: number | null
  cash: number | null
  invested_value: number | null
  total_value: number | null
  net_value: number | null
  daily_return_pct: number | null
  cumulative_return_pct: number | null
  max_drawdown_pct: number | null
  equity_weight: number | null
  bond_weight: number | null
  gold_weight: number | null
  cash_weight: number | null
}

interface AccountRow {
  initial_capital: number | null
  cash: number | null
}

interface HoldingRow {
  trade_date: string
  fund_code: string
  fund_name: string
  theme: string
  asset_class: string | null
  role: string | null
  shares: number | null
  nav: number | null
  market_value: number | null
  weight: number | null
  daily_pnl: number | null
  cumulative_return_pct: number | null
  holding_days: number | null
}

interface ActionRow {
  action_id: number
  review_id: number | null
  bot_id: string
  fund_code: string
  fund_name: string
  action_type: string
  final_decision: string | null
  amount: number | null
  shares: number | null
  nav_used: number | null
  reason: string | null
  bot_reason: string | null
  /** 该笔动作对应订单的手续费（fund_bot_orders.fee，关联子查询带出；无匹配订单为 null）。 */
  fee: number | null
  action_date: string
}

interface ReviewRow {
  review_id: number
  review_date: string | null
  regime: string | null
  decision: string | null
  reason: string | null
  turnover_ratio: number | null
}

interface BotSeriesPoint {
  trade_date: string
  net_value: number
  total_value: number
  cash: number
  invested_value: number
  daily_return_pct: number | null
  cumulative_return_pct: number | null
  max_drawdown_pct: number | null
  equity_weight: number | null
  bond_weight: number | null
  gold_weight: number | null
  cash_weight: number | null
}

interface BotAction extends ActionRow {
  side: 'buy' | 'sell' | 'hold'
  weight_before: number
  weight_after: number
  weight_delta: number
}

interface BenchmarkPoint {
  trade_date: string
  nav: number
  net_value: number
}

interface BotBenchmark {
  fundCode: string
  fundName: string
  anchorDate: string
  baselineNav: number
  series: BenchmarkPoint[]
}

interface UserTxnMark {
  date: string
  side: 'buy' | 'sell'
  amount: number
  count: number
}

interface RealUserSeries {
  fundCode: string
  fundName: string
  cycleId: string
  clearReturn2: number
  bigLossRate: number | null
  bigProfitRate: number | null
  txnCount: number
  series: { trade_date: string; net_value: number }[]
  txns: UserTxnMark[]
}

interface BotDataset {
  botId: string
  firstTradeDate: string
  latestTradeDate: string
  days: number
  initialCapital: number
  cash: number
  totalValue: number
  netValue: number
  cumulativeReturnPct: number
  dailyReturnPct: number
  maxDrawdownPct: number
  actionCount: number
  buyCount: number
  sellCount: number
  holdCount: number
  holdingsCount: number
  /** 本 run 全部订单的手续费合计（fund_bot_orders.fee，申购费 + 赎回费）。 */
  totalFee: number
  buyFee: number
  sellFee: number
  equityWeight: number | null
  bondWeight: number | null
  goldWeight: number | null
  cashWeight: number | null
  series: BotSeriesPoint[]
  // 该 bot 在本 run 真正「决策/反思」过的交易日（runtime 下存在 bot 当日目录的日期，升序）。
  // 月度/周度回测时 series 是逐日 NAV、决策却只在月末/周末 → 反思面板按这个列表翻页，
  // 才会一格一格落在有内容的决策日，而不是逐日空翻。
  reflectionDates: string[]
  actions: BotAction[]
  holdings: HoldingRow[]
  holdingsByDate: Record<string, Omit<HoldingRow, 'trade_date'>[]>
  reviews: ReviewRow[]
  benchmark: BotBenchmark | null
  realUsers: RealUserSeries[]
  runId: string
  availableRuns: BotRunRef[]
  /** 本 run 实际生效的模型供应商 + 模型名（读 research-loop.yaml，不含 key）；取不到为 null。 */
  model: RunModelInfo | null
}

interface BotRunRef {
  runId: string
  latestDate: string
  /** 受可买池污染过的历史 run（scripts/detect-universe-contamination.py 写的 marker 存在）。
   *  仅是元数据提示，bot 数据本身没有被改动——这个 run 期间 bot 看到的可买池可能是别人 run
   *  的池子，所以做过非预期的下单。 */
  contaminated?: boolean
}

/** Summary variant used in /api/backtest/data (polled every 10 s).
 *  holdingsByDate is stripped — it is only needed on the per-bot detail view. */
type BotDatasetSummary = Omit<BotDataset, 'holdingsByDate' | 'realUsers'>

interface Dataset {
  generatedAt: string
  dbPath: string
  summary: {
    botCount: number
    latestTradeDate: string
    totalValue: number
    averageReturnPct: number
    bestBotId: string
    bestReturnPct: number
    worstBotId: string
    worstReturnPct: number
    /** 全部 bot（各自当前展示 run）手续费合计。 */
    totalFee: number
  }
  bots: BotDatasetSummary[]
}

function quoteSql(s: string): string {
  return `'${s.replace(/'/g, "''")}'`
}

/** bot 每天滚动刷新、注回 prompt 的自我反思内容（来自当日 sent.md / reply.json 的真值切片）。
 *  - memoryWindow: 【交易记忆窗口】= buildHistoryWindow 产出的"最近原样 + 更早 4 维度压缩"笔记
 *  - decision:     reply.json 的 reply 字段 = bot 当日收尾的决策总结 */
interface BotReflection {
  tradeDate: string
  memoryWindow: string
  decision: string
}

/** sent.md 是按 `【…】` 顶级标记分段的当日完整 prompt 原文。切成 header→body 段，
 *  好按标记前缀挑出反思相关的块。第一个 `【` 之前的内容（通常没有）丢弃。 */
function sliceSentSections(md: string): { header: string; body: string }[] {
  const out: { header: string; lines: string[] }[] = []
  let cur: { header: string; lines: string[] } | null = null
  for (const line of md.split('\n')) {
    if (line.startsWith('【')) { cur = { header: line, lines: [line] }; out.push(cur) }
    else if (cur) cur.lines.push(line)
  }
  return out.map(s => ({ header: s.header, body: s.lines.join('\n').trim() }))
}

/** 读当日 sent.md / reply.json，best-effort 拼出反思内容。文件缺失（首日 / 旧 run）
 *  时对应字段留空字符串，由前端兜底提示——保证功能可加性，不抛错。 */
function loadReflection(worldRoot: string, runId: string, botId: string, date: string): BotReflection {
  const result: BotReflection = { tradeDate: date, memoryWindow: '', decision: '' }
  const sentPath = sentFile(worldRoot, runId, date, botId)
  if (existsSync(sentPath)) {
    const sections = sliceSentSections(readFileSync(sentPath, 'utf8'))
    const pick = (prefix: string) => sections.find(s => s.header.startsWith(prefix))?.body ?? ''
    result.memoryWindow = pick('【交易记忆窗口')
  }
  const replyPath = replyFile(worldRoot, runId, date, botId)
  if (existsSync(replyPath)) {
    try {
      const parsed = JSON.parse(readFileSync(replyPath, 'utf8')) as { reply?: unknown }
      if (typeof parsed.reply === 'string') result.decision = parsed.reply
    } catch { /* best-effort：reply.json 损坏就留空 */ }
  }
  return result
}

// Persistent in-process SQLite handles, one per dbPath. Replaces the old per-query
// `sqlite3` CLI spawn: forking the CLI + reopening the ~280MB fund.db cost ~7ms per
// query, and loadDataset fires ~200 queries per /api/backtest/data poll → ~1.5s of
// pure spawn overhead (the SQL itself is ~150ms). An in-process handle drops that to
// ~2ms total. Opened read-write (matching the CLI default) so WAL/-shm attach while
// live world runs write; busy_timeout waits out the rare writer/checkpoint lock window
// instead of failing with SQLITE_BUSY. Each .all() is its own implicit txn, so the
// handle holds no lock between queries and never blocks a writer's checkpoint.
const _dbHandles = new Map<string, DatabaseSync>()
function getDb(dbPath: string): DatabaseSync {
  let db = _dbHandles.get(dbPath)
  if (!db) {
    db = new DatabaseSync(dbPath)
    db.exec('PRAGMA busy_timeout = 5000')
    _dbHandles.set(dbPath, db)
  }
  return db
}

function queryRows<T>(dbPath: string, sql: string): T[] {
  return getDb(dbPath).prepare(sql).all() as T[]
}

function latestFundNavDate(dbPath: string): string {
  const row = queryRows<{ d: string | null }>(dbPath, 'SELECT MAX(nav_date) AS d FROM fund_nav')[0]
  return row?.d ?? ''
}

function dedupeByDate<T extends { trade_date: string }>(rows: T[]): T[] {
  const seen = new Map<string, T>()
  for (const row of rows) seen.set(row.trade_date, row)
  return [...seen.values()]
}

function num(v: unknown): number {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : 0
}

function classifyAction(actionType: string, finalDecision: string | null): 'buy' | 'sell' | 'hold' {
  const token = `${actionType} ${finalDecision ?? ''}`.toUpperCase()
  if (/(ADD|BUY|INCREASE)/.test(token)) return 'buy'
  if (/(REDUCE|SELL|TAKE_PROFIT|STOP_LOSS|DECREASE)/.test(token)) return 'sell'
  return 'hold'
}

/** 每个 bot 最多展示的真实用户数。 */
const MAX_REAL_USERS = 10

/** 真实用户交易类型 → 买/卖/跳过。
 *  买入: 139 定时定额投资, 122 申购; 卖出: 124 赎回, 142 强行赎回。
 *  其余(129 分红设置 / 136,137 转换 / 126,127 转托管 / 1T1,1T2 账户转入转出)为中性, 不打点。 */
export function classifyBusinType(businType: string): 'buy' | 'sell' | null {
  if (businType === '139' || businType === '122') return 'buy'
  if (businType === '124' || businType === '142') return 'sell'
  return null
}

/** 把逐笔交易按 (cycle, 日期, 方向) 合并: amount 求和、count 计数, 中性交易丢弃。
 *  返回 cycle_id -> 按日期(再按方向)升序的标记数组。 */
export function mergeUserTxns(
  rows: { cycle_id: string; busin_type: string; amount: number | null; txn_date: string }[],
): Map<string, UserTxnMark[]> {
  const byCycle = new Map<string, Map<string, UserTxnMark>>()
  for (const r of rows) {
    const side = classifyBusinType(r.busin_type)
    if (!side) continue
    let marks = byCycle.get(r.cycle_id)
    if (!marks) { marks = new Map(); byCycle.set(r.cycle_id, marks) }
    const key = `${r.txn_date}|${side}`
    const cur = marks.get(key)
    if (cur) { cur.amount += num(r.amount); cur.count++ }
    else marks.set(key, { date: r.txn_date, side, amount: num(r.amount), count: 1 })
  }
  const out = new Map<string, UserTxnMark[]>()
  for (const [cid, marks] of byCycle) {
    out.set(cid, [...marks.values()].sort((a, b) =>
      a.date < b.date ? -1 : a.date > b.date ? 1 : a.side < b.side ? -1 : 1))
  }
  return out
}

/** 在候选基金池间分配总配额：先 floor(total/n) 平均，余数按候选数从多到少补，
 *  每池不超过其候选数。返回 fund_code -> 配额。 */
export function allocateQuota(poolSizes: Map<string, number>, total: number): Map<string, number> {
  const alloc = new Map<string, number>()
  const pools = [...poolSizes.keys()]
  for (const p of pools) alloc.set(p, 0)
  if (!pools.length || total <= 0) return alloc
  const base = Math.floor(total / pools.length)
  for (const p of pools) alloc.set(p, Math.min(base, poolSizes.get(p) ?? 0))
  let remaining = total - [...alloc.values()].reduce((s, v) => s + v, 0)
  const order = [...pools].sort((a, b) => (poolSizes.get(b)! - poolSizes.get(a)!) || a.localeCompare(b))
  while (remaining > 0) {
    let progressed = false
    for (const p of order) {
      if (remaining <= 0) break
      if (alloc.get(p)! < (poolSizes.get(p) ?? 0)) {
        alloc.set(p, alloc.get(p)! + 1)
        remaining--
        progressed = true
      }
    }
    if (!progressed) break  // 所有池已封顶,剩余配额无处可放
  }
  return alloc
}

/** 在按 clear_return2 升序的候选里挑 quota 个代表：先锚定 最差/最好/中位/p25/p75,
 *  再按均匀分布补足,去重,返回保持升序的子集。 */
export function selectRepresentative<T>(sortedAsc: T[], quota: number): T[] {
  const n = sortedAsc.length
  if (quota <= 0 || n === 0) return []
  if (quota >= n) return [...sortedAsc]
  const idxAt = (frac: number) => Math.round(frac * (n - 1))
  const picks: number[] = []
  const take = (i: number) => { if (picks.length < quota && !picks.includes(i)) picks.push(i) }
  for (const a of [0, 1, 0.5, 0.25, 0.75]) take(idxAt(a))   // 代表性锚点
  for (let s = 0; picks.length < quota && s < n * 2; s++) take(idxAt(s / Math.max(1, quota - 1)))
  for (let i = 0; picks.length < quota && i < n; i++) take(i)  // 兜底填满
  return picks.sort((a, b) => a - b).map(i => sortedAsc[i])
}

// 未建仓 bot 没有首买基金时,用沪深300(510300 沪深300ETF华泰柏瑞)作默认参照,
// 这样任何 bot 都至少有一条参照曲线。
const DEFAULT_BENCHMARK_FUND = '510300'
// 多指数权益基金 bot 做多基金投资策略，统一用规模最大的沪深300 ETF
// (510300 沪深300ETF华泰柏瑞) 作为比较基准，而不是首笔买入标的。
// bot101/102/103 及其测试分身/克隆（bot101t_k1 等）都算——口径与 message.ts botKindOf 的
// multi-fund 一致：bot1+2位数字开头(可带后缀)。单基金 bot(bot1..bot20) 仍用首买标的。
function isLongEquityFundBot(botId: string): boolean {
  return /^bot1\d{2}(?:[^0-9].*)?$/.test(botId)
}
const OOS_RUN_ID_PREFIXES = ['oos-', 'live-']
function isOosRunId(runId: string): boolean {
  return OOS_RUN_ID_PREFIXES.some(prefix => runId.startsWith(prefix))
}

export function pickBenchmarkFund(actions: BotAction[], holdings: HoldingRow[], botId = ''): string {
  if (isLongEquityFundBot(botId)) return DEFAULT_BENCHMARK_FUND
  const firstBuy = actions.find(a => a.side === 'buy')
  return firstBuy?.fund_code || holdings[0]?.fund_code || DEFAULT_BENCHMARK_FUND
}

async function loadBenchmarkSeries(
  dbPath: string,
  fundCode: string,
  anchorDate: string,
  endDate: string,
): Promise<BotBenchmark | null> {
  if (!fundCode || !anchorDate || !endDate) return null
  const rows = await queryRows<{ nav_date: string; nav: number | null; fund_name: string | null }>(dbPath, `
    SELECT n.nav_date, n.nav, COALESCE(i.fund_name, n.fund_code) AS fund_name
    FROM fund_nav n
    LEFT JOIN fund_info i ON i.fund_code = n.fund_code
    WHERE n.fund_code = ${quoteSql(fundCode)}
      AND n.nav_date >= ${quoteSql(anchorDate)}
      AND n.nav_date <= ${quoteSql(endDate)}
      AND n.nav IS NOT NULL
    ORDER BY n.nav_date ASC
  `)
  if (!rows.length) return null
  const baselineNav = num(rows[0].nav)
  if (baselineNav <= 0) return null
  const fundName = rows[0].fund_name ?? fundCode
  const series: BenchmarkPoint[] = rows.map(r => ({
    trade_date: r.nav_date,
    nav: num(r.nav),
    net_value: num(r.nav) / baselineNav,
  }))
  return { fundCode, fundName, anchorDate, baselineNav, series }
}

async function loadBenchmark(
  dbPath: string,
  botId: string,
  actions: BotAction[],
  holdings: HoldingRow[],
  firstTradeDate: string,
  latestTradeDate: string,
): Promise<BotBenchmark | null> {
  const fundCode = pickBenchmarkFund(actions, holdings, botId)
  const anchorDate = firstTradeDate || actions.find(a => a.side === 'buy')?.action_date || ''
  if (!anchorDate || !latestTradeDate) return null
  return loadBenchmarkSeries(dbPath, fundCode, anchorDate, latestTradeDate)
}

// ---- 看板「隐藏记录」：纯展示层过滤，DB 一行不动 ----
// hidden-records.json = { hidden: [{botId, runId}] }。被隐藏的 (bot, run) 在 listRunsForBot
// 处被滤掉 → 该 run 从主列表/run 选择器消失；某 bot 全部 run 被隐藏则整行消失。可随时恢复。
interface HiddenRecord { botId: string; runId: string }
function hiddenKey(botId: string, runId: string): string { return `${botId} ${runId}` }
function loadHiddenRecords(worldRoot: string): HiddenRecord[] {
  try {
    const parsed = JSON.parse(readFileSync(hiddenRecordsFile(worldRoot), 'utf8')) as { hidden?: unknown }
    const arr = Array.isArray(parsed.hidden) ? parsed.hidden : []
    return arr.filter((r): r is HiddenRecord =>
      !!r && typeof (r as HiddenRecord).botId === 'string' && typeof (r as HiddenRecord).runId === 'string')
  } catch { return [] }  // 文件不存在/损坏 → 视作没有隐藏项
}
function saveHiddenRecords(worldRoot: string, list: HiddenRecord[]): void {
  writeFileSync(hiddenRecordsFile(worldRoot), JSON.stringify({ hidden: list }, null, 2))
}
function loadHiddenSet(worldRoot: string): Set<string> {
  return new Set(loadHiddenRecords(worldRoot).map(r => hiddenKey(r.botId, r.runId)))
}

// ---- Run 评测看板「合格/不合格」手动标记：全员共享，存服务器一份 ----
// run-verdicts.json = { "<run_id>|<bot>": "pass"|"fail" }。只记「偏离系统默认判定」的覆盖，
// 与默认一致的不入文件（前端 setMark 与默认相同 → 删除该 key）。纯展示层，DB 一行不动。
// 替代旧的浏览器 localStorage（per-browser、互不可见）——现在所有人读写同一份。
export type RunVerdict = 'pass' | 'fail'
/** key 合法性：run_id|bot，两段都是 ID 白名单字符、`|` 分隔。挡掉异常 key 写入文件。 */
const VERDICT_KEY_RE = /^[A-Za-z0-9._-]+\|[A-Za-z0-9._-]+$/
export function loadRunVerdicts(worldRoot: string): Record<string, RunVerdict> {
  try {
    const parsed = JSON.parse(readFileSync(runVerdictsFile(worldRoot), 'utf8')) as unknown
    if (!isPlainObject(parsed)) return {}
    const out: Record<string, RunVerdict> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (VERDICT_KEY_RE.test(k) && (v === 'pass' || v === 'fail')) out[k] = v
    }
    return out
  } catch { return {} }  // 文件不存在/损坏 → 视作无覆盖
}
export function saveRunVerdicts(worldRoot: string, map: Record<string, RunVerdict>): void {
  writeFileSync(runVerdictsFile(worldRoot), JSON.stringify(map, null, 2))
}

export interface MethodologyRevision { ts: string; reason: string; new_size: number | null; prior_size: number | null }
export interface MethodologyPayload {
  botId: string; runId: string
  strategyId: string | null; strategyTitle: string | null
  initial: string | null; latest: string | null
  revised: boolean; revisions: MethodologyRevision[]
}

// 只读重建某 (bot, run) 的方法论三件套：初始（从 run 冻结策略库快照重建）、最新
// （workspace METHODOLOGY.md）、进化轨迹（<bot>.revisions.jsonl）。任何缺失均降级为
// null / 空数组，不抛错。
export function loadMethodology(worldRoot: string, runId: string, botId: string): MethodologyPayload {
  const ws = shadowWorkspaceDir(worldRoot, runId, botId)

  let latest: string | null = null
  const latestPath = join(ws, 'METHODOLOGY.md')
  if (existsSync(latestPath)) { try { latest = readFileSync(latestPath, 'utf8') } catch { /* ignore */ } }

  let strategyId: string | null = null
  let strategyTitle: string | null = null
  let buyableCodes: string[] = []
  const assignPath = join(runDir(worldRoot, runId), 'strategy-assignments.json')
  if (existsSync(assignPath)) {
    try {
      const a = JSON.parse(readFileSync(assignPath, 'utf8')) as { bots?: Record<string, Record<string, unknown>> }
      const b = a.bots?.[botId]
      if (b) {
        if (typeof b.strategy_id === 'string') strategyId = b.strategy_id
        if (typeof b.strategy_title === 'string') strategyTitle = b.strategy_title
        if (Array.isArray(b.buyable_fund_codes)) buyableCodes = (b.buyable_fund_codes as unknown[]).filter((c): c is string => typeof c === 'string')
      }
    } catch { /* ignore malformed assignments */ }
  }

  let initial: string | null = null
  const frozenRoot = join(ws, 'strategies', 'index-products')
  if (strategyId && existsSync(join(frozenRoot, 'manifest.yaml'))) {
    try {
      const lib = loadStrategyLibrary(frozenRoot)
      const strat = lib.strategies.get(strategyId)
      if (strat) initial = renderActiveMethodology({ botId, strategy: strat, buyableFundCodes: buyableCodes })
    } catch { /* frozen lib unreadable → leave initial null */ }
  }

  const revisions: MethodologyRevision[] = []
  const revPath = strategyRevisionsFile(worldRoot, runId, botId)
  if (existsSync(revPath)) {
    for (const line of readFileSync(revPath, 'utf8').split('\n')) {
      const s = line.trim()
      if (!s) continue
      try {
        const o = JSON.parse(s) as Record<string, unknown>
        revisions.push({
          ts: typeof o.ts === 'string' ? o.ts : '',
          reason: typeof o.reason === 'string' ? o.reason : '',
          new_size: typeof o.new_size === 'number' ? o.new_size : null,
          prior_size: typeof o.prior_size === 'number' ? o.prior_size : null,
        })
      } catch { /* skip bad line */ }
    }
  }

  return { botId, runId, strategyId, strategyTitle, initial, latest, revised: revisions.length > 0, revisions }
}

async function listAllBotIds(dbPath: string): Promise<string[]> {
  const rows = await queryRows<{ bot_id: string }>(dbPath, `
    SELECT DISTINCT bot_id FROM (
      SELECT bot_id FROM fund_bot_daily_snapshots
      UNION SELECT bot_id FROM fund_bot_position_snapshots
      UNION SELECT bot_id FROM fund_bot_actions
      UNION SELECT bot_id FROM fund_bot_accounts
      UNION SELECT bot_id FROM fund_bot_reviews
      UNION SELECT bot_id FROM fund_bot_holdings
      UNION SELECT bot_id FROM fund_bot_orders
      UNION SELECT bot_id FROM fund_bot_performance
    )
    ORDER BY bot_id
  `)
  return rows.map(r => r.bot_id)
}

async function listRunsForBot(dbPath: string, worldRoot: string, botId: string): Promise<BotRunRef[]> {
  const b = quoteSql(botId)
  // ⚠️ 这里必须 UNION ALL 而非 UNION：sqlite 3.53.2（brew 2026-06-09 升级）的优化器会把
  // 「UNION 去重的升序中间结果 + GROUP BY 同列」当成已满足 ORDER BY，把 DESC 方向丢掉，
  // 导致 run 列表变成旧→新、看板默认选中最老的 run。UNION ALL 语义等价（GROUP BY 会去重）
  // 且绕开该回归；下面再做一次 JS 排序兜底，使顺序不再依赖外部 sqlite3 二进制的优化器行为。
  const rows = await queryRows<{ run_id: string; latest_date: string | null }>(dbPath, `
    WITH per_bot AS (
      SELECT run_id, trade_date AS d FROM fund_bot_daily_snapshots WHERE bot_id = ${b}
      UNION ALL SELECT run_id, trade_date AS d FROM fund_bot_position_snapshots WHERE bot_id = ${b}
      UNION ALL SELECT run_id, action_date AS d FROM fund_bot_actions WHERE bot_id = ${b}
      UNION ALL SELECT run_id, NULL AS d FROM fund_bot_accounts WHERE bot_id = ${b}
      UNION ALL SELECT run_id, review_date AS d FROM fund_bot_reviews WHERE bot_id = ${b}
      UNION ALL SELECT run_id, COALESCE(exit_date, entry_date) AS d FROM fund_bot_holdings WHERE bot_id = ${b} AND run_id IS NOT NULL AND run_id <> ''
      UNION ALL SELECT run_id, trade_date AS d FROM fund_bot_performance WHERE bot_id = ${b}
      UNION ALL SELECT order_run_id AS run_id, order_date AS d FROM fund_bot_orders WHERE bot_id = ${b} AND order_run_id IS NOT NULL AND order_run_id <> ''
      UNION ALL SELECT settle_run_id AS run_id, COALESCE(confirm_date, order_date) AS d FROM fund_bot_orders WHERE bot_id = ${b} AND settle_run_id IS NOT NULL AND settle_run_id <> ''
    )
    SELECT run_id, MAX(d) AS latest_date
    FROM per_bot
    WHERE run_id IS NOT NULL AND run_id <> ''
    GROUP BY run_id
    ORDER BY run_id DESC
  `)
  // 新 run 在前（run_id 含启动时间戳，字典序即时间序）；不信任 SQL 层的 ORDER BY，见上。
  rows.sort((a, b) => (a.run_id < b.run_id ? 1 : a.run_id > b.run_id ? -1 : 0))
  const hidden = loadHiddenSet(worldRoot)
  return rows
    .filter(r => !isOosRunId(r.run_id))
    .filter(r => !hidden.has(hiddenKey(botId, r.run_id)))  // 隐藏的 (bot, run) 不进列表/选择器
    .map(r => {
      const ref: BotRunRef = { runId: r.run_id, latestDate: r.latest_date ?? '' }
      if (existsSync(universeContaminationFile(worldRoot, r.run_id))) ref.contaminated = true
      return ref
    })
}

async function tableExists(dbPath: string, name: string): Promise<boolean> {
  const rows = await queryRows<{ n: number }>(dbPath,
    `SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name=${quoteSql(name)}`)
  return num(rows[0]?.n) > 0
}

/** 取 bot 触碰过的基金上的真实用户曲线：候选→按基金分配配额→池内代表性选取→拉曲线。
 *  real_user_* 表不存在(未落库)时返回 []——保证功能可加性,不破坏看板。 */
async function loadRealUsers(dbPath: string, fundCodes: string[]): Promise<RealUserSeries[]> {
  const codes = [...new Set(fundCodes)].filter(Boolean)
  if (!codes.length) return []
  if (!(await tableExists(dbPath, 'real_user_cycles'))) return []
  const inList = codes.map(quoteSql).join(',')
  const cycles = await queryRows<{
    cycle_id: string; fund_code: string; fund_name: string
    clear_return2: number | null; big_loss_rate: number | null; big_profit_rate: number | null
    txn_count: number
  }>(dbPath, `
    SELECT c.cycle_id, c.fund_code,
           COALESCE(i.fund_name, c.fund_code) AS fund_name,
           c.clear_return2, c.big_loss_rate, c.big_profit_rate,
           (SELECT COUNT(*) FROM real_user_txns t WHERE t.cycle_id = c.cycle_id) AS txn_count
    FROM real_user_cycles c
    LEFT JOIN fund_info i ON i.fund_code = c.fund_code
    WHERE c.fund_code IN (${inList})
      AND EXISTS (SELECT 1 FROM real_user_curves v WHERE v.cycle_id = c.cycle_id)
    ORDER BY c.fund_code ASC, c.clear_return2 ASC
  `)
  if (!cycles.length) return []

  const byFund = new Map<string, typeof cycles>()
  for (const c of cycles) {
    const arr = byFund.get(c.fund_code) ?? []
    arr.push(c)
    byFund.set(c.fund_code, arr)
  }
  const poolSizes = new Map<string, number>()
  for (const [code, arr] of byFund) poolSizes.set(code, arr.length)
  const quota = allocateQuota(poolSizes, MAX_REAL_USERS)

  const chosen: typeof cycles = []
  for (const [code, arr] of byFund) {
    chosen.push(...selectRepresentative(arr, quota.get(code) ?? 0))  // arr 已按 clear_return2 升序
  }
  if (!chosen.length) return []

  const curveRows = await queryRows<{ cycle_id: string; trade_date: string; net_value: number }>(dbPath, `
    SELECT cycle_id, trade_date, net_value
    FROM real_user_curves
    WHERE cycle_id IN (${chosen.map(c => quoteSql(c.cycle_id)).join(',')})
    ORDER BY cycle_id ASC, trade_date ASC
  `)
  const curveByCycle = new Map<string, { trade_date: string; net_value: number }[]>()
  for (const r of curveRows) {
    const arr = curveByCycle.get(r.cycle_id) ?? []
    arr.push({ trade_date: r.trade_date, net_value: num(r.net_value) })
    curveByCycle.set(r.cycle_id, arr)
  }

  const txnRows = await queryRows<{ cycle_id: string; busin_type: string; amount: number | null; txn_date: string }>(dbPath, `
    SELECT cycle_id, busin_type, amount, txn_date
    FROM real_user_txns
    WHERE cycle_id IN (${chosen.map(c => quoteSql(c.cycle_id)).join(',')})
  `)
  const txnsByCycle = mergeUserTxns(txnRows)

  return chosen.map(c => ({
    fundCode: c.fund_code,
    fundName: c.fund_name,
    cycleId: c.cycle_id,
    clearReturn2: num(c.clear_return2),
    bigLossRate: c.big_loss_rate,
    bigProfitRate: c.big_profit_rate,
    txnCount: num(c.txn_count),
    series: curveByCycle.get(c.cycle_id) ?? [],
    txns: txnsByCycle.get(c.cycle_id) ?? [],
  })).filter(u => u.series.length > 0)
}

/** 列出该 bot 在本 run 真正决策过的交易日：扫 runDir 下形如 YYYY-MM-DD 的日期目录，
 *  只保留「当天真发生过决策/对话」的日期——即存在 sent.md（被唤起决策）或 reply.json（决策回复）的日期。
 *  ⚠️ 不能只判目录存在：月度/周度回测里 bot 每个交易日都会醒来做系统侧结算(close_my_day.json)、
 *  留下空目录，但只在月初/周初真正决策。按目录存在算会把结算日也当成决策日 → 频率被误判成日度、
 *  日历逐日可选。反思面板的左右箭头/日历按这个列表翻页，月度回测时它天然是月度。
 *  best-effort：run 目录不存在（旧 run / 已清理）时返回空数组，前端回落到逐日 series。 */
function listReflectionDates(worldRoot: string, runId: string, botId: string): string[] {
  const root = runDir(worldRoot, runId)
  let entries: string[]
  try { entries = readdirSync(root) }
  catch { return [] }
  return entries
    .filter(name => /^\d{4}-\d{2}-\d{2}$/.test(name)
      && existsSync(botDayDir(worldRoot, runId, name, botId))
      && (existsSync(sentFile(worldRoot, runId, name, botId)) || existsSync(replyFile(worldRoot, runId, name, botId))))
    .sort()
}

async function loadBotForRun(dbPath: string, worldRoot: string, botId: string, runId: string, availableRuns: BotRunRef[]): Promise<BotDataset | null> {
  const runIdSql = quoteSql(runId)
  const botIdSql = quoteSql(botId)

  const daily = dedupeByDate(await queryRows<DailyRow>(dbPath, `
    SELECT trade_date, initial_capital, cash, invested_value, total_value, net_value,
           daily_return_pct, cumulative_return_pct, max_drawdown_pct,
           equity_weight, bond_weight, gold_weight, cash_weight
    FROM fund_bot_daily_snapshots
    WHERE bot_id = ${botIdSql} AND run_id = ${runIdSql}
    ORDER BY trade_date ASC
  `))
  const accounts = await queryRows<AccountRow>(dbPath, `
    SELECT initial_capital, cash
    FROM fund_bot_accounts
    WHERE bot_id = ${botIdSql} AND run_id = ${runIdSql}
    LIMIT 1
  `)
  const latest = daily[daily.length - 1] ?? null
  const first = daily[0] ?? null
  const latestDate = latest?.trade_date ?? ''
  const allHoldings = await queryRows<HoldingRow>(dbPath, `
    SELECT p.trade_date,
           p.fund_code,
           COALESCE(i.fund_name, p.fund_code) AS fund_name,
           COALESCE(i.theme, '') AS theme,
           p.asset_class, p.role, p.shares, p.nav, p.market_value, p.weight,
           p.daily_pnl, p.cumulative_return_pct, p.holding_days
    FROM fund_bot_position_snapshots p
    LEFT JOIN fund_info i ON i.fund_code = p.fund_code
    WHERE p.bot_id = ${botIdSql} AND p.run_id = ${runIdSql}
    ORDER BY p.trade_date ASC, p.market_value DESC, p.fund_code ASC
  `)
  // Legacy `holdings` = latestDate snapshot (consumed by existing front-end code).
  const holdings = allHoldings.filter(h => h.trade_date === latestDate)
  // New: per-day index — drop trade_date from row body since it becomes the index key.
  const holdingsByDate = buildHoldingsByDate(allHoldings)
  // LEFT JOIN fund_bot_orders to surface the bot's actual decision rationale
  // (orders.action_reason — written by the bot at place_buy/sell time). The
  // actions.reason column is just an auto-generated settle bookkeeping string.
  // bot_reason 用关联子查询而非 LEFT JOIN：当同一 action_date 有多条匹配 order
  // （例如 bot 误调用工具下了两笔），LEFT JOIN 会做笛卡尔积，把 N 条 action × M 条
  // order 放大成 N×M 行（一笔买入显示成四笔）。子查询保证每条 action 恰好一行，
  // 行数严格等于真实 action 数。
  const actionsRaw = await queryRows<ActionRow>(dbPath, `
    SELECT a.action_id, a.review_id, a.bot_id, a.fund_code,
           COALESCE(i.fund_name, a.fund_code) AS fund_name,
           a.action_type, a.final_decision, a.amount, a.shares, a.nav_used,
           a.reason,
           (SELECT o.action_reason FROM fund_bot_orders o
             WHERE o.bot_id = a.bot_id
               AND o.fund_code = a.fund_code
               AND o.order_date = a.action_date
               AND o.settle_run_id = a.run_id
               AND ( (a.action_type='ADD' AND o.order_type='buy')
                  OR (a.action_type='REDUCE' AND o.order_type='sell') )
             ORDER BY o.order_id ASC LIMIT 1) AS bot_reason,
           (SELECT o.fee FROM fund_bot_orders o
             WHERE o.bot_id = a.bot_id
               AND o.fund_code = a.fund_code
               AND o.order_date = a.action_date
               AND o.settle_run_id = a.run_id
               AND ( (a.action_type='ADD' AND o.order_type='buy')
                  OR (a.action_type='REDUCE' AND o.order_type='sell') )
             ORDER BY o.order_id ASC LIMIT 1) AS fee,
           a.action_date
    FROM fund_bot_actions a
    LEFT JOIN fund_info i ON i.fund_code = a.fund_code
    WHERE a.bot_id = ${botIdSql} AND a.run_id = ${runIdSql}
    ORDER BY a.action_date ASC, a.action_id ASC
  `)
  // 手续费合计：按 (bot, run) 聚合 fund_bot_orders.fee。归属优先用 order_run_id（下单
  // 所在 run）；老数据 order_run_id 为空时回退 settle_run_id，避免漏统计。
  const feeRows = await queryRows<{ order_type: string; fee_sum: number | null }>(dbPath, `
    SELECT order_type, SUM(COALESCE(fee, 0)) AS fee_sum
    FROM fund_bot_orders
    WHERE bot_id = ${botIdSql}
      AND (order_run_id = ${runIdSql}
        OR ((order_run_id IS NULL OR order_run_id = '') AND settle_run_id = ${runIdSql}))
    GROUP BY order_type
  `)
  const buyFee = num(feeRows.find(r => r.order_type === 'buy')?.fee_sum)
  const sellFee = num(feeRows.find(r => r.order_type === 'sell')?.fee_sum)
  const reviews = await queryRows<ReviewRow>(dbPath, `
    SELECT review_id, review_date, regime, decision, reason, turnover_ratio
    FROM fund_bot_reviews
    WHERE bot_id = ${botIdSql} AND run_id = ${runIdSql}
    ORDER BY review_date ASC, review_id ASC
  `)

  // We accept the run if discovery surfaced it (it appeared in listRunsForBot), even
  // when the snapshot tables are empty (e.g. older runs whose raw snapshots were swept
  // but fund_bot_performance / fund_bot_orders still remember them). The bot card will
  // simply render empty chart / actions / holdings — better than hiding the run.

  const actions: BotAction[] = actionsRaw.map(action => ({
    ...action,
    side: classifyAction(action.action_type, action.final_decision),
    ...computeActionWeights(holdingsByDate, action.action_date, action.fund_code),
  }))
  const firstDateForBench = first?.trade_date || actions.find(a => a.side === 'buy')?.action_date || ''
  const lastDateForBench = latest?.trade_date || actions[actions.length - 1]?.action_date || ''
  const benchmark = lastDateForBench
    ? await loadBenchmark(dbPath, botId, actions, holdings, firstDateForBench, lastDateForBench)
    : null

  const touchedFunds = [...new Set([
    ...allHoldings.map(h => h.fund_code),
    ...actions.map(a => a.fund_code),
  ])].filter(Boolean)
  const realUsers = await loadRealUsers(dbPath, touchedFunds)

  return {
    botId,
    firstTradeDate: first?.trade_date ?? (actions[0]?.action_date ?? ''),
    latestTradeDate: latest?.trade_date ?? (actions[actions.length - 1]?.action_date ?? ''),
    days: daily.length,
    initialCapital: num(latest?.initial_capital ?? accounts[0]?.initial_capital),
    cash: num(latest?.cash ?? accounts[0]?.cash),
    totalValue: num(latest?.total_value),
    netValue: num(latest?.net_value) || 1,
    cumulativeReturnPct: num(latest?.cumulative_return_pct),
    dailyReturnPct: num(latest?.daily_return_pct),
    maxDrawdownPct: num(latest?.max_drawdown_pct),
    actionCount: actions.length,
    buyCount: actions.filter(a => a.side === 'buy').length,
    sellCount: actions.filter(a => a.side === 'sell').length,
    holdCount: actions.filter(a => a.side === 'hold').length,
    holdingsCount: holdings.length,
    totalFee: buyFee + sellFee,
    buyFee,
    sellFee,
    equityWeight: latest?.equity_weight ?? null,
    bondWeight: latest?.bond_weight ?? null,
    goldWeight: latest?.gold_weight ?? null,
    cashWeight: latest?.cash_weight ?? null,
    series: daily.map(row => ({
      trade_date: row.trade_date,
      net_value: num(row.net_value) || 1,
      total_value: num(row.total_value),
      cash: num(row.cash),
      invested_value: num(row.invested_value),
      daily_return_pct: row.daily_return_pct,
      cumulative_return_pct: row.cumulative_return_pct,
      max_drawdown_pct: row.max_drawdown_pct,
      equity_weight: row.equity_weight,
      bond_weight: row.bond_weight,
      gold_weight: row.gold_weight,
      cash_weight: row.cash_weight,
    })),
    reflectionDates: listReflectionDates(worldRoot, runId, botId),
    actions,
    holdings,
    holdingsByDate,
    reviews,
    benchmark,
    realUsers,
    runId,
    availableRuns,
    model: readRunModel(worldRoot, runId, botId),
  }
}

async function loadDataset(dbPath: string, worldRoot: string): Promise<Dataset> {
  const botIds = await listAllBotIds(dbPath)
  const bots: BotDatasetSummary[] = []
  for (const botId of botIds) {
    const runs = await listRunsForBot(dbPath, worldRoot, botId)
    if (!runs.length) continue
    const bot = await loadBotForRun(dbPath, worldRoot, botId, runs[0].runId, runs)
    if (bot) {
      // /api/backtest/data is polled every 10s by the frontend, which reads
      // holdingsByDate only on the per-bot detail view (/api/backtest/bot).
      // Strip the per-day history here to keep poll payload small.
      const { holdingsByDate: _drop, realUsers: _dropUsers, ...summary } = bot
      bots.push(summary)
    }
  }
  bots.sort((a, b) => b.cumulativeReturnPct - a.cumulativeReturnPct || b.totalValue - a.totalValue || a.botId.localeCompare(b.botId))
  return {
    generatedAt: new Date().toISOString(),
    dbPath,
    summary: {
      botCount: bots.length,
      latestTradeDate: bots.reduce((m, b) => (b.latestTradeDate > m ? b.latestTradeDate : m), ''),
      totalValue: bots.reduce((sum, b) => sum + b.totalValue, 0),
      averageReturnPct: bots.length ? bots.reduce((sum, b) => sum + b.cumulativeReturnPct, 0) / bots.length : 0,
      bestBotId: bots[0]?.botId ?? '',
      bestReturnPct: bots[0]?.cumulativeReturnPct ?? 0,
      worstBotId: bots[bots.length - 1]?.botId ?? '',
      worstReturnPct: bots[bots.length - 1]?.cumulativeReturnPct ?? 0,
      totalFee: bots.reduce((sum, b) => sum + b.totalFee, 0),
    },
    bots,
  }
}

/** /api/backtest/all-runs：所有历史 run 的轻量汇总（runs.html 的 ingestHistory 用它补全 CSV
 *  没有的 run）。只取列表视图字段，不拉持仓/操作明细。口径与 bot 卡片一致：
 *  absReturnPct = 末日 cumulative_return_pct，maxDrawdownPct = 末日 max_drawdown_pct，
 *  annReturnPct = 按交易日数做 252 年化。index/indexName 取自该 run 的 strategy-assignments.json，
 *  fund 取末日市值最大的持仓基金（拿不到则空，前端回退按 index 归类）。 */
async function loadAllRunsSummary(dbPath: string, worldRoot: string): Promise<{ runs: Array<Record<string, unknown>> }> {
  const botIds = await listAllBotIds(dbPath)
  const runs: Array<Record<string, unknown>> = []
  for (const botId of botIds) {
    const b = quoteSql(botId)
    const refs = await listRunsForBot(dbPath, worldRoot, botId)
    for (const ref of refs) {
      const r = quoteSql(ref.runId)
      // 末日累计收益 / 区间最大回撤 / 交易日数：一次查询取齐。
      const agg = queryRows<{ days: number; cum: number | null; mdd: number | null }>(dbPath, `
        SELECT (SELECT COUNT(DISTINCT trade_date) FROM fund_bot_daily_snapshots
                 WHERE bot_id = ${b} AND run_id = ${r}) AS days,
               (SELECT cumulative_return_pct FROM fund_bot_daily_snapshots
                 WHERE bot_id = ${b} AND run_id = ${r} ORDER BY trade_date DESC LIMIT 1) AS cum,
               (SELECT max_drawdown_pct FROM fund_bot_daily_snapshots
                 WHERE bot_id = ${b} AND run_id = ${r} ORDER BY trade_date DESC LIMIT 1) AS mdd
      `)[0]
      const days = num(agg?.days)
      const absReturnPct = agg?.cum == null ? null : num(agg.cum)
      const maxDrawdownPct = agg?.mdd == null ? null : num(agg.mdd)
      // 252 年化：(1 + cum/100)^(252/days) - 1。base<=0（亏损≥100%）或无收益/无天数 → null。
      const base = absReturnPct == null ? null : 1 + absReturnPct / 100
      const annReturnPct = base != null && base > 0 && days > 0
        ? (Math.pow(base, 252 / days) - 1) * 100
        : null
      // 主力持仓基金：末日市值最大的一只。
      const top = queryRows<{ fund_code: string }>(dbPath, `
        SELECT fund_code FROM fund_bot_position_snapshots
        WHERE bot_id = ${b} AND run_id = ${r}
          AND trade_date = (SELECT MAX(trade_date) FROM fund_bot_position_snapshots
                             WHERE bot_id = ${b} AND run_id = ${r})
        ORDER BY market_value DESC, fund_code ASC LIMIT 1
      `)[0]
      const strat = readRunStrategies(worldRoot, ref.runId)?.[botId]
      runs.push({
        runId: ref.runId,
        botId,
        index: strat?.targetIndex ?? '',
        indexName: strat?.title ?? '',
        fund: top?.fund_code ?? '',
        absReturnPct,
        annReturnPct,
        maxDrawdownPct,
      })
    }
  }
  return { runs }
}

// fund_bot_run_eval 列 -> runs.html 内嵌 CSV 的中文列名。前端按中文键消费，故接口按此映射回吐，
// 把「解析内嵌 CSV」换成「fetch 本接口」即可，下游分组/默认判定逻辑一行不用动。
const RUN_EVAL_COL_MAP: Array<[string, string]> = [
  ['launch_time', '启动时间'], ['run_id', 'run_id'], ['bot', 'bot'],
  ['strategy', '策略'], ['target_index', '对标指数'], ['buyable_fund', '可买基金'],
  ['status', '状态'], ['window_start', '窗口起'], ['window_end', '窗口止'],
  ['months', '回测月数'], ['progress', '进度'],
  ['abs_return_pct', '绝对收益%'], ['ann_return_pct', '年化收益%'], ['max_drawdown_pct', '最大回撤%'],
  ['passive_full_pct', '被动满仓%'], ['passive_ann_pct', '年化被动%'], ['excess_timing_pct', '超额_择时%'],
  ['up_capture_pct', '上行捕获%'], ['down_protect_pct', '下行保护%'],
  ['ops_buy', '操作_买'], ['ops_sell', '操作_卖'], ['ops_total', '操作合计'],
  ['end_cash_pct', '末日现金%'],
  ['evaluation', '评价'], ['retail_rating', '普通投资者评级'], ['review', '综合点评'], ['note', '备注'],
]

/** /api/backtest/run-evals：人工评测表（原 runs.html 内嵌 CSV，已迁入 fund_bot_run_eval）。
 *  每行用 runs.html 的中文列名做 key，数值 NULL → ''，与旧 CSV 解析口径完全一致。
 *  表不存在 → 返回空数组（前端降级为只剩 all-runs 历史 run）。 */
async function loadRunEvals(dbPath: string): Promise<{ rows: Array<Record<string, string>> }> {
  if (!(await tableExists(dbPath, 'fund_bot_run_eval'))) return { rows: [] }
  const dbCols = RUN_EVAL_COL_MAP.map(([db]) => db)
  const raw = queryRows<Record<string, unknown>>(dbPath,
    `SELECT ${dbCols.join(', ')} FROM fund_bot_run_eval ORDER BY launch_time, run_id, bot`)
  const rows = raw.map((r) => {
    const o: Record<string, string> = {}
    for (const [db, zh] of RUN_EVAL_COL_MAP) {
      const v = r[db]
      o[zh] = v == null ? '' : String(v)
    }
    return o
  })
  return { rows }
}

// 侧栏「末日决策」标识数据：每个 (run, bot) 在其最后一个交易日当天有没有买卖动作，
// 及方向（加仓 add / 减仓 reduce / 清仓 clear）。末日在持有观望的 (run,bot) 不返回。
// 末日 D = fund_bot_daily_snapshots 里该 (run,bot) 的 MAX(trade_date)；只取 action_date=D
// 的动作。清仓靠 D 当天快照「现金占比≥99.5%」判定：equity/bond/gold_weight 三列多数 run
// 未落库(全为0)不可用，故改用 cash_weight（为空时退化 cash/total_value）。实测清仓 run
// 末日 cash_weight=1.0 同日已反映；减仓 run 现金占比 0.2~0.67 仍持仓，据此与清仓区分。
async function loadLatestDecisions(dbPath: string): Promise<{ decisions: Record<string, 'add' | 'reduce' | 'clear'> }> {
  if (!(await tableExists(dbPath, 'fund_bot_daily_snapshots')) || !(await tableExists(dbPath, 'fund_bot_actions'))) {
    return { decisions: {} }
  }
  const rows = queryRows<{
    run_id: string; bot_id: string;
    cash_weight: number | null; cash: number | null; total_value: number | null;
    n_add: number; n_reduce: number; add_amt: number | null; reduce_amt: number | null;
  }>(dbPath, `
    WITH last_day AS (
      SELECT run_id, bot_id, MAX(trade_date) AS d
      FROM fund_bot_daily_snapshots GROUP BY run_id, bot_id
    ),
    last_acts AS (
      SELECT a.run_id, a.bot_id,
             SUM(CASE WHEN a.action_type = 'ADD'    THEN 1 ELSE 0 END) AS n_add,
             SUM(CASE WHEN a.action_type = 'REDUCE' THEN 1 ELSE 0 END) AS n_reduce,
             SUM(CASE WHEN a.action_type = 'ADD'    THEN COALESCE(a.amount, 0) ELSE 0 END) AS add_amt,
             SUM(CASE WHEN a.action_type = 'REDUCE' THEN COALESCE(a.amount, 0) ELSE 0 END) AS reduce_amt
      FROM fund_bot_actions a
      JOIN last_day l ON l.run_id = a.run_id AND l.bot_id = a.bot_id AND a.action_date = l.d
      GROUP BY a.run_id, a.bot_id
    )
    SELECT la.run_id, la.bot_id,
           s.cash_weight, s.cash, s.total_value,
           la.n_add, la.n_reduce, la.add_amt, la.reduce_amt
    FROM last_acts la
    JOIN last_day l ON l.run_id = la.run_id AND l.bot_id = la.bot_id
    JOIN fund_bot_daily_snapshots s
      ON s.run_id = la.run_id AND s.bot_id = la.bot_id AND s.trade_date = l.d
  `)
  const decisions: Record<string, 'add' | 'reduce' | 'clear'> = {}
  for (const r of rows) {
    const hasAdd = num(r.n_add) > 0, hasReduce = num(r.n_reduce) > 0
    if (!hasAdd && !hasReduce) continue
    // 末日几乎全现金 → 清仓；否则纯减仓算减仓。cash_weight 优先，缺失退化 cash/total_value。
    const cashRatio = r.cash_weight != null ? num(r.cash_weight)
      : (num(r.total_value) > 0 ? num(r.cash) / num(r.total_value) : 0)
    let kind: 'add' | 'reduce' | 'clear'
    if (hasReduce && !hasAdd && cashRatio >= 0.995) kind = 'clear'
    else if (num(r.add_amt) - num(r.reduce_amt) >= 0 && hasAdd) kind = 'add'
    else kind = 'reduce'
    decisions[`${r.run_id}|${r.bot_id}`] = kind
  }
  return { decisions }
}

interface MarketReportRow {
  id: number
  report_type: string
  as_of_date: string
  scope: string
  content_md: string
  structured_json: string | null
  agent_run_id: string | null
  generated_at: string | null
}

interface MarketReportDateRow {
  as_of_date: string
  report_count: number
  generated_at: string | null
}

const MARKET_REPORT_TYPES = ["market_context", "market_mainline", "mainline_rotation"] as const
const OOS_BOTS = [
  { botId: 'bot101', runId: 'oos-bot101-daily' },
  { botId: 'bot102', runId: 'oos-bot102-daily' },
  { botId: 'bot103', runId: 'oos-bot103-daily' },
] as const
const OOS_BOT_IDS = new Set<string>(OOS_BOTS.map(b => b.botId))
const OOS_HISTORY_START = '2026-04-01'
const OOS_BACKTEST_HISTORY_RUNS: Record<string, string> = {
  bot101: 'dash-2026-06-23T08-47-44',
  bot102: 'dash-2026-06-23T08-48-10',
  bot103: 'dash-2026-06-15T06-30-57',
}

function defaultOosRunId(botId: string): string {
  return OOS_BOTS.find(b => b.botId === botId)?.runId ?? `oos-${botId}-daily`
}

function oosBotDateWhereSql(): string {
  return OOS_BOTS.map(b => `(live_run_id = ${quoteSql(b.runId)} AND bot_id = ${quoteSql(b.botId)})`).join(' OR ')
}

function finiteNumber(v: unknown): number | null {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/** 把「历史净值序列 + 实盘净值序列」几何拼接成一条连续曲线（纯函数，不碰 DB）。
 *  历史段：首点净值归一到 1.0；实盘段：整体 ×(历史末点/实盘首点)，从历史末点续上。
 *  liveSegment 默认 'live'；OOS 传 'daily_oos' 以保持 market-reports.html 的着色。 */
export function stitchSeriesRows(
  historyRows: Array<Record<string, unknown>>,
  liveRows: Array<Record<string, unknown>>,
  opts: { liveSegment?: string } = {},
): Array<Record<string, unknown>> {
  const liveSegment = opts.liveSegment ?? 'live'
  const extended: Array<Record<string, unknown>> = []
  const firstHistoryNav = finiteNumber(historyRows[0]?.net_value)
  if (firstHistoryNav != null && firstHistoryNav > 0) {
    for (const row of historyRows) {
      const rawNav = finiteNumber(row.net_value)
      if (rawNav == null) continue
      const nav = rawNav / firstHistoryNav
      extended.push({ ...row, net_value: nav, cumulative_return_pct: (nav - 1) * 100, segment: 'backtest', raw_net_value: rawNav })
    }
  }
  const filteredLive = liveRows.filter(r => typeof r.trade_date === 'string' && finiteNumber(r.net_value) != null)
  const firstLiveNav = finiteNumber(filteredLive[0]?.net_value)
  const lastHistoryNav = finiteNumber(extended[extended.length - 1]?.net_value)
  const liveScale = firstLiveNav != null && firstLiveNav > 0 && lastHistoryNav != null && lastHistoryNav > 0
    ? lastHistoryNav / firstLiveNav : 1
  for (const row of filteredLive) {
    const rawNav = finiteNumber(row.net_value)
    if (rawNav == null) continue
    const nav = rawNav * liveScale
    extended.push({ ...row, net_value: nav, cumulative_return_pct: (nav - 1) * 100, segment: liveSegment, raw_net_value: rawNav })
  }
  return extended
}

/** 拼接后完整序列上的业绩指标：绝对收益（末点-1）、最大回撤（跑动峰值法）、252 年化。
 *  与 loadAllRunsSummary 同口径：base=1+abs/100，base>0 且 days>0 才算年化，否则 null。 */
export function computeStitchedMetrics(extended: Array<Record<string, unknown>>): {
  absReturnPct: number | null; annReturnPct: number | null; maxDrawdownPct: number | null; days: number
} {
  const navs: number[] = []
  for (const r of extended) { const n = finiteNumber(r.net_value); if (n != null) navs.push(n) }
  const days = navs.length
  if (!days) return { absReturnPct: null, annReturnPct: null, maxDrawdownPct: null, days: 0 }
  const absReturnPct = (navs[days - 1] - 1) * 100
  let peak = -Infinity, mdd = 0
  for (const n of navs) { if (n > peak) peak = n; const dd = n / peak - 1; if (dd < mdd) mdd = dd }
  const maxDrawdownPct = mdd * 100
  const base = 1 + absReturnPct / 100
  const annReturnPct = base > 0 && days > 0 ? (Math.pow(base, 252 / days) - 1) * 100 : null
  return { absReturnPct, annReturnPct, maxDrawdownPct, days }
}

function parseStructuredJson(raw: string | null): unknown {
  if (!raw || !raw.trim()) return null
  try { return JSON.parse(raw) }
  catch { return null }
}

function loadMarketReports(dbPath: string, requestedDate = ""): Record<string, unknown> {
  // 日期列表 = 「有日报的日子」∪ OOS bot101/102/103 有快照的交易日。关键：OOS 净值/持仓/订单是
  // 确定性的 bot 账户数据（不依赖 LLM），而 market_reports 是 LLM prepass 产物、可能某天
  // 生成失败缺失。若只用 market_reports 取日期，缺报告的那天就算 bot 账户数据齐全也不会
  // 出现在列表里 → OOS 区块被拖在上一个有报告的日子。合并后缺报告日仍可选中，报告卡走
  // 前端 empty 兜底，net_value 照常显示。report_count=0 的日子就是"有账户、无报告"。
  //
  // 但「有报告」只数 3 份日报（MARKET_REPORT_TYPES）——macro_news 是周/月度、且会**提前**
  // 为未来日生成（如今天 06-26 当日报/账户都还没跑时，库里已有 06-26 的 macro_news）。若把
  // macro_news 也算进"有报告"，这种「只有 macro_news、日报与账户都没跑」的日子就会冒成一个
  // 数据没跑全的尾日。故此处按日报类型过滤把它挡掉；macro_news 仍会在已入列的选中日通过
  // 下方"≤选中日取最近一期"的回填正常显示。满日计数也从 4 回到干净的 3（前端 "/3"）。
  const dailyTypesSql = MARKET_REPORT_TYPES.map(t => quoteSql(t)).join(", ")
  const oosWhere = oosBotDateWhereSql()
  const dates = queryRows<MarketReportDateRow>(dbPath,
    "SELECT as_of_date, SUM(report_count) AS report_count, MAX(generated_at) AS generated_at FROM (" +
    "  SELECT as_of_date, COUNT(*) AS report_count, MAX(generated_at) AS generated_at" +
    "    FROM market_reports WHERE scope = " + quoteSql("global") + " AND report_type IN (" + dailyTypesSql + ") GROUP BY as_of_date" +
    "  UNION ALL" +
    "  SELECT trade_date AS as_of_date, 0 AS report_count, NULL AS generated_at" +
    "    FROM oos_bot_daily_snapshots WHERE (" + oosWhere + ") AND trade_date <= (SELECT MAX(nav_date) FROM fund_nav) GROUP BY trade_date" +
    ") GROUP BY as_of_date ORDER BY as_of_date DESC")
  const validDate = /^\d{4}-\d{2}-\d{2}$/.test(requestedDate) ? requestedDate : ""
  const selectedDate = validDate || dates[0]?.as_of_date || ""
  const rows = selectedDate ? queryRows<MarketReportRow>(dbPath, "SELECT id, report_type, as_of_date, scope, content_md, structured_json, agent_run_id, generated_at FROM market_reports WHERE scope = " + quoteSql("global") + " AND as_of_date = " + quoteSql(selectedDate) + " ORDER BY report_type ASC") : []
  const reports: Record<string, unknown> = {}
  for (const type of MARKET_REPORT_TYPES) reports[type] = null
  for (const row of rows) reports[row.report_type] = { ...row, structured: parseStructuredJson(row.structured_json) }
  // macro_news（res3 资讯研究室）是周度/月度、非每日 → 选中日若无，取 ≤选中日 的最近一期（PIT，与回测 bot get_market_report 看到的一致）
  if (reports["macro_news"] == null && selectedDate) {
    const newsRows = queryRows<MarketReportRow>(dbPath, "SELECT id, report_type, as_of_date, scope, content_md, structured_json, agent_run_id, generated_at FROM market_reports WHERE scope = " + quoteSql("global") + " AND report_type = " + quoteSql("macro_news") + " AND as_of_date <= " + quoteSql(selectedDate) + " ORDER BY as_of_date DESC LIMIT 1")
    if (newsRows[0]) reports["macro_news"] = { ...newsRows[0], structured: parseStructuredJson(newsRows[0].structured_json) }
  }
  return { selectedDate, dates, reportTypes: [...MARKET_REPORT_TYPES, "macro_news"], reports }
}

/** 读某 report_type 在 as-of 当日及之前的最近一期（PIT，与 strategy-server.get_market_report 同口径）。
 *  注入 bot101 对话引擎，让 chat 里的 bot 能现场查「当前/历史」市场主线等研报。 */
function lookupMarketReportAsOf(dbPath: string, reportType: string, asOf: string): { as_of_date: string; content_md: string } | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(asOf)) return null
  const rows = queryRows<{ as_of_date: string; content_md: string }>(dbPath,
    "SELECT as_of_date, content_md FROM market_reports WHERE report_type = " + quoteSql(reportType) +
    " AND scope = 'global' AND as_of_date <= " + quoteSql(asOf) + " ORDER BY as_of_date DESC LIMIT 1")
  return rows[0] ?? null
}

/** 解析 bot101 对话的数据时点：把请求日夹到「≤ 请求日、且确有完整每日决策的最近一个交易日」。
 *  decisionDates 须按日期降序（= loadMarketReports().dates 的顺序）。
 *  - reqDate 空 → 取最新决策日（dates[0]）；agents.html 固定发浏览器当天，而当日日报/账户常还没生成，
 *    会被夹到上一交易日 → 会话一开就注入最新一份完整决策，而非当天那份空壳（市场主线缺失）。
 *  - reqDate 早于所有决策日 → 原样返回（让上层取空，不前跳造成穿越）。 */
export function resolveChatAsOf(decisionDates: string[], reqDate: string): string {
  if (!reqDate) return decisionDates[0] ?? ''
  if (decisionDates.includes(reqDate)) return reqDate
  return decisionDates.find(d => d <= reqDate) ?? reqDate
}


function loadOosExtendedSeries(dbPath: string, botId: string, runId: string, liveSeries: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  const historyRunId = OOS_BACKTEST_HISTORY_RUNS[botId]
  const liveRows = liveSeries.filter(r => typeof r.trade_date === 'string' && finiteNumber(r.net_value) != null)
  const firstLiveDate = typeof liveRows[0]?.trade_date === 'string' ? String(liveRows[0].trade_date) : ''
  const historyRows = historyRunId ? queryRows<Record<string, unknown>>(dbPath,
    'SELECT trade_date, total_value, net_value, daily_return_pct, cumulative_return_pct, ' +
    'max_drawdown_pct, equity_weight, bond_weight, gold_weight, cash_weight ' +
    'FROM fund_bot_daily_snapshots WHERE bot_id = ' + quoteSql(botId) +
    ' AND run_id = ' + quoteSql(historyRunId) +
    ' AND trade_date >= ' + quoteSql(OOS_HISTORY_START) +
    (firstLiveDate ? ' AND trade_date < ' + quoteSql(firstLiveDate) : '') +
    ' ORDER BY trade_date ASC') : []

  const extended = stitchSeriesRows(historyRows, liveRows, { liveSegment: 'daily_oos' })
  if (!extended.length) {
    return liveRows.map(row => ({ ...row, segment: 'daily_oos', source_run_id: runId, raw_net_value: row.net_value }))
  }
  // 保持原字段：历史段挂 historyRunId、实盘段挂当前 runId。
  return extended.map(r => ({ ...r, source_run_id: r.segment === 'backtest' ? historyRunId : runId }))
}


function loadOosBot(dbPath: string, botId = 'bot101', runId = defaultOosRunId(botId), requestedDate = ''): Record<string, unknown> {
  const runIdSql = quoteSql(runId)
  const botIdSql = quoteSql(botId)
  const dateRe = /^\d{4}-\d{2}-\d{2}$/
  const navMaxDate = latestFundNavDate(dbPath)
  const navMaxDateSql = navMaxDate ? quoteSql(navMaxDate) : quoteSql('0000-00-00')
  const dates = queryRows<{ trade_date: string }>(dbPath,
    'SELECT trade_date FROM oos_bot_daily_snapshots WHERE live_run_id = ' + runIdSql +
    ' AND bot_id = ' + botIdSql + ' ORDER BY trade_date DESC').map(r => r.trade_date)
  const validDate = dateRe.test(requestedDate) ? requestedDate : ''
  const selectedDate = validDate || dates[0] || ''
  const dateSql = quoteSql(selectedDate)
  const accountDateCap = selectedDate && navMaxDate ? (selectedDate <= navMaxDate ? selectedDate : navMaxDate) : (selectedDate || navMaxDate)
  const accountDate = accountDateCap ? queryRows<{ trade_date: string }>(dbPath,
    'SELECT trade_date FROM oos_bot_daily_snapshots WHERE live_run_id = ' + runIdSql +
    ' AND bot_id = ' + botIdSql +
    ' AND trade_date <= ' + quoteSql(accountDateCap) +
    ' ORDER BY trade_date DESC LIMIT 1')[0]?.trade_date ?? '' : ''
  const accountDateSql = quoteSql(accountDate)
  const series = queryRows<Record<string, unknown>>(dbPath,
    'SELECT trade_date, total_value, net_value, daily_return_pct, cumulative_return_pct, ' +
    'max_drawdown_pct, equity_weight, bond_weight, gold_weight, cash_weight ' +
    'FROM oos_bot_daily_snapshots WHERE live_run_id = ' + runIdSql +
    ' AND bot_id = ' + botIdSql +
    ' AND trade_date <= ' + navMaxDateSql +
    ' ORDER BY trade_date ASC')
  const snapshot = accountDate ? queryRows<Record<string, unknown>>(dbPath,
    'SELECT * FROM oos_bot_daily_snapshots WHERE live_run_id = ' + runIdSql +
    ' AND bot_id = ' + botIdSql + ' AND trade_date = ' + accountDateSql + ' LIMIT 1')[0] ?? null : null
  const positions = accountDate ? queryRows<Record<string, unknown>>(dbPath,
    'SELECT p.*, COALESCE(i.fund_name, p.fund_code) AS fund_name, COALESCE(i.theme, \'\') AS theme ' +
    'FROM oos_bot_position_snapshots p LEFT JOIN fund_info i ON i.fund_code = p.fund_code ' +
    'WHERE p.live_run_id = ' + runIdSql + ' AND p.bot_id = ' + botIdSql +
    ' AND p.trade_date = ' + accountDateSql + ' ORDER BY p.weight DESC, p.fund_code ASC') : []
  const orders = selectedDate ? queryRows<Record<string, unknown>>(dbPath,
    'SELECT o.*, COALESCE(i.fund_name, o.fund_code) AS fund_name, COALESCE(i.theme, \'\') AS theme ' +
    'FROM oos_bot_orders o LEFT JOIN fund_info i ON i.fund_code = o.fund_code ' +
    'WHERE o.live_run_id = ' + runIdSql + ' AND o.bot_id = ' + botIdSql +
    ' AND o.order_date = ' + dateSql + ' ORDER BY o.source_order_id ASC') : []
  const actions = selectedDate ? queryRows<Record<string, unknown>>(dbPath,
    'SELECT a.*, COALESCE(i.fund_name, a.fund_code) AS fund_name, COALESCE(i.theme, \'\') AS theme ' +
    'FROM oos_bot_actions a LEFT JOIN fund_info i ON i.fund_code = a.fund_code ' +
    'WHERE a.live_run_id = ' + runIdSql + ' AND a.bot_id = ' + botIdSql +
    ' AND a.action_date = ' + dateSql + ' ORDER BY a.source_action_id ASC') : []
  const reports = selectedDate ? queryRows<Record<string, unknown>>(dbPath,
    'SELECT report_type, as_of_date, generated_at, chars FROM oos_market_report_status ' +
    'WHERE live_run_id = ' + runIdSql + ' AND as_of_date = ' + dateSql + ' ORDER BY report_type ASC') : []
  // 全历史买卖动作（买卖点 marker 用，非选中日；按日期升序）
  const actionsAll = queryRows<Record<string, unknown>>(dbPath,
    'SELECT a.action_date, a.action_type, a.amount, a.fund_code, COALESCE(i.fund_name, a.fund_code) AS fund_name ' +
    'FROM oos_bot_actions a LEFT JOIN fund_info i ON i.fund_code = a.fund_code ' +
    'WHERE a.live_run_id = ' + runIdSql + ' AND a.bot_id = ' + botIdSql +
    ' ORDER BY a.action_date ASC, a.source_action_id ASC')
  // 全历史逐日持仓权重（总仓位堆叠子图用）→ { 日期: [{fund_code, fund_name, weight}] }
  const posRows = queryRows<Record<string, unknown>>(dbPath,
    'SELECT p.trade_date, p.fund_code, p.weight, COALESCE(i.fund_name, p.fund_code) AS fund_name ' +
    'FROM oos_bot_position_snapshots p LEFT JOIN fund_info i ON i.fund_code = p.fund_code ' +
    'WHERE p.live_run_id = ' + runIdSql + ' AND p.bot_id = ' + botIdSql +
    ' AND p.trade_date <= ' + navMaxDateSql +
    ' ORDER BY p.trade_date ASC, p.weight DESC')
  const holdingsByDate: Record<string, Array<Record<string, unknown>>> = {}
  for (const r of posRows) {
    const d = String(r.trade_date)
    if (!holdingsByDate[d]) holdingsByDate[d] = []
    holdingsByDate[d].push({ fund_code: r.fund_code, fund_name: r.fund_name, weight: r.weight })
  }
  const extendedSeries = loadOosExtendedSeries(dbPath, botId, runId, series)
  return { runId, botId, selectedDate, accountDate, dates, navMaxDate, series, extendedSeries, historyStart: OOS_HISTORY_START, snapshot, positions, orders, actions, reports, actionsAll, holdingsByDate }
}

/** 把当前选中日的「三份研报 + macro_news + bot101 账户/持仓」压成一段紧凑 markdown，
 *  注进 bot101 对话引擎的 system prompt，让它一上来就知道用户正盯着哪天、自己的账户长啥样。
 *  研报正文按每份截断，控 token；真正要细节时 bot 自己再调工具拿。 */
function formatBot101ChatContext(reportsPayload: Record<string, unknown>, oos: Record<string, unknown>): string {
  const out: string[] = []
  const date = String(reportsPayload.selectedDate ?? oos.selectedDate ?? '')
  out.push(`选中日期：${date || '（无）'}`)

  const reports = isPlainObject(reportsPayload.reports) ? reportsPayload.reports : {}
  const REPORT_LABELS: Array<[string, string]> = [
    ['market_context', '市场行情判断'], ['market_mainline', '市场主线'],
    ['mainline_rotation', '主线 Rotation'], ['macro_news', '宏观资讯要点'],
  ]
  out.push('', '## 当日研报')
  for (const [type, label] of REPORT_LABELS) {
    const r = isPlainObject(reports[type]) ? reports[type] as Record<string, unknown> : null
    if (!r) { out.push(`### ${label}：缺失`); continue }
    const md = typeof r.content_md === 'string' ? r.content_md.trim() : ''
    const asOf = typeof r.as_of_date === 'string' ? r.as_of_date : ''
    out.push(`### ${label}${asOf && asOf !== date ? `（最近一期 ${asOf}）` : ''}`)
    out.push(md ? (md.length > 1200 ? md.slice(0, 1200) + ' …（略）' : md) : '（无正文）')
  }

  const snap = isPlainObject(oos.snapshot) ? oos.snapshot as Record<string, unknown> : null
  out.push('', '## bot101 当日账户（固定 OOS run）')
  if (snap) {
    const pct = (v: unknown) => v == null ? '--' : Number(v).toFixed(2) + '%'
    const w = (v: unknown) => v == null ? '--' : (Number(v) * 100).toFixed(1) + '%'
    out.push(`- 净值 ${snap.net_value ?? '--'} ｜ 总资产 ¥${snap.total_value ?? '--'} ｜ 累计收益 ${pct(snap.cumulative_return_pct)} ｜ 最大回撤 ${pct(snap.max_drawdown_pct)}`)
    out.push(`- 权重：股 ${w(snap.equity_weight)} / 债 ${w(snap.bond_weight)} / 金 ${w(snap.gold_weight)} / 现金 ${w(snap.cash_weight)}`)
  } else {
    out.push('（该日无账户快照）')
  }
  const positions = Array.isArray(oos.positions) ? oos.positions as Array<Record<string, unknown>> : []
  if (positions.length) {
    out.push('当日持仓：')
    for (const p of positions.slice(0, 20)) {
      const wt = p.weight == null ? '--' : (Number(p.weight) * 100).toFixed(2) + '%'
      out.push(`- ${p.fund_name ?? p.fund_code}（${p.fund_code}${p.theme ? ' · ' + p.theme : ''}）权重 ${wt}，持有 ${p.holding_days ?? 0} 天`)
    }
  } else {
    out.push('当日无持仓（空仓）。')
  }
  // 当日决策的实际动作（买/卖）——「每日决策结果」最直接的一面，让 bot 一上来就知道自己当天做了什么。
  const actions = Array.isArray(oos.actions) ? oos.actions as Array<Record<string, unknown>> : []
  if (actions.length) {
    out.push('当日决策动作（买/卖）：')
    for (const a of actions.slice(0, 20)) {
      const amt = a.amount == null || a.amount === '' ? '' : `，¥${a.amount}`
      out.push(`- ${a.action_type ?? a.final_decision ?? '动作'} ${a.fund_name ?? a.fund_code ?? ''}${amt}`)
    }
  } else {
    out.push('当日无买卖动作（维持持仓）。')
  }
  return out.join('\n')
}

function isPlainObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x)
}
function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
  res.end(JSON.stringify(body))
}

function sendHtml(res: ServerResponse, html: string): void {
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store, no-cache, must-revalidate', 'pragma': 'no-cache', 'expires': '0' })
  res.end(html)
}

/** 本 run 各 bot 的策略分配（runtime/runs/<runId>/strategy-assignments.json，setup 时落盘，
 *  见 run.ts）。给实时 Run 控制面板显示「这个 run 在跑什么方法论」。轻量视图：只带
 *  id/标题/标的/池大小，不带基金码全量（multi_equity 的池 1000+ 只，面板用不上）。
 *  老 run 没该文件 → undefined，前端优雅退化为只显 bot id。 */
function readRunStrategies(
  worldRoot: string,
  runId: string,
): Record<string, { strategyId: string; title: string; targetIndex: string; fundCount: number }> | undefined {
  try {
    const raw = JSON.parse(readFileSync(join(runDir(worldRoot, runId), 'strategy-assignments.json'), 'utf8')) as {
      bots?: Record<string, { strategy_id?: string; strategy_title?: string; target_index?: string; buyable_fund_codes?: string[] }>
    }
    if (!raw?.bots) return undefined
    const out: Record<string, { strategyId: string; title: string; targetIndex: string; fundCount: number }> = {}
    for (const [botId, a] of Object.entries(raw.bots)) {
      out[botId] = {
        strategyId: a.strategy_id ?? '',
        title: a.strategy_title ?? '',
        targetIndex: a.target_index ?? '',
        fundCount: Array.isArray(a.buyable_fund_codes) ? a.buyable_fund_codes.length : 0,
      }
    }
    return Object.keys(out).length ? out : undefined
  } catch { return undefined }
}

/** Light per-run view for /api/backtest/runs — only what the control panel renders. */
function runSummary(worldRoot: string, s: WorldState): Record<string, unknown> {
  return {
    runId: s.run_id,
    status: s.status,
    currentDate: s.current_date,
    cursor: s.cursor,
    total: s.trading_dates.length,
    bots: s.bots,
    loop: s.loop,
    startedAt: s.started_at,
    updatedAt: s.updated_at,
    strategies: readRunStrategies(worldRoot, s.run_id),
  }
}

function readJsonBody(req: IncomingMessage, limitBytes = 64 * 1024): Promise<Record<string, unknown>> {
  return new Promise((resolveP, reject) => {
    let raw = ''
    let tooBig = false
    req.on('data', (c: Buffer) => {
      raw += c.toString()
      if (raw.length > limitBytes) { tooBig = true; req.destroy() }
    })
    req.on('end', () => {
      if (tooBig) return reject(new Error('request body too large'))
      if (!raw.trim()) return resolveP({})
      try { resolveP(JSON.parse(raw) as Record<string, unknown>) }
      catch { reject(new Error('invalid JSON body')) }
    })
    req.on('error', reject)
  })
}

function parseArgs(argv: string[]): { host: string; port: number; dbPath: string; worldRoot: string } {
  let host = '127.0.0.1'
  let port = 48080
  let dbPath = DEFAULT_DB
  let worldRoot = DEFAULT_WORLD_ROOT
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    if (arg === '--host' && argv[i + 1]) host = argv[++i]
    else if (arg === '--port' && argv[i + 1]) port = Number(argv[++i])
    else if (arg === '--db' && argv[i + 1]) dbPath = argv[++i]
    else if (arg === '--world-root' && argv[i + 1]) worldRoot = resolve(argv[++i])
  }
  return { host, port, dbPath, worldRoot }
}

async function main(argv = process.argv.slice(2)): Promise<number> {
  const { host, port, dbPath, worldRoot } = parseArgs(argv)
  if (!existsSync(dbPath)) {
    process.stderr.write(`fund db not found: ${dbPath}
`)
    return 1
  }
  const html = readFileSync(DEFAULT_HTML, 'utf8')
  const marketReportsHtml = readFileSync(DEFAULT_MARKET_REPORTS_HTML, 'utf8')
  const oosBot101Html = readFileSync(DEFAULT_OOS_BOT101_HTML, 'utf8')
  // bot101 交互式对话引擎：懒连 MCP 上游，复用 bot 自己的 LLM 端点 + 只读工具集。
  // 注入研报库直查函数 → chat 里的 bot101 多出本地 get_market_report（查当前/历史市场主线等）。
  const chatEngine: Bot101ChatEngine = createBot101ChatEngine({
    worldRoot,
    loadMarketReportAsOf: (reportType, asOf) => lookupMarketReportAsOf(dbPath, reportType, asOf),
  })
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    void (async () => {
      const url = new URL(req.url ?? '/', `http://${req.headers.host ?? `${host}:${port}`}`)
      if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
        sendHtml(res, html)
        return
      }
      if (req.method === 'GET' && url.pathname === '/market-reports.html') {
        sendHtml(res, marketReportsHtml)
        return
      }
      if (req.method === 'GET' && url.pathname === '/oos-bot101.html') {
        sendHtml(res, oosBot101Html)
        return
      }
      if (req.method === 'GET' && url.pathname === '/runs.html') {
        sendHtml(res, readFileSync(DEFAULT_RUNS_HTML, 'utf8'))  // 按请求读盘，HTML 改动免重启
        return
      }
      // 集成对话页（物理AI/链芯/市场报告 三栏同屏）。按请求读盘，HTML 改动免重启。
      if (req.method === 'GET' && url.pathname === '/agents.html') {
        sendHtml(res, readFileSync(DEFAULT_AGENTS_HTML, 'utf8'))
        return
      }
      if (req.method === 'GET' && url.pathname === '/health') {
        sendJson(res, 200, { status: 'ok', dbPath })
        return
      }
      if (req.method === 'GET' && url.pathname === '/api/backtest/data') {
        sendJson(res, 200, await loadDataset(dbPath, worldRoot))
        return
      }
      if (req.method === 'GET' && url.pathname === '/api/backtest/all-runs') {
        sendJson(res, 200, await loadAllRunsSummary(dbPath, worldRoot))
        return
      }
      if (req.method === 'GET' && url.pathname === '/api/market-reports') {
        sendJson(res, 200, loadMarketReports(dbPath, url.searchParams.get('date') ?? ''))
        return
      }
      // 方案 B+：两张日度主线卡的「今日盘中实时板块快照」。只对今日返回 applicable=true，
      // 历史日/取数失败均返回 applicable=false，前端据此决定是否渲染这一栏。
      if (req.method === 'GET' && url.pathname === '/api/market-reports/intraday-boards') {
        sendJson(res, 200, await fetchIntradayBoards({ repoRoot: REPO_ROOT, dbPath, date: url.searchParams.get('date') ?? '' }))
        return
      }
      const oosMatch = url.pathname.match(/^\/api\/oos\/([A-Za-z0-9._-]+)$/)
      if (req.method === 'GET' && oosMatch) {
        const botId = oosMatch[1]
        if (!OOS_BOT_IDS.has(botId)) { sendJson(res, 404, { error: 'unknown OOS bot' }); return }
        sendJson(res, 200, loadOosBot(dbPath, botId, url.searchParams.get('run_id') ?? defaultOosRunId(botId), url.searchParams.get('date') ?? ''))
        return
      }
      // 与 bot101 实时对话：真 agentic（bot 可现场调 MCP 只读工具拉 PIT 数据再回答）。
      // 入参 { messages:[{role,content}], date }；date 决定数据时点（PIT 锁死该日及之前）。
      if (req.method === 'POST' && url.pathname === '/api/bot101/chat') {
        let body: Record<string, unknown>
        try { body = await readJsonBody(req) }
        catch (err) { sendJson(res, 400, { error: err instanceof Error ? err.message : String(err) }); return }
        const rawMsgs = Array.isArray(body.messages) ? body.messages : []
        const messages: ChatMessage[] = rawMsgs
          .filter((m): m is ChatMessage => isPlainObject(m) && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string')
          .map(m => ({ role: m.role, content: m.content }))
        if (messages.length === 0) { sendJson(res, 400, { error: 'messages 不能为空' }); return }
        // 数据时点：把前端请求日夹到「≤ 请求日、确有完整每日决策的最近一个交易日」。agents.html 固定发
        // 浏览器当天，但当日日报/账户常还没生成 → 夹到上一交易日，保证会话一开就注入最新一份完整决策。
        const reqDate = typeof body.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(body.date) ? body.date : ''
        const probe = loadMarketReports(dbPath, '')
        const decisionDates = (Array.isArray(probe.dates) ? probe.dates as Array<{ as_of_date: string }> : []).map(d => d.as_of_date)
        const asOf = resolveChatAsOf(decisionDates, reqDate)
        const reportsPayload = asOf === String(probe.selectedDate ?? '') ? probe : loadMarketReports(dbPath, asOf)
        const oos = loadOosBot(dbPath, 'bot101', 'oos-bot101-daily', asOf)
        const pageContext = formatBot101ChatContext(reportsPayload, oos)

        // 流式：body.stream=true → SSE，逐 token + 工具进度实时推；否则一次性 JSON。
        if (body.stream === true) {
          res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache, no-transform', 'connection': 'keep-alive', 'x-accel-buffering': 'no' })
          const emit = (event: string, data: Record<string, unknown>): void => {
            if (!res.writableEnded) res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
          }
          const ac = new AbortController()
          req.on('close', () => ac.abort())
          emit('meta', { asOfDate: asOf })
          try {
            const result = await chatEngine.runTurnStream({ messages, asOfDate: asOf, pageContext, signal: ac.signal }, emit)
            emit('done', { asOfDate: asOf, ...result })
          } catch (err) {
            emit('error', { error: 'bot101 对话失败: ' + (err instanceof Error ? err.message : String(err)) })
          } finally {
            if (!res.writableEnded) res.end()
          }
          return
        }

        try {
          const result = await chatEngine.runTurn({ messages, asOfDate: asOf, pageContext })
          sendJson(res, 200, { asOfDate: asOf, ...result })
        } catch (err) {
          sendJson(res, 502, { error: 'bot101 对话失败: ' + (err instanceof Error ? err.message : String(err)) })
        }
        return
      }
      if (req.method === 'GET' && url.pathname === '/api/backtest/bot') {
        const botId = url.searchParams.get('bot_id') ?? ''
        const runId = url.searchParams.get('run_id') ?? ''
        if (!botId || !runId) { sendJson(res, 400, { error: 'bot_id and run_id required' }); return }
        const runs = await listRunsForBot(dbPath, worldRoot, botId)
        if (!runs.some(r => r.runId === runId)) { sendJson(res, 404, { error: 'bot or run not found' }); return }
        const bot = await loadBotForRun(dbPath, worldRoot, botId, runId, runs)
        if (!bot) { sendJson(res, 404, { error: 'no data for bot/run' }); return }
        sendJson(res, 200, bot)
        return
      }
      // bot 当日滚动反思（交易记忆窗口 / 决策回复）——读 runtime 下的 sent.md /
      // reply.json 真值切片。参数走 ID/日期白名单正则，挡掉 ../ 路径穿越。
      if (req.method === 'GET' && url.pathname === '/api/backtest/bot-reflection') {
        const botId = url.searchParams.get('bot_id') ?? ''
        const runId = url.searchParams.get('run_id') ?? ''
        const tradeDate = url.searchParams.get('trade_date') ?? ''
        const idRe = /^[A-Za-z0-9._-]+$/
        if (!idRe.test(botId) || !idRe.test(runId) || !/^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
          sendJson(res, 400, { error: 'bot_id, run_id, trade_date required and must be well-formed' })
          return
        }
        sendJson(res, 200, loadReflection(worldRoot, runId, botId, tradeDate))
        return
      }
      // 某 (bot, run) 的方法论三件套：初始（冻结库重建）/ 最新（workspace）/ 修订轨迹。
      // 只读；ID 走白名单正则挡路径穿越；数据缺失由 loadMethodology 内部降级。
      if (req.method === 'GET' && url.pathname === '/api/backtest/bot-methodology') {
        const botId = url.searchParams.get('bot_id') ?? ''
        const runId = url.searchParams.get('run_id') ?? ''
        const idRe = /^[A-Za-z0-9._-]+$/
        if (!idRe.test(botId) || !idRe.test(runId)) {
          sendJson(res, 400, { error: 'bot_id, run_id required and must be well-formed' })
          return
        }
        sendJson(res, 200, loadMethodology(worldRoot, runId, botId))
        return
      }
      // bot 用户画像（USER.md）——优先读 run 快照 workspaces/<bot>/USER.md（与该次
      // 回测实际投喂 bot 的版本一致），缺失时回退仓库 bots/<bot>/USER.md 当前版。
      // ID 走白名单正则，挡掉 ../ 路径穿越；找不到文件返回空 content，前端兜底提示。
      if (req.method === 'GET' && url.pathname === '/api/backtest/bot-user-md') {
        const botId = url.searchParams.get('bot_id') ?? ''
        const runId = url.searchParams.get('run_id') ?? ''
        const idRe = /^[A-Za-z0-9._-]+$/
        if (!idRe.test(botId)) { sendJson(res, 400, { error: 'bot_id required and must be well-formed' }); return }
        let content = ''
        let source = ''
        if (runId && idRe.test(runId)) {
          const snap = join(shadowWorkspaceDir(worldRoot, runId, botId), 'USER.md')
          if (existsSync(snap)) { content = readFileSync(snap, 'utf8'); source = 'run-snapshot' }
        }
        if (!content) {
          // worldRoot 默认 = <repo>/world/runtime → ../../bots 即 agent_invest_lab/bots
          const fallback = join(worldRoot, '..', '..', 'bots', botId, 'USER.md')
          if (existsSync(fallback)) { content = readFileSync(fallback, 'utf8'); source = 'bots-dir' }
        }
        sendJson(res, 200, { botId, runId, source, content })
        return
      }
      // Live run control: list controllable runs (running/paused) read from per-run
      // state.json, and trigger pause/stop. Resume stays CLI-only (it must spawn a
      // long-lived `world resume` with the world config, which the dashboard lacks).
      if (req.method === 'GET' && url.pathname === '/api/backtest/runs') {
        sendJson(res, 200, { runs: listControllableRuns(worldRoot).map(s => runSummary(worldRoot, s)) })
        return
      }
      if (req.method === 'POST' && (url.pathname === '/api/backtest/runs/pause' || url.pathname === '/api/backtest/runs/stop')) {
        let body: Record<string, unknown>
        try { body = await readJsonBody(req) }
        catch (err) { sendJson(res, 400, { ok: false, error: err instanceof Error ? err.message : String(err) }); return }
        const runId = typeof body.runId === 'string' ? body.runId : ''
        if (!runId) { sendJson(res, 400, { ok: false, error: 'runId required' }); return }
        const r = url.pathname.endsWith('/pause') ? requestPause(worldRoot, runId) : requestStop(worldRoot, runId)
        sendJson(res, r.ok ? 200 : 409, r)
        return
      }
      // 隐藏记录管理（纯展示层，DB 不动）：列出 / 隐藏 / 恢复某条 (bot, run)。
      if (req.method === 'GET' && url.pathname === '/api/backtest/records/hidden') {
        sendJson(res, 200, { hidden: loadHiddenRecords(worldRoot) })
        return
      }
      if (req.method === 'POST' && (url.pathname === '/api/backtest/records/hide' || url.pathname === '/api/backtest/records/unhide')) {
        let body: Record<string, unknown>
        try { body = await readJsonBody(req) }
        catch (err) { sendJson(res, 400, { ok: false, error: err instanceof Error ? err.message : String(err) }); return }
        const botId = typeof body.botId === 'string' ? body.botId : ''
        const runId = typeof body.runId === 'string' ? body.runId : ''
        const idRe = /^[A-Za-z0-9._-]+$/
        if (!idRe.test(botId) || !idRe.test(runId)) {
          sendJson(res, 400, { ok: false, error: 'botId and runId required and must be well-formed' })
          return
        }
        const list = loadHiddenRecords(worldRoot)
        if (url.pathname.endsWith('/hide')) {
          if (!list.some(r => r.botId === botId && r.runId === runId)) list.push({ botId, runId })
        } else {
          const i = list.findIndex(r => r.botId === botId && r.runId === runId)
          if (i >= 0) list.splice(i, 1)
        }
        saveHiddenRecords(worldRoot, list)
        sendJson(res, 200, { ok: true, hidden: list })
        return
      }
      // Run 评测「合格/不合格」手动标记（全员共享，存 run-verdicts.json）。
      // 只记偏离系统默认判定的覆盖；与默认一致的由前端传空 verdict 删除。
      if (req.method === 'GET' && url.pathname === '/api/backtest/run-verdicts') {
        sendJson(res, 200, { verdicts: loadRunVerdicts(worldRoot) })
        return
      }
      // Run 人工评测表（原 runs.html 内嵌 CSV，已迁入 fund_bot_run_eval；前端 fetch 此接口取代内嵌 CSV）。
      if (req.method === 'GET' && url.pathname === '/api/backtest/run-evals') {
        sendJson(res, 200, await loadRunEvals(dbPath))
        return
      }
      // 侧栏「末日决策」标识：每个 (run,bot) 末日当天动作方向（add/reduce/clear），
      // 前端据此在指数目录项上打三色标（仅合格 run 计入，聚合在前端做）。
      if (req.method === 'GET' && url.pathname === '/api/backtest/latest-decisions') {
        sendJson(res, 200, await loadLatestDecisions(dbPath))
        return
      }
      // 单条 set：{ key:"<run_id>|<bot>", verdict:"pass"|"fail"|"" }。
      // verdict 为空/'default' → 删除该 key（回到系统默认判定）。
      if (req.method === 'POST' && url.pathname === '/api/backtest/run-verdicts/set') {
        let body: Record<string, unknown>
        try { body = await readJsonBody(req) }
        catch (err) { sendJson(res, 400, { ok: false, error: err instanceof Error ? err.message : String(err) }); return }
        const key = typeof body.key === 'string' ? body.key : ''
        if (!VERDICT_KEY_RE.test(key)) { sendJson(res, 400, { ok: false, error: 'key required and must be "<run_id>|<bot>"' }); return }
        const verdict = body.verdict
        const map = loadRunVerdicts(worldRoot)
        if (verdict === 'pass' || verdict === 'fail') {
          map[key] = verdict
        } else if (verdict === '' || verdict == null || verdict === 'default') {
          delete map[key]
        } else {
          sendJson(res, 400, { ok: false, error: 'verdict must be "pass", "fail", or "" (clear)' })
          return
        }
        saveRunVerdicts(worldRoot, map)
        sendJson(res, 200, { ok: true, verdicts: map })
        return
      }
      // 批量：{ verdicts:{…}, mode:"init"|"replace" }。
      // init = 仅当服务器当前为空才整体写入（一次性迁移，幂等、不覆盖已有共享数据）；
      // replace = 整体替换（{} 即清空，支撑前端「重置」按钮）。
      if (req.method === 'POST' && url.pathname === '/api/backtest/run-verdicts/bulk') {
        let body: Record<string, unknown>
        try { body = await readJsonBody(req) }
        catch (err) { sendJson(res, 400, { ok: false, error: err instanceof Error ? err.message : String(err) }); return }
        const mode = body.mode === 'replace' ? 'replace' : 'init'
        const incoming = isPlainObject(body.verdicts) ? body.verdicts : {}
        const clean: Record<string, RunVerdict> = {}
        for (const [k, v] of Object.entries(incoming)) {
          if (VERDICT_KEY_RE.test(k) && (v === 'pass' || v === 'fail')) clean[k] = v
        }
        const current = loadRunVerdicts(worldRoot)
        if (mode === 'init') {
          if (Object.keys(current).length > 0) { sendJson(res, 200, { ok: true, migrated: 0, verdicts: current }); return }
          saveRunVerdicts(worldRoot, clean)
          sendJson(res, 200, { ok: true, migrated: Object.keys(clean).length, verdicts: clean })
          return
        }
        saveRunVerdicts(worldRoot, clean)
        sendJson(res, 200, { ok: true, verdicts: clean })
        return
      }
      if (req.method === 'GET' && url.pathname === '/api/backtest/benchmark') {
        const fund = url.searchParams.get('fund') ?? ''
        const from = url.searchParams.get('from') ?? ''
        const to = url.searchParams.get('to') ?? ''
        if (!fund || !from || !to) { sendJson(res, 400, { error: 'fund, from, to required' }); return }
        sendJson(res, 200, await loadBenchmarkSeries(dbPath, fund, from, to))
        return
      }
      sendJson(res, 404, { error: 'not found' })
    })().catch(err => sendJson(res, 500, { error: err instanceof Error ? err.message : String(err) }))
  })

  await new Promise<void>((resolveP, reject) => {
    server.once('error', reject)
    server.listen(port, host, () => {
      server.off('error', reject)
      resolveP()
    })
  })

  const actualPort = (server.address() as { port: number }).port
  process.stdout.write(`backtest dashboard listening on http://${host}:${actualPort}/
`)
  process.on('SIGINT', () => { server.close(() => process.exit(0)) })
  process.on('SIGTERM', () => { server.close(() => process.exit(0)) })
  await new Promise<void>(() => {})
  return 0
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  void main().then(code => { process.exitCode = code })
}
