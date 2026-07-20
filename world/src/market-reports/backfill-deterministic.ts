// market-reports 确定性历史回补 —— 不走 LLM reporter，直接用 v5 引擎逐月生成
// market_mainline / mainline_rotation 两类报告并落 fund.db 的 market_reports 表。
//
// 口径（2026-06-10 与用户对齐）：
//   - 主线真值 = scripts/v5_mainline_plan.py（get_v5_mainline_plan 的底层引擎：
//     月度 v4 状态机 + board_fund_match 双测度），与已验证 v5 回测同口径。
//   - rotation 的核心/卫星组合直接取 v5 月度状态机；「连续 in_top5 / 破 MA60 / 出 top15
//     天数」按 board_trend_daily 日度数据真算，仅作展示与"距触发还差几日"提示，
//     不构成另一套日度状态机。
//   - market_context 不在本驱动范围（由 prepass LLM 回补，见 world-market-reports-context-only.yaml）。
//
// 与 prepass-driver 同样的工程语义：独立编排、默认幂等跳过已存在 (report_type, as_of_date)、
// 中断重跑即续、写库走 sqlite3 CLI（fund.db 是 WAL、可能被 live run 并发写，带 busy timeout）。
//
// 用法（在 world/ 下）：
//   node --experimental-strip-types src/market-reports/backfill-deterministic.ts \
//     --from 2025-02 --to 2026-06 [--fund-db <path>] [--run-id <id>] [--force]

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve, join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadCalendar, computeTradingDates } from '../calendar.ts'
import {
  ensureMarketReportsTable, runSqlite, sqlStr,
  normalizeMarketMainlineSubmission, type V5MainlinePlanForReport,
} from '../strategy-server/server.ts'

const HERE = dirname(fileURLToPath(import.meta.url))           // world/src/market-reports
const REPO_ROOT = resolve(HERE, '..', '..', '..')              // agent_invest_lab
const V5_SCRIPT = join(REPO_ROOT, 'scripts', 'v5_mainline_plan.py')
const SCOUT_DB = process.env.SCOUT_DB ?? join(REPO_ROOT, 'data', 'market.db')
const COARSE_THEMES = process.env.COARSE_THEMES ?? join(REPO_ROOT, 'market_pipeline', 'openclaw', 'scout', 'coarse_themes.json')

// v5 阈值（与 v5_mainline_plan.py / skill 文本一致，仅用于日度计数器展示）
const K_ENTRY = 5
const TOPK_EXIT = 15
const DEFENSE_DIST = -0.03
const DOMINANT_MIN = 4
const LOOKBACK_TDAYS = 130   // regime_days / 计数器最多回看的交易日数

// skill 日度纪律阈值（仅用于叙事「距触发还差几日」的刻度，真实换仓走 v5 月度状态机）
const CORE_PROMOTE_DAYS = 40 // 连续≥40交易日 top5 且站 MA60 → 晋核心
const SAT_ENTRY_DAYS = 5     // 连续≥5日 top3 → 纳卫星
const BREAK_MA60_EXIT = 3    // 连续≥3日破 MA60 → 剔除
const OUT_TOP15_EXIT = 20    // 连续≥20日出 top15 → 剔除
const HYSTERESIS = 0.03      // 年线迟滞带 ±3%

// ── 基础工具 ────────────────────────────────────────────────────────────────

function argVal(argv: string[], flag: string): string | undefined {
  const i = argv.indexOf(flag)
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined
}

function log(msg: string): void { process.stdout.write(`[backfill-det] ${msg}\n`) }

const ymd = (s: string): string => s.replace(/-/g, '').slice(0, 8)
const dashed = (s: string): string => `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`

/** SELECT（sqlite3 -json）→ 行数组。空结果 → []。 */
function queryJson(dbPath: string, sql: string): Array<Record<string, unknown>> {
  const out = runSqlite(dbPath, sql).trim()
  return out ? JSON.parse(out) as Array<Record<string, unknown>> : []
}

/** 取窗口内每个自然月的第一个交易日（与 prepass-driver 同逻辑）。 */
function monthFirstTradingDays(dates: string[]): string[] {
  const out: string[] = []
  let lastKey = ''
  for (const d of dates) {
    const key = d.slice(0, 7)
    if (key !== lastKey) { out.push(d); lastKey = key }
  }
  return out
}

/** 日期所在 ISO 周的周一（YYYY-MM-DD），作为周分组 key。 */
function isoWeekMonday(d: string): string {
  const dt = new Date(`${d}T00:00:00Z`)
  dt.setUTCDate(dt.getUTCDate() - (dt.getUTCDay() + 6) % 7)  // Mon=0..Sun=6 回退到周一
  return dt.toISOString().slice(0, 10)
}

/** 取窗口内每个 ISO 周（周一起算）的第一个交易日。 */
function weekFirstTradingDays(dates: string[]): string[] {
  const out: string[] = []
  let lastKey = ''
  for (const d of dates) {
    const key = isoWeekMonday(d)
    if (key !== lastKey) { out.push(d); lastKey = key }
  }
  return out
}

function reportExists(dbPath: string, reportType: string, asOf: string): boolean {
  const rows = queryJson(dbPath, `SELECT 1 AS x FROM market_reports WHERE report_type=${sqlStr(reportType)} AND scope='global' AND as_of_date=${sqlStr(asOf)} LIMIT 1;`)
  return rows.length > 0
}

function upsertReport(dbPath: string, reportType: string, asOf: string, contentMd: string, structuredJson: string, runId: string): void {
  const sql = `INSERT INTO market_reports (report_type, as_of_date, scope, content_md, structured_json, agent_run_id) `
    + `VALUES (${sqlStr(reportType)}, ${sqlStr(asOf)}, 'global', ${sqlStr(contentMd)}, ${sqlStr(structuredJson)}, ${sqlStr(runId)}) `
    + `ON CONFLICT(report_type, as_of_date, scope) DO UPDATE SET `
    + `content_md=excluded.content_md, structured_json=excluded.structured_json, `
    + `agent_run_id=excluded.agent_run_id, generated_at=datetime('now');`
  runSqlite(dbPath, sql)
}

// ── v5 引擎调用（不带 --compact：需要 state_history 做相邻期 diff）───────────

type V5Holding = { code: string; name: string; role: string; rank: number | null; above_ma60: boolean; since: string; out_count: number }
type V5Plan = V5MainlinePlanForReport & {
  top15?: Array<{ code: string; name: string; rank: number; group: string; above_ma60: boolean; ret60: number; ret20: number; ret5: number }>
  state_history?: Array<{ trade_date: string; holdings: V5Holding[] }>
}

function loadV5Plan(date: string): V5Plan {
  const out = execFileSync('python3', [V5_SCRIPT, '--date', date, '--include-funds'], {
    encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
  })
  return JSON.parse(out) as V5Plan
}

// ── 日度数据缓存（scout market.db，只读）────────────────────────────────────
// 每个决策日批量拉一次，本地算 regime_days 与各板块连续天数计数器。

type DayCache = {
  tdaysDesc: string[]                                    // 截至决策日的交易日（raw，降序，最多 LOOKBACK_TDAYS+1）
  tdaysDescFull: string[]                                // 同上但含 MA120 余量（LOOKBACK+120），供逐日算均线
  hs300Close: Map<string, number>                        // raw date → close（多取 120 日供 MA120）
  top15ByDay: Map<string, Array<{ code: string; group: string }>>
  boardByDay: Map<string, Map<string, { rank: number; above_ma60: number }>>  // raw date → board_code → row
}

function loadGroups(): Map<string, string> {
  const themes = JSON.parse(readFileSync(COARSE_THEMES, 'utf8')) as { groups?: Array<{ name: string; boards?: Array<{ code: string }> }> }
  const m = new Map<string, string>()
  for (const g of themes.groups ?? []) for (const b of g.boards ?? []) m.set(b.code, g.name)
  return m
}

function buildDayCache(decisionRaw: string, tradingRawAll: string[], boardCodes: string[], groups: Map<string, string>): DayCache {
  const di = tradingRawAll.indexOf(decisionRaw)
  const lo = Math.max(0, (di < 0 ? tradingRawAll.length : di) - (LOOKBACK_TDAYS + 120))
  const windowRaw = tradingRawAll.slice(lo, (di < 0 ? tradingRawAll.length : di + 1))
  const cutoff = windowRaw[0]
  const tdaysDescFull = [...windowRaw].reverse()
  const tdaysDesc = tdaysDescFull.slice(0, LOOKBACK_TDAYS + 1)

  const hs300Close = new Map<string, number>()
  for (const r of queryJson(SCOUT_DB,
    `SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' AND trade_date>=${sqlStr(cutoff)} AND trade_date<=${sqlStr(decisionRaw)};`)) {
    hs300Close.set(String(r.trade_date), Number(r.close))
  }

  const top15ByDay = new Map<string, Array<{ code: string; group: string }>>()
  for (const r of queryJson(SCOUT_DB,
    `SELECT trade_date, board_code FROM board_trend_daily WHERE rank<=15 AND trade_date>=${sqlStr(cutoff)} AND trade_date<=${sqlStr(decisionRaw)};`)) {
    const d = String(r.trade_date), code = String(r.board_code)
    if (!top15ByDay.has(d)) top15ByDay.set(d, [])
    top15ByDay.get(d)!.push({ code, group: groups.get(code) ?? '其他' })
  }

  const boardByDay = new Map<string, Map<string, { rank: number; above_ma60: number }>>()
  if (boardCodes.length) {
    const inList = boardCodes.map(sqlStr).join(',')
    for (const r of queryJson(SCOUT_DB,
      `SELECT trade_date, board_code, rank, above_ma60 FROM board_trend_daily WHERE board_code IN (${inList}) AND trade_date>=${sqlStr(cutoff)} AND trade_date<=${sqlStr(decisionRaw)};`)) {
      const d = String(r.trade_date)
      if (!boardByDay.has(d)) boardByDay.set(d, new Map())
      boardByDay.get(d)!.set(String(r.board_code), { rank: Number(r.rank), above_ma60: Number(r.above_ma60) })
    }
  }
  return { tdaysDesc, tdaysDescFull, hs300Close, top15ByDay, boardByDay }
}

/** 某交易日的 v5 regime（与 v5_mainline_plan.py 的 regime() 同口径）。 */
function regimeOnDay(cache: DayCache, rawDate: string): string {
  // MA120：取该日（含）往前 120 个交易日的 close
  const di = cache.tdaysDescFull.indexOf(rawDate)
  if (di < 0) return '数据不足'
  const closes: number[] = []
  for (let i = di; i < cache.tdaysDescFull.length && closes.length < 120; i++) {
    const c = cache.hs300Close.get(cache.tdaysDescFull[i])
    if (c !== undefined) closes.push(c)
  }
  if (closes.length < 120) return '数据不足'
  const dist = closes[0] / (closes.reduce((a, b) => a + b, 0) / closes.length) - 1
  if (dist < DEFENSE_DIST) return '防御·红利'
  const counts = new Map<string, number>()
  for (const t of cache.top15ByDay.get(rawDate) ?? []) counts.set(t.group, (counts.get(t.group) ?? 0) + 1)
  const domCount = Math.max(0, ...counts.values())
  return domCount >= DOMINANT_MIN ? '抱主线·v4' : '无主线·宽基'
}

/** regime 连续维持天数（从锚定日往回数同名 regime）。
 *  anchorRaw 缺省 = 从 cache 最新日（tdaysDesc[0]）起算（monthly/weekly：cache 即建在决策日）；
 *  daily 下 cache 建在「实际交易日」、但 regime 节锚定 v5「月首决策日」以与所示 dist/集中度证据自洽，
 *  故传决策日做锚：先跳到该日再起算。 */
function regimeDays(cache: DayCache, currentRegime: string, anchorRaw?: string): number {
  let n = 0
  let started = anchorRaw === undefined
  for (const d of cache.tdaysDesc) {
    if (!started) { if (d === anchorRaw) started = true; else continue }
    if (regimeOnDay(cache, d) === currentRegime) n++
    else break
    if (n >= LOOKBACK_TDAYS) break
  }
  return n
}

/** 单板块日度连续计数器。缺行语义与 v5 状态机一致：无行 = rank 999（出 top15）+ 破 MA60。 */
function boardCounters(cache: DayCache, boardCode: string): { in_top5_days: number; out_top15_days: number; break_ma60_days: number } {
  let inTop5 = 0, outTop15 = 0, breakMa60 = 0
  let inTop5Done = false, outTop15Done = false, breakMa60Done = false
  for (const d of cache.tdaysDesc) {
    const row = cache.boardByDay.get(d)?.get(boardCode)
    const rank = row ? row.rank : 999
    const above = row ? row.above_ma60 === 1 : false
    if (!inTop5Done) { if (rank <= K_ENTRY) inTop5++; else inTop5Done = true }
    if (!outTop15Done) { if (rank > TOPK_EXIT) outTop15++; else outTop15Done = true }
    if (!breakMa60Done) { if (!above) breakMa60++; else breakMa60Done = true }
    if (inTop5Done && outTop15Done && breakMa60Done) break
  }
  return { in_top5_days: inTop5, out_top15_days: outTop15, break_ma60_days: breakMa60 }
}

/** 交易日计数（含两端），用于 held_days。 */
function tradingDaysBetween(tradingRawAll: string[], sinceRaw: string, untilRaw: string): number {
  const a = tradingRawAll.indexOf(sinceRaw), b = tradingRawAll.indexOf(untilRaw)
  if (a < 0 || b < 0 || b < a) return 0
  return b - a + 1
}

// ── 报告渲染 ────────────────────────────────────────────────────────────────

const BACKFILL_NOTE = '> 本报告由确定性回补管线（backfill-deterministic，v5 引擎）生成：主线/组合真值取 '
  + '`get_v5_mainline_plan` 同源计算，无 LLM 叙事与研报/催化剂判读段。'

type FundSel = { code?: string; name?: string; index_name?: string; verdict?: string; overlap?: number; corr?: number; beta?: number; scale_yi?: number }

function fmtPct(v: unknown, digits = 1): string {
  return typeof v === 'number' && Number.isFinite(v) ? (v * 100).toFixed(digits) + '%' : '—'
}
function fmtNum(v: unknown, digits = 2): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—'
}

/** market_mainline 正文（reporter-mainline AGENTS 正文结构 ①~④ 的确定性版本）。 */
function renderMainlineBody(plan: V5Plan): string {
  const rg = plan.regime ?? {}
  const conc = rg.concentration ?? {}
  const holdings = (plan.v4_holdings ?? []) as unknown as V5Holding[]
  const top15 = plan.top15 ?? []
  const fm = plan.fund_matches ?? []
  const lines: string[] = []
  lines.push(BACKFILL_NOTE, '')
  lines.push('## ① 主线结论')
  if ((rg.name ?? '').startsWith('抱主线')) {
    lines.push(`- 主线主题：**${conc.dominant_group ?? '—'}**（top15 集中度 ${conc.dominant_count ?? '—'} 个）`)
    lines.push(`- 主线板块：${holdings.map(h => `${h.code} ${h.name}[${h.role}]`).join('、') || '—'}`)
  } else if ((rg.name ?? '').startsWith('防御')) {
    lines.push('- **无可投主线（防御态）**：沪深300 跌破年线超 3%，全退红利。')
  } else {
    lines.push('- **无主线**：主线发散（top15 最大一类 <4 个），方向 = 全市场宽基。')
  }
  lines.push('', '## ② v5 判断证据')
  lines.push(`- regime：**${rg.name ?? '—'}**；HS300 距 MA120：${fmtPct(rg.hs300_vs_ma120, 2)}`)
  lines.push(`- top15 分组计数：${Object.entries(conc.counts ?? {}).map(([g, n]) => `${g}=${n}`).join('、') || '—'}`)
  if (top15.length) {
    lines.push('', '| rank | 板块 | 分组 | 站MA60 | ret20 |', '|---|---|---|---|---|')
    for (const t of top15) lines.push(`| ${t.rank} | ${t.code} ${t.name} | ${t.group} | ${t.above_ma60 ? '√' : '✗'} | ${fmtPct(t.ret20)} |`)
  }
  lines.push('', '## ③ 可投基金池（board_fund_match 双测度）')
  if (fm.length) {
    lines.push('| 主线板块 | 角色 | 基金 | 档位 | 重叠 | 相关 | β | 规模(亿) |', '|---|---|---|---|---|---|---|---|')
    for (const m of fm) {
      const s = (m.selected ?? {}) as FundSel
      lines.push(`| ${m.board_code} ${m.board_name ?? ''} | ${m.role ?? ''} | ${s.code ? `${s.code} ${s.name ?? ''}` : '（无匹配）'} | ${s.verdict ?? '—'} | ${fmtPct(s.overlap)} | ${fmtNum(s.corr)} | ${fmtNum(s.beta)} | ${fmtNum(s.scale_yi, 1)} |`)
    }
  } else {
    const p0 = (plan.portfolio ?? [])[0] as Record<string, unknown> | undefined
    lines.push(`- 非抱主线态，底线产品：${p0 ? `${p0.code} ${p0.name}（${p0.role}）` : '—'}`)
  }
  lines.push('', '## ④ 候选备选')
  for (const m of fm) {
    const cands = (m.candidates ?? []) as FundSel[]
    if (cands.length > 1) lines.push(`- ${m.board_code}：${cands.slice(1).map(c => `${c.code} ${c.name}(${c.verdict}, corr=${fmtNum(c.corr)})`).join('、')}`)
  }
  return lines.join('\n')
}

type RotationRow = { role: string; sector: string | null; fund_code: string | null; fund_name: string | null; held_days: number; above_ma60: boolean | null; crowding_pct: null }
// 跨期 diff 用：板块代码 → 中文角色（核心/卫星/防御/宽基）。
type HoldIdentity = { code: string; role: string }

/** mainline_rotation 报告（reporter-rotation AGENTS 正文结构 ①~⑤ + structured 契约）。 */
function renderRotation(
  plan: V5Plan, cache: DayCache, tradingRawAll: string[], decisionRaw: string,
  prev: { regime: string; identity: HoldIdentity[] } | null,
  regimeAnchorRaw?: string,
): { content: string; structured: string; identity: HoldIdentity[] } {
  const rg = plan.regime?.name ?? '—'
  const holdings = (plan.v4_holdings ?? []) as unknown as V5Holding[]
  const fm = new Map((plan.fund_matches ?? []).map(m => [m.board_code ?? '', (m.selected ?? null) as FundSel | null]))
  const isMainline = rg.startsWith('抱主线')
  const rgDays = regimeDays(cache, rg, regimeAnchorRaw)

  // 组合行：抱主线 → v4 核心/卫星 + 双测度基金；其余 → v5 的红利/宽基底线 ETF
  const portfolio: RotationRow[] = []
  const identity: HoldIdentity[] = []
  if (isMainline) {
    for (const h of holdings) {
      const sel = fm.get(h.code) ?? null
      portfolio.push({
        role: h.role === '核心' ? 'core' : 'satellite',
        sector: `${h.code} ${h.name}`,
        fund_code: sel?.code ?? null,
        fund_name: sel?.name ?? null,
        held_days: tradingDaysBetween(tradingRawAll, ymd(h.since), decisionRaw),
        above_ma60: h.above_ma60,
        crowding_pct: null,
      })
      identity.push({ code: h.code, role: h.role })
    }
  } else {
    for (const p of (plan.portfolio ?? []) as Array<Record<string, unknown>>) {
      portfolio.push({
        role: String(p.role ?? ''), sector: null,
        fund_code: String(p.code ?? ''), fund_name: String(p.name ?? ''),
        held_days: 0, above_ma60: null, crowding_pct: null,
      })
      identity.push({ code: String(p.code ?? ''), role: String(p.role ?? '') })
    }
  }

  // 计数器：在管板块（v4 holdings）∪ 当日 top5 候选
  const counterCodes = new Map<string, string>()
  for (const h of holdings) counterCodes.set(h.code, h.name)
  for (const t of plan.top15 ?? []) if (t.rank <= K_ENTRY) counterCodes.set(t.code, t.name)
  const counters = [...counterCodes.entries()].map(([code, name]) => ({
    sector: `${code} ${name}`, ...boardCounters(cache, code),
  }))

  // 今日动作：与上一期组合按板块代码 diff（区分真进出 vs 角色升降）
  // prev=null = 序列首期（如 2025-01-02，v5 状态机起点，无上一期可比）→ 首期建仓
  let todayAction = '维持不动'
  if (!prev) {
    if (portfolio.length) todayAction = `首期建仓：${portfolio.map(p => p.fund_code ?? p.sector).join('、')}`
  } else if (prev) {
    if (prev.regime !== rg) {
      todayAction = `regime 切换：${prev.regime} → ${rg}，组合切至 ${portfolio.map(p => p.fund_code ?? p.sector).join('、')}`
    } else {
      const prevMap = new Map(prev.identity.map(h => [h.code, h.role]))
      const curMap = new Map(identity.map(h => [h.code, h.role]))
      const added = identity.filter(h => !prevMap.has(h.code))
      const removed = prev.identity.filter(h => !curMap.has(h.code))
      const changed = identity.filter(h => prevMap.has(h.code) && prevMap.get(h.code) !== h.role)
      const parts: string[] = []
      if (added.length) parts.push(`新进 ${added.map(h => `${h.role}${h.code}`).join('、')}`)
      if (changed.length) parts.push(`升降 ${changed.map(h => `${h.code} ${prevMap.get(h.code)}→${h.role}`).join('、')}`)
      if (removed.length) parts.push(`剔除 ${removed.map(h => `${h.role}${h.code}`).join('、')}（v4 状态机：破MA60 或 连续2期出top${TOPK_EXIT}）`)
      if (parts.length) todayAction = parts.join('；')
    }
  }

  const falsification = `核心若破MA60或连续2个月度决策期出top${TOPK_EXIT}则剔除（v5 月度状态机口径）；`
    + `regime 信号翻转（年线±3%/集中度≥${DOMINANT_MIN}）则切打法。日度计数器仅作刻度展示。`

  // ── content_md：模板化叙事（全确定性，无 LLM）──────────────────────────────
  const dist = plan.regime?.hs300_vs_ma120 ?? null
  const conc = plan.regime?.concentration ?? {}
  const counterByCode = new Map(counters.map(c => [c.sector.split(' ')[0], c]))
  // 「在管」= 真实组合里的板块：抱主线态才有板块持仓；宽基/防御态组合是 ETF、无板块在管。
  const heldBoardCodes = new Set(isMainline ? holdings.map(h => h.code) : [])

  // regime 推理叙述
  const trendDesc = dist === null ? '年线数据不足'
    : dist >= 0 ? `站年线上方 ${fmtPct(dist, 2)}${dist >= HYSTERESIS ? '（超 +3% 迟滞带上沿，趋势确认）' : '（在 ±3% 迟滞带内）'}`
    : `跌破年线 ${fmtPct(Math.abs(dist), 2)}${dist <= DEFENSE_DIST ? '（破 -3% 迟滞带下沿，触发防御）' : '（在 ±3% 迟滞带内，未触发防御）'}`
  const concDesc = `top15 中「${conc.dominant_group ?? '—'}」占 ${conc.dominant_count ?? 0} 个`
    + `（阈值 ≥${DOMINANT_MIN} = 有主线土壤）→ ${(conc.dominant_count ?? 0) >= DOMINANT_MIN ? '主线集中' : '主线发散'}`
  const playbook = isMainline ? '走第1-3层，构建核心+卫星组合，精选主线载体'
    : rg.startsWith('防御') ? '全退红利（中证红利/红利低波），不碰主线'
    : '退全市场宽基（沪深300/A500），等主线明朗，不精选主题'

  const lines: string[] = []
  lines.push(BACKFILL_NOTE, '')
  lines.push('## ① regime（环境判定）')
  lines.push(`- **${rg}**（按日度口径已连续维持 ${rgDays} 个交易日）`)
  lines.push(`- 大盘趋势：HS300 距 MA120(年线) ${fmtPct(dist, 2)} → ${trendDesc}`)
  lines.push(`- 主线集中度：${concDesc}`)
  lines.push(`- 打法：${playbook}`)

  lines.push('', '## ② 组合（等权）与选基理由')
  if (isMainline) {
    for (const h of holdings) {
      const sel = fm.get(h.code) ?? null
      const ct = counterByCode.get(h.code)
      const roleDesc = h.role === '核心'
        ? '大主线·惯性持有（回调不割，仍站MA60则不动；连续3日破MA60或连续2期出top15才剔除）'
        : '新兴主线·卫星（连续top3纳入，最小持有15日）'
      lines.push(`### [${h.role}] ${h.code} ${h.name}`)
      lines.push(`- 定位：${roleDesc}`)
      lines.push(`- 现状：动量 rank ${h.rank ?? '—'}，${h.above_ma60 ? '站MA60 √' : '破MA60 ✗'}，已持 ${tradingDaysBetween(tradingRawAll, ymd(h.since), decisionRaw)} 交易日`
        + (ct ? `，连续 in_top5 ${ct.in_top5_days} 日` : ''))
      if (sel?.code) {
        lines.push(`- 载体：**${sel.code} ${sel.name ?? ''}**（跟踪${sel.index_name ?? '—'}）`)
        lines.push(`  - 双测度：成分重叠 ${fmtPct(sel.overlap)}、净值相关 ${fmtNum(sel.corr)} → **${sel.verdict ?? '—'}**`
          + `；规模 ${fmtNum(sel.scale_yi, 1)} 亿，β=${fmtNum(sel.beta)}`)
      } else {
        lines.push('- 载体：（双测度无合格匹配基金，待人工兜底）')
      }
      if (h.role === '卫星' && ct) {
        const gap = CORE_PROMOTE_DAYS - ct.in_top5_days
        lines.push(`- 晋升距离：连续 in_top5 ${ct.in_top5_days} 日，距核心晋升刻度(${CORE_PROMOTE_DAYS}日)${gap > 0 ? `还差 ${gap} 日` : '已达标'}`)
      }
    }
  } else {
    for (const p of portfolio) {
      lines.push(`- **[${p.role}] ${p.fund_code} ${p.fund_name ?? ''}**：${rg.startsWith('防御') ? '防御态底线产品（退红利）' : '无主线态底线产品（退宽基）'}`)
    }
  }

  lines.push('', '## ③ 今日动作', `- ${todayAction}`)
  if (todayAction === '维持不动') {
    lines.push('- 说明：所有持仓未触发换仓条件（核心未破MA60/未连续出top15、卫星未连续跌出top5），按「稳为先」维持原组合。')
  }

  lines.push('', '## ④ 计数器与触发距离（日度刻度，仅展示）')
  lines.push('| 板块 | 在管 | 连续in_top5 | 连续out_top15 | 连续破MA60 | 距核心晋升 | 距出局剔除 |',
             '|---|---|---|---|---|---|---|')
  for (const c of counters) {
    const code = c.sector.split(' ')[0]
    const held = heldBoardCodes.has(code)
    const promoteGap = CORE_PROMOTE_DAYS - c.in_top5_days
    const exitGap = Math.min(BREAK_MA60_EXIT - c.break_ma60_days, OUT_TOP15_EXIT - c.out_top15_days)
    const promoteCell = c.in_top5_days >= CORE_PROMOTE_DAYS ? '已达标' : (promoteGap > 0 && c.in_top5_days > 0 ? `差${promoteGap}日` : '—')
    const exitCell = (c.break_ma60_days > 0 || c.out_top15_days > 0) ? `差${exitGap}日` : '安全'
    lines.push(`| ${c.sector} | ${held ? '√' : '候选'} | ${c.in_top5_days} | ${c.out_top15_days} | ${c.break_ma60_days} | ${promoteCell} | ${exitCell} |`)
  }
  lines.push(`> 计数口径：连续 in_top5 ≥${CORE_PROMOTE_DAYS}日且站MA60→晋核心；连续破MA60 ≥${BREAK_MA60_EXIT}日 或 连续出top${TOPK_EXIT} ≥${OUT_TOP15_EXIT}日→剔除；卫星纳入需连续 top3 ≥${SAT_ENTRY_DAYS}日。`)

  lines.push('', '## ⑤ 再平衡 / 证伪', `- ${falsification}`)

  const structured = JSON.stringify({
    regime: rg,
    regime_days: rgDays,
    portfolio: portfolio.map(p => ({
      role: p.role, sector: p.sector, fund_code: p.fund_code, fund_name: p.fund_name,
      held_days: p.held_days, above_ma60: p.above_ma60, crowding_pct: p.crowding_pct,
    })),
    today_action: todayAction,
    counters: counters.map(c => ({ sector: c.sector, in_topk_days: c.in_top5_days, out_topk_days: c.out_top15_days, break_ma60_days: c.break_ma60_days })),
    rebalance_falsification: falsification,
    source: 'backfill-deterministic-v5',
    decision_trade_date: plan.decision_trade_date ?? '',
  })
  return { content: lines.join('\n'), structured, identity }
}

// ── 主流程 ──────────────────────────────────────────────────────────────────

async function main(argv = process.argv.slice(2)): Promise<number> {
  if (argv.includes('-h') || argv.includes('--help')) {
    process.stdout.write(
      'market-reports 确定性回补（market_mainline + mainline_rotation）\n' +
      '  --from / --to    回补窗口（YYYY-MM 或 YYYY-MM-DD），按决策日频率生成\n' +
      '  --freq <f>       决策日频率 monthly（缺省，每月第一个交易日）| weekly（每 ISO 周第一个交易日）| daily（每个交易日）\n' +
      '  --fund-db <p>    缺省 <repo>/data/fund.db\n' +
      '  --calendar <p>   缺省 <repo>/world/runtime/calendar.json\n' +
      '  --run-id <id>    缺省 backfill-det[-weekly]-<from>-<to>\n' +
      '  --force          覆盖已存在的 (report_type, as_of_date)（缺省跳过）\n')
    return 0
  }
  const fromArg = argVal(argv, '--from') ?? '2025-02'
  const toArg = argVal(argv, '--to') ?? '2026-06'
  const from = fromArg.length === 7 ? `${fromArg}-01` : fromArg
  const to = toArg.length === 7 ? `${toArg}-31` : toArg
  const freq = argVal(argv, '--freq') ?? 'monthly'
  if (freq !== 'monthly' && freq !== 'weekly' && freq !== 'daily') { process.stderr.write(`--freq 只支持 monthly|weekly|daily，得到 ${freq}\n`); return 2 }
  const fundDb = resolve(argVal(argv, '--fund-db') ?? join(REPO_ROOT, 'data', 'fund.db'))
  const calendarPath = resolve(argVal(argv, '--calendar') ?? join(REPO_ROOT, 'world', 'runtime', 'calendar.json'))
  const freqSuffix = freq === 'weekly' ? '-weekly' : freq === 'daily' ? '-daily' : ''
  const runId = argVal(argv, '--run-id') ?? `backfill-det${freqSuffix}-${ymd(from).slice(0, 6)}-${ymd(to).slice(0, 6)}`
  const force = argv.includes('--force')

  const cal = loadCalendar(calendarPath)
  // 多取 from 前一个月做 warm-up（算 today_action 的上一期组合），不落库。
  // 日期窗从 2024-01 起：regimeOnDay 的 MA120 + regime_days 回看共需 ~250 个交易日余量。
  const allDates = computeTradingDates(cal, '2024-01-01', to)
  const tradingRawAll = allDates.map(ymd)
  const decisionDays = freq === 'weekly' ? weekFirstTradingDays(allDates)
    : freq === 'daily' ? allDates
    : monthFirstTradingDays(allDates)
  const freqLabel = freq === 'weekly' ? '周度' : freq === 'daily' ? '日度' : '月度'
  const firstIdx = decisionDays.findIndex(d => d >= from)
  if (firstIdx < 0) { process.stderr.write(`窗口 ${from}..${to} 内无${freqLabel}决策日\n`); return 2 }
  const loopDays = decisionDays.slice(Math.max(0, firstIdx - 1))
  log(`run=${runId} db=${fundDb}`)
  log(`窗口 ${from}..${to} → ${loopDays.length - (firstIdx > 0 ? 1 : 0)} 个${freqLabel}决策日（含 1 个 warm-up：${loopDays[0]}）`)

  ensureMarketReportsTable(fundDb)
  const groups = loadGroups()

  let written = 0, skipped = 0, failed = 0
  let prev: { regime: string; identity: HoldIdentity[] } | null = null
  // v5 plan 按自然月缓存：daily 频率下同一月内 decision 恒为月首（v5 是月度状态机），
  // board_fund_match（慢，可能经 PIT 调 18078）每月只算一次，月内其余交易日复用。
  const planCache = new Map<string, V5Plan>()
  for (let i = 0; i < loopDays.length; i++) {
    const date = loopDays[i]
    const isWarmup = i === 0 && firstIdx > 0
    const monthKey = date.slice(0, 7)
    let plan = planCache.get(monthKey)
    let freshPlan = false
    if (!plan) {
      try {
        plan = loadV5Plan(date); freshPlan = true; planCache.set(monthKey, plan)
      } catch (e) {
        // warm-up 落在 v5 数据起点(2025-01)之前会预期失败 → 不计 failed，本期按序列首期处理(prev=null)
        if (isWarmup) { log(`(warm-up ${date}：v5 无数据[早于状态机起点]，跳过，本期按首期建仓处理)`); prev = null; continue }
        log(`✗ ${date} v5 引擎失败：${e instanceof Error ? e.message : String(e)}`)
        failed++; prev = null; continue
      }
    }
    // daily：计数器/已持天数/regime_days 按「实际交易日」算（逐日变化的展示信号，与用户敲定的口径）；
    // monthly/weekly：沿用 v5 月度决策日（月首），原口径不变。
    const counterRaw = freq === 'daily' ? ymd(date) : ymd(plan.decision_trade_date ?? date)
    const holdings = (plan.v4_holdings ?? []) as unknown as V5Holding[]
    const counterBoards = new Set<string>(holdings.map(h => h.code))
    for (const t of plan.top15 ?? []) if (t.rank <= K_ENTRY) counterBoards.add(t.code)
    const cache = buildDayCache(counterRaw, tradingRawAll, [...counterBoards], groups)

    // regime 节锚定 v5 月首决策日（与所示 dist/集中度自洽）；计数器/held_days 用 counterRaw（实际日）。
    const regimeAnchorRaw = ymd(plan.decision_trade_date ?? date)
    const rot = renderRotation(plan, cache, tradingRawAll, counterRaw, prev, regimeAnchorRaw)
    prev = { regime: plan.regime?.name ?? '—', identity: rot.identity }
    if (isWarmup) { log(`(warm-up ${date}：仅取上一期组合，不落库)`); continue }

    // market_mainline：v5 规范化头 + 确定性正文（与线上 submit 同一渲染路径）
    try {
      if (!force && reportExists(fundDb, 'market_mainline', date)) { skipped++; log(`- ${date} market_mainline 已存在，跳过`) }
      else {
        const body = renderMainlineBody(plan)
        const normalized = normalizeMarketMainlineSubmission(join(REPO_ROOT, 'world', 'runtime'), date, body, plan)
        upsertReport(fundDb, 'market_mainline', date, normalized.content, normalized.structured, runId)
        if (!reportExists(fundDb, 'market_mainline', date)) throw new Error('写后校验未命中')
        written++; log(`✓ ${date} market_mainline 落库（regime=${plan.regime?.name}）`)
      }
    } catch (e) { failed++; log(`✗ ${date} market_mainline 失败：${e instanceof Error ? e.message : String(e)}`) }

    // mainline_rotation
    try {
      if (!force && reportExists(fundDb, 'mainline_rotation', date)) { skipped++; log(`- ${date} mainline_rotation 已存在，跳过`) }
      else {
        upsertReport(fundDb, 'mainline_rotation', date, rot.content, rot.structured, runId)
        if (!reportExists(fundDb, 'mainline_rotation', date)) throw new Error('写后校验未命中')
        written++; log(`✓ ${date} mainline_rotation 落库（action=${JSON.parse(rot.structured).today_action}）`)
      }
    } catch (e) { failed++; log(`✗ ${date} mainline_rotation 失败：${e instanceof Error ? e.message : String(e)}`) }

    // 仅在真正调用了 v5 引擎（新月/新决策）后小睡：board_fund_match 可能经 PIT 调
    // idx_constituents，18078 与 live run 共用；daily 月内缓存命中的日子不碰引擎，无需 sleep。
    if (freshPlan && i < loopDays.length - 1) await new Promise(r => setTimeout(r, 2000))
  }

  // 覆盖矩阵摘要
  const rows = queryJson(fundDb,
    `SELECT substr(as_of_date,1,7) AS m, report_type, count(*) AS n FROM market_reports `
    + `WHERE as_of_date>=${sqlStr(from)} AND as_of_date<=${sqlStr(to)} GROUP BY m, report_type ORDER BY m;`)
  const byMonth = new Map<string, Record<string, number>>()
  for (const r of rows) {
    const m = String(r.m)
    if (!byMonth.has(m)) byMonth.set(m, {})
    byMonth.get(m)![String(r.report_type)] = Number(r.n)
  }
  log('—— 覆盖矩阵（窗口内）——')
  for (const [m, t] of byMonth) log(`${m}  context=${t.market_context ?? 0} mainline=${t.market_mainline ?? 0} rotation=${t.mainline_rotation ?? 0}`)
  log(`done: written=${written} skipped=${skipped} failed=${failed}`)
  return failed ? 1 : 0
}

main().then(c => process.exit(c)).catch(e => { console.error(e); process.exit(1) })
