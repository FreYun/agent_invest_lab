// Prefetched per-bot per-day data injected into the daily prompt. Goal: bot
// shouldn't have to spend round-trips re-discovering the same routine inputs
// (account snapshot, recent PnL trend, NAV trend for held funds, major index
// snapshots) every morning. World-side prefetches these and renderDailyMessage
// pastes formatted blocks into the prompt.
//
// All data is best-effort: each fetcher returns null on failure and the
// renderer just skips the corresponding block. A broken simworld upstream
// should never block a world day from running.

import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { runFundCli } from './run.ts'
import { callSimworldTool } from './simworld-client.ts'
import { runDir } from './paths.ts'

// 5 indices the user wants on every day's prompt. Match simworld's accepted
// "<6-digit>.{SH|SZ}" canonical form so we don't depend on alias resolution.
export const DEFAULT_INDEX_CODES: { code: string; name: string }[] = [
  { code: '000001.SH', name: '上证综指' },
  { code: '000300.SH', name: '沪深300' },
  { code: '399006.SZ', name: '创业板指' },
  { code: '000688.SH', name: '科创50' },
  { code: '000852.SH', name: '中证1000' },
]

// ============================================================================
// Types — small, prompt-friendly. Everything renderer reads is here.
// ============================================================================

export interface AccountSnapshot {
  asOfDate: string
  account: {
    initial_capital: number
    cash_available: number
    cash_in_transit: number
    market_value: number
    total_value: number
  }
  holdings: HoldingRow[]
  pendingOrders: PendingOrderRow[]
}

export interface HoldingRow {
  fund_code: string
  fund_name: string
  shares: number
  amount_invested: number
  latest_nav: number
  market_value: number
  unrealized_pnl_pct: number
  weight: number
  holding_days: number
  entry_date: string
}

export interface PendingOrderRow {
  order_id: number
  fund_code: string
  order_type: 'buy' | 'sell' | string
  order_date: string
  order_amount: number
  reference_nav: number
}

export interface PnlTrendPoint {
  date: string
  total_value: number
  net_value: number
  daily_return_pct: number
  cumulative_return_pct: number
  max_drawdown_pct: number
}

export interface FundSeries {
  fund_code: string
  fund_name?: string
  nav_series: { date: string; nav: number; daily_return_pct: number }[]  // most-recent last
  return_1m_pct: number | null
  return_3m_pct: number | null
}

export interface IndexQuote {
  code: string
  name: string
  latest_date: string
  latest_close: number
  ma5: number | null
  ma20: number | null
  vs_ma5_pct: number | null
  vs_ma20_pct: number | null
}

export interface BenchmarkSeries {
  code: string                                    // e.g. '000300.SH'
  name: string                                    // e.g. '沪深300'
  pointsByDate: Record<string, number>            // ISO date → cumulative % since runStartDate (run-start day = 0)
  latestCumulativePct: number | null              // convenience: cumulative pct at the last available trading date < asOfDate
}

// Mirror of portfolio_get_my_performance's payload — surfaced wholesale so the
// daily prompt exposes every metric (sharpe / calmar / vol / win-loss / best
// day etc.) rather than cherry-picking. Keep field names aligned with the
// upstream JSON so it's obvious where each number came from.
export interface PerformanceSummary {
  first_date: string
  last_date: string
  trading_days: number
  initial_capital: number
  latest_total_value: number
  latest_net_value: number
  total_return_pct: number
  annualized_return_pct: number
  max_drawdown_pct: number
  max_drawdown_date: string
  volatility_pct_annualized: number
  sharpe_ratio_rf0: number
  win_days: number
  loss_days: number
  flat_days: number
  best_day: { date: string; return_pct: number } | null
  worst_day: { date: string; return_pct: number } | null
}

export interface TradesSummary {
  buy_count: number
  sell_count: number
  total_buy_amount: number
  total_sell_proceeds: number
  total_fees: number
  round_trips_count: number
}

export interface IntervalMetricRow {
  period: '1m' | '3m' | '6m' | '1y' | 'since_inception'
  return_pct: number
  max_drawdown_pct: number
  volatility_pct: number
  sharpe_ratio: number
  calmar_ratio: number | null
  data_points: number
  window_target_days: number | null
  fallback: boolean
}

export interface IntervalMetrics {
  as_of_perf_date: string | null
  rf_annual_pct: number
  rf_daily_pct: number
  trading_days_per_year: number | null
  rows: IntervalMetricRow[]
}

export interface CompletedPosition {
  fund_code: string
  fund_name: string
  entry_date: string
  exit_date: string
  holding_days: number | null
  total_invested: number
  total_proceeds: number
  net_pl: number
  return_pct: number
  fees: number
}

export interface PerformanceData {
  asOfDate: string
  summary: PerformanceSummary | null
  trades: TradesSummary | null
  intervals: IntervalMetrics | null
  completedPositions: CompletedPosition[]
  // Daily series — kept here too so pnlTrendBlock has a single source instead
  // of reading close_my_day.json files separately.
  dailySeries: PnlTrendPoint[]
}

export interface DailyContextData {
  account?: AccountSnapshot
  pnlTrend?: PnlTrendPoint[]
  performance?: PerformanceData
  fundSeries?: FundSeries[]
  indices?: IndexQuote[]
  benchmark?: BenchmarkSeries
}

// ============================================================================
// Account snapshot via fund CLI — bypasses MCP HTTP, reads same DB the bot
// would. Settle ran just before this on the calling path so the snapshot
// reflects today's post-settle starting state.
// ============================================================================

async function fetchAccountSnapshot(opts: {
  fundMcpCli: string
  botId: string
  runId: string
  asOfDate: string
}): Promise<AccountSnapshot | null> {
  try {
    const r = await runFundCli(opts.fundMcpCli, 'get_my_history',
      ['--bot-id', opts.botId, '--run-id', opts.runId, '--limit', '30'],
      { timeoutMs: 30_000 })
    if (r.code !== 0) return null
    const d = JSON.parse(r.stdout) as Record<string, unknown>
    if (!d.success) return null
    const account = d.account as AccountSnapshot['account'] | undefined
    if (!account) return null
    const rawHoldings = Array.isArray(d.holdings) ? d.holdings as Record<string, unknown>[] : []
    // portfolio_get_my_history hands back the raw holding row — fund_bot_holdings
    // doesn't store weight, so compute it client-side off market_value / total.
    const totalValue = Number(account.total_value ?? 0)
    const holdings: HoldingRow[] = rawHoldings
      .filter(h => h.status === 'active')
      .map(h => {
        const mv = Number(h.market_value ?? 0)
        return {
          fund_code: String(h.fund_code ?? ''),
          fund_name: String(h.fund_name ?? ''),
          shares: Number(h.shares ?? 0),
          amount_invested: Number(h.amount_invested ?? 0),
          latest_nav: Number(h.latest_nav ?? 0),
          market_value: mv,
          unrealized_pnl_pct: Number(h.unrealized_pnl_pct ?? 0),
          weight: totalValue > 0 ? mv / totalValue : 0,
          holding_days: Number(h.holding_days ?? 0),
          entry_date: String(h.entry_date ?? ''),
        }
      })
    const rawOrders = Array.isArray(d.orders) ? d.orders as Record<string, unknown>[] : []
    const pendingOrders: PendingOrderRow[] = rawOrders
      .filter(o => o.status === 'pending')
      .map(o => ({
        order_id: Number(o.order_id ?? 0),
        fund_code: String(o.fund_code ?? ''),
        order_type: String(o.order_type ?? ''),
        order_date: String(o.order_date ?? ''),
        order_amount: Number(o.order_amount ?? 0),
        reference_nav: Number(o.reference_nav ?? 0),
      }))
    return { asOfDate: opts.asOfDate, account, holdings, pendingOrders }
  } catch {
    return null
  }
}

// ============================================================================
// Full performance payload (summary / interval metrics / trades / closed
// positions / daily series) via portfolio_get_my_performance CLI. Single
// source of truth — the daily series here also feeds pnlTrendBlock so we
// don't read close_my_day.json files separately anymore.
//
// Returns null if as_of_date precedes the first snapshot (Day 1; no perf
// history yet) or if the CLI errors out. Caller treats null as "no perf
// block to render" (also implies no PnL trend block).
// ============================================================================

async function fetchPerformance(opts: {
  fundMcpCli: string
  botId: string
  runId: string
  asOfDate: string
  dailySeriesLimit: number
}): Promise<PerformanceData | null> {
  try {
    const r = await runFundCli(opts.fundMcpCli, 'get_my_performance', [
      '--bot-id', opts.botId,
      '--run-id', opts.runId,
      '--as-of-date', opts.asOfDate,
      '--daily-series-limit', String(opts.dailySeriesLimit),
    ], { timeoutMs: 30_000 })
    if (r.code !== 0) return null
    const d = JSON.parse(r.stdout) as Record<string, unknown>
    if (d.success !== true) return null

    const summary = parseSummary(d.summary)
    const trades = parseTrades(d.trades_summary)
    const intervals = parseIntervalMetrics(d.interval_metrics)
    const completedPositions = parseCompletedPositions(d.completed_positions)
    const dailySeries = parseDailySeries(d.daily_series)

    return { asOfDate: opts.asOfDate, summary, trades, intervals, completedPositions, dailySeries }
  } catch {
    return null
  }
}

function parseSummary(raw: unknown): PerformanceSummary | null {
  if (!raw || typeof raw !== 'object') return null
  const s = raw as Record<string, unknown>
  return {
    first_date: String(s.first_date ?? ''),
    last_date: String(s.last_date ?? ''),
    trading_days: Number(s.trading_days ?? 0),
    initial_capital: Number(s.initial_capital ?? 0),
    latest_total_value: Number(s.latest_total_value ?? 0),
    latest_net_value: Number(s.latest_net_value ?? 1),
    total_return_pct: Number(s.total_return_pct ?? 0),
    annualized_return_pct: Number(s.annualized_return_pct ?? 0),
    max_drawdown_pct: Number(s.max_drawdown_pct ?? 0),
    max_drawdown_date: String(s.max_drawdown_date ?? ''),
    volatility_pct_annualized: Number(s.volatility_pct_annualized ?? 0),
    sharpe_ratio_rf0: Number(s.sharpe_ratio_rf0 ?? 0),
    win_days: Number(s.win_days ?? 0),
    loss_days: Number(s.loss_days ?? 0),
    flat_days: Number(s.flat_days ?? 0),
    best_day: parseDayPoint(s.best_day),
    worst_day: parseDayPoint(s.worst_day),
  }
}

function parseDayPoint(raw: unknown): { date: string; return_pct: number } | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  return { date: String(r.date ?? ''), return_pct: Number(r.return_pct ?? 0) }
}

function parseTrades(raw: unknown): TradesSummary | null {
  if (!raw || typeof raw !== 'object') return null
  const t = raw as Record<string, unknown>
  return {
    buy_count: Number(t.buy_count ?? 0),
    sell_count: Number(t.sell_count ?? 0),
    total_buy_amount: Number(t.total_buy_amount ?? 0),
    total_sell_proceeds: Number(t.total_sell_proceeds ?? 0),
    total_fees: Number(t.total_fees ?? 0),
    round_trips_count: Number(t.round_trips_count ?? 0),
  }
}

function parseIntervalMetrics(raw: unknown): IntervalMetrics | null {
  if (!raw || typeof raw !== 'object') return null
  const im = raw as Record<string, unknown>
  const metricsRaw = (im.metrics ?? {}) as Record<string, unknown>
  // Preserve a stable display order (short window → long window → cumulative).
  const order: IntervalMetricRow['period'][] = ['1m', '3m', '6m', '1y', 'since_inception']
  const rows: IntervalMetricRow[] = []
  for (const p of order) {
    const m = metricsRaw[p]
    if (!m || typeof m !== 'object') continue
    const mr = m as Record<string, unknown>
    rows.push({
      period: p,
      return_pct: Number(mr.return_pct ?? 0),
      max_drawdown_pct: Number(mr.max_drawdown_pct ?? 0),
      volatility_pct: Number(mr.volatility_pct ?? 0),
      sharpe_ratio: Number(mr.sharpe_ratio ?? 0),
      calmar_ratio: mr.calmar_ratio === null || mr.calmar_ratio === undefined ? null : Number(mr.calmar_ratio),
      data_points: Number(mr.data_points ?? 0),
      window_target_days: mr.window_target_days === null || mr.window_target_days === undefined ? null : Number(mr.window_target_days),
      fallback: !!mr.fallback,
    })
  }
  return {
    as_of_perf_date: typeof im.as_of_perf_date === 'string' ? im.as_of_perf_date : null,
    rf_annual_pct: Number(im.rf_annual_pct ?? 0),
    rf_daily_pct: Number(im.rf_daily_pct ?? 0),
    trading_days_per_year: typeof im.trading_days_per_year === 'number' ? im.trading_days_per_year : null,
    rows,
  }
}

function parseCompletedPositions(raw: unknown): CompletedPosition[] {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((p): p is Record<string, unknown> => !!p && typeof p === 'object')
    .map(p => ({
      fund_code: String(p.fund_code ?? ''),
      fund_name: String(p.fund_name ?? ''),
      entry_date: String(p.entry_date ?? ''),
      exit_date: String(p.exit_date ?? ''),
      holding_days: p.holding_days === null || p.holding_days === undefined ? null : Number(p.holding_days),
      total_invested: Number(p.total_invested ?? 0),
      total_proceeds: Number(p.total_proceeds ?? 0),
      net_pl: Number(p.net_pl ?? 0),
      return_pct: Number(p.return_pct ?? 0),
      fees: Number(p.fees ?? 0),
    }))
}

function parseDailySeries(raw: unknown): PnlTrendPoint[] {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((p): p is Record<string, unknown> => !!p && typeof p === 'object')
    .map(p => ({
      date: String(p.trade_date ?? ''),
      total_value: Number(p.total_value ?? 0),
      net_value: Number(p.net_value ?? 1),
      daily_return_pct: Number(p.daily_return_pct ?? 0),
      cumulative_return_pct: Number(p.cumulative_return_pct ?? 0),
      max_drawdown_pct: Number(p.max_drawdown_pct ?? 0),
    }))
}

// ============================================================================
// Fallback PnL trend reader — kept as a safety net for situations where the
// perf CLI fails but close_my_day.json files exist on disk (legacy runs from
// before the cli_tools.py init_db() fix landed). Once those rotate out we can
// retire this and rely solely on portfolio_get_my_performance.
// ============================================================================

async function fetchPnlTrend(opts: {
  worldRoot: string
  runId: string
  botId: string
  asOfDate: string
  windowDays: number
}): Promise<PnlTrendPoint[] | null> {
  const root = runDir(opts.worldRoot, opts.runId)
  if (!existsSync(root)) return null
  let dateDirs: string[]
  try {
    dateDirs = readdirSync(root)
      .filter(n => /^\d{4}-\d{2}-\d{2}$/.test(n) && n < opts.asOfDate)
      .sort()
  } catch {
    return null
  }
  if (dateDirs.length === 0) return null
  const recent = dateDirs.slice(-opts.windowDays)
  const points: PnlTrendPoint[] = []
  for (const d of recent) {
    const p = join(root, d, opts.botId, 'close_my_day.json')
    if (!existsSync(p)) continue
    try {
      const snap = JSON.parse(readFileSync(p, 'utf8')) as Record<string, unknown>
      if (!snap.success) continue
      const assets = (snap.assets ?? {}) as Record<string, unknown>
      const pnl = (snap.pnl ?? {}) as Record<string, unknown>
      points.push({
        date: String(snap.trade_date ?? d),
        total_value: Number(assets.total_value ?? 0),
        net_value: Number(assets.net_value ?? 1),
        daily_return_pct: Number(pnl.daily_return_pct ?? 0),
        cumulative_return_pct: Number(pnl.cumulative_return_pct ?? 0),
        max_drawdown_pct: Number(pnl.max_drawdown_pct ?? 0),
      })
    } catch { /* skip malformed */ }
  }
  return points.length ? points : null
}

// ============================================================================
// Fund NAV series + computed 1m/3m returns. Skipping fund_performance because
// it's marked 是否可用=false for the index ETFs the lab pins (510300 etc.) —
// we just compute returns from the NAV series directly.
// ============================================================================

async function fetchFundSeries(opts: {
  simworldUrl: string
  fundCodes: string[]
  asOfDate: string
  fundNames: Map<string, string>
}): Promise<FundSeries[]> {
  if (opts.fundCodes.length === 0) return []
  const simDt = `${opts.asOfDate} 15:00:00`
  // Pull ~70 trading days so we have headroom to compute 3m return (~63 days).
  const startDate = computeWindowStart(opts.asOfDate, 100)  // 100 calendar ≈ 70 trading
  const endDate = priorDay(opts.asOfDate)
  const out: FundSeries[] = []
  for (const code of opts.fundCodes) {
    try {
      const raw = await callSimworldTool(opts.simworldUrl, 'fund_nav', {
        fund_codes: [code],
        simulated_datetime: simDt,
        start_date: startDate,
        end_date: endDate,
      }) as { items?: { 基金代码?: string; 是否可用?: boolean; 净值记录?: { 交易日期?: string; 复权单位净值?: number; 日收益率?: number }[] }[] } | null
      const item = raw?.items?.[0]
      if (!item || !item['是否可用'] || !Array.isArray(item['净值记录'])) continue
      // Last 20 trading days for the prompt display.
      const allRecs = item['净值记录']
        .map(r => ({
          date: String(r['交易日期'] ?? '').slice(0, 10),
          nav: Number(r['复权单位净值'] ?? 0),
          daily_return_pct: Number(r['日收益率'] ?? 0),
        }))
        .filter(r => r.date && r.nav > 0)
      if (allRecs.length === 0) continue
      const window20 = allRecs.slice(-20)
      // 1m ≈ ~22 trading days back; 3m ≈ ~63
      const last = allRecs[allRecs.length - 1]
      const ret1m = pctChange(navAt(allRecs, 22), last.nav)
      const ret3m = pctChange(navAt(allRecs, 63), last.nav)
      out.push({
        fund_code: code,
        fund_name: opts.fundNames.get(code) ?? '',
        nav_series: window20,
        return_1m_pct: ret1m,
        return_3m_pct: ret3m,
      })
    } catch { /* skip this fund on error */ }
  }
  return out
}

function navAt(series: { nav: number }[], backN: number): number | null {
  if (series.length === 0) return null
  const idx = series.length - 1 - backN
  if (idx < 0) return null
  return series[idx].nav
}

function pctChange(from: number | null, to: number): number | null {
  if (from === null || from <= 0) return null
  return ((to - from) / from) * 100
}

function computeWindowStart(asOfDate: string, daysBack: number): string {
  const d = new Date(asOfDate + 'T00:00:00Z')
  d.setUTCDate(d.getUTCDate() - daysBack)
  return d.toISOString().slice(0, 10)
}

function priorDay(asOfDate: string): string {
  const d = new Date(asOfDate + 'T00:00:00Z')
  d.setUTCDate(d.getUTCDate() - 1)
  return d.toISOString().slice(0, 10)
}

// ============================================================================
// Index snapshots — latest close + MA5/MA20. simworld returns ~20 records by
// default which is exactly enough for MA20.
// ============================================================================

async function fetchIndexSnapshots(opts: {
  simworldUrl: string
  asOfDate: string
  indices: { code: string; name: string }[]
}): Promise<IndexQuote[]> {
  if (opts.indices.length === 0) return []
  const simDt = `${opts.asOfDate} 15:00:00`
  try {
    const raw = await callSimworldTool(opts.simworldUrl, 'market_index_quote', {
      market: 'cn',
      symbols: opts.indices.map(i => i.code),
      simulated_datetime: simDt,
    }) as { items?: { 指数标识?: string; 是否可用?: boolean; 行情记录?: { 日期?: string; 收盘?: number }[] }[] } | null
    if (!raw?.items) return []
    const byCode = new Map<string, IndexQuote>()
    for (const item of raw.items) {
      const code = String(item['指数标识'] ?? '')
      if (!item['是否可用'] || !Array.isArray(item['行情记录'])) continue
      const closes = item['行情记录']
        .map(r => ({ date: String(r['日期'] ?? '').slice(0, 10), close: Number(r['收盘'] ?? 0) }))
        .filter(r => r.date && r.close > 0)
      if (closes.length === 0) continue
      const last = closes[closes.length - 1]
      const ma5 = mean(closes.slice(-5).map(r => r.close))
      const ma20 = closes.length >= 20 ? mean(closes.slice(-20).map(r => r.close)) : null
      byCode.set(code, {
        code,
        name: opts.indices.find(i => i.code === code)?.name ?? code,
        latest_date: last.date,
        latest_close: last.close,
        ma5,
        ma20,
        vs_ma5_pct: pctChange(ma5, last.close),
        vs_ma20_pct: pctChange(ma20, last.close),
      })
    }
    // Preserve requested order so the prompt is stable.
    return opts.indices.map(i => byCode.get(i.code)).filter((x): x is IndexQuote => !!x)
  } catch {
    return []
  }
}

function mean(xs: number[]): number {
  if (xs.length === 0) return 0
  let s = 0
  for (const x of xs) s += x
  return s / xs.length
}

// ============================================================================
// Benchmark cumulative return — pulls the benchmark index over [runStartDate,
// asOfDate-1] from simworld and computes each trading day's pct change vs the
// run-start day's close. The PnL trend renderer joins on date so each row gets
// "you vs benchmark since run start" — bot can immediately see whether it's
// adding alpha or just riding beta.
//
// Default benchmark is 沪深300 (000300.SH). The only buyable in current lab
// runs is 510300 (沪深300 ETF), so 沪深300 index is a tight benchmark; if pool
// expands later, caller can override via opts.benchmarkCode.
// ============================================================================

const DEFAULT_BENCHMARK = { code: '000300.SH', name: '沪深300' }

async function fetchBenchmark(opts: {
  simworldUrl: string
  code: string
  name: string
  runStartDate: string
  asOfDate: string
}): Promise<BenchmarkSeries | null> {
  const simDt = `${opts.asOfDate} 15:00:00`
  try {
    const raw = await callSimworldTool(opts.simworldUrl, 'market_index_quote', {
      market: 'cn',
      symbols: [opts.code],
      simulated_datetime: simDt,
      start_date: opts.runStartDate,
      end_date: priorDay(opts.asOfDate),
    }) as { items?: { 是否可用?: boolean; 行情记录?: { 日期?: string; 收盘?: number }[] }[] } | null
    const item = raw?.items?.[0]
    if (!item || !item['是否可用'] || !Array.isArray(item['行情记录'])) return null
    const closes = item['行情记录']
      .map(r => ({ date: String(r['日期'] ?? '').slice(0, 10), close: Number(r['收盘'] ?? 0) }))
      .filter(r => r.date && r.close > 0)
    if (closes.length === 0) return null
    // Anchor on the first available trading-day's close >= runStartDate. If the
    // run started on a non-trading day the API returns rows from the next
    // trading day onwards, which is exactly what we want.
    const base = closes[0].close
    const pointsByDate: Record<string, number> = {}
    for (const r of closes) {
      pointsByDate[r.date] = ((r.close - base) / base) * 100
    }
    const lastDate = closes[closes.length - 1].date
    return {
      code: opts.code,
      name: opts.name,
      pointsByDate,
      latestCumulativePct: pointsByDate[lastDate] ?? null,
    }
  } catch {
    return null
  }
}

// ============================================================================
// Public entry point.
// ============================================================================

export interface FetchDailyContextOptions {
  worldRoot: string
  runId: string
  botId: string
  asOfDate: string
  fundMcpCli: string | undefined
  simworldUrl: string | undefined
  // First trading day of the run — anchor for benchmark cumulative %. If
  // omitted, benchmark block is skipped (can't compute "since when?" without
  // a reference point).
  runStartDate?: string
  pnlTrendDays?: number
  indices?: { code: string; name: string }[]
  benchmark?: { code: string; name: string }
}

export async function fetchDailyContext(opts: FetchDailyContextOptions): Promise<DailyContextData> {
  const pnlTrendDays = opts.pnlTrendDays ?? 10
  const indices = opts.indices ?? DEFAULT_INDEX_CODES
  const benchmark = opts.benchmark ?? DEFAULT_BENCHMARK
  const out: DailyContextData = {}

  // Run portfolio (CLI) + simworld (HTTP) calls in parallel — they're independent.
  // Account snapshot also feeds fundSeries (knows which codes are held), so we
  // chain fundSeries after account; everything else can run concurrently with it.
  const accountPromise: Promise<AccountSnapshot | null> = opts.fundMcpCli
    ? fetchAccountSnapshot({ fundMcpCli: opts.fundMcpCli, botId: opts.botId, runId: opts.runId, asOfDate: opts.asOfDate })
    : Promise.resolve(null)
  // Performance is the primary source of truth for PnL trend (its daily_series
  // mirrors close_my_day output) plus the broader metrics block. Keep the file
  // reader as a fallback for legacy runs whose close_my_day persisted before
  // the cli_tools init_db() fix.
  const perfPromise: Promise<PerformanceData | null> = opts.fundMcpCli
    ? fetchPerformance({ fundMcpCli: opts.fundMcpCli, botId: opts.botId, runId: opts.runId, asOfDate: opts.asOfDate, dailySeriesLimit: pnlTrendDays })
    : Promise.resolve(null)
  const pnlFilePromise = fetchPnlTrend({ worldRoot: opts.worldRoot, runId: opts.runId, botId: opts.botId, asOfDate: opts.asOfDate, windowDays: pnlTrendDays })
  const indexPromise: Promise<IndexQuote[]> = opts.simworldUrl
    ? fetchIndexSnapshots({ simworldUrl: opts.simworldUrl, asOfDate: opts.asOfDate, indices })
    : Promise.resolve([])
  // Benchmark needs runStartDate to anchor cumulative; skip cleanly otherwise.
  const benchmarkPromise: Promise<BenchmarkSeries | null> = (opts.simworldUrl && opts.runStartDate)
    ? fetchBenchmark({ simworldUrl: opts.simworldUrl, code: benchmark.code, name: benchmark.name, runStartDate: opts.runStartDate, asOfDate: opts.asOfDate })
    : Promise.resolve(null)

  const [account, perf, pnlFromFiles, indexSnapshots, benchmarkSeries] = await Promise.all([accountPromise, perfPromise, pnlFilePromise, indexPromise, benchmarkPromise])
  if (account) out.account = account
  if (perf) out.performance = perf
  // PnL trend: prefer perf.dailySeries (CLI, always fresh); fall back to
  // close_my_day.json reads for runs where CLI route is broken.
  const trendFromPerf = perf?.dailySeries.length ? perf.dailySeries.slice(-pnlTrendDays) : null
  const trend = trendFromPerf ?? pnlFromFiles
  if (trend && trend.length) out.pnlTrend = trend
  if (indexSnapshots.length) out.indices = indexSnapshots
  if (benchmarkSeries) out.benchmark = benchmarkSeries

  if (account && opts.simworldUrl) {
    const heldCodes = account.holdings.map(h => h.fund_code).filter(Boolean)
    const fundNames = new Map(account.holdings.map(h => [h.fund_code, h.fund_name]))
    if (heldCodes.length > 0) {
      const series = await fetchFundSeries({ simworldUrl: opts.simworldUrl, fundCodes: heldCodes, asOfDate: opts.asOfDate, fundNames })
      if (series.length) out.fundSeries = series
    }
  }

  return out
}
