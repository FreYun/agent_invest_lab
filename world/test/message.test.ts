import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderDailyMessage, weekdayOf } from '../src/message.ts'
import { dayDir } from '../src/paths.ts'

function tmpWorldWithOverview(date: string, overviewMd: string): string {
  const w = mkdtempSync(join(tmpdir(), 'msg-'))
  mkdirSync(dayDir(w, date), { recursive: true })
  writeFileSync(join(dayDir(w, date), 'overview.md'), overviewMd)
  return w
}

test('weekdayOf returns English weekday for a UTC date', () => {
  assert.equal(weekdayOf('2024-03-15'), 'Friday')
  assert.equal(weekdayOf('2024-03-18'), 'Monday')
})

test('first-day message: full cold-start rules, mcp-only tool policy, points to simworld-data + portfolio_*; no inline overview', () => {
  const w = tmpWorldWithOverview('2024-03-15', '上证 +1.2%，半导体领涨。')
  const m = renderDailyMessage({ worldRoot: w, date: '2024-03-15', isFirstDay: true, quotesPath: '/abs/world/days/2024-03-15/quotes.json', journalRelPath: 'memory/trading/journal.md' })
  assert.match(m, /当前世界日期：2024-03-15（Friday）/)
  assert.match(m, /discover_tools/)
  assert.match(m, /mem0_search/)
  assert.match(m, /mem0_add/)
  assert.match(m, /mcp__\*/)
  assert.match(m, /文件读写、web_fetch、bash/)
  // 行情走 simworld-data；账户走 portfolio_* 自助查
  assert.match(m, /simworld-data/)
  assert.match(m, /portfolio_get_my_history/)
  assert.match(m, /portfolio_place_buy_order/)
  // No file-IO references
  assert.doesNotMatch(m, /memory\/trading\/journal\.md/)
  // 静态 overview 不再注入
  assert.doesNotMatch(m, /上证 \+1\.2%，半导体领涨。/)
  rmSync(w, { recursive: true, force: true })
})

test('non-first-day message: concise rules only, no AUTONOMY / playbook / day-type banner', () => {
  const w = tmpWorldWithOverview('2024-03-18', '上证 -0.4%。')
  const full = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.ok(brief.length < full.length)
  assert.match(brief, /当前世界日期：2024-03-18（Monday）/)
  assert.match(brief, /规则同前/)
  assert.match(brief, /mem0_search/)
  assert.match(brief, /mem0_add/)
  assert.match(brief, /simworld-data/)
  // AUTONOMY 已被删除（属于 embedded strategy：规定看什么、平静日默认 HOLD、大事多研究）；
  // 节奏由 bot 自己 Day 1 写下的策略决定，prompt 不再注入任何节奏指引。
  assert.doesNotMatch(brief, /今天的节奏由你定/)
  assert.doesNotMatch(brief, /今天是普通交易日/)
  assert.doesNotMatch(brief, /今天是研究日/)
  // No inline overview, no journal reference
  assert.doesNotMatch(brief, /上证 -0\.4%。/)
  assert.doesNotMatch(brief, /memory\/trading\/journal\.md/)
  rmSync(w, { recursive: true, force: true })
})

test('first-day broadcasts the per-run curated buyable funds list; later days do not (bot self-queries via portfolio_get_buyable_funds)', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const codes = ['510300', '159915', '002611']
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md', buyableFundCodes: codes })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md', buyableFundCodes: codes })
  // Day 1: explicit list of codes appears
  for (const c of codes) assert.match(first, new RegExp(c))
  assert.match(first, /本轮可买基金/)
  // Day 2+: not re-broadcast
  assert.doesNotMatch(brief, /本轮可买基金/)
  for (const c of codes) assert.doesNotMatch(brief, new RegExp(c))
  // Day 1 without list (e.g., world.yaml didn't set buyable_fund_codes): block omitted
  const firstNoList = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.doesNotMatch(firstNoList, /本轮可买基金/)
  rmSync(w, { recursive: true, force: true })
})

test('first-day has no AUTONOMY block (cold start needs explicit handholding, not self-pacing)', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.doesNotMatch(first, /今天的节奏由你定/)
  assert.doesNotMatch(first, /今天是普通交易日/)
  assert.doesNotMatch(first, /今天是研究日/)
  rmSync(w, { recursive: true, force: true })
})

test('first-day prompt does NOT embed any strategy framework (regime / timing_stance / 盈亏档 / 动能档 / 决策矩阵 / mem0 key 约定)', () => {
  // Guard test: 我们决定让 bot 完全自由发挥，不预设择时框架。任何把 prompt 拉回
  // "Step 1 市场观点 / 盈亏档动能档 / 强制 mem0 key" 这套硬约束的改动应该让这个 test 立刻挂。
  const w = tmpWorldWithOverview('2024-03-15', 'overview')
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-15', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  // Step 1 / regime / timing_stance 类标签
  assert.doesNotMatch(first, /Step 1：市场观点/)
  assert.doesNotMatch(first, /Step 2：单基金择时/)
  assert.doesNotMatch(first, /六维证据/)
  assert.doesNotMatch(first, /regime/)
  assert.doesNotMatch(first, /timing_stance/)
  assert.doesNotMatch(first, /aggressively_add/)
  assert.doesNotMatch(first, /risk_off/)
  // 盈亏 / 动能 分档框架
  assert.doesNotMatch(first, /盈亏档/)
  assert.doesNotMatch(first, /动能档/)
  assert.doesNotMatch(first, /大幅盈利/)
  assert.doesNotMatch(first, /高点回撤/)
  assert.doesNotMatch(first, /加速向上/)
  assert.doesNotMatch(first, /已转弱/)
  assert.doesNotMatch(first, /决策矩阵/)
  // 强制 mem0 key 约定
  assert.doesNotMatch(first, /market_view:\{/)
  assert.doesNotMatch(first, /action:\{/)
  // 执行顺序 / 硬阈值 这种结构性引导
  assert.doesNotMatch(first, /今日执行顺序/)
  assert.doesNotMatch(first, /硬阈值/)
  rmSync(w, { recursive: true, force: true })
})

test('prefetched daily context blocks (account / pnl / held NAV / indices) get injected when present, omitted when absent', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  // Without dailyContext: no injected blocks at all.
  const noCtx = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.doesNotMatch(noCtx, /账户快照/)
  assert.doesNotMatch(noCtx, /近 \d+ 个交易日 PnL 走势/)
  assert.doesNotMatch(noCtx, /持仓基金近 20 交易日 NAV/)
  assert.doesNotMatch(noCtx, /主要指数（5 个/)
  assert.doesNotMatch(noCtx, /整体绩效/)
  assert.doesNotMatch(noCtx, /区间业绩/)

  // With all four blocks populated.
  const withCtx = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md',
    dailyContext: {
      account: {
        asOfDate: '2024-03-18',
        account: { initial_capital: 1_000_000, cash_available: 700_000, cash_in_transit: 100_000, market_value: 200_000, total_value: 1_000_000 },
        holdings: [
          { fund_code: '510300', fund_name: '沪深300ETF', shares: 89000, amount_invested: 200_000, latest_nav: 2.25, market_value: 200_250, unrealized_pnl_pct: 0.12, weight: 0.2, holding_days: 5, entry_date: '2024-03-13' },
        ],
        pendingOrders: [
          { order_id: 42, fund_code: '510300', order_type: 'buy', order_date: '2024-03-18', order_amount: 100_000, reference_nav: 2.24 },
        ],
      },
      pnlTrend: [
        { date: '2024-03-15', total_value: 999_500, net_value: 0.9995, daily_return_pct: -0.05, cumulative_return_pct: -0.05, max_drawdown_pct: -0.05 },
        { date: '2024-03-16', total_value: 1_000_200, net_value: 1.0002, daily_return_pct: 0.07, cumulative_return_pct: 0.02, max_drawdown_pct: -0.05 },
      ],
      performance: {
        asOfDate: '2024-03-18',
        summary: {
          first_date: '2024-03-15', last_date: '2024-03-16', trading_days: 2,
          initial_capital: 1_000_000, latest_total_value: 1_000_200, latest_net_value: 1.0002,
          total_return_pct: 0.02, annualized_return_pct: 2.5, max_drawdown_pct: -0.05, max_drawdown_date: '2024-03-15',
          volatility_pct_annualized: 1.2, sharpe_ratio_rf0: 0.45,
          win_days: 1, loss_days: 1, flat_days: 0,
          best_day: { date: '2024-03-16', return_pct: 0.07 },
          worst_day: { date: '2024-03-15', return_pct: -0.05 },
        },
        trades: { buy_count: 1, sell_count: 0, total_buy_amount: 200_000, total_sell_proceeds: 0, total_fees: 30, round_trips_count: 0 },
        intervals: {
          as_of_perf_date: '2024-03-16', rf_annual_pct: 1.8, rf_daily_pct: 0.007143, trading_days_per_year: 252,
          rows: [
            { period: '1m', return_pct: 0.02, max_drawdown_pct: -0.05, volatility_pct: 0.18, sharpe_ratio: 0.0033, calmar_ratio: 0.4, data_points: 2, window_target_days: 21, fallback: true },
            { period: 'since_inception', return_pct: 0.02, max_drawdown_pct: -0.05, volatility_pct: 0.18, sharpe_ratio: 0.0033, calmar_ratio: 0.4, data_points: 2, window_target_days: null, fallback: false },
          ],
        },
        completedPositions: [
          { fund_code: '510880', fund_name: '红利ETF', entry_date: '2024-02-01', exit_date: '2024-03-05', holding_days: 33, total_invested: 100_000, total_proceeds: 105_500, net_pl: 5_500, return_pct: 5.5, fees: 25 },
        ],
        dailySeries: [],
      },
      benchmark: {
        code: '000300.SH', name: '沪深300',
        pointsByDate: { '2024-03-15': -0.30, '2024-03-16': 0.10 },
        latestCumulativePct: 0.10,
      },
      fundSeries: [
        {
          fund_code: '510300', fund_name: '沪深300ETF',
          nav_series: [
            { date: '2024-03-14', nav: 2.24, daily_return_pct: 0.5 },
            { date: '2024-03-15', nav: 2.25, daily_return_pct: 0.4 },
          ],
          return_1m_pct: 1.2, return_3m_pct: 3.4,
        },
      ],
      indices: [
        { code: '000300.SH', name: '沪深300', latest_date: '2024-03-15', latest_close: 3500.12, ma5: 3490.00, ma20: 3470.00, vs_ma5_pct: 0.29, vs_ma20_pct: 0.87 },
      ],
    },
  })
  // Account block — initial capital + holding row + pending order
  assert.match(withCtx, /账户快照（今日 settle 后）/)
  assert.match(withCtx, /可用现金 ¥700,?000|¥700000/)
  assert.match(withCtx, /510300/)
  assert.match(withCtx, /沪深300ETF/)
  assert.match(withCtx, /待结算订单/)
  // PnL trend block — header mentions benchmark, summary line states alpha
  assert.match(withCtx, /近 2 个交易日 PnL 走势/)
  assert.match(withCtx, /基准 = 沪深300（000300\.SH）/)
  assert.match(withCtx, /2024-03-15/)
  assert.match(withCtx, /2024-03-16/)
  // Summary anchored on last trend point (2024-03-16): you +0.02% vs benchmark +0.10% → 跑输 -0.08 pp
  assert.match(withCtx, /截至 2024-03-16/)
  assert.match(withCtx, /你累计 \+0\.02%/)
  assert.match(withCtx, /沪深300 累计 \+0\.10%/)
  assert.match(withCtx, /跑输 -0\.08% pp/)
  // Per-row 超额 column present
  assert.match(withCtx, /超额\(pp\)/)
  // Performance summary block — every metric we have should be in there
  assert.match(withCtx, /整体绩效/)
  assert.match(withCtx, /累计收益 \+0\.02%/)
  assert.match(withCtx, /年化 \+2\.50%/)
  assert.match(withCtx, /Sharpe \(rf=0\) 0\.45/)
  assert.match(withCtx, /最大回撤 -0\.05%/)
  assert.match(withCtx, /日级胜负：1 胜 \/ 1 负 \/ 0 平/)
  assert.match(withCtx, /最佳单日：2024-03-16 \+0\.07%/)
  // Trades summary
  assert.match(withCtx, /交易统计/)
  assert.match(withCtx, /买入 1 笔.*总金额 ¥200,?000/)
  // Completed positions
  assert.match(withCtx, /已平仓持仓/)
  assert.match(withCtx, /510880.*红利ETF.*2024-02-01.*2024-03-05/)
  assert.match(withCtx, /\+5\.50%/)
  // Interval metrics block — both periods + fallback marker
  assert.match(withCtx, /区间业绩（截至 2024-03-16/)
  assert.match(withCtx, /1m\s+\+0\.02%/)
  assert.match(withCtx, /since_inception\s+\+0\.02%/)
  assert.match(withCtx, /窗口数据不足/)
  // Held NAV block
  assert.match(withCtx, /持仓基金近 20 交易日 NAV/)
  assert.match(withCtx, /近 1m \+1\.20%/)
  assert.match(withCtx, /近 3m \+3\.40%/)
  // Index block
  assert.match(withCtx, /主要指数（5 个/)
  assert.match(withCtx, /000300\.SH/)
  assert.match(withCtx, /MA5 3490/)
  assert.match(withCtx, /MA20 3470/)
  rmSync(w, { recursive: true, force: true })
})

test('Day N brief stays shorter than Day 1, contains no playbook / no AUTONOMY / no embedded pacing', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  assert.ok(brief.length < first.length, 'Day N brief must remain shorter than Day 1 long version')
  // AUTONOMY 已被删除——"先看市场 / 平静日 HOLD / 大事多研究" 都是 embedded strategy
  assert.doesNotMatch(brief, /今天的节奏由你定/)
  assert.doesNotMatch(brief, /平静日浪费 chat/)
  // Day N 也不该泄漏出已废弃的两步法 / 分档 / mem0 key 约定
  assert.doesNotMatch(brief, /今日两步法/)
  assert.doesNotMatch(brief, /market_view:/)
  assert.doesNotMatch(brief, /action:\{/)
  assert.doesNotMatch(brief, /盈亏档/)
  assert.doesNotMatch(brief, /动能档/)
  assert.doesNotMatch(brief, /regime/)
  rmSync(w, { recursive: true, force: true })
})

test('Day 1 prompt asks bot to write a strategy with the MY_STRATEGY prefix and the four required components', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md' })
  // 指令本身
  assert.match(first, /今天的第一件事：写下属于你的投资策略/)
  // 必须以这个 prefix 起头——world 靠这个反查
  assert.match(first, /# MY_STRATEGY/)
  // 四要素都要提到（写法/顺序/详略自由，但要素不能漏）
  assert.match(first, /核心信念/)
  assert.match(first, /买入/)
  assert.match(first, /卖出/)
  assert.match(first, /仓位/)
  // mem0_add 写法（不规定具体调用形式）
  assert.match(first, /mem0_add/)
  rmSync(w, { recursive: true, force: true })
})

test('Day N injects the bot-written strategy when provided; omits the block when missing', () => {
  const w = tmpWorldWithOverview('2024-03-19', 'overview')
  const strategyText = '# MY_STRATEGY\n核心信念：大盘择时无法稳定盈利，长期持有沪深300。\n买入：每月固定 10 万定投。\n卖出：仅在估值百分位>90% 时减半仓。\n仓位：满仓 90%，留 10% 现金应急。'
  const withStrategy = renderDailyMessage({
    worldRoot: w, date: '2024-03-19', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md',
    strategy: strategyText,
  })
  // 注入的 header + 策略原文一字不漏
  assert.match(withStrategy, /你 Day 1 写下的策略/)
  assert.match(withStrategy, /# MY_STRATEGY/)
  assert.match(withStrategy, /大盘择时无法稳定盈利/)
  assert.match(withStrategy, /每月固定 10 万定投/)

  // 不传 strategy → 整块跳过（bot 没写 / world 抽取失败时的回退）
  const noStrategy = renderDailyMessage({
    worldRoot: w, date: '2024-03-19', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md',
  })
  assert.doesNotMatch(noStrategy, /你 Day 1 写下的策略/)
  assert.doesNotMatch(noStrategy, /# MY_STRATEGY/)

  // 空字符串 / 全空白也按"没策略"处理（不渲染空 header）
  const blankStrategy = renderDailyMessage({
    worldRoot: w, date: '2024-03-19', isFirstDay: false, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md',
    strategy: '   \n  \t  \n',
  })
  assert.doesNotMatch(blankStrategy, /你 Day 1 写下的策略/)
  rmSync(w, { recursive: true, force: true })
})

test('Day 1 injects the backtest horizon (N trading days) but no end date; Day N never injects horizon even if passed', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  // Day 1 with horizon
  const first = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: true,
    quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md',
    tradingDaysTotal: 60,
  })
  // 数字 + "交易日"
  assert.match(first, /60 个交易日/)
  // 给了约月 / 年的换算辅助但不暴露起止
  assert.match(first, /约 2\.9 个月/)
  assert.match(first, /0\.24 年/)
  // "第 1 天" 提示
  assert.match(first, /今天是第 1 天/)
  // 不会写一个具体的 end date — only today's date should appear among ISO dates
  const isoDates = first.match(/\d{4}-\d{2}-\d{2}/g) ?? []
  for (const d of isoDates) assert.equal(d, '2024-03-18', `Day 1 prompt should only mention today's date as YYYY-MM-DD; saw ${d}`)

  // Day 1 without horizon → 不注入这块
  const noHorizon = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: true,
    quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md',
  })
  assert.doesNotMatch(noHorizon, /个交易日（约/)
  assert.doesNotMatch(noHorizon, /今天是第 1 天/)

  // Day N — 即便传了也不注入（防 endgame 倒计时）
  const brief = renderDailyMessage({
    worldRoot: w, date: '2024-03-19', isFirstDay: false,
    quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md',
    tradingDaysTotal: 60,
  })
  assert.doesNotMatch(brief, /60 个交易日/)
  assert.doesNotMatch(brief, /今天是第 1 天/)
  rmSync(w, { recursive: true, force: true })
})

test('Day 1 ignores the strategy field even if passed (bot has not written it yet — block belongs to Day N)', () => {
  const w = tmpWorldWithOverview('2024-03-15', 'overview')
  const first = renderDailyMessage({
    worldRoot: w, date: '2024-03-15', isFirstDay: true, quotesPath: '/q.json', journalRelPath: 'memory/trading/journal.md',
    strategy: '# MY_STRATEGY\n核心信念：测试 Day 1 不该看见这段',
  })
  // 策略注入块在 Day 1 不应该出现（Day 1 只发"请写策略"的指令）
  assert.doesNotMatch(first, /你 Day 1 写下的策略/)
  rmSync(w, { recursive: true, force: true })
})
