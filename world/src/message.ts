import { resolveOverview } from './overview.ts'

export interface DailyMessageContext {
  worldRoot: string
  date: string
  isFirstDay: boolean
  quotesPath: string        // 绝对路径
  journalRelPath: string    // 相对 bot workspace，例如 memory/trading/journal.md
}

export function weekdayOf(isoDate: string): string {
  return new Date(isoDate + 'T00:00:00Z').toLocaleString('en-US', { weekday: 'long', timeZone: 'UTC' })
}

function fullRules(date: string, weekday: string, quotesPath: string, journalRelPath: string): string {
  return `你现在身处一个金融模拟世界。当前世界日期：${date}（${weekday}）。
这是一个回放历史行情的沙盘——你只能看到 ${date} 当天及之前的信息，不存在「未来数据」。不要使用任何能取到当前真实时间或未来行情的工具/知识。

【你的任务】根据今天的行情，按你自己的投资风格做出今天的交易决策并执行。

【交易系统】交易引擎以工具形式提供（查持仓、查可用资金、下单、查成交、撤单等）。
不要凭记忆猜工具名——先用 discover_tools 看清楚有哪些交易工具，再调用。
你的持仓、现金、累计盈亏都从交易系统的工具里查；本消息不会替你列出来。

【行情数据】今天的全量行情在文件：${quotesPath}（需要细节就读它）。
下面是当天精简概览：`
}

function briefRules(date: string, weekday: string, quotesPath: string): string {
  return `金融模拟世界——当前世界日期：${date}（${weekday}）。规则同前（回放沙盘，无未来数据；先 discover_tools 看交易工具再下单；持仓/盈亏自己查；决策前读 ${'`'}memory/trading/journal.md${'`'} 并 mem0_search，决策后追加 journal 并 mem0_add；这是交易回合不是研究项目）。
今天的全量行情在文件：${quotesPath}。
下面是当天精简概览：`
}

const FOOTER_FULL = (journalRelPath: string) => `

【记忆与连续性】每个世界日是独立会话，你不会自动记得昨天。
- 决策前：读你的交易日志 ${journalRelPath}；用 mem0_search 调取相关的历史交易记忆。
- 决策后：把今天的判断、操作、理由追加到 ${journalRelPath}；把关键结论用 mem0_add 存进记忆。

【边界】这是一次交易回合，不是一个研究项目——可以快速查证，但不要开 start_research 大坑。
今天结束前，确保该下的单都下了、journal 写了。`

export function renderDailyMessage(ctx: DailyMessageContext): string {
  const weekday = weekdayOf(ctx.date)
  const overview = resolveOverview(ctx.worldRoot, ctx.date)
  if (ctx.isFirstDay) {
    return `${fullRules(ctx.date, weekday, ctx.quotesPath, ctx.journalRelPath)}\n\n${overview}${FOOTER_FULL(ctx.journalRelPath)}\n`
  }
  return `${briefRules(ctx.date, weekday, ctx.quotesPath)}\n\n${overview}\n`
}
