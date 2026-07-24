// 盘中「当日研究室简报」装配：从 fund.db 的报告表按 PIT（as_of_date <= 世界日）取每类最新一份，
// 拼成一段 markdown 注入 decide 前的 daily message。数据源与多基金 marketReportsBlock 同源同表，
// 但只取「res 四研判室 + macro_news + market_context」这些宏观/背景研判，**不含** market_mainline（主线板块+基金池）
// 与 mainline_rotation（核心/卫星组合骨架）——那套是多基金配置 bot 的组合指令，单指数 bot 只能买一只指数基金，用不上。
//
// 为什么不再读 .openclaw/workspace-resN/memory 文件房间：那条路只按文件名取「最大日期」一份，不卡 as_of_date，
// 回测里会把 live 才写的 2026 报告注入进 2025 的决策日 → 未来函数。板块室/res14 未进 fund.db（只能读文件），
// 故本简报放弃板块维度，改喂 fund.db 里 PIT 齐全（2025 全年每日）的宏观研判。
import { runSqlite, sqlStr } from './strategy-server/server.ts'

interface ReportDef { table: 'res_reports' | 'market_reports'; type: string; label: string }

export const BRIEFING_REPORT_TYPES = [
  'market_context',
  'macro_news',
  'market_mainline',
  'mainline_rotation',
  'market_strategy',
  'policy_analysis',
  'intl_relations',
  'cross_market_linkage',
] as const
export type BriefingReportType = typeof BRIEFING_REPORT_TYPES[number]

// 注入清单（顺序即呈现顺序）。全部 scope='global'；单指数实验可按类型开关注入。
const BASE_BRIEFING_REPORTS: Array<ReportDef & { type: BriefingReportType }> = [
  { table: 'market_reports', type: 'market_context', label: 'market_context · 行情 / regime / 风险状态' },
  { table: 'market_reports', type: 'macro_news',     label: 'macro_news · 宏观 / 政策 / 事件资讯' },
  { table: 'market_reports', type: 'market_mainline', label: 'market_mainline · 主线板块 / 可投基金池' },
  { table: 'market_reports', type: 'mainline_rotation', label: 'mainline_rotation · 主线轮动 / 组合骨架' },
]

const RES_BRIEFING_REPORTS: Array<ReportDef & { type: BriefingReportType }> = [
  { table: 'res_reports',    type: 'market_strategy',      label: 'res · 市场策略研判' },
  { table: 'res_reports',    type: 'policy_analysis',      label: 'res · 政策分析' },
  { table: 'res_reports',    type: 'intl_relations',       label: 'res · 国际关系' },
  { table: 'res_reports',    type: 'cross_market_linkage', label: 'res · 跨市场联动' },
]

const SECTION_CHAR_CAP = 8000
const STALE_DAYS = 7

function daysBetween(from: string, to: string): number {
  const a = Date.parse(from + 'T00:00:00Z'), b = Date.parse(to + 'T00:00:00Z')
  if (Number.isNaN(a) || Number.isNaN(b)) return 0
  return Math.round((b - a) / 86400000)
}

// PIT 取一份：同 (report_type, scope) 历史回填可能多行 → as_of_date DESC, id DESC 取世界日当天或之前最新一份。
// 与 run.ts 的 readMarketReportsForInjection 口径一致。无匹配返回 null。
function readLatestPIT(fundDbPath: string, table: string, type: string, asOfDate: string): { date: string; content: string } | null {
  const sql = `SELECT as_of_date, content_md FROM ${table} WHERE report_type=${sqlStr(type)} AND scope='global' `
    + `AND as_of_date<=${sqlStr(asOfDate)} ORDER BY as_of_date DESC, id DESC LIMIT 1;`
  try {
    const out = runSqlite(fundDbPath, sql).trim()
    const rows = out ? (JSON.parse(out) as { as_of_date: string; content_md: string }[]) : []
    const r = rows[0]
    if (!r || !r.content_md || !r.content_md.trim()) return null
    return { date: r.as_of_date, content: r.content_md.trim() }
  } catch {
    return null
  }
}

// 返回简报正文 markdown（不含外层标题，由 message.ts 的 briefingBlock 包裹）。无任何可读内容时返回 ''。
export function assembleBriefing(opts: { fundDbPath: string; asOfDate: string; reports?: readonly BriefingReportType[] }): string {
  const sections: string[] = []
  const enabledReports = new Set(opts.reports ?? BRIEFING_REPORT_TYPES)
  const reportDefs = [
    ...BASE_BRIEFING_REPORTS,
    ...RES_BRIEFING_REPORTS,
  ].filter(def => enabledReports.has(def.type))
  for (const def of reportDefs) {
    const rpt = readLatestPIT(opts.fundDbPath, def.table, def.type, opts.asOfDate)
    if (!rpt) continue
    const ago = daysBetween(rpt.date, opts.asOfDate)
    const stale = ago >= STALE_DAYS ? ' · 已过期' : ''
    let body = rpt.content
    if (body.length > SECTION_CHAR_CAP) body = body.slice(0, SECTION_CHAR_CAP) + '\n…（已截断）'
    sections.push(`【${def.label} · 报告日 ${rpt.date} · 距今 ${ago} 天${stale}】\n${body}`)
  }
  return sections.length ? sections.join('\n\n---\n\n') : ''
}
