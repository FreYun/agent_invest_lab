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
  assert.match(withCtx, /1m\s+\+0\.02%\s+\+5\.17%/)
  assert.match(withCtx, /since_inception\s+\+0\.02%\s+\+5\.17%/)
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
  // FOOTER_BRIEF 补三条：(1) 决策必须当日执行；(2) 列了 todo 就要做（或明确说放下并写理由）；(3) 结束前必须 mem0_add。
  const w = tmpWorldWithOverview('2024-03-19', 'overview')

  // Day N 必须带上 footer 的三条规则
  const brief = renderDailyMessage({ worldRoot: w, date: '2024-03-19', isFirstDay: false, botId: 'bot-test', quotesPath: '/q.json' })
  assert.match(brief, /结束之前必做/)
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

test('Day N 复盘日（trading_days % 5 === 0）注入策略强制复盘硬契约：第一步定性大趋势 → 第二步业绩对照 → 第三步二选一；非复盘日不注入', () => {
  // 这个 hook 历史上被悄悄从 message.ts 删过一次（dash-2026-06-01 run 带着它跑，之后 working tree 丢了）。
  // 这条测试是 guard：① 复盘日（每 5 个交易日）必须注入硬契约；② 三步结构齐全（用户要求"先定性大趋势"）；
  // ③ 你 vs 躺平的 alpha / 回撤差口径正确；④ 非复盘日（trading_days=4）返回空、不注入。
  const w = tmpWorldWithOverview('2024-03-25', 'overview')
  const perf = {
    asOfDate: '2024-03-25',
    summary: {
      first_date: '2024-03-15', last_date: '2024-03-25', trading_days: 5,
      initial_capital: 1_000_000, latest_total_value: 1_030_000, latest_net_value: 1.03,
      total_return_pct: 3.0, annualized_return_pct: 9.0, max_drawdown_pct: -8.0, max_drawdown_date: '2024-03-20',
      volatility_pct_annualized: 5.0, sharpe_ratio_rf0: 0.5,
      win_days: 3, loss_days: 2, flat_days: 0,
      best_day: null, worst_day: null,
    },
    trades: { buy_count: 0, sell_count: 0, total_buy_amount: 0, total_sell_proceeds: 0, total_fees: 0, round_trips_count: 0 },
    intervals: null,
    completedPositions: [],
  }
  const benchmark = {
    code: '000300.SH', name: '沪深300', pointsByDate: {}, latestCumulativePct: 10.0,
    metrics: { return_pct: 10.0, max_drawdown_pct: -5.0, volatility_pct: 6.0, sharpe_ratio: 0.6, calmar_ratio: 2.0, data_points: 5 },
  }
  const review = renderDailyMessage({
    worldRoot: w, date: '2024-03-25', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { performance: perf, benchmark },
  })
  // 硬契约标题 + 三步结构
  assert.match(review, /【⚠ 第 5 个交易日 · 策略强制复盘（每 5 个交易日一次，今天不可跳过）】/)
  assert.match(review, /▍ 第一步 · 先对当前市场大趋势做一句话定性判断/)
  assert.match(review, /▍ 第二步 · 你 vs 沪深300（不择时买入持有）/)
  assert.match(review, /▍ 第三步 · 今天必须二选一/)
  // alpha = 3.0 - 10.0 = -7.00pct（跑输）；ddGap = -8.0 - (-5.0) = -3.00pct（你回撤更深）
  assert.match(review, /超额 -7\.00pct（你已跑输躺平 7\.00pct）/)
  assert.match(review, /差 -3\.00pct（负=你回撤更深）/)
  // 二选一里的 update_my_strategy 带上字面 bot_id
  assert.match(review, /update_my_strategy\(bot_id="bot7"/)
  // 复盘块在最末尾（recency 最高）
  assert.match(review.trimEnd(), /该改就改，别用"不轻易改"麻痹自己。$/)

  // 非复盘日（trading_days=4）→ 不注入
  const perf4 = { ...perf, summary: { ...perf.summary, trading_days: 4 } }
  const noReview = renderDailyMessage({
    worldRoot: w, date: '2024-03-25', isFirstDay: false, botId: 'bot7', quotesPath: '/q.json',
    dailyContext: { performance: perf4, benchmark },
  })
  assert.doesNotMatch(noReview, /策略强制复盘/)
  rmSync(w, { recursive: true, force: true })
})
