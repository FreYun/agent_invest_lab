import { execFile } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { replyFile, sentFile, universeContaminationFile } from '../paths.ts'
import { listControllableRuns, type WorldState } from '../state.ts'
import { requestPause, requestStop } from '../run-control.ts'
import { buildHoldingsByDate, computeActionWeights } from './positions.ts'

const execFileAsync = promisify(execFile)
const HERE = dirname(fileURLToPath(import.meta.url))
const DEFAULT_DB = join(HERE, '../../../data/fund.db')
// 默认 worldRoot = <repo>/world/runtime；和 paths.ts 里其它 per-run helper 的约定一致。
// 仅用来定位每 run 的 universe-contamination.json marker 文件，不影响 DB 查询。
const DEFAULT_WORLD_ROOT = join(HERE, '../../runtime')
const DEFAULT_HTML = join(HERE, 'index.html')

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
  equityWeight: number | null
  bondWeight: number | null
  goldWeight: number | null
  cashWeight: number | null
  series: BotSeriesPoint[]
  actions: BotAction[]
  holdings: HoldingRow[]
  holdingsByDate: Record<string, Omit<HoldingRow, 'trade_date'>[]>
  reviews: ReviewRow[]
  benchmark: BotBenchmark | null
  realUsers: RealUserSeries[]
  runId: string
  availableRuns: BotRunRef[]
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

async function queryRows<T>(dbPath: string, sql: string): Promise<T[]> {
  // fund.db is WAL-mode and written concurrently by live world runs; without a busy
  // timeout the sqlite3 CLI fails instantly with SQLITE_BUSY ("database is locked")
  // during a writer/checkpoint window, surfacing as flaky 500s. Wait it out instead.
  const { stdout } = await execFileAsync('sqlite3', ['-json', '-cmd', '.timeout 5000', dbPath, sql], { maxBuffer: 16 * 1024 * 1024 })
  const text = stdout.trim()
  if (!text) return []
  const parsed = JSON.parse(text) as T[]
  return Array.isArray(parsed) ? parsed : []
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

export function pickBenchmarkFund(actions: BotAction[], holdings: HoldingRow[]): string {
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
  actions: BotAction[],
  holdings: HoldingRow[],
  firstTradeDate: string,
  latestTradeDate: string,
): Promise<BotBenchmark | null> {
  const fundCode = pickBenchmarkFund(actions, holdings)
  const anchorDate = firstTradeDate || actions.find(a => a.side === 'buy')?.action_date || ''
  if (!anchorDate || !latestTradeDate) return null
  return loadBenchmarkSeries(dbPath, fundCode, anchorDate, latestTradeDate)
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
  const rows = await queryRows<{ run_id: string; latest_date: string | null }>(dbPath, `
    WITH per_bot AS (
      SELECT run_id, trade_date AS d FROM fund_bot_daily_snapshots WHERE bot_id = ${b}
      UNION SELECT run_id, trade_date AS d FROM fund_bot_position_snapshots WHERE bot_id = ${b}
      UNION SELECT run_id, action_date AS d FROM fund_bot_actions WHERE bot_id = ${b}
      UNION SELECT run_id, NULL AS d FROM fund_bot_accounts WHERE bot_id = ${b}
      UNION SELECT run_id, review_date AS d FROM fund_bot_reviews WHERE bot_id = ${b}
      UNION SELECT run_id, COALESCE(exit_date, entry_date) AS d FROM fund_bot_holdings WHERE bot_id = ${b} AND run_id IS NOT NULL AND run_id <> ''
      UNION SELECT run_id, trade_date AS d FROM fund_bot_performance WHERE bot_id = ${b}
      UNION SELECT order_run_id AS run_id, order_date AS d FROM fund_bot_orders WHERE bot_id = ${b} AND order_run_id IS NOT NULL AND order_run_id <> ''
      UNION SELECT settle_run_id AS run_id, COALESCE(confirm_date, order_date) AS d FROM fund_bot_orders WHERE bot_id = ${b} AND settle_run_id IS NOT NULL AND settle_run_id <> ''
    )
    SELECT run_id, MAX(d) AS latest_date
    FROM per_bot
    WHERE run_id IS NOT NULL AND run_id <> ''
    GROUP BY run_id
    ORDER BY run_id DESC
  `)
  return rows.map(r => {
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

async function loadBotForRun(dbPath: string, botId: string, runId: string, availableRuns: BotRunRef[]): Promise<BotDataset | null> {
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
  const actionsRaw = await queryRows<ActionRow>(dbPath, `
    SELECT a.action_id, a.review_id, a.bot_id, a.fund_code,
           COALESCE(i.fund_name, a.fund_code) AS fund_name,
           a.action_type, a.final_decision, a.amount, a.shares, a.nav_used,
           a.reason, o.action_reason AS bot_reason, a.action_date
    FROM fund_bot_actions a
    LEFT JOIN fund_info i ON i.fund_code = a.fund_code
    LEFT JOIN fund_bot_orders o
      ON o.bot_id = a.bot_id
      AND o.fund_code = a.fund_code
      AND o.order_date = a.action_date
      AND o.settle_run_id = a.run_id
      AND ( (a.action_type='ADD' AND o.order_type='buy')
         OR (a.action_type='REDUCE' AND o.order_type='sell') )
    WHERE a.bot_id = ${botIdSql} AND a.run_id = ${runIdSql}
    ORDER BY a.action_date ASC, a.action_id ASC
  `)
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
    ? await loadBenchmark(dbPath, actions, holdings, firstDateForBench, lastDateForBench)
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
    actions,
    holdings,
    holdingsByDate,
    reviews,
    benchmark,
    realUsers,
    runId,
    availableRuns,
  }
}

async function loadDataset(dbPath: string, worldRoot: string): Promise<Dataset> {
  const botIds = await listAllBotIds(dbPath)
  const bots: BotDatasetSummary[] = []
  for (const botId of botIds) {
    const runs = await listRunsForBot(dbPath, worldRoot, botId)
    if (!runs.length) continue
    const bot = await loadBotForRun(dbPath, botId, runs[0].runId, runs)
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
    },
    bots,
  }
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
  res.end(JSON.stringify(body))
}

function sendHtml(res: ServerResponse, html: string): void {
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-cache, must-revalidate' })
  res.end(html)
}

/** Light per-run view for /api/backtest/runs — only what the control panel renders. */
function runSummary(s: WorldState): Record<string, unknown> {
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
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    void (async () => {
      const url = new URL(req.url ?? '/', `http://${req.headers.host ?? `${host}:${port}`}`)
      if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
        sendHtml(res, html)
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
      if (req.method === 'GET' && url.pathname === '/api/backtest/bot') {
        const botId = url.searchParams.get('bot_id') ?? ''
        const runId = url.searchParams.get('run_id') ?? ''
        if (!botId || !runId) { sendJson(res, 400, { error: 'bot_id and run_id required' }); return }
        const runs = await listRunsForBot(dbPath, worldRoot, botId)
        if (!runs.some(r => r.runId === runId)) { sendJson(res, 404, { error: 'bot or run not found' }); return }
        const bot = await loadBotForRun(dbPath, botId, runId, runs)
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
      // Live run control: list controllable runs (running/paused) read from per-run
      // state.json, and trigger pause/stop. Resume stays CLI-only (it must spawn a
      // long-lived `world resume` with the world config, which the dashboard lacks).
      if (req.method === 'GET' && url.pathname === '/api/backtest/runs') {
        sendJson(res, 200, { runs: listControllableRuns(worldRoot).map(runSummary) })
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
