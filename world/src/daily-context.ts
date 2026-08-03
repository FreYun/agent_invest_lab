// Prefetched per-bot per-day data injected into the daily prompt. Goal: bot
// shouldn't have to spend round-trips re-discovering the same routine inputs
// (account snapshot, NAV trend for held funds, major index snapshots) every
// morning. World-side prefetches these and renderDailyMessage pastes formatted
// blocks into the prompt.
//
// All data is best-effort: each fetcher returns null on failure and the
// renderer just skips the corresponding block. A broken simworld upstream
// should never block a world day from running.

import { runFundCli } from './run.ts'
import { callSimworldTool } from './simworld-client.ts'

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
    cash_receivable?: number
    market_value: number
    total_value: number
  }
  holdings: HoldingRow[]
  pendingOrders: PendingOrderRow[]
  // 最近订单（pending + confirmed，order_date desc）。供 message.ts 的交易纪律块做
  // 确定性核算：载体清仓后 10 交易日再进冷却、同向 5 日一单防拆单。缺省 = 老快照 / 测试 mock，
  // 纪律块整体跳过，行为不变。
  recentOrders?: RecentOrderRow[]
}

export interface RecentOrderRow {
  fund_code: string
  order_type: 'buy' | 'sell' | string
  order_date: string
  status: string
  order_amount: number
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
  // 长周期趋势锚——方法论要求趋势在 MA60/120/200 上判，而非 MA5/20。预喂这几条，
  // 让 bot 不必每天自己再调 market_index_quote 取长窗口（否则它会偷懒拿 MA5 当扳机）。
  ma60: number | null
  ma120: number | null
  ma200: number | null
  vs_ma60_pct: number | null
  vs_ma200_pct: number | null
  // 由均线排列推出的粗趋势标签：多头排列 / 空头排列 / 纠缠（过渡）。null = 数据不足。
  trend: '多头排列' | '空头排列' | '纠缠' | null
}

export interface BenchmarkSeries {
  code: string                                    // e.g. '000300.SH'，or 'buyable-pool' for multi-fund
  name: string                                    // e.g. '沪深300'，or '买池 N 只等权 B&H' for multi-fund
  pointsByDate: Record<string, number>            // ISO date → cumulative % since runStartDate (run-start day = 0)
  latestCumulativePct: number | null              // convenience: cumulative pct at the last available trading date < asOfDate
  // since-inception 区间风险调整指标，口径完全对齐 bot 的 _compute_bot_performance
  // since_inception 行（rf=1%/252，vol/sharpe ×√252、calmar=年化收益/|mdd|）——让 daily prompt
  // 能并排打出"你 vs 不择时躺平"的 Sharpe / Calmar / 回撤 / 波动，而不仅是累计收益。
  metrics?: BenchmarkMetrics
}

export interface BenchmarkMetrics {
  return_pct: number                              // (last_nv / first_nv - 1) * 100（区间原值）
  max_drawdown_pct: number                        // peak-to-trough on the B&H net-value series
  volatility_pct: number | null                   // stdev(daily%) × √252（年化，样本 N-1），<2 个日收益 → null
  sharpe_ratio: number | null                     // (mean_daily - rf_daily) / std_daily × √252（年化）
  calmar_ratio: number | null                     // 年化收益 / |mdd|；mdd≈0 → null
  data_points: number                             // 参与计算的 NAV 点数（= 交易日数）
}

// 单基金交易费率（每天都注入 daily prompt）。purchase_fee/redeem_tiers 来自 fund_info，
// 经 fund-portfolio-mcp 的 _fund_fee_rates 标准化；mgmt+custody 是 NAV 已扣除的年化项，
// 仅作信息项展示（bot 不需要"决策时再扣"）。
export interface FundFee {
  fund_code: string
  fund_name: string
  found: boolean
  purchase_fee_pct?: number                       // BUY 时按金额收取，0.12 → 0.12%
  redeem_tiers?: { max_days: number | null; rate_pct: number }[]  // 持有天数阶梯：例 [{<7d:1.5%},{<30d:0.5%},{≥30d:0%}]
  mgmt_fee_pct_annual?: number                    // NAV 内
  custody_fee_pct_annual?: number                 // NAV 内
  sales_service_fee_pct_annual?: number           // NAV 内
  purchase_status?: string                        // open / suspended / ...
  redeem_status?: string
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
  /** 最新账户净值相对本 run 历史峰值的回撤；不同于永久保留的历史最大回撤。 */
  current_drawdown_pct?: number
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
  annualized_return_pct: number | null
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
}

export interface DailyContextData {
  account?: AccountSnapshot
  performance?: PerformanceData
  fundSeries?: FundSeries[]
  indices?: IndexQuote[]
  benchmark?: BenchmarkSeries
  fundFees?: FundFee[]
  // multi-fund bot 的可买池主题+因子+1y业绩 meta（单基金 bot 这块不渲染——它只交易自己的一只）。
  // 单条 SQL JOIN fund_info + fund_style(latest) + fund_performance(1y PIT) 拉的 lightweight 快照。
  buyablePoolMeta?: BuyablePoolMeta
}

export interface BuyablePoolMeta {
  rows: BuyablePoolMetaRow[]
  // 数据 PIT 锚点（便于 prompt 给 bot 说明"看到的 style/perf 是哪天的口径"）
  styleAsOf: string | null   // fund_style 的取数日；当前库内只有 2026-03-31 单截面，回测日 < 该日属未来信息（已知小漏）
  perf1yAsOf: string | null  // fund_performance(1y) 的取数日；回测日 < 2026-04-27 时为 null（perf 数据从那天才有快照）
}

export interface BuyablePoolMetaRow {
  fund_code: string
  fund_name: string | null
  theme: string | null          // "科技" / "新能源" / "全市场" / 多类共振如 "新能源,科技"
  scale: number | null          // 规模（亿元）
  size_style: string | null     // 大盘 / 中盘 / 小盘
  invest_style: string | null   // 价值 / 平衡 / 成长
  p1y_return_pct: number | null
  p1y_rank_pct: number | null   // 同类百分位，低 = 排名靠前（更好）
  p1y_rank_text: string | null  // "3274/3745" 格式
  p1y_mdd_pct: number | null
  p1y_sharpe: number | null
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
      ['--bot-id', opts.botId, '--run-id', opts.runId, '--limit', '60'],
      { timeoutMs: 30_000 })
    if (r.code !== 0) return null
    const d = JSON.parse(r.stdout) as Record<string, unknown>
    if (!d.success) return null
    const account = d.account as AccountSnapshot['account'] | undefined
    if (!account) return null
    const rawHoldings = Array.isArray(d.holdings) ? d.holdings as Record<string, unknown>[] : []
    // portfolio_get_my_history hands back raw fund_bot_holdings rows. Filter on
    // shares * NAV, not raw market_value, because stale active rows can have
    // near-zero shares with an old market_value that was never cleared.
    const materialHoldings: HoldingRow[] = rawHoldings
      .filter(h => {
        if (h.status !== 'active') return false
        const shares = Math.abs(Number(h.shares ?? 0))
        const nav = Number(h.latest_nav ?? 0)
        const mv = Math.abs(Number(h.market_value ?? 0))
        const effectiveMv = nav > 0 ? shares * nav : mv
        return shares > 1e-6 && effectiveMv >= 1
      })
      .map(h => ({
        fund_code: String(h.fund_code ?? ''),
        fund_name: String(h.fund_name ?? ''),
        shares: Number(h.shares ?? 0),
        amount_invested: Number(h.amount_invested ?? 0),
        latest_nav: Number(h.latest_nav ?? 0),
        market_value: Number(h.market_value ?? 0),
        unrealized_pnl_pct: Number(h.unrealized_pnl_pct ?? 0),
        weight: 0,
        holding_days: Number(h.holding_days ?? 0),
        entry_date: String(h.entry_date ?? ''),
      }))
    const marketValue = materialHoldings.reduce((sum, h) => sum + h.market_value, 0)
    const cashAvailable = Number(account.cash_available ?? 0)
    const cashInTransit = Number(account.cash_in_transit ?? 0)
    const cashReceivable = Number(account.cash_receivable ?? 0)
    const accountForPrompt: AccountSnapshot['account'] = {
      ...account,
      cash_available: cashAvailable,
      cash_in_transit: cashInTransit,
      cash_receivable: cashReceivable,
      market_value: marketValue,
      total_value: cashAvailable + cashInTransit + cashReceivable + marketValue,
    }
    const totalValue = Number(accountForPrompt.total_value ?? 0)
    const holdings: HoldingRow[] = materialHoldings.map(h => ({
      ...h,
      weight: totalValue > 0 ? h.market_value / totalValue : 0,
    }))
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
    const recentOrders: RecentOrderRow[] = rawOrders
      .filter(o => o.status === 'pending' || o.status === 'confirmed')
      .map(o => ({
        fund_code: String(o.fund_code ?? ''),
        order_type: String(o.order_type ?? ''),
        order_date: String(o.order_date ?? ''),
        status: String(o.status ?? ''),
        order_amount: Number(o.order_amount ?? 0),
      }))
    return { asOfDate: opts.asOfDate, account: accountForPrompt, holdings, pendingOrders, recentOrders }
  } catch {
    return null
  }
}

// ============================================================================
// Full performance payload (summary / interval metrics / trades / closed
// positions) via portfolio_get_my_performance CLI.
//
// Returns null if as_of_date precedes the first snapshot (Day 1; no perf
// history yet) or if the CLI errors out. Caller treats null as "no perf
// block to render".
// ============================================================================

async function fetchPerformance(opts: {
  fundMcpCli: string
  botId: string
  runId: string
  asOfDate: string
}): Promise<PerformanceData | null> {
  try {
    const r = await runFundCli(opts.fundMcpCli, 'get_my_performance', [
      '--bot-id', opts.botId,
      '--run-id', opts.runId,
      '--as-of-date', opts.asOfDate,
    ], { timeoutMs: 30_000 })
    if (r.code !== 0) return null
    const d = JSON.parse(r.stdout) as Record<string, unknown>
    if (d.success !== true) return null

    const summary = parseSummary(d.summary)
    const trades = parseTrades(d.trades_summary)
    const intervals = parseIntervalMetrics(d.interval_metrics)
    const completedPositions = parseCompletedPositions(d.completed_positions)

    return { asOfDate: opts.asOfDate, summary, trades, intervals, completedPositions }
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
    current_drawdown_pct: Number(s.current_drawdown_pct ?? 0),
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
      annualized_return_pct: mr.annualized_return_pct === null || mr.annualized_return_pct === undefined ? null : Number(mr.annualized_return_pct),
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
  let raw: { items?: { 基金代码?: string; 是否可用?: boolean; 净值记录?: { 交易日期?: string; 复权单位净值?: number; 日收益率?: number }[] }[] } | null = null
  try {
    raw = await callSimworldTool(opts.simworldUrl, 'fund_nav', {
      fund_codes: opts.fundCodes,
      simulated_datetime: simDt,
      start_date: startDate,
      end_date: endDate,
    }, { timeoutMs: 60_000 }) as { items?: { 基金代码?: string; 是否可用?: boolean; 净值记录?: { 交易日期?: string; 复权单位净值?: number; 日收益率?: number }[] }[] } | null
  } catch {
    return []
  }
  for (const item of raw?.items ?? []) {
    try {
      if (!item || !item['是否可用'] || !Array.isArray(item['净值记录'])) continue
      const code = String(item['基金代码'] ?? '').trim()
      if (!code) continue
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
// Index snapshots — latest close + 短均线(MA5/MA20) + 长趋势锚(MA60/120/200) +
// 趋势标签。方法论要求趋势在长均线上判，所以拉一个能算 MA200 的窗口（~320 自然日
// ≈ 220 交易日），让 bot 直接拿到长趋势，不必为判方向再自己调 market_index_quote。
// ============================================================================

// 由收盘 vs MA60/120/200 的排列推趋势标签。需要至少 MA60；MA120/200 缺失时按
// 已有的均线宽松判断（多头=价在所有可得长均线之上且短长依次递减；空头反之）。
function classifyTrend(close: number, ma60: number | null, ma120: number | null, ma200: number | null): IndexQuote['trend'] {
  if (ma60 === null) return null
  const longMas = [ma60, ma120, ma200].filter((x): x is number => x !== null)
  const bullStack = longMas.every((m, i) => i === 0 || longMas[i - 1] >= m)  // MA60>=MA120>=MA200
  const bearStack = longMas.every((m, i) => i === 0 || longMas[i - 1] <= m)
  if (close > ma60 && bullStack) return '多头排列'
  if (close < ma60 && bearStack) return '空头排列'
  return '纠缠'
}

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
      // ~220 交易日窗口，够算 MA200；end 收到 asOfDate（PIT 由 simulated_datetime 兜底）。
      start_date: computeWindowStart(opts.asOfDate, 320),
      end_date: opts.asOfDate,
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
      const maOf = (n: number) => closes.length >= n ? mean(closes.slice(-n).map(r => r.close)) : null
      const ma5 = mean(closes.slice(-5).map(r => r.close))
      const ma20 = maOf(20)
      const ma60 = maOf(60)
      const ma120 = maOf(120)
      const ma200 = maOf(200)
      byCode.set(code, {
        code,
        name: opts.indices.find(i => i.code === code)?.name ?? code,
        latest_date: last.date,
        latest_close: last.close,
        ma5,
        ma20,
        vs_ma5_pct: pctChange(ma5, last.close),
        vs_ma20_pct: pctChange(ma20, last.close),
        ma60,
        ma120,
        ma200,
        vs_ma60_pct: pctChange(ma60, last.close),
        vs_ma200_pct: pctChange(ma200, last.close),
        trend: classifyTrend(last.close, ma60, ma120, ma200),
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
// Benchmark cumulative return — pulls the benchmark over [runStartDate,
// asOfDate-1] and computes each trading day's pct change vs the run-start day's
// price. The PnL trend renderer joins on date so each row gets "you vs benchmark
// since run start" — bot can immediately see whether it's adding alpha or just
// riding beta.
//
// Two fetchers:
//   - fetchIndexBenchmark: pulls an index series via market_index_quote
//     (default 沪深300, useful when buyable pool is unspecified or covers
//     broad-market ETFs).
//   - fetchFundPoolBenchmark: pulls fund NAV via fund_nav for the buyable池
//     codes, equal-weights them, and uses the composite as the benchmark.
//     This is THE benchmark when the system is testing single-fund timing —
//     bot's alpha = your timing vs naive B&H of the exact fund(s) it's allowed
//     to trade. For bot7 (buyable池 = [016729]) this collapses to "you vs
//     016729 NAV B&H" which is exactly the right reference.
// ============================================================================

const DEFAULT_BENCHMARK = { code: '000300.SH', name: '沪深300' }

// rf / 年化口径常量——必须与 fund-portfolio-mcp server.py 的 _BOT_PERF_* 完全一致，
// 否则 bot 的区间 Sharpe 和这里算的基准 Sharpe 就不是同口径，对比失真。
const BENCH_RF_ANNUAL_PCT = 1
const BENCH_TRADING_DAYS_PER_YEAR = 252
const BENCH_RF_DAILY_PCT = BENCH_RF_ANNUAL_PCT / BENCH_TRADING_DAYS_PER_YEAR
const BENCH_ANN_FACTOR = Math.sqrt(BENCH_TRADING_DAYS_PER_YEAR)  // √252，对齐 _BOT_PERF_ANN_FACTOR

// 样本标准差（N-1 分母）——对齐 server.py:_stdev。<2 点返回 0。
function sampleStdev(values: number[]): number {
  const n = values.length
  if (n < 2) return 0
  const mean = values.reduce((a, b) => a + b, 0) / n
  const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1)
  return Math.sqrt(variance)
}

// peak-to-trough（%）——对齐 server.py:_calc_max_drawdown。
function maxDrawdownPct(navList: number[]): number {
  if (navList.length === 0) return 0
  let peak = navList[0]
  let maxDd = 0
  for (const nav of navList) {
    if (nav > peak) peak = nav
    const dd = ((nav - peak) / peak) * 100
    if (dd < maxDd) maxDd = dd
  }
  return maxDd
}

// 从 B&H 净值序列（norm = nav/base，runStartDate 起锚定 1.0）算 since-inception 区间指标。
// 口径完全对齐 bot 的 _compute_bot_performance since_inception（统一年化口径）：
//   - return_pct = (last/first - 1)*100（区间原值）
//   - 日收益序列首日补 0（对齐 bot Day-1 snapshot 的 daily_return_pct=0：prev_total=initial→0），
//     使日收益点数 = 交易日数 N，mean/std 与 bot 同口径
//   - volatility = stdev(daily%) × √252；sharpe = (mean - rf_daily)/std × √252；
//     calmar = 年化收益/|mdd|（年化基数 = 净值点数 - 1，对齐 server.py）
function computeBenchmarkMetrics(navSeries: number[]): BenchmarkMetrics | undefined {
  if (navSeries.length < 2) return undefined
  const first = navSeries[0]
  const last = navSeries[navSeries.length - 1]
  if (!(first > 0)) return undefined
  const return_pct = (last / first - 1) * 100
  const mdd = maxDrawdownPct(navSeries)
  // 首日 0% + 后续逐日收益 → N 个点，与 bot since_inception 的 daily_return_pct 序列对齐。
  const daily: number[] = [0]
  for (let i = 1; i < navSeries.length; i++) {
    if (navSeries[i - 1] > 0) daily.push((navSeries[i] / navSeries[i - 1] - 1) * 100)
  }
  let volatility_pct: number | null = null
  let sharpe_ratio: number | null = null
  if (daily.length >= 2) {
    const mean = daily.reduce((a, b) => a + b, 0) / daily.length
    const std = sampleStdev(daily)
    volatility_pct = std * BENCH_ANN_FACTOR
    sharpe_ratio = std > 1e-9 ? (mean - BENCH_RF_DAILY_PCT) / std * BENCH_ANN_FACTOR : null
  }
  // 年化收益（仅用于 calmar；1+r<=0 时幂运算无意义 → null）
  const span = navSeries.length - 1
  const annBase = 1 + return_pct / 100
  const annReturn = span > 0 && annBase > 0
    ? (Math.pow(annBase, BENCH_TRADING_DAYS_PER_YEAR / span) - 1) * 100
    : null
  const calmar_ratio = Math.abs(mdd) > 1e-9 && annReturn !== null ? annReturn / Math.abs(mdd) : null
  return { return_pct, max_drawdown_pct: mdd, volatility_pct, sharpe_ratio, calmar_ratio, data_points: navSeries.length }
}

export interface BenchmarkDailyState {
  lastDate: string
  lastDayMovePct: number | null            // (close[n]-close[n-1])/close[n-1]*100；无前一日 → null
  drawdownFromRecentHighPct: number | null // <=0；窗口内 peak→trough，复用 maxDrawdownPct
}

/** 从收盘序列（升序）算崩盘判定的两个标量。纯函数，便于单测。 */
export function computeCrashSignalFromCloses(closes: { date: string; close: number }[]): BenchmarkDailyState | null {
  if (closes.length === 0) return null
  const last = closes[closes.length - 1]
  const prev = closes.length >= 2 ? closes[closes.length - 2] : undefined
  const lastDayMovePct = prev && prev.close > 0 ? (last.close - prev.close) / prev.close * 100 : null
  const drawdownFromRecentHighPct = maxDrawdownPct(closes.map(c => c.close))
  return { lastDate: last.date, lastDayMovePct, drawdownFromRecentHighPct }
}

function shiftIsoDaysBack(iso: string, n: number): string {
  const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() - n)
  return d.toISOString().slice(0, 10)
}

/** 拉基准指数近 lookbackDays 交易日收盘（PIT：end_date = asOfDate 前一交易日），
 *  返回崩盘判定标量。复用 fetchIndexBenchmark 的 market_index_quote 路径。best-effort。 */
export async function fetchBenchmarkDailyState(opts: {
  simworldUrl: string; code: string; asOfDate: string; lookbackDays?: number
}): Promise<BenchmarkDailyState | null> {
  const lookback = opts.lookbackDays ?? 60
  const startDate = shiftIsoDaysBack(opts.asOfDate, lookback * 2) // 日历日预估，够覆盖 lookback 交易日
  const simDt = `${opts.asOfDate} 15:00:00`
  try {
    const raw = await callSimworldTool(opts.simworldUrl, 'market_index_quote', {
      market: 'cn',
      symbols: [opts.code],
      simulated_datetime: simDt,
      start_date: startDate,
      end_date: priorDay(opts.asOfDate),
    }) as { items?: { 是否可用?: boolean; 行情记录?: { 日期?: string; 收盘?: number }[] }[] } | null
    const item = raw?.items?.[0]
    if (!item || !item['是否可用'] || !Array.isArray(item['行情记录'])) return null
    const closes = item['行情记录']
      .map(r => ({ date: String(r['日期'] ?? '').slice(0, 10), close: Number(r['收盘'] ?? 0) }))
      .filter(r => r.date && r.close > 0)
    return computeCrashSignalFromCloses(closes)
  } catch {
    return null
  }
}

async function fetchIndexBenchmark(opts: {
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
    const navSeries: number[] = []
    for (const r of closes) {
      pointsByDate[r.date] = ((r.close - base) / base) * 100
      navSeries.push(r.close / base)
    }
    const lastDate = closes[closes.length - 1].date
    return {
      code: opts.code,
      name: opts.name,
      pointsByDate,
      latestCumulativePct: pointsByDate[lastDate] ?? null,
      metrics: computeBenchmarkMetrics(navSeries),
    }
  } catch {
    return null
  }
}

// 用 buyable 池基金 NAV 等权合成基准。单只 → 退化为该基金 B&H；多只 → 各自从
// runStartDate 锚定 = 1，每天取算术平均（缺值的基金当天跳过该基金，不在分母里
// 灌零——保证 sparse 日期表现真实）。这是"如果你完全不择时就这么躺平"的最直接
// 对比，bot 看 alpha 列就知道每天的择时是赚还是亏。
//
// 系统层工具调用红线：全池 / 大池数据必须批量拉取。禁止在这里按 fund_code 循环
// 打 MCP（865 只池子会放大成 865 个 session）；要么一次 fund_nav(fund_codes=[...])，
// 要么走 fund-portfolio-mcp 的批量 CLI / 本地 SQL。少量持仓基金的展示逻辑不适用此限制。
async function fetchFundPoolBenchmark(opts: {
  simworldUrl: string
  fundCodes: string[]
  runStartDate: string
  asOfDate: string
}): Promise<BenchmarkSeries | null> {
  if (opts.fundCodes.length === 0) return null
  const simDt = `${opts.asOfDate} 15:00:00`
  const perFundNorm: Record<string, Record<string, number>> = {}
  const perFundName: Record<string, string> = {}
  const allDates = new Set<string>()
  let raw: { items?: { 基金代码?: string; 基金名称?: string; 是否可用?: boolean; 净值记录?: { 交易日期?: string; 复权单位净值?: number }[] }[] } | null = null
  try {
    raw = await callSimworldTool(opts.simworldUrl, 'fund_nav', {
      fund_codes: opts.fundCodes,
      simulated_datetime: simDt,
      start_date: opts.runStartDate,
      end_date: priorDay(opts.asOfDate),
    }, { timeoutMs: 60_000 }) as { items?: { 基金代码?: string; 基金名称?: string; 是否可用?: boolean; 净值记录?: { 交易日期?: string; 复权单位净值?: number }[] }[] } | null
  } catch {
    return null
  }
  for (const item of raw?.items ?? []) {
    try {
      if (!item || !item['是否可用'] || !Array.isArray(item['净值记录'])) continue
      const code = String(item['基金代码'] ?? '').trim()
      if (!code) continue
      const series = item['净值记录']
        .map(r => ({ date: String(r['交易日期'] ?? '').slice(0, 10), nav: Number(r['复权单位净值'] ?? 0) }))
        .filter(r => r.date && r.nav > 0)
      if (series.length === 0) continue
      const base = series[0].nav
      const norm: Record<string, number> = {}
      for (const r of series) {
        norm[r.date] = r.nav / base
        allDates.add(r.date)
      }
      perFundNorm[code] = norm
      perFundName[code] = item['基金名称'] ?? code
    } catch { /* skip fund on error */ }
  }
  const present = Object.keys(perFundNorm)
  if (present.length === 0) return null
  const sortedDates = Array.from(allDates).sort()
  const pointsByDate: Record<string, number> = {}
  const navSeries: number[] = []  // 等权合成净值（单只 → 该基金 norm），喂给区间指标
  for (const d of sortedDates) {
    let sum = 0, n = 0
    for (const code of present) {
      const v = perFundNorm[code][d]
      if (v !== undefined) { sum += v; n++ }
    }
    if (n === 0) continue
    const compositeNv = sum / n
    pointsByDate[d] = (compositeNv - 1) * 100
    navSeries.push(compositeNv)
  }
  const lastDate = sortedDates[sortedDates.length - 1]
  const name = present.length === 1
    ? `${perFundName[present[0]]} B&H`
    : `买池 ${present.length} 只等权 B&H`
  const code = present.length === 1 ? present[0] : 'buyable-pool'
  return { code, name, pointsByDate, latestCumulativePct: pointsByDate[lastDate] ?? null, metrics: computeBenchmarkMetrics(navSeries) }
}

// 调 fund-portfolio-mcp 的 get_fund_fees CLI 子命令读 fund_info 费率字段。fee
// 表是静态/半静态——每天调一次很便宜，比 simworld fund_rate 一只一只 HTTP 来得快。
async function fetchFundFees(opts: {
  fundMcpCli: string
  fundCodes: string[]
}): Promise<FundFee[]> {
  if (opts.fundCodes.length === 0) return []
  try {
    const r = await runFundCli(opts.fundMcpCli, 'get_fund_fees', ['--fund-codes', opts.fundCodes.join(',')], { timeoutMs: 15_000 })
    if (r.code !== 0) return []
    const parsed = JSON.parse(r.stdout) as { success?: boolean; fees?: FundFee[] }
    return Array.isArray(parsed?.fees) ? parsed.fees : []
  } catch {
    return []
  }
}

// 多基金 bot 的可买池 meta：主题分类 + 市值/风格因子 + 1y 业绩（含同类排名）。
// 单条 JOIN SQL，比让 bot 自己 N 次 get_fund_detail 高效得多。
async function fetchBuyablePoolMeta(opts: {
  fundMcpCli: string
  fundCodes: string[]
  asOfDate: string
}): Promise<BuyablePoolMeta | null> {
  if (opts.fundCodes.length === 0) return null
  try {
    const r = await runFundCli(opts.fundMcpCli, 'get_pool_meta',
      ['--fund-codes', opts.fundCodes.join(','), '--as-of-date', opts.asOfDate],
      { timeoutMs: 30_000 })
    if (r.code !== 0) return null
    const parsed = JSON.parse(r.stdout) as {
      success?: boolean
      pool?: BuyablePoolMetaRow[]
      style_as_of?: string | null
      perf_1y_as_of?: string | null
    }
    if (!parsed?.success || !Array.isArray(parsed.pool)) return null
    return {
      rows: parsed.pool,
      styleAsOf: parsed.style_as_of ?? null,
      perf1yAsOf: parsed.perf_1y_as_of ?? null,
    }
  } catch {
    return null
  }
}

// ============================================================================
// Public entry point.
// ============================================================================

export interface FetchDailyContextOptions {
  runId: string
  botId: string
  asOfDate: string
  fundMcpCli: string | undefined
  simworldUrl: string | undefined
  // First trading day of the run — anchor for benchmark cumulative %. If
  // omitted, benchmark block is skipped (can't compute "since when?" without
  // a reference point).
  runStartDate?: string
  indices?: { code: string; name: string }[]
  // Caller-pinned benchmark INDEX (legacy override). Ignored when
  // buyableFundCodes is provided — in that case fundPoolBenchmark wins.
  benchmark?: { code: string; name: string }
  // 当 buyable 池可见时，benchmark 改用池内基金 NAV 等权 B&H——单基金运行（如
  // bot7 只交易 016729）下 bot 直接看到"你 vs 不择时躺平"的 alpha 列。
  // 不传则回退到 INDEX benchmark（默认 沪深300）。
  buyableFundCodes?: string[]
  // 【费率块】的精简入参：只查这些基金的申购/赎回费率，塞进 daily prompt。
  // 传空数组或不传 → 回退到 buyableFundCodes 全池（老行为）。多基金 bot（bot101/102/103）
  // 走"持仓 + 日度报告推荐载体"~10 只子集，比全池 759 只每天省 ~30k tokens 输入。
  // 单基金 bot 不传即可（池就 1 只，无差别）。上游代码在 run.ts 里 pre-compute。
  relevantFundCodes?: string[]
}

export async function fetchDailyContext(opts: FetchDailyContextOptions): Promise<DailyContextData> {
  const indices = opts.indices ?? DEFAULT_INDEX_CODES
  const out: DailyContextData = {}

  // Run portfolio (CLI) + simworld (HTTP) calls in parallel — they're independent.
  // Account snapshot also feeds fundSeries (knows which codes are held), so we
  // chain fundSeries after account; everything else can run concurrently with it.
  const accountPromise: Promise<AccountSnapshot | null> = opts.fundMcpCli
    ? fetchAccountSnapshot({ fundMcpCli: opts.fundMcpCli, botId: opts.botId, runId: opts.runId, asOfDate: opts.asOfDate })
    : Promise.resolve(null)
  const perfPromise: Promise<PerformanceData | null> = opts.fundMcpCli
    ? fetchPerformance({ fundMcpCli: opts.fundMcpCli, botId: opts.botId, runId: opts.runId, asOfDate: opts.asOfDate })
    : Promise.resolve(null)
  const indexPromise: Promise<IndexQuote[]> = opts.simworldUrl
    ? fetchIndexSnapshots({ simworldUrl: opts.simworldUrl, asOfDate: opts.asOfDate, indices })
    : Promise.resolve([])
  // Benchmark：buyable 池有则用池内 NAV 等权（"你 vs 不择时"，单基金运行下=该基金 B&H）；
  // 否则回退到指数 benchmark（默认 沪深300）。两条路径都需要 runStartDate 锚定。
  const benchmarkIndex = opts.benchmark ?? DEFAULT_BENCHMARK
  const benchmarkPromise: Promise<BenchmarkSeries | null> = (() => {
    if (!opts.simworldUrl || !opts.runStartDate) return Promise.resolve(null)
    if (opts.buyableFundCodes && opts.buyableFundCodes.length > 0) {
      return fetchFundPoolBenchmark({
        simworldUrl: opts.simworldUrl,
        fundCodes: opts.buyableFundCodes,
        runStartDate: opts.runStartDate,
        asOfDate: opts.asOfDate,
      })
    }
    return fetchIndexBenchmark({
      simworldUrl: opts.simworldUrl,
      code: benchmarkIndex.code,
      name: benchmarkIndex.name,
      runStartDate: opts.runStartDate,
      asOfDate: opts.asOfDate,
    })
  })()
  // Fund fee schedule — fees rarely change, but we re-fetch every day for PIT
  // correctness (sqlite read via CLI, cost tiny). 优先使用 relevantFundCodes
  // 子集（持仓 + 日度报告推荐载体，通常 ≤10 只），显著砍掉 daily prompt 里的费率块体积；
  // 未传子集 / 子集为空 → 回退全 buyable 池（老行为，避免破坏 caller）。
  const feeFundCodes: string[] = (opts.relevantFundCodes && opts.relevantFundCodes.length > 0)
    ? opts.relevantFundCodes
    : (opts.buyableFundCodes ?? [])
  const feesPromise: Promise<FundFee[]> = (opts.fundMcpCli && feeFundCodes.length > 0)
    ? fetchFundFees({ fundMcpCli: opts.fundMcpCli, fundCodes: feeFundCodes })
    : Promise.resolve([])
  // 可买池 meta（主题/因子/1y业绩）——给 multi-fund bot 在 prompt 里渲染【可买池主题分布】。
  // 单基金 bot 不渲染这块（message.ts 按 botKind 自决），但我们还是 fetch 一下，因为：
  // (1) 池子小时 SQL ~10ms，几乎无开销；(2) 把"渲染策略"留给 message.ts，daily-context 不感知 bot 语义。
  const poolMetaPromise: Promise<BuyablePoolMeta | null> = (opts.fundMcpCli && opts.buyableFundCodes && opts.buyableFundCodes.length > 0)
    ? fetchBuyablePoolMeta({ fundMcpCli: opts.fundMcpCli, fundCodes: opts.buyableFundCodes, asOfDate: opts.asOfDate })
    : Promise.resolve(null)

  const [account, perf, indexSnapshots, benchmarkSeries, fundFees, poolMeta] = await Promise.all([accountPromise, perfPromise, indexPromise, benchmarkPromise, feesPromise, poolMetaPromise])
  if (account) out.account = account
  if (perf) out.performance = perf
  if (indexSnapshots.length) out.indices = indexSnapshots
  if (benchmarkSeries) out.benchmark = benchmarkSeries
  if (fundFees.length) out.fundFees = fundFees
  if (poolMeta && poolMeta.rows.length) out.buyablePoolMeta = poolMeta

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
