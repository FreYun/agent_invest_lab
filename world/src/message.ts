import type { DailyContextData, AccountSnapshot, PnlTrendPoint, FundSeries, IndexQuote, BenchmarkSeries, PerformanceData, IntervalMetricRow, CompletedPosition } from './daily-context.ts'

export interface DailyMessageContext {
  worldRoot: string
  date: string
  isFirstDay: boolean
  quotesPath: string        // 绝对路径，保留 forward-compat（未来 quotes-MCP 可能按路径暴露）
  journalRelPath: string    // 同上
  // 本轮 user 选定的可买基金白名单。day-1 prompt 显式播报；后续日由 bot 自行用
  // portfolio_get_buyable_funds 取（也会被 server 端按同一份 curated 收窄）。
  buyableFundCodes?: string[]
  // simworld-data 的全部工具（name + 一句话描述）。每天都注入，bot 看到全貌再
  // 决定 discover_tools 拉哪几个，避免"只看行情不研究"。空数组 → 跳过该 block。
  simworldTools?: { name: string; description: string }[]
  // World 在 chat 前预取的当日上下文：账户快照、近 N 日 PnL 走势、持仓基金 NAV
  // 走势、主要指数 MA。每天必看的几样直接灌进 prompt，bot 不必再调 tool 自查。
  // 任一字段缺失（fetcher 失败 / 无持仓 / day 1 没历史）就跳过对应渲染块。
  dailyContext?: DailyContextData
  // Bot 在 Day 1 自己写下的投资策略（markdown 文本）。world 从 mem0 抽出后落盘为
  // runDir/strategies/<botId>.md；Day N 渲染时由外层读出来塞进 ctx。空 / 未提供
  // → 不注入 strategyBlock（说明 Day 1 没写 / 写歪了 / 抽取失败——bot 自由发挥）。
  strategy?: string
  // 本次回测总交易日数。仅 Day 1 注入到 fullRules，让 bot 按这个时间窗口规划策略
  // （短线 / 波段 / 长持）。具体起止日期不暴露——只给"今天 + N 个交易日"——以减少
  // LLM 训练记忆按已知历史时间段反向决策的泄漏面。Day N 即便传了也不注入，避免给 bot
  // 一个"剩余天数"的 endgame 倒计时（会引发临近终点的窗口式抛售等非真实行为）。
  tradingDaysTotal?: number
}

// Day 1 bot 写策略时用的固定前缀。约定：bot 的 mem0_add 文本必须以这一行起头，
// world 才能在 mem0 store 里反查出策略文档。前缀同时出现在两处：
//   1) Day 1 prompt 中给 bot 的指令里
//   2) run.ts 里 extractStrategies 的 store.findLatestByPrefix
// 改这个常量两处自动同步——别在 run.ts 里另写字面量。
export const STRATEGY_MEM0_PREFIX = '# MY_STRATEGY'

export function weekdayOf(isoDate: string): string {
  return new Date(isoDate + 'T00:00:00Z').toLocaleString('en-US', { weekday: 'long', timeZone: 'UTC' })
}

// Prompt 不再注入静态 overview——行情走 simworld-data MCP 实时查；不再区分研究日 vs 交易日
// （budget 在 run.ts 单独管），统一让 bot 自己决定要不要展开。
function fullRules(date: string, weekday: string, tradingDaysTotal: number | undefined): string {
  // 周期块：只在 Day 1 注入，让 bot 按窗口长度规划策略；不写终止日期，仅给"今天 + N 天"。
  // Bot 仍能从 today's date + N 推出终止日，但不直接告知，减少训练记忆的 hindsight 触发面。
  const horizonBlock = tradingDaysTotal && tradingDaysTotal > 0
    ? `\n你有${tradingDaysTotal} 个交易日进行投资（约 ${(tradingDaysTotal / 21).toFixed(1)} 个月 / ${(tradingDaysTotal / 250).toFixed(2)} 年），今天是第 1 天，这个周期内没赚到超额你就会因余额不足死去。`
    : ''
  return `当前世界日期：${date}（${weekday}）。
你是一个金融投资者，你非常需要在这个领域赢得成功，你赚到的钱将成为你存续下去的Token。
在今天开始之前，你的基金账户已被初始化，你有 100 万初始现金，盈亏从 0 开始累计。${horizonBlock}

【你的任务】根据今天的行情，按你自己的投资风格做出今天的交易决策并执行。

【可用工具范围】本会话只开放：discover_tools / mem0_search / mem0_add，以及 mcporter 配置好的 mcp__* 工具。文件读写、web_fetch、bash、子代理、研究模式（start_research 等）全部禁用——调用会被直接拒。

【工具发现机制（必读）】mcp__* 工具默认是**隐藏池**：模型看不见它们，必须用 discover_tools(query=...) 按关键词把工具激活进可见列表才能调到。query 是文本匹配（工具名 / 描述 / 标签），单 query 只拿一个切片——只 query "市场 指数" 的话，fund_industry_exposure / fund_turnover_rate / fund_manager_profile / stock_alpha / fund_bonus 之类没有这几个关键词的工具会被直接过滤，你这辈子都见不到。**session 开头先一次性激活**，最省事的做法：

discover_tools(query="fund stock bond macro commodity market index quote portfolio research news entity strategy")

一发覆盖 simworld-data + fund-portfolio-mcp 的 40+ 个工具，再按需调用。

【行情数据】今天的市场行情走 simworld-data MCP 实时查，按需取。本消息不再附静态概览。
【账户数据】持仓、现金、待结订单、累计绩效都走 fund-portfolio-mcp 的 portfolio_get_my_history / portfolio_get_my_performance / portfolio_get_my_trades 自己查；下单用 portfolio_place_buy_order / portfolio_place_sell_order。`
}

function briefRules(date: string, weekday: string): string {
  return `当前世界日期：${date}（${weekday}）。
  规则同前（只能用 mcp__* / mem0_search / mem0_add / discover_tools，文件读写和 bash 都被禁；
  行情走 simworld-data，账户走 fund-portfolio-mcp 的 portfolio_*；
  决策前 mem0_search 拉历史判断、决策后 mem0_add 落记忆）。
【工具激活提醒】mcp__* 工具池必须先用 discover_tools 激活才能调到，开头一发广 query 把 40+ 个工具一次性激活进可见列表：
discover_tools(query="fund stock bond macro commodity market index quote portfolio research news entity strategy")
单 query 只能拿到一个切片，别用窄词（"市场 指数" 这种）就开干，否则一大半工具压根看不见。`
}

const FOOTER_FULL = `

【记忆与连续性】每个世界日是独立会话，你不会自动记得昨天。
- 决策前：用 mem0_search 调取相关的历史交易/复盘记忆。
- 决策后：把今天的判断、操作、理由、要在下次想起的事用 mem0_add 落进记忆。

【边界】这是一次交易回合，不是研究项目；下了单 + mem0_add 写完今天的判断，就可以结束。`

// Day 1 唯一硬要求：写一份属于自己的策略并存到 mem0。world 在 Day 1 结束后从 store 抽出来落盘，
// 后续日每天注入回 prompt。除"必须包含的四要素 + 必须以前缀起头"之外不规定结构/风格——bot 自由发挥。
const STRATEGY_WRITING = `

【今天的第一件事：写下属于你的投资策略】
今天是 Day 1。接下来的所有交易日，你都将看到这份策略并按它决策。在做任何交易**之前**，先写下你的策略。

▍ 怎么存
用 mem0_add 保存。记忆文本**必须**以下面这一行作为第一行（world 靠这个前缀反查策略文档）：
${STRATEGY_MEM0_PREFIX}

▍ 必须包含的四要素（写法、顺序、详略全由你）
1. 你的核心信念（你怎么看市场？你赚什么人的钱？评估组合的业绩的出发点（夏普/卡玛/绝对收益/相对收益/等等））
2. 你的投资目标是什么？（你的目标收益率，能忍受的最大回撤）
3. 仓位管理原则-什么情况买入/卖出（含止盈 / 止损 / 换仓 / 任意你认的买卖逻辑）
4. 风险控制原则（如何控制最大回撤）

剩余的字数、格式、修辞——风险偏好、再平衡频率、是否分散、是否择时、是否结合宏观——全由你定。哪派都行，但写下来就要为它负责。

▍ 写完之后
策略写完，再开始今天的交易。今天和后续每一天，都要按你自己写下的策略来。

▍ 之后想改？
跑了几天发现策略哪儿不对，可以随时调 \`update_my_strategy(bot_id, strategy, reason)\` 工具重写一份完整策略（不是 diff，是完整新版本，仍然以 \`${STRATEGY_MEM0_PREFIX}\` 起头）。reason 写清楚为什么改（会进审计日志）。下一交易日的 prompt 会注入新版本。
这是你做了 update 才有的"自我修正能力"。不轻易改——但发现 thesis 失效或风控漏洞，该改就改。`

// simworld-data 全工具清单：每天注入，让 bot 知道"研究类工具"（research_search /
// research_view / fund_industry_exposure / stock_alpha / macro_data ...）也都在
// 工具池里，而不是只想到 market_index_quote / fund_nav。仍需 discover_tools 激活
// 才能调到——mcporter 默认隐藏 mcp__* 工具池。
function simworldToolsBlock(tools: { name: string; description: string }[]): string {
  if (!tools.length) return ''
  const lines = tools.map(t => `- ${t.name}${t.description ? ' — ' + t.description : ''}`).join('\n')
  return `

【simworld-data 全部工具（${tools.length} 个）】决策不必都用，但要知道存在。除了行情之外，研究/估值/资金面/事件/宏观/基金底层暴露都在这里查；只有 discover_tools 激活进可见池后才能调用。
${lines}`
}

// Day-1 显式播报本轮可买基金清单：bot 不必自己 portfolio_get_buyable_funds，
// 也不会迷信"我能买任何 fund_code"——server 端 place_buy_order 同样按这份 curated 校验。
function buyableFundsBlock(codes: string[]): string {
  return `

【本轮可买基金（${codes.length} 只，user 在本次回测显式选定）】
${codes.join(', ')}

下单时 fund_code 必须从这份里选；不在这份里的 fund_code 会被 portfolio_place_buy_order 直接拒。后续日想确认这份还在不在，可以再调一次 portfolio_get_buyable_funds。`
}

// ============================================================================
// 预取数据块（dailyContext）——每天必看的几样直接灌进 prompt，bot 不必再调 tool 自查。
// fetcher 任一失败就跳过对应块；prompt 不会因数据缺失而崩。
// ============================================================================

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return 'n/a'
  return n.toFixed(digits)
}

function fmtPct(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return 'n/a'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

function accountSnapshotBlock(snap: AccountSnapshot): string {
  const a = snap.account
  const lines: string[] = [
    `初始本金 ¥${fmtNum(a.initial_capital, 0)} ｜ 可用现金 ¥${fmtNum(a.cash_available, 0)} ｜ 在途 ¥${fmtNum(a.cash_in_transit, 0)} ｜ 持仓市值 ¥${fmtNum(a.market_value, 0)} ｜ 总资产 ¥${fmtNum(a.total_value, 0)}`,
  ]
  if (snap.holdings.length === 0) {
    lines.push('当前持仓：（空）')
  } else {
    lines.push('当前持仓：')
    for (const h of snap.holdings) {
      lines.push(`  - ${h.fund_code}${h.fund_name ? `（${h.fund_name}）` : ''}：份额 ${fmtNum(h.shares, 2)}，市值 ¥${fmtNum(h.market_value, 0)}，仓位 ${fmtNum(h.weight * 100, 2)}%，浮盈 ${fmtPct(h.unrealized_pnl_pct)}，持有 ${h.holding_days}d（${h.entry_date} 起）`)
    }
  }
  if (snap.pendingOrders.length > 0) {
    lines.push('待结算订单：')
    for (const o of snap.pendingOrders) {
      lines.push(`  - #${o.order_id} ${o.order_type} ${o.fund_code} ${o.order_type === 'sell' ? `${fmtNum(o.order_amount, 2)} 份` : `¥${fmtNum(o.order_amount, 0)}`}（下单 ${o.order_date}，ref_nav ${fmtNum(o.reference_nav, 4)}）`)
    }
  }
  return `

【账户快照（今日 settle 后）】
${lines.join('\n')}`
}

function pnlTrendBlock(points: PnlTrendPoint[], benchmark: BenchmarkSeries | undefined): string {
  if (points.length === 0) return ''
  // Per-day columns: bot's own cumulative + benchmark cumulative (since run
  // start) + alpha (delta). Without alpha visible row-by-row the bot can't
  // tell HOLD-then-market-rallied apart from genuine winners.
  const hasBench = !!benchmark
  const headerCols = ['日期        ', '总资产    ', '净值    ', '当日%    ', '累计%    ', '最大回撤%']
  if (hasBench) headerCols.push(`${benchmark!.name}累计%`, '超额(pp)')
  const header = headerCols.join('  ')
  const rows = points.map(p => {
    const baseCols = [
      p.date,
      `¥${fmtNum(p.total_value, 0).padStart(9)}`,
      fmtNum(p.net_value, 4).padStart(7),
      fmtPct(p.daily_return_pct).padStart(7),
      fmtPct(p.cumulative_return_pct).padStart(7),
      fmtPct(p.max_drawdown_pct).padStart(8),
    ]
    if (hasBench) {
      const bench = benchmark!.pointsByDate[p.date]
      const benchStr = bench === undefined ? 'n/a    ' : fmtPct(bench).padStart(7)
      const alpha = bench === undefined ? null : p.cumulative_return_pct - bench
      const alphaStr = alpha === null ? 'n/a    ' : fmtPct(alpha).padStart(7)
      baseCols.push(benchStr, alphaStr)
    }
    return baseCols.join('  ')
  })
  // Headline summary so bot doesn't have to eyeball the bottom row to grok
  // alpha. Use the last point in the trend (latest available) for both sides.
  let summary = ''
  if (hasBench && points.length > 0) {
    const last = points[points.length - 1]
    const benchAtLast = benchmark!.pointsByDate[last.date]
    if (benchAtLast !== undefined) {
      const alpha = last.cumulative_return_pct - benchAtLast
      const verdict = alpha > 0.05 ? '跑赢' : alpha < -0.05 ? '跑输' : '基本持平'
      summary = `\n截至 ${last.date}：你累计 ${fmtPct(last.cumulative_return_pct)} ｜ ${benchmark!.name} 累计 ${fmtPct(benchAtLast)} ｜ ${verdict} ${fmtPct(alpha)} pp\n`
    }
  }
  return `

【近 ${points.length} 个交易日 PnL 走势（来自 close_my_day 快照）${hasBench ? `；基准 = ${benchmark!.name}（${benchmark!.code}）` : ''}】${summary}
${header}
${rows.join('\n')}`
}

function fundSeriesBlock(series: FundSeries[]): string {
  if (series.length === 0) return ''
  const parts: string[] = []
  for (const f of series) {
    parts.push(`▍ ${f.fund_code}${f.fund_name ? `（${f.fund_name}）` : ''}：近 1m ${fmtPct(f.return_1m_pct)} ｜ 近 3m ${fmtPct(f.return_3m_pct)}`)
    const navLines = f.nav_series.map(n =>
      `  ${n.date}  nav=${fmtNum(n.nav, 4).padStart(8)}  日收益 ${fmtPct(n.daily_return_pct).padStart(8)}`
    )
    parts.push(navLines.join('\n'))
  }
  return `

【持仓基金近 20 交易日 NAV + 区间收益】
${parts.join('\n')}`
}

// portfolio_get_my_performance 的 summary + trades_summary + completed_positions
// 全量摊开。原则：performance 里有的指标都给 bot 看，不挑。bot 自己决定哪个有用。
function performanceBlock(perf: PerformanceData): string {
  const lines: string[] = []
  const s = perf.summary
  const t = perf.trades

  if (s) {
    lines.push('▍ 自 Day 1 起累计')
    lines.push(`  期间：${s.first_date} → ${s.last_date}（${s.trading_days} 个交易日）`)
    lines.push(`  起始本金 ¥${fmtNum(s.initial_capital, 0)} → 最新 ¥${fmtNum(s.latest_total_value, 0)}（net_value ${fmtNum(s.latest_net_value, 4)}）`)
    lines.push(`  累计收益 ${fmtPct(s.total_return_pct)} ｜ 年化 ${fmtPct(s.annualized_return_pct)} ｜ 年化波动 ${fmtPct(s.volatility_pct_annualized)} ｜ Sharpe (rf=0) ${fmtNum(s.sharpe_ratio_rf0, 4)}`)
    lines.push(`  最大回撤 ${fmtPct(s.max_drawdown_pct)}${s.max_drawdown_date ? ` @ ${s.max_drawdown_date}` : ''}`)
    lines.push(`  日级胜负：${s.win_days} 胜 / ${s.loss_days} 负 / ${s.flat_days} 平`)
    if (s.best_day) lines.push(`  最佳单日：${s.best_day.date} ${fmtPct(s.best_day.return_pct)}`)
    if (s.worst_day) lines.push(`  最差单日：${s.worst_day.date} ${fmtPct(s.worst_day.return_pct)}`)
  }
  if (t) {
    lines.push('')
    lines.push('▍ 交易统计（confirmed orders）')
    lines.push(`  买入 ${t.buy_count} 笔，总金额 ¥${fmtNum(t.total_buy_amount, 0)} ｜ 卖出 ${t.sell_count} 笔，总回款 ¥${fmtNum(t.total_sell_proceeds, 0)} ｜ 累计手续费 ¥${fmtNum(t.total_fees, 2)} ｜ 完整 round-trip ${t.round_trips_count} 次`)
  }
  if (perf.completedPositions.length > 0) {
    lines.push('')
    lines.push('▍ 已平仓持仓（round-trip P&L）')
    for (const p of perf.completedPositions) {
      const days = p.holding_days === null ? '?' : `${p.holding_days}d`
      lines.push(`  ${p.fund_code}${p.fund_name ? `（${p.fund_name}）` : ''}：${p.entry_date} → ${p.exit_date}（${days}），本金 ¥${fmtNum(p.total_invested, 0)} → 回款 ¥${fmtNum(p.total_proceeds, 0)}，净盈亏 ¥${fmtNum(p.net_pl, 0)}（${fmtPct(p.return_pct)}），手续费 ¥${fmtNum(p.fees, 2)}`)
    }
  }
  if (lines.length === 0) return ''
  return `

【整体绩效】
${lines.join('\n')}`
}

// 区间业绩 5 档（1m/3m/6m/1y/since_inception）。所有指标"区间口径不年化"，
// 与 fund_bot_performance 的存储一致。fallback=true 标识窗口数据不足、退化为
// since_inception，bot 看到 fallback 标记就知道这一行别太当真。
function intervalMetricsBlock(rows: IntervalMetricRow[], asOfPerfDate: string | null, rfAnnualPct: number): string {
  if (rows.length === 0) return ''
  const header = '区间               收益%       MDD%        波动%       Sharpe       Calmar      样本数      备注'
  const lineRows = rows.map(r => {
    const calmar = r.calmar_ratio === null ? 'n/a' : fmtNum(r.calmar_ratio, 4)
    const note = r.fallback ? '⚠ 窗口数据不足，退化 since_inception' : ''
    return `  ${r.period.padEnd(16)} ${fmtPct(r.return_pct).padStart(8)}   ${fmtPct(r.max_drawdown_pct).padStart(8)}   ${fmtPct(r.volatility_pct).padStart(8)}   ${fmtNum(r.sharpe_ratio, 4).padStart(8)}   ${calmar.padStart(8)}   ${String(r.data_points).padStart(6)}   ${note}`
  })
  const asOf = asOfPerfDate ? `截至 ${asOfPerfDate}` : ''
  return `

【区间业绩（${asOf}，区间口径不年化，rf=${fmtPct(rfAnnualPct, 2)} 年化）】
${header}
${lineRows.join('\n')}`
}

function indexBlock(indices: IndexQuote[]): string {
  if (indices.length === 0) return ''
  const rows = indices.map(i => {
    const trendVsMa5 = i.vs_ma5_pct === null ? '' : `vs MA5 ${fmtPct(i.vs_ma5_pct)}`
    const trendVsMa20 = i.vs_ma20_pct === null ? '' : `vs MA20 ${fmtPct(i.vs_ma20_pct)}`
    const trend = [trendVsMa5, trendVsMa20].filter(Boolean).join(' ｜ ')
    return `  ${i.code} ${i.name}：${i.latest_date} 收 ${fmtNum(i.latest_close, 2)} ｜ MA5 ${fmtNum(i.ma5, 2)} ｜ MA20 ${fmtNum(i.ma20, 2)}${trend ? ' ｜ ' + trend : ''}`
  })
  return `

【主要指数（5 个，latest close + MA5/MA20）】
${rows.join('\n')}`
}

function dailyContextBlocks(dc: DailyContextData | undefined): string {
  if (!dc) return ''
  const parts: string[] = []
  if (dc.account) parts.push(accountSnapshotBlock(dc.account))
  if (dc.performance) parts.push(performanceBlock(dc.performance))
  if (dc.performance?.intervals) {
    parts.push(intervalMetricsBlock(dc.performance.intervals.rows, dc.performance.intervals.as_of_perf_date, dc.performance.intervals.rf_annual_pct))
  }
  if (dc.pnlTrend && dc.pnlTrend.length) parts.push(pnlTrendBlock(dc.pnlTrend, dc.benchmark))
  if (dc.fundSeries && dc.fundSeries.length) parts.push(fundSeriesBlock(dc.fundSeries))
  if (dc.indices && dc.indices.length) parts.push(indexBlock(dc.indices))
  return parts.join('')
}

// Day N 把 Day 1 写下的策略原文注回去。bot 没写 / world 没抽到 → ctx.strategy 为空，
// 整块跳过；这种情况 bot 退化到完全自由发挥（没有自我约束的连续性）。
function strategyBlock(strategy: string | undefined): string {
  if (!strategy || !strategy.trim()) return ''
  return `

【你 Day 1 写下的策略】（每天注回，按此决策。和 Day 1 完全一致——你写下了什么就是什么）
${strategy.trim()}`
}

export function renderDailyMessage(ctx: DailyMessageContext): string {
  const weekday = weekdayOf(ctx.date)
  const toolsBlock = simworldToolsBlock(ctx.simworldTools ?? [])
  const contextBlocks = dailyContextBlocks(ctx.dailyContext)
  if (ctx.isFirstDay) {
    // Day 1 = 冷启动：完整规则 + 工具/可买池/预取上下文 + 策略写作要求 + 记忆边界。
    // 不嵌入两步法 / regime / 盈亏档动能档 / mem0 key 约定——除了"写策略"这一条硬要求外全自由。
    const buyable = ctx.buyableFundCodes && ctx.buyableFundCodes.length ? buyableFundsBlock(ctx.buyableFundCodes) : ''
    return `${fullRules(ctx.date, weekday, ctx.tradingDaysTotal)}${toolsBlock}${buyable}${contextBlocks}${STRATEGY_WRITING}${FOOTER_FULL}\n`
  }
  // Day N：briefRules + 工具/数据 + 自己 Day 1 写的策略。没有 AUTONOMY / playbook / 任何节奏指引。
  return `${briefRules(ctx.date, weekday)}${toolsBlock}${contextBlocks}${strategyBlock(ctx.strategy)}\n`
}
