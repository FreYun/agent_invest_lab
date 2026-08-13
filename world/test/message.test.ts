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
  const m = renderDailyMessage({ worldRoot: w, date: '2024-03-15', isFirstDay: true, botId: 'bot-test', quotesPath: '/abs/world/days/2024-03-15/quotes.json' })
  assert.match(m, /当前世界日期：2024-03-15（Friday）/)
  // mcp__* 工具池现在静态全部可见（loop server chat_auto_activate_deferred=true）；
  // prompt 不再教 discover_tools 激活流程。任何把这套话术加回来的改动都要让这条挂。
  assert.doesNotMatch(m, /discover_tools/)
  assert.match(m, /mem0_search/)
  assert.match(m, /mem0_add/)
  assert.match(m, /mcp__\*/)
  // 强调"mcp__* 全部静态可见、不需要激活步骤"——这是 #1 的核心断言
  assert.match(m, /已直接挂进工具列表/)
  assert.match(m, /无需任何激活步骤/)
  assert.match(m, /文件读写、web_fetch、bash/)
  // 行情走 simworld-data；账户/绩效/PnL/持仓 NAV/指数 MA 全部预取注入下方，
  // prompt 必须明确告诉 bot "不要重复调 portfolio_get_my_*"。
  assert.match(m, /simworld-data/)
  assert.match(m, /不要重复调用 mcp__fund_portfolio_mcp__portfolio_get_my_history/)
  assert.match(m, /portfolio_place_buy_order/)
  // 不再有"自己查 portfolio_get_my_*"这种鼓励重复 fetch 的句式
  assert.doesNotMatch(m, /portfolio_get_my_history \/ portfolio_get_my_performance \/ portfolio_get_my_trades 自己查/)
  // No file-IO references
  assert.doesNotMatch(m, /memory\/trading\/journal\.md/)
  // 静态 overview 不再注入
  assert.doesNotMatch(m, /上证 \+1\.2%，半导体领涨。/)
  rmSync(w, { recursive: true, force: true })
})

test('non-first-day message: concise rules only, no AUTONOMY / playbook / day-type banner', () => {
  const w = tmpWorldWithOverview('2024-03-18', '上证 -0.4%。')
  const full = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot-test', quotesPath: '/q.json' })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot-test', quotesPath: '/q.json' })
  // 注意：Day N 不再"比 Day 1 短"——dash-2026-05-19T08-52-36 bot7 从 Day 6 起塌成 mem0_search × 2 + mem0_add
  // 的 reward hack，60 天没碰新数据。briefRules 现在显式给 5 步节奏 + FOOTER_BRIEF 加"纯 mem0 不算"硬契约，
  // Day N 总长可能反超 Day 1，但用 token 换 thesis 验证是值得的。
  void full
  assert.match(brief, /当前世界日期：2024-03-18（Monday）/)
  assert.match(brief, /mem0_search/)
  assert.match(brief, /mem0_add/)
  assert.match(brief, /simworld-data/)
  // Day N brief 也不能教 discover_tools——loop server 已经把所有 mcp__* 静态挂出。
  assert.doesNotMatch(brief, /discover_tools/)
  // Day N 也明确告诉 bot 不要重复 fetch 已注入的账户数据
  assert.match(brief, /不要重复调 mcp__fund_portfolio_mcp__portfolio_get_my_history/)
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

test('every day broadcasts the per-run curated buyable pool; guidance is keyed on bot KIND (bot101+ multi, bot1..20 single), NOT on pool size', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const codes = ['510300', '159915', '002611']

  // single-fund bot (bot1..20) — even with a multi-code pool, guidance must stay "单指数择时"
  // and NOT promote rotation. The pool/bot-kind decoupling is the whole point — the dashboard
  // user can prefill any pool, but the bot's decision style is fixed by its identity.
  const singleFirst = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot1', quotesPath: '/q.json', buyableFundCodes: codes })
  const singleBrief = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot1', quotesPath: '/q.json', buyableFundCodes: codes })
  for (const msg of [singleFirst, singleBrief]) {
    assert.match(msg, /本 bot 当前产品可买池/)
    for (const c of codes) assert.match(msg, new RegExp(c))
    assert.match(msg, /你是单基金 bot/)
    assert.match(msg, /单指数择时/)
    // single-fund guidance explicitly forbids rotation — the word 轮动 must appear here as a "不要做" instruction
    assert.match(msg, /不要做标的轮动/)
    // Must NOT promote the multi-fund 三件事 framing
    assert.doesNotMatch(msg, /组合配置（各基金目标权重）/)
  }

  // multi-fund bot (bot101+) — even with a 1-code pool, guidance must stay 组合配置 / 轮动 framing
  const multiFirst = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot101', quotesPath: '/q.json', buyableFundCodes: codes })
  const multiBrief = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json', buyableFundCodes: codes })
  for (const msg of [multiFirst, multiBrief]) {
    assert.match(msg, /本 bot 当前产品可买池/)
    assert.match(msg, /你是多基金 bot/)
    assert.match(msg, /组合配置（各基金目标权重）/)
    assert.match(msg, /池内轮动/)
    // Must NOT promote the single-fund 单指数择时 framing
    assert.doesNotMatch(msg, /单指数择时/)
  }

  // Multi-fund bot with a 1-only pool — guidance still multi-fund (decoupled from pool size)
  const multiOneCode = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot102', quotesPath: '/q.json', buyableFundCodes: ['510300'] })
  assert.match(multiOneCode, /你是多基金 bot/)
  assert.doesNotMatch(multiOneCode, /单指数择时/)

  // multi-asset bot (bot_multi) — must not be mistaken for single-fund or equity-only multi-fund rotation
  const multiAsset = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot_multi', quotesPath: '/q.json', buyableFundCodes: codes })
  assert.match(multiAsset, /你是大类资产配置 bot/)
  assert.match(multiAsset, /A股基金 \/ 债券基金 \/ 黄金基金 \/ 货币或现金/)
  assert.match(multiAsset, /先做资产配置，再做基金选择/)
  assert.match(multiAsset, /账户现金直接承担现金角色/)
  assert.doesNotMatch(multiAsset, /你是单基金 bot/)
  assert.doesNotMatch(multiAsset, /你是多基金 bot/)
  assert.doesNotMatch(multiAsset, /池内轮动（换标的）/)

  // No list provided (world.yaml didn't set buyable_fund_codes): block omitted
  const firstNoList = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot1', quotesPath: '/q.json' })
  assert.doesNotMatch(firstNoList, /本 bot 当前产品可买池/)
  rmSync(w, { recursive: true, force: true })
})

test('first-day has no AUTONOMY block (cold start needs explicit handholding, not self-pacing)', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot-test', quotesPath: '/q.json' })
  assert.doesNotMatch(first, /今天的节奏由你定/)
  assert.doesNotMatch(first, /今天是普通交易日/)
  assert.doesNotMatch(first, /今天是研究日/)
  rmSync(w, { recursive: true, force: true })
})

test('first-day prompt does NOT embed any strategy framework (regime / timing_stance / 盈亏档 / 动能档 / 决策矩阵 / mem0 key 约定)', () => {
  // Guard test: 我们决定让 bot 完全自由发挥，不预设择时框架。任何把 prompt 拉回
  // "Step 1 市场观点 / 盈亏档动能档 / 强制 mem0 key" 这套硬约束的改动应该让这个 test 立刻挂。
  const w = tmpWorldWithOverview('2024-03-15', 'overview')
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-15', isFirstDay: true, botId: 'bot-test', quotesPath: '/q.json' })
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
  // Without dailyContext: no injected BLOCKS at all. We check for the actual
  // bracketed block headers — the prefetch directive in fullRules now mentions
  // these phrases inline (so the bot knows what's in those blocks when present),
  // but the rendered block itself (【...】) must not appear when no data is
  // injected. Same for "区间业绩" — appears in directive text but never as a
  // block when intervals is empty.
  const noCtx = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot-test', quotesPath: '/q.json' })
  assert.doesNotMatch(noCtx, /【账户快照（今日 settle 后）】/)
  assert.doesNotMatch(noCtx, /【近 \d+ 个交易日 PnL 走势/)
  assert.doesNotMatch(noCtx, /【持仓基金近 20 交易日 NAV/)
  assert.doesNotMatch(noCtx, /【主要指数（5 个/)
  assert.doesNotMatch(noCtx, /【整体绩效】/)
  assert.doesNotMatch(noCtx, /【区间业绩（/)

  // With all four blocks populated.
  const withCtx = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot-test', quotesPath: '/q.json',
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
      performance: {
        asOfDate: '2024-03-18',
        summary: {
          first_date: '2024-03-15', last_date: '2024-03-16', trading_days: 2,
          initial_capital: 1_000_000, latest_total_value: 1_000_200, latest_net_value: 1.0002,
          total_return_pct: 0.02, annualized_return_pct: 2.5, max_drawdown_pct: -0.05, max_drawdown_date: '2024-03-15',
          current_drawdown_pct: -4.75, peak_total_value: 1_050_000, peak_total_value_date: '2024-03-14',
          rolling_20d_drawdown_pct: -1.25, rolling_20d_peak_total_value: 1_012_850,
          rolling_20d_peak_total_value_date: '2024-03-12', rolling_20d_observations: 20,
          profit_giveback_amount: 49_800, profit_giveback_pct_of_initial: 4.98, peak_profit_giveback_ratio_pct: 99.6,
          volatility_pct_annualized: 1.2, sharpe_ratio_rf0: 0.45,
          win_days: 1, loss_days: 1, flat_days: 0,
          best_day: { date: '2024-03-16', return_pct: 0.07 },
          worst_day: { date: '2024-03-15', return_pct: -0.05 },
        },
        trades: { buy_count: 1, sell_count: 0, total_buy_amount: 200_000, total_sell_proceeds: 0, total_fees: 30, round_trips_count: 0 },
        intervals: {
          as_of_perf_date: '2024-03-16', rf_annual_pct: 1.8, rf_daily_pct: 0.007143, trading_days_per_year: 252,
          rows: [
            { period: '1m', return_pct: 0.02, annualized_return_pct: 5.17, max_drawdown_pct: -0.05, volatility_pct: 2.86, sharpe_ratio: 0.0524, calmar_ratio: 103.4, data_points: 2, window_target_days: 21, fallback: true },
            { period: 'since_inception', return_pct: 0.02, annualized_return_pct: 5.17, max_drawdown_pct: -0.05, volatility_pct: 2.86, sharpe_ratio: 0.0524, calmar_ratio: 103.4, data_points: 2, window_target_days: null, fallback: false },
          ],
        },
        completedPositions: [
          { fund_code: '510880', fund_name: '红利ETF', entry_date: '2024-02-01', exit_date: '2024-03-05', holding_days: 33, total_invested: 100_000, total_proceeds: 105_500, net_pl: 5_500, return_pct: 5.5, fees: 25 },
        ],
      },
      benchmark: {
        code: '000300.SH', name: '沪深300',
        pointsByDate: { '2024-03-15': -0.30, '2024-03-16': 0.10 },
        latestCumulativePct: 0.10,
        metrics: { return_pct: 0.10, max_drawdown_pct: -0.30, volatility_pct: 0.21, sharpe_ratio: 0.0050, calmar_ratio: 0.33, data_points: 2 },
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
        { code: '000300.SH', name: '沪深300', latest_date: '2024-03-15', latest_close: 3500.12, ma5: 3490.00, ma20: 3470.00, vs_ma5_pct: 0.29, vs_ma20_pct: 0.87, ma60: 3450.00, ma120: 3400.00, ma200: 3350.00, vs_ma60_pct: 1.45, vs_ma200_pct: 4.48, trend: '多头排列' },
      ],
    },
  })
  // Account block — initial capital + holding row + pending order
  assert.match(withCtx, /账户快照（今日 settle 后）/)
  assert.match(withCtx, /可用现金 ¥700,?000|¥700000/)
  assert.match(withCtx, /510300/)
  assert.match(withCtx, /沪深300ETF/)
  assert.match(withCtx, /待结算订单/)
  // 账户级累计盈亏行：fixture 是 total=initial(1e6) → 累计 0%；holdings[0] mv=200250 cost=200000 → 浮盈 +¥250、已实现 -¥250。
  // 单基行用 "单基浮盈"（不再用裸 "浮盈"）以免被 bot 误当成账户口径。
  assert.match(withCtx, /账户累计盈亏 0\.00%（¥0）= 持仓浮盈 ¥250 \+ 已实现盈亏 ¥-250/)
  assert.match(withCtx, /单基浮盈 \+0\.12%/)
  assert.doesNotMatch(withCtx, /，浮盈 /)
  // PnL trend / benchmark / 你 vs 基准 blocks have been trimmed from message.ts
  // (see commit cdc5913 — "trim PnL/benchmark blocks"). Guard against re-introduction:
  assert.doesNotMatch(withCtx, /近 \d+ 个交易日 PnL 走势/)
  assert.doesNotMatch(withCtx, /你 vs 基准（since inception/)
  // Performance summary block — only the metrics that the renderer actually emits
  assert.match(withCtx, /整体绩效/)
  assert.match(withCtx, /累计收益 \+0\.02%/)
  assert.match(withCtx, /Sharpe \(rf=0\) 0\.45/)
  assert.match(withCtx, /最大回撤 -0\.05%/)
  assert.match(withCtx, /近20交易日高水位回撤 -1\.25%.*20日峰值 ¥1,?012,?850.*2024-03-12.*样本 20 日/)
  assert.match(withCtx, /≥6% 降档只使用上一行“近20交易日高水位回撤”/)
  assert.match(withCtx, /全历史高水位回撤 -4\.75%.*历史峰值 ¥1,?050,?000.*2024-03-14/)
  assert.match(withCtx, /收益回吐 \+4\.98%.*¥49,?800.*峰值利润回吐率 \+99\.60%/)
  // 年化收益 / 日级胜负 / 最佳单日 also dropped from performanceBlock — guard against regression
  assert.doesNotMatch(withCtx, /年化 \+/)
  assert.doesNotMatch(withCtx, /日级胜负/)
  assert.doesNotMatch(withCtx, /最佳单日/)
  // Trades summary
  assert.match(withCtx, /交易统计/)
  assert.match(withCtx, /买入 1 笔.*总金额 ¥200,?000/)
  // Completed positions
  assert.match(withCtx, /已平仓持仓/)
  assert.match(withCtx, /510880.*红利ETF.*2024-02-01.*2024-03-05/)
  assert.match(withCtx, /\+5\.50%/)
  // Interval metrics block — both periods + fallback marker + 年化口径标注/列
  assert.match(withCtx, /区间业绩（截至 2024-03-16/)
  assert.match(withCtx, /按252交易日年化/)
  assert.match(withCtx, /年化收益%/)
  assert.match(withCtx, /躺平%/)
  assert.match(withCtx, /超额pp/)
  // 滚动超额算术：基准 pointsByDate 是「相对 run 起点的累计%」，1m 窗口(data_points=2)
  // 取 -0.30% → +0.10% 换算成区间收益 = +0.40%，超额 = 0.02 - 0.40 = -0.38pct。
  assert.match(withCtx, /1m\s+\+0\.02%\s+\+0\.40%\s+-0\.38pct\s+\+5\.17%/)
  // since_inception 的躺平列直接取基准累计 +0.10%，超额 = 0.02 - 0.10 = -0.08pct。
  assert.match(withCtx, /since_inception\s+\+0\.02%\s+\+0\.10%\s+-0\.08pct\s+\+5\.17%/)
  assert.match(withCtx, /窗口数据不足/)
  // Held NAV block
  assert.match(withCtx, /持仓基金近 20 交易日 NAV/)
  assert.match(withCtx, /近 1m \+1\.20%/)
  assert.match(withCtx, /近 3m \+3\.40%/)
  // Index block — 趋势标签 + 长均线锚先行，短均线退为情绪、只给 vs%
  assert.match(withCtx, /主要指数（5 个/)
  assert.match(withCtx, /000300\.SH/)
  assert.match(withCtx, /【多头排列】/)
  assert.match(withCtx, /趋势锚（判方向看这个）/)
  assert.match(withCtx, /MA60 3450/)
  assert.match(withCtx, /MA200 3350/)
  assert.match(withCtx, /短期情绪（非趋势扳机）/)
  assert.match(withCtx, /vs MA5 \+0\.29%/)
  rmSync(w, { recursive: true, force: true })
})

test('Day N brief contains no playbook / no AUTONOMY / no embedded pacing (length parity with Day 1 acceptable since v2 rhythm)', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'overview')
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot-test', quotesPath: '/q.json' })
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot-test', quotesPath: '/q.json' })
  // Day N 不再要求"比 Day 1 短"——见上面 non-first-day 测试的注释。
  void first
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

test('backtest horizon is never injected on either Day 1 or Day N (avoid endgame countdown leak — bot 应按市场决策，不按倒计时决策)', () => {
  // 历史曾经在 Day 1 注入 "N 个交易日 / 约 X 个月 / 今天是第 1 天" —— 现在 fullRules 已不渲染。
  // 即便 caller 仍然传 tradingDaysTotal（向后兼容），prompt 里也不应出现 horizon 字样。
  // 这个 test 作为防回归的 guard：把 horizon 重新加回来会立刻挂。
  const w = tmpWorldWithOverview('2024-03-18', 'overview')

  // Day 1 + 传 horizon: 不出现
  const first = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot-test',
    quotesPath: '/q.json',
    tradingDaysTotal: 60,
  })
  assert.doesNotMatch(first, /60 个交易日/)
  assert.doesNotMatch(first, /今天是第 1 天/)
  assert.doesNotMatch(first, /个交易日（约/)
  assert.doesNotMatch(first, /约 \d+(\.\d+)? 个月/)

  // Day N + 传 horizon: 同样不出现（防 endgame 倒计时）
  const brief = renderDailyMessage({
    worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot-test',
    quotesPath: '/q.json',
    tradingDaysTotal: 60,
  })
  assert.doesNotMatch(brief, /60 个交易日/)
  assert.doesNotMatch(brief, /今天是第 1 天/)
  rmSync(w, { recursive: true, force: true })
})

test('Day N footer (termination contract): decision-must-execute-today + forbids "list-todo-without-doing" + requires mem0_add before day-end; Day 1 keeps its own FOOTER_FULL', () => {
  // 真实事故 1：bot7 dash-2026-05-19T08-42-08 Day 2 跑了 23.9s，2 个 mem0_search 后写了 4 条"待办"就停，
  // 既没调 simworld 去查，也没 mem0_add 复盘。Day 1 的 FOOTER_FULL 有"下了单 + mem0_add 写完就可以结束"这条
  // termination contract，Day N 之前没有 footer，LLM 默认"没事做了就停"。
  // 真实事故 2：bot7 dash-2026-05-19 Day 29，巡检明确写"2/07 减仓至 40%"，mem0_add 落库，但当日没调任何 sell 工具
  // ——把决策推到下一日 = 决策蒸发（下一日新会话不继承"明天计划"）。
  // 真实事故 3：bot102 oos-bot101-daily 2026-07-28，reply.json 里 reply 字段 791 字全是 belief yaml，
  // 详细决策留在 msg[2] 的 540 字中间轮（工具调用之前），market-reports 页面「当日思考」卡片一片空。
  // FOOTER_BRIEF 现补五条：(1) 决策必须当日执行；(2) 列了 todo 就要做（或明确说放下并写理由）；
  // (3) 结束前必须 mem0_add；(4) 最后一条 assistant message = 结构化当日决策 markdown。
  const w = tmpWorldWithOverview('2024-03-19', 'overview')

  // Day N 必须带上 footer 的五条规则
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot-test', quotesPath: '/q.json' })
  assert.match(brief, /结束之前必做/)
  assert.match(brief, /todo_write 是每日必做，不是可选优化/)
  assert.match(brief, /第一项实质工具调用必须是 `todo_write`/)
  // 规则 1：决策必须当日执行——含执行工具名 + "决策从未发生" 反例
  assert.match(brief, /今天的决策今天就发生/)
  assert.match(brief, /portfolio_place_sell_order/)
  assert.match(brief, /portfolio_place_buy_order/)
  assert.match(brief, /决策从未发生/)
  // 规则 2：列待办就要做（或显式放下）
  assert.match(brief, /待办.*要查的.*要验证/)
  assert.match(brief, /列了不做\s*=\s*没列/)
  // 规则 3：结束前必须 mem0_add
  assert.match(brief, /结束前一次 mem0_add/)
  assert.match(brief, /没 mem0_add 就结束/)
  // 规则 0（v2 新增，最重要）：纯 mem0 不算完成一天——必须至少 1 次非 mem0 工具调用。
  // 真实事故：dash-2026-05-19T08-52-36 bot7 从 Day 6 起 9 iter → 3 iter，60 天只调 mem0_search+mem0_add，
  // 60% 仓位整月 HOLD，dailyContext 里的新数据完全没被验证。
  assert.match(brief, /纯 mem0_search \+ mem0_add 不算完成一天/)
  assert.match(brief, /至少 1 次调用非 mem0 的研究\/行情\/数据工具/)
  assert.match(brief, /不查就 mem0 落库\s*=\s*自欺欺人/)
  // 规则 4（bot102 2026-07-28 修复）：最后一条 assistant message 必须是结构化当日决策 markdown。
  // 关键字段（风险状态 / 市场环境 / 主线判断 / 今日动作 / 执行结果 / 总仓位 / 关键观察）都在，
  // 至少 1 个表格，且明确禁止"决策完成/methodology 已更新/mem0_add 已存/单个 belief yaml"这种短收尾。
  assert.match(brief, /最后一条 assistant message = 结构化当日决策 markdown/)
  assert.match(brief, /至少 1 个 markdown 表格/)
  assert.match(brief, /风险状态/)
  assert.match(brief, /市场环境/)
  assert.match(brief, /主线判断/)
  assert.match(brief, /今日动作/)
  assert.match(brief, /执行结果/)
  assert.match(brief, /总仓位/)
  assert.match(brief, /关键观察/)
  assert.match(brief, /禁止每天固定写四项/)
  assert.match(brief, /有买入写 `### 为什么买` \+ `### 买入动作`/)
  assert.match(brief, /无买入只写 `### 为什么不买`/)
  assert.match(brief, /有卖出写 `### 为什么卖` \+ `### 卖出动作`/)
  assert.match(brief, /无卖出只写 `### 为什么不卖`/)
  assert.match(brief, /当日思考」卡片唯一展示的入口/)
  assert.match(brief, /决策完成/)
  assert.match(brief, /belief yaml/)

  // briefRules 也补了 5 步节奏——把"先看数据 → 拉新维度 → mem0 对比 → 决策 → 落库"显式写出，
  // 把"调至少 1 个非 mem0 工具"标为 ② 步骤+黑体警告
  assert.match(brief, /今天的节奏（按顺序）/)
  assert.match(brief, /先看下方【\.\.\.】数据块/)
  assert.match(brief, /调至少 1 个非 mem0 工具拉今日新数据/)
  assert.match(brief, /这一步缺，整天等于没做/)

  // Day 1 走的是 FOOTER_FULL 路径，不应被 FOOTER_BRIEF 替换或追加双 footer
  const first = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: true, botId: 'bot-test', quotesPath: '/q.json' })
  // FOOTER_FULL 的两个 header 还在
  assert.match(first, /记忆与连续性/)
  assert.match(first, /每日决策硬契约/)
  assert.match(first, /第一项实质工具调用必须是 `todo_write`/)
  assert.match(first, /不要每天固定写四项/)
  assert.match(first, /有买入时只写 `### 为什么买` \+ `### 买入动作`/)
  assert.match(first, /无买入时只写 `### 为什么不买`/)
  assert.match(first, /有卖出时只写 `### 为什么卖` \+ `### 卖出动作`/)
  assert.match(first, /无卖出时只写 `### 为什么不卖`/)
  assert.match(first, /边界】这是一次交易回合/)
  // FOOTER_BRIEF 的 header / 规则不该出现在 Day 1（Day 1 用 FOOTER_FULL，不双注入）
  assert.doesNotMatch(first, /结束之前必做/)
  assert.doesNotMatch(first, /今天的决策今天就发生/)
  // Day N 总长可能反超 Day 1——见 non-first-day 测试的注释。

  rmSync(w, { recursive: true, force: true })
})

test('Day 1 always injects methodology hint (system prompt has the actual content)', () => {
  const w = tmpWorldWithOverview('2024-03-15', 'overview')
  const first = renderDailyMessage({
    worldRoot: w, date: '2024-03-15', isFirstDay: true, botId: 'bot-test',
    quotesPath: '/q.json',
  })
  // hint 指向 system prompt 的 ## METHODOLOGY.md section
  assert.match(first, /## METHODOLOGY\.md/)
  assert.match(first, /你的 active methodology 已就位/)
  // update_my_strategy 工具可以改 methodology
  assert.match(first, /update_my_strategy\(bot_id, strategy, reason\)/)
  // 不再有"写策略文档"的指令
  assert.doesNotMatch(first, /今天的第一件事：写下属于你的投资策略/)
  assert.doesNotMatch(first, /必须包含的四要素/)
  // fullRules / footer 仍在
  assert.match(first, /当前世界日期：2024-03-15/)
  assert.match(first, /mem0_search/)
  assert.match(first, /记忆与连续性/)
  rmSync(w, { recursive: true, force: true })
})

test('Day N always injects methodology hint (no strategy block, system prompt has the actual content)', () => {
  const w = tmpWorldWithOverview('2024-03-19', 'overview')
  const brief = renderDailyMessage({
    worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot-test',
    quotesPath: '/q.json',
  })
  // hint 指向 system prompt 的 ## METHODOLOGY.md section
  assert.match(brief, /## METHODOLOGY\.md/)
  assert.match(brief, /update_my_strategy\(bot_id, strategy, reason\)/)
  // 不再有"Day 1 写下的策略"块或 # MY_STRATEGY 前缀
  assert.doesNotMatch(brief, /你 Day 1 写下的策略/)
  assert.doesNotMatch(brief, /# MY_STRATEGY/)
  // briefRules 头部仍然在
  assert.match(brief, /当前世界日期：2024-03-19/)
  rmSync(w, { recursive: true, force: true })
})

// 复盘块 fixture 工厂：可调 total_return_pct（决定 alpha）与账户仓位（决定是否触发踏空升级）。
function reviewFixture(opts: { tradingDays?: number; totalReturnPct?: number; benchReturnPct?: number; marketValue?: number } = {}) {
  const perf = {
    asOfDate: '2024-03-25',
    summary: {
      first_date: '2024-03-15', last_date: '2024-03-25', trading_days: opts.tradingDays ?? 5,
      initial_capital: 1_000_000, latest_total_value: 1_030_000, latest_net_value: 1.03,
      total_return_pct: opts.totalReturnPct ?? 3.0, annualized_return_pct: 9.0, max_drawdown_pct: -8.0, max_drawdown_date: '2024-03-20',
      volatility_pct_annualized: 5.0, sharpe_ratio_rf0: 0.5,
      win_days: 3, loss_days: 2, flat_days: 0,
      best_day: null, worst_day: null,
    },
    trades: { buy_count: 0, sell_count: 0, total_buy_amount: 0, total_sell_proceeds: 0, total_fees: 0, round_trips_count: 0 },
    intervals: null,
    completedPositions: [],
  }
  const benchmark = {
    code: '000300.SH', name: '沪深300', pointsByDate: {}, latestCumulativePct: opts.benchReturnPct ?? 10.0,
    metrics: { return_pct: opts.benchReturnPct ?? 10.0, max_drawdown_pct: -5.0, volatility_pct: 6.0, sharpe_ratio: 0.6, calmar_ratio: 2.0, data_points: 5 },
  }
  const account = opts.marketValue === undefined ? undefined : {
    asOfDate: '2024-03-25',
    account: { initial_capital: 1_000_000, cash_available: 1_000_000 - opts.marketValue, cash_in_transit: 0, market_value: opts.marketValue, total_value: 1_000_000 },
    holdings: [], pendingOrders: [],
  }
  return { performance: perf, benchmark, account }
}

test('单指数复盘日（trading_days%5===0）日常分支软化：去掉"自 Day1 累计 vs 躺平"全程记分牌，改滚动自评 + 无证伪就维持；非复盘日不注入', () => {
  // 病根（bot19 化工）：复盘块每 5 日把"自 Day 1 累计跑输躺平 -34pct"摆给 bot，逼它重写 →
  // 过拟合成大盘+情绪择时器。软化后单指数日常分支不再亮全程记分牌、不逼为"做点什么"而改。
  const w = tmpWorldWithOverview('2024-03-25', 'overview')
  const ctx = reviewFixture({ totalReturnPct: 3.0, benchReturnPct: 10.0 })  // alpha=-7（未达 -10 升级线）、无账户 → 日常分支
  const review = renderDailyMessage({
    worldRoot: w, date: '2024-03-25', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { performance: ctx.performance, benchmark: ctx.benchmark },
  })
  // 硬契约标题 + 三步结构仍在
  assert.match(review, /【⚠ 第 5 个交易日 · 策略强制复盘（每 5 个交易日一次，今天不可跳过）】/)
  assert.match(review, /▍ 第一步 · 先对当前市场大趋势做一句话定性判断/)
  // 软化关键：第二步是滚动自评、明确"刻意不摆全程记分"；不出现累计 vs 躺平的 alpha 行
  assert.match(review, /看你自己近一段的滚动表现/)
  assert.match(review, /刻意不在这里摆"自 Day 1 累计跑赢没跑赢躺平"的全程记分/)
  assert.doesNotMatch(review, /你已跑输躺平/)
  assert.doesNotMatch(review, /超额 -7\.00pct/)
  // 第三步：可"维持"、不逼乱改，但 update_my_strategy 仍是选项①
  assert.match(review, /结论可以是"维持"/)
  assert.match(review, /不必为了"做点什么"而改/)
  assert.match(review, /update_my_strategy\(bot_id="bot7"/)
  assert.match(review.trimEnd(), /也别用"必须做点什么"逼自己乱改。$/)

  // 非复盘日（trading_days=4）→ 不注入
  const ctx4 = reviewFixture({ tradingDays: 4 })
  const noReview = renderDailyMessage({
    worldRoot: w, date: '2024-03-25', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { performance: ctx4.performance, benchmark: ctx4.benchmark },
  })
  assert.doesNotMatch(noReview, /策略强制复盘/)
  rmSync(w, { recursive: true, force: true })
})

test('单指数复盘日 · 踏空升级条款（累计跑输躺平≥10pct 且仓位<20%）仍保留：亮累计 vs 躺平 + 强制重写', () => {
  const w = tmpWorldWithOverview('2024-03-25', 'overview')
  // alpha = -5 - 10 = -15 ≤ -10；仓位 100k/1M = 10% < 20% → 触发升级
  const ctx = reviewFixture({ totalReturnPct: -5.0, benchReturnPct: 10.0, marketValue: 100_000 })
  const review = renderDailyMessage({
    worldRoot: w, date: '2024-03-25', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { performance: ctx.performance, benchmark: ctx.benchmark, account: ctx.account },
  })
  // 踏空场景：累计 vs 躺平硬对照亮出来（错过的涨幅 = 逼它动手的证据）
  assert.match(review, /▍ 第二步 · 你 vs 沪深300（不择时买入持有）· 自 Day 1 起累计/)
  assert.match(review, /你已跑输躺平 15\.00pct/)
  assert.match(review, /⛔ 升级条款已触发/)
  assert.match(review, /今天必须调 `mcp__strategy_mcp__update_my_strategy\(bot_id="bot7"/)
  rmSync(w, { recursive: true, force: true })
})

test('多基金（bot101）复盘块走原版逻辑，软化不波及：日常分支仍亮"自 Day1 累计 vs 躺平" + 今天必须二选一', () => {
  const w = tmpWorldWithOverview('2024-03-25', 'overview')
  const ctx = reviewFixture({ totalReturnPct: 3.0, benchReturnPct: 10.0 })  // alpha=-7、无账户 → 原版日常分支
  const review = renderDailyMessage({
    worldRoot: w, date: '2024-03-25', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json',
    dailyContext: { performance: ctx.performance, benchmark: ctx.benchmark },
  })
  // 原版行为：累计 vs 躺平记分牌 + 二选一 + 原版结尾，一字未动
  assert.match(review, /▍ 第二步 · 你 vs 沪深300（不择时买入持有）/)
  assert.match(review, /超额 -7\.00pct（你已跑输躺平 7\.00pct）/)
  assert.match(review, /▍ 第三步 · 今天必须二选一/)
  assert.match(review.trimEnd(), /该改就改，别用"不轻易改"麻痹自己。$/)
  // 软化版独有措辞绝不出现在多基金路径
  assert.doesNotMatch(review, /看你自己近一段的滚动表现/)
  assert.doesNotMatch(review, /结论可以是"维持"/)
  rmSync(w, { recursive: true, force: true })
})

test('belief↔仓位 言行一致核对：看多却空仓 / 看空却重仓 都硬拦；同向、中性、缺 belief、缺账户 → 不拦', () => {
  // 病根 guard：bot 每天写 belief（t+20 上涨概率）却让仓位与它脱钩，可"嘴上看多、仓位空仓"两头都占
  // → 长期踏空（bot6 军工 / bot10 黄金）。这块把仓位焊回 bot 自己说出口的信念，方向矛盾就每天硬拦。
  const w = tmpWorldWithOverview('2024-03-20', 'overview')
  const mkAcct = (marketValue: number) => ({
    asOfDate: '2024-03-20',
    account: { initial_capital: 1_000_000, cash_available: 1_000_000 - marketValue, cash_in_transit: 0, market_value: marketValue, total_value: 1_000_000 },
    holdings: [], pendingOrders: [],
  })
  const flat = mkAcct(0)            // 0% 仓位
  const heavy = mkAcct(600_000)     // 60% 仓位

  // ① 看多(t+20=0.62)却空仓(0%) → 踏空，硬拦
  const bullFlat = renderDailyMessage({
    worldRoot: w, date: '2024-03-20', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { account: flat }, latestBelief: { tPlus5: 0.6, tPlus20: 0.62 },
  })
  assert.match(bullFlat, /【⚖ 言行一致核对（belief ↔ 仓位）· 今天必须消除矛盾】/)
  assert.match(bullFlat, /净看多/)
  assert.match(bullFlat, /把保守偷换成永久空仓/)
  // 言行一致块在最末尾（非复盘日 review 为空，本块收尾，recency 最高）
  assert.match(bullFlat.trimEnd(), /每天核对，不只复盘日。这不替你做方向判断，只禁止"想的"和"做的"打架。）$/)

  // ② 看多且已在场(60%) → 一致，不拦
  const bullIn = renderDailyMessage({
    worldRoot: w, date: '2024-03-20', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { account: heavy }, latestBelief: { tPlus5: 0.6, tPlus20: 0.62 },
  })
  assert.doesNotMatch(bullIn, /言行一致核对/)

  // ③ 看空(t+20=0.38)却重仓(60%) → 做错，硬拦
  const bearHeavy = renderDailyMessage({
    worldRoot: w, date: '2024-03-20', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { account: heavy }, latestBelief: { tPlus5: 0.4, tPlus20: 0.38 },
  })
  assert.match(bearHeavy, /净看空/)
  assert.match(bearHeavy, /信号走坏却不撤/)

  // ④ 看空且已空仓(0%) → 一致，不拦
  const bearFlat = renderDailyMessage({
    worldRoot: w, date: '2024-03-20', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { account: flat }, latestBelief: { tPlus5: 0.4, tPlus20: 0.38 },
  })
  assert.doesNotMatch(bearFlat, /言行一致核对/)

  // ⑤ 中性(0.5) → 不拦
  const neutral = renderDailyMessage({
    worldRoot: w, date: '2024-03-20', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { account: flat }, latestBelief: { tPlus5: 0.5, tPlus20: 0.5 },
  })
  assert.doesNotMatch(neutral, /言行一致核对/)

  // ⑥ 无 latestBelief → 无法判定，不拦
  const noBelief = renderDailyMessage({
    worldRoot: w, date: '2024-03-20', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { account: flat },
  })
  assert.doesNotMatch(noBelief, /言行一致核对/)

  // ⑦ 有 belief 但缺账户快照 → 无法判定，不拦
  const noAcct = renderDailyMessage({
    worldRoot: w, date: '2024-03-20', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    latestBelief: { tPlus5: 0.6, tPlus20: 0.62 },
  })
  assert.doesNotMatch(noAcct, /言行一致核对/)

  rmSync(w, { recursive: true, force: true })
})

test('强制深研提示准确区分账户回撤事件与固定周期', () => {
  const w = tmpWorldWithOverview('2024-03-20', '')
  const m = renderDailyMessage({
    worldRoot: w, date: '2024-03-20', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    deepResearchEnabled: true,
    deepResearchForced: true,
    deepResearchReasons: ['account-drawdown'],
    deepResearchGapDays: 2,
    deepResearchLastDate: '2024-03-18',
  })
  assert.match(m, /账户当前净值回撤跨入更深的阈值倍数档位/)
  assert.doesNotMatch(m, /达调度硬上限/)
  rmSync(w, { recursive: true, force: true })
})

test('charterBlock: 宪章未声明→声明指引注入；已声明无复评→不注入；复评到期/过期→复评提醒注入；无 charterBlock→跳过', () => {
  const w = tmpWorldWithOverview('2024-03-18', '')
  // 无 charterBlock：不渲染任何配置宪章块（charter_enforcement 未启用的 run 行为不变）
  const noCharter = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json' })
  assert.doesNotMatch(noCharter, /【配置宪章/)
  // 未声明 → 声明指引（Day 1 和 Day N 均测试）
  const undeclaredDay1 = renderDailyMessage({ worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot101', quotesPath: '/q.json', charterBlock: '【配置宪章：今日必须声明】\n本 run 启用了配置宪章。' })
  assert.match(undeclaredDay1, /【配置宪章：今日必须声明】/)
  assert.match(undeclaredDay1, /本 run 启用了配置宪章/)
  const undeclaredDayN = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json', charterBlock: '【配置宪章：今日必须声明】\n声明前所有买入单都会被拒。' })
  assert.match(undeclaredDayN, /【配置宪章：今日必须声明】/)
  assert.match(undeclaredDayN, /声明前所有买入单都会被拒/)
  // 复评到期
  const reviewDue = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json', charterBlock: '【配置宪章：卫星复评今日到期】\n上次复评 2024-03-14，cadence=5 交易日。' })
  assert.match(reviewDue, /【配置宪章：卫星复评今日到期】/)
  assert.match(reviewDue, /cadence=5/)
  // 复评过期
  const reviewOverdue = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json', charterBlock: '【配置宪章：卫星复评已过期】\n复评过期期间核心基金买单会被拒。' })
  assert.match(reviewOverdue, /【配置宪章：卫星复评已过期】/)
  assert.match(reviewOverdue, /复评过期期间核心基金买单会被拒/)
  // 已声明无复评触发：传 undefined → 不渲染
  const declared = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json', charterBlock: undefined })
  assert.doesNotMatch(declared, /【配置宪章/)
  rmSync(w, { recursive: true, force: true })
})

// ── 交易纪律核对（载体冷却 + 防拆单）──────────────────────────────────────────
// 系统块由 recentOrders + benchmark 交易日历确定性推算。断言口径：
//   冷却 = 清仓日后第 1..10 交易日封禁（left = 10+1-elapsed，与 min_hold 同防 off-by-one）；
//   拆单窗口 = 同向近 5 交易日；数据缺失 → 整块消失、载体核对回 ✗（行为不变）。
const DISCIPLINE_MAINLINE = [
  'regime：**抱主线·v4**（第 3 天）',
  '## ⑤ 可投基金池',
  '| 在管板块 | 角色 | 指定载体 | 纯度 |',
  '|---|---|---|---|',
  '| BK1106.DC 创新药 | 卫星 | 012738 广发创新药ETF联接C | ✓纯载体 |',
].join('\n')

function disciplineCtxBase(w: string) {
  return {
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot105', quotesPath: '/q.json',
    marketReports: { context: 'ctx', mainline: DISCIPLINE_MAINLINE, rotation: 'rot' },
  }
}

const DISCIPLINE_BENCH = {
  code: 'buyable-pool', name: '买池等权',
  pointsByDate: { '2024-03-11': 0, '2024-03-12': 0.1, '2024-03-13': 0.2, '2024-03-14': 0.1, '2024-03-15': 0.3 },
  latestCumulativePct: 0.3,
}

function disciplineAccount(recentOrders: unknown) {
  return {
    asOfDate: '2024-03-18',
    account: { initial_capital: 1_000_000, cash_available: 800_000, cash_in_transit: 0, market_value: 200_000, total_value: 1_000_000 },
    holdings: [
      { fund_code: '510300', fund_name: '沪深300ETF', shares: 89000, amount_invested: 200_000, latest_nav: 2.25, market_value: 200_000, unrealized_pnl_pct: 0.1, weight: 0.2, holding_days: 5, entry_date: '2024-03-13' },
    ],
    pendingOrders: [],
    recentOrders,
  }
}

test('交易纪律核对：清仓载体渲染 ⏳ 冷却（不亮 ✗）+ 冷却/拆单/避险一次到位行', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  const m = renderDailyMessage({
    ...disciplineCtxBase(w),
    dailyContext: {
      account: disciplineAccount([
        // 012738 于 03-12 清仓（confirmed sell，且不在当前持仓）→ elapsed=(03-12,03-18]={13,14,15,18}=4 → 余 7
        { fund_code: '012738', order_type: 'sell', order_date: '2024-03-12', status: 'confirmed', order_amount: 50000 },
        // 510300 于 03-15 有确认买单 → 拆单窗口内（elapsed=1 <5）
        { fund_code: '510300', order_type: 'buy', order_date: '2024-03-15', status: 'confirmed', order_amount: 100000 },
      ]),
      benchmark: DISCIPLINE_BENCH,
    },
  } as Parameters<typeof renderDailyMessage>[0])
  // 载体核对：冷却中的指定载体 ⏳，不亮 ✗、不触发 anyMissing 施压文案
  assert.match(m, /⏳ 冷却中（2024-03-12 清仓，余 7 交易日不得买回）/)
  assert.doesNotMatch(m, /✗ 未建仓/)
  assert.doesNotMatch(m, /上表有 ✗\/△ 板块/)
  // 纪律块三要素
  assert.match(m, /交易纪律核对（系统核算 · 载体冷却 \+ 防拆单）/)
  assert.match(m, /012738：你于 2024-03-12 清仓卖出，\*\*余 7 个交易日\*\*内不得买回/)
  assert.match(m, /510300 近 5 个交易日已有买入单（2024-03-15）——今天再下同向单 = 拆单违规/)
  assert.match(m, /引擎明令动作\*\*不受此限/)
  assert.match(m, /避险 \/ 止损卖出必须一次到位/)
  rmSync(w, { recursive: true, force: true })
})

test('交易纪律核对：冷却期满回 ✗；仍持有的基金卖单不算清仓；无 recentOrders 整块消失', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  // 期满：清仓在 10+ 个交易日前（日历只有 5+今日=6 天，elapsed=6 → 余 5 仍在冷却；改用足长日历验证期满）
  const longBench = {
    code: 'buyable-pool', name: '买池等权',
    pointsByDate: Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`2024-03-${String(i + 4).padStart(2, '0')}`, 0])),
    latestCumulativePct: 0,
  }
  const expired = renderDailyMessage({
    ...disciplineCtxBase(w),
    dailyContext: {
      account: disciplineAccount([
        // 清仓于 03-04，日历 03-04..03-15 + 今日 03-18 → elapsed=(03-04,03-18]=12 ≥ 11 → 冷却结束
        { fund_code: '012738', order_type: 'sell', order_date: '2024-03-04', status: 'confirmed', order_amount: 50000 },
      ]),
      benchmark: longBench,
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(expired, /⏳ 冷却中/)
  assert.match(expired, /✗ 未建仓/)
  assert.match(expired, /当前无冷却中基金/)

  // 仍持有（部分卖出）→ 不算清仓不冷却
  const partial = renderDailyMessage({
    ...disciplineCtxBase(w),
    dailyContext: {
      account: {
        ...disciplineAccount([
          { fund_code: '012738', order_type: 'sell', order_date: '2024-03-14', status: 'confirmed', order_amount: 20000 },
        ]),
        holdings: [
          { fund_code: '012738', fund_name: '广发创新药ETF联接C', shares: 10000, amount_invested: 15000, latest_nav: 1.5, market_value: 15000, unrealized_pnl_pct: 0, weight: 0.015, holding_days: 9, entry_date: '2024-03-05' },
        ],
      },
      benchmark: DISCIPLINE_BENCH,
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(partial, /⏳ 冷却中/)
  // 012738 仍持有 1.5% → 载体核对显示 △，拆单行仍提示其卖出单在窗口内
  assert.match(partial, /△ 仅 1\.5%/)
  assert.match(partial, /012738 近 5 个交易日已有卖出单（2024-03-14）/)

  // 无 recentOrders（老快照）→ 纪律块整体消失，载体核对回 ✗（行为不变）
  const legacy = renderDailyMessage({
    ...disciplineCtxBase(w),
    dailyContext: { account: disciplineAccount(undefined), benchmark: DISCIPLINE_BENCH },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(legacy, /交易纪律核对/)
  assert.match(legacy, /✗ 未建仓/)
  rmSync(w, { recursive: true, force: true })
})

test('交易纪律核对：显式交易日历可独立支撑整块（benchmark 为空也渲染）；两者皆缺才消失', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  const orders = [
    { fund_code: '012738', order_type: 'sell', order_date: '2024-03-12', status: 'confirmed', order_amount: 50000 },
    { fund_code: '510300', order_type: 'buy', order_date: '2024-03-15', status: 'confirmed', order_amount: 100000 },
  ]
  // 实盘/大买池下 benchmark 恒为 null（runStartDate 退化 + 整池 NAV 60s 超时）。这时必须靠
  // run.ts 传入的真实交易日历撑住整块，否则纪律核对静默消失——bot105d 381 天就是这么丢的。
  const viaCalendar = renderDailyMessage({
    ...disciplineCtxBase(w),
    tradingCalendar: ['2024-03-11', '2024-03-12', '2024-03-13', '2024-03-14', '2024-03-15', '2024-03-18'],
    dailyContext: { account: disciplineAccount(orders) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(viaCalendar, /交易纪律核对（系统核算 · 载体冷却 \+ 防拆单）/)
  assert.match(viaCalendar, /012738：你于 2024-03-12 清仓卖出，\*\*余 7 个交易日\*\*内不得买回/)
  assert.match(viaCalendar, /510300 近 5 个交易日已有买入单（2024-03-15）/)
  assert.match(viaCalendar, /⏳ 冷却中（2024-03-12 清仓，余 7 交易日不得买回）/)

  // 日历末尾就是今日（不需要补 asOfDate）→ 计数不能多算一天，仍是余 7。
  const endsToday = renderDailyMessage({
    ...disciplineCtxBase(w),
    tradingCalendar: ['2024-03-12', '2024-03-13', '2024-03-14', '2024-03-15', '2024-03-18'],
    dailyContext: { account: disciplineAccount(orders) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(endsToday, /\*\*余 7 个交易日\*\*内不得买回/)

  // 显式日历优先于 benchmark：给一条只到 03-15 的短 benchmark + 完整日历，结果按日历算。
  const explicitWins = renderDailyMessage({
    ...disciplineCtxBase(w),
    tradingCalendar: Array.from({ length: 12 }, (_, i) => `2024-03-${String(i + 4).padStart(2, '0')}`).concat('2024-03-18'),
    dailyContext: {
      account: disciplineAccount([{ fund_code: '012738', order_type: 'sell', order_date: '2024-03-04', status: 'confirmed', order_amount: 50000 }]),
      benchmark: DISCIPLINE_BENCH,
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(explicitWins, /当前无冷却中基金/) // 按长日历 elapsed≥11 → 期满
  assert.doesNotMatch(explicitWins, /⏳ 冷却中/)

  // 两者皆缺 → 整块消失（旧行为，不回归）
  const neither = renderDailyMessage({
    ...disciplineCtxBase(w),
    dailyContext: { account: disciplineAccount(orders) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(neither, /交易纪律核对/)
  rmSync(w, { recursive: true, force: true })
})

// ── 单指数持有承诺块 ──────────────────────────────────────────────
const HC_FEES = [
  { fund_code: '019875', fund_name: 'CS稀金属ETF联接C', found: true,
    purchase_fee_pct: 0, redeem_tiers: [{ max_days: 7, rate_pct: 1.5 }, { max_days: null, rate_pct: 0 }],
    mgmt_fee_pct_annual: 0.5, custody_fee_pct_annual: 0.1, sales_service_fee_pct_annual: 0.3 },
]
const HC_BENCH = {
  code: 'buyable-pool', name: '买池等权',
  pointsByDate: { '2024-03-11': 0, '2024-03-12': 0.1, '2024-03-13': 0.2, '2024-03-14': 0.1, '2024-03-15': 0.3 },
  latestCumulativePct: 0.3,
}
function hcAccount(recentOrders: unknown, holdings: unknown) {
  return {
    asOfDate: '2024-03-18',
    account: { initial_capital: 1_000_000, cash_available: 500_000, cash_in_transit: 0, market_value: 500_000, total_value: 1_000_000 },
    holdings, pendingOrders: [], recentOrders,
  }
}

test('持有承诺块：单指数 bot 恒渲染建仓承诺 + 免赎档；多基金不渲染；无费率空串', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  const single = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-03-01', status: 'confirmed', order_amount: 300000 }],
        [{ fund_code: '019875', fund_name: 'CS稀金属ETF联接C', shares: 140000, amount_invested: 300000, latest_nav: 2.14, market_value: 300000, weight: 0.3 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(single, /建仓 = 最短持有承诺（系统硬闸 · 跨日生效）/)
  assert.match(single, /019875（CS稀金属ETF联接C）：今日建仓\/加仓 → 锁 7 自然日（不足 7 天确定亏 1\.50% 早赎费）/)

  // 多基金 bot101 → 不渲染（门控）
  const multi = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot101', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount([], []),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(multi, /建仓 = 最短持有承诺/)

  // 无 fundFees → 空串
  const noFees = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: { benchmark: HC_BENCH, account: hcAccount([], []) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(noFees, /建仓 = 最短持有承诺/)
  rmSync(w, { recursive: true, force: true })
})

test('持有承诺块·思考闸：窗内买入渲染早赎费+距免赎+思考闸；持满7日不渲染闸行', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  // 03-15 买入（asOfDate 03-18，自然日差=3 <7 → 在窗），持仓市值 300000
  const inWin = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-03-15', status: 'confirmed', order_amount: 300000 }],
        [{ fund_code: '019875', fund_name: 'CS稀金属ETF联接C', shares: 140000, amount_invested: 300000, latest_nav: 2.14, market_value: 300000, weight: 0.3 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(inWin, /窗内持仓 · 今日若卖出的确切成本/)
  assert.match(inWin, /019875：2024-03-15 买入（已持 3 自然日，距免赎还剩 4 天）/)
  assert.match(inWin, /今日卖出早赎费 ≈ ¥4,?500（1\.50%）/) // 1.5% × 300000 = 4500
  assert.match(inWin, /先在 mem0 写下经过思考的充分理由再下单/)
  assert.match(inWin, /审计会核：窗内卖出而无实质论证 = 违规/)

  // 持满 7 自然日（02-01 买）→ 无闸行，但承诺行仍在
  const held = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-02-01', status: 'confirmed', order_amount: 300000 }],
        [{ fund_code: '019875', fund_name: 'CS稀金属ETF联接C', shares: 140000, amount_invested: 300000, latest_nav: 2.14, market_value: 300000, weight: 0.3 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(held, /今日建仓\/加仓 → 锁 7 自然日/)      // 承诺行在
  assert.doesNotMatch(held, /窗内持仓 · 今日若卖出/) // 无闸行
  rmSync(w, { recursive: true, force: true })
})

test('持有承诺块·思考闸：off-by-one（第7自然日=已免赎，不渲染）+ 已清仓不警示', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  // 03-11 买，到 03-18 = 7 自然日 → calDays=7 不 <7 → 免赎，无闸行
  const day7 = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-03-11', status: 'confirmed', order_amount: 300000 }],
        [{ fund_code: '019875', fund_name: 'CS稀金属ETF联接C', shares: 140000, amount_invested: 300000, latest_nav: 2.14, market_value: 300000, weight: 0.3 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(day7, /窗内持仓 · 今日若卖出/)

  // 已清仓（holdings 无该基金）→ 无在窗份额可警示
  const sold = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: HC_FEES, benchmark: HC_BENCH,
      account: hcAccount(
        [{ fund_code: '019875', order_type: 'buy', order_date: '2024-03-15', status: 'confirmed', order_amount: 300000 }],
        [],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.doesNotMatch(sold, /窗内持仓 · 今日若卖出/)
  rmSync(w, { recursive: true, force: true })
})

test('持有承诺块·边界：无 recentOrders 承诺行仍渲染无闸行；Day1 路径也渲染；多档基金锚首免赎档', () => {
  const w = tmpWorldWithOverview('2024-03-18', 'x')
  // 无 recentOrders（老快照）：承诺行在、无闸行、不报错
  const legacy = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: { fundFees: HC_FEES, benchmark: HC_BENCH, account: hcAccount(undefined, []) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(legacy, /今日建仓\/加仓 → 锁 7 自然日/)
  assert.doesNotMatch(legacy, /窗内持仓 · 今日若卖出/)

  // Day1 路径同样渲染
  const day1 = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: true, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: { fundFees: HC_FEES, benchmark: HC_BENCH, account: hcAccount([], []) },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(day1, /建仓 = 最短持有承诺（系统硬闸 · 跨日生效）/)

  // 多档赎回阶梯：<7d 1.5% / <30d 0.5% / ≥30d 0% → 免赎档=30、rate(0)=1.5
  const multiTier = [{ fund_code: '013403', fund_name: '某基', found: true, purchase_fee_pct: 0,
    redeem_tiers: [{ max_days: 7, rate_pct: 1.5 }, { max_days: 30, rate_pct: 0.5 }, { max_days: null, rate_pct: 0 }] }]
  const mt = renderDailyMessage({
    worldRoot: w, date: '2024-03-18', isFirstDay: false, botId: 'bot20', quotesPath: '/q.json',
    dailyContext: {
      fundFees: multiTier, benchmark: HC_BENCH,
      account: hcAccount(
        // 03-01 买，calDays=17 <30 在窗，命中 <30d 档 0.5%
        [{ fund_code: '013403', order_type: 'buy', order_date: '2024-03-01', status: 'confirmed', order_amount: 200000 }],
        [{ fund_code: '013403', fund_name: '某基', shares: 100000, amount_invested: 200000, latest_nav: 2.0, market_value: 200000, weight: 0.2 }],
      ),
    },
  } as Parameters<typeof renderDailyMessage>[0])
  assert.match(mt, /013403（某基）：今日建仓\/加仓 → 锁 30 自然日（不足 30 天确定亏 1\.50% 早赎费）/)
  assert.match(mt, /已持 17 自然日，距免赎还剩 13 天）。今日卖出早赎费 ≈ ¥1,?000（0\.50%）/) // 0.5%×200000=1000
  rmSync(w, { recursive: true, force: true })
})

test('holdCommitmentBlock：单指数硬锁口径 max(7,窗口)、零赎费也列、深研日补重研句', () => {
  const w = '/tmp'
  const dc = {
    fundFees: [
      { fund_code: '510300', fund_name: '沪深300', redeem_tiers: [{ max_days: 7, rate_pct: 1.5 }, { max_days: null, rate_pct: 0 }] },
      { fund_code: '008591', fund_name: '零赎费基', redeem_tiers: [{ max_days: null, rate_pct: 0 }] },
    ],
  } as any

  // 单指数、普通日：显示硬锁口径，两只都列（含零赎费），无重研句
  const single = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot5d', quotesPath: '/q.json', dailyContext: dc })
  assert.match(single, /建仓 = 最短持有承诺/)
  assert.match(single, /510300/)
  assert.match(single, /锁 7 自然日/)           // 510300 窗口7 → max(7,7)=7
  assert.match(single, /008591/)                 // 零赎费基金也列
  assert.doesNotMatch(single, /可在后续深研日重研/)

  // 单指数、深研可触发日：补重研通道句
  const deepDay = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot5d', quotesPath: '/q.json', dailyContext: dc, deepResearchAuthorized: true })
  assert.match(deepDay, /可在后续深研日重研/)

  // 多指数 bot：不渲染该块
  const multi = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot105d', quotesPath: '/q.json', dailyContext: dc })
  assert.doesNotMatch(multi, /建仓 = 最短持有承诺/)
})
