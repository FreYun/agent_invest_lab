export interface DailyMessageContext {
  worldRoot: string
  date: string
  isFirstDay: boolean
  quotesPath: string        // 绝对路径，保留 forward-compat（未来 quotes-MCP 可能按路径暴露）
  journalRelPath: string    // 同上
  // 本轮 user 选定的可买基金白名单。day-1 prompt 显式播报；后续日由 bot 自行用
  // portfolio_get_buyable_funds 取（也会被 server 端按同一份 curated 收窄）。
  buyableFundCodes?: string[]
}

export function weekdayOf(isoDate: string): string {
  return new Date(isoDate + 'T00:00:00Z').toLocaleString('en-US', { weekday: 'long', timeZone: 'UTC' })
}

// Prompt 不再注入静态 overview——行情走 simworld-data MCP 实时查；不再区分研究日 vs 交易日
// （budget 在 run.ts 单独管），统一让 bot 自己决定要不要展开。
function fullRules(date: string, weekday: string): string {
  return `当前世界日期：${date}（${weekday}）。
你是一个金融投资者，你非常需要在这个领域赢得成功，你赚到的钱将成为你存续下去的Token。
在今天开始之前，你的基金账户已被初始化，你有 100 万初始现金，盈亏从 0 开始累计。

【你的任务】根据今天的行情，按你自己的投资风格做出今天的交易决策并执行。

【可用工具范围】本会话只开放：discover_tools / mem0_search / mem0_add，以及 mcporter 配置好的 mcp__* 工具。文件读写、web_fetch、bash、子代理、研究模式（start_research 等）全部禁用——调用会被直接拒。

【工具发现机制（必读）】mcp__* 工具默认是**隐藏池**：模型看不见它们，必须用 discover_tools(query=...) 按关键词把工具激活进可见列表才能调到。query 是文本匹配（工具名 / 描述 / 标签），单 query 只拿一个切片——只 query "市场 指数" 的话，fund_industry_exposure / fund_turnover_rate / fund_manager_profile / stock_alpha / fund_bonus 之类没有这几个关键词的工具会被直接过滤，你这辈子都见不到。**session 开头先一次性激活**，最省事的做法：

discover_tools(query="fund stock bond macro commodity market index quote portfolio research news entity")

一发覆盖 simworld-data + fund-portfolio-mcp 的 40+ 个工具，再按需调用。

【行情数据】今天的市场行情走 simworld-data MCP 实时查，按需取。本消息不再附静态概览。
【账户数据】持仓、现金、待结订单、累计绩效都走 fund-portfolio-mcp 的 portfolio_get_my_history / portfolio_get_my_performance / portfolio_get_my_trades 自己查；下单用 portfolio_place_buy_order / portfolio_place_sell_order。`
}

function briefRules(date: string, weekday: string): string {
  return `当前世界日期：${date}（${weekday}）。规则同前（回放沙盘，无未来数据；只能用 mcp__* / mem0_search / mem0_add / discover_tools，文件读写和 bash 都被禁；行情走 simworld-data，账户走 fund-portfolio-mcp 的 portfolio_*；决策前 mem0_search 拉历史判断、决策后 mem0_add 落记忆）。
【工具激活提醒】mcp__* 工具池每天重置 + 默认隐藏，必须先用 discover_tools 激活才能调到。开头一发广 query 把 40+ 个工具一次性激活进可见列表：
discover_tools(query="fund stock bond macro commodity market index quote portfolio research news entity")
单 query 只能拿到一个切片，别用窄词（"市场 指数" 这种）就开干，否则一大半工具压根看不见。`
}

const FOOTER_FULL = `

【记忆与连续性】每个世界日是独立会话，你不会自动记得昨天。
- 决策前：用 mem0_search 调取相关的历史交易/复盘记忆。
- 决策后：把今天的判断、操作、理由、要在下次想起的事用 mem0_add 落进记忆。

【边界】这是一次交易回合，不是研究项目；下了单 + mem0_add 写完今天的判断，就可以结束。`

// 给 bot 的自治节奏：平静日就快速收摊；遇到大事再展开调研轮数。区别于以前固定的研究日 vs 交易日 banner。
const AUTONOMY = `
【今天的节奏由你定】
- 先用 simworld-data 看一眼今天的市场（指数 / 自己的核心持仓 / 任何前期调研在意的标的）
- 没有显著波动 / 黑天鹅 / 重大宏观事件 → 简短判断（持有/微调/不动）+ mem0_add 收尾，**别拉长**
- 真有大事（板块剧震、政策窗口、自己持仓里的基金大幅偏离预期）→ 展开多轮调研，对比、验证、再决策、再 mem0_add
- 决定权在你，平静日浪费 chat 轮数没意义，乱世惜时也是浪费机会`

// Day-1 显式播报本轮可买基金清单：bot 不必自己 portfolio_get_buyable_funds，
// 也不会迷信"我能买任何 fund_code"——server 端 place_buy_order 同样按这份 curated 校验。
function buyableFundsBlock(codes: string[]): string {
  return `

【本轮可买基金（${codes.length} 只，user 在本次回测显式选定）】
${codes.join(', ')}

下单时 fund_code 必须从这份里选；不在这份里的 fund_code 会被 portfolio_place_buy_order 直接拒。后续日想确认这份还在不在，可以再调一次 portfolio_get_buyable_funds。`
}

export function renderDailyMessage(ctx: DailyMessageContext): string {
  const weekday = weekdayOf(ctx.date)
  if (ctx.isFirstDay) {
    // Day 1 = 冷启动：发完整规则 + 落地约束，不附 AUTONOMY（首日还在熟悉工具，硬性引导多一些）
    const buyable = ctx.buyableFundCodes && ctx.buyableFundCodes.length ? buyableFundsBlock(ctx.buyableFundCodes) : ''
    return `${fullRules(ctx.date, weekday)}${buyable}${FOOTER_FULL}\n`
  }
  return `${briefRules(ctx.date, weekday)}${AUTONOMY}\n`
}
