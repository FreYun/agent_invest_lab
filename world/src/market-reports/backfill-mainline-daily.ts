// market_mainline_daily + mainline_rotation_daily 确定性生成/回补 —— 日度主线（skill 日度纪律状态机）
//
// 与月度 market_mainline / mainline_rotation（v5 月度状态机，bot 消费的真源）完全并行、互不影响：
//   - 引擎 = scripts/mainline_daily_plan.py（逐交易日推进的确定性状态机，
//     阈值取自 mainline-rotation skill 的日度纪律：40日晋核心/3日破MA60或
//     20日出top15剔除/卫星5日进出/15日最小持有+冷却/regime 5日确认+迟滞带）。
//   - 同一份日度重放渲染两份报告：market_mainline_daily（主线识别视角）与
//     mainline_rotation_daily（组合/换仓视角，真动作而非月度版的"刻度展示"）。
//   - 仅 48080 看板展示，bot 不读、不进 strategy-server get_market_report 的
//     消费面 → 现有回测/live 零影响。
//
// 工程语义与 backfill-deterministic 一致：默认幂等跳过已存在 (report_type, as_of_date)、
// 中断重跑即续、写库走 sqlite3 CLI（fund.db WAL、可能被 live run 并发写）。
//
// 用法（在 world/ 下）：
//   node --experimental-strip-types src/market-reports/backfill-mainline-daily.ts \
//     --from 2025-01-02 --to 2026-07-01 [--fund-db <path>] [--run-id <id>] [--force] [--no-funds]

import { execFileSync } from 'node:child_process'
import { resolve, join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { ensureMarketReportsTable, runSqlite, sqlStr } from '../strategy-server/server.ts'

const HERE = dirname(fileURLToPath(import.meta.url))           // world/src/market-reports
const REPO_ROOT = resolve(HERE, '..', '..', '..')              // agent_invest_lab
const ENGINE = join(REPO_ROOT, 'scripts', 'mainline_daily_plan.py')
const TYPE_MAINLINE = 'market_mainline_daily'
const TYPE_ROTATION = 'mainline_rotation_daily'

function argVal(argv: string[], flag: string): string | undefined {
  const i = argv.indexOf(flag)
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined
}

function log(msg: string): void { process.stdout.write(`[mainline-daily] ${msg}\n`) }

function reportExists(dbPath: string, reportType: string, asOf: string): boolean {
  const out = runSqlite(dbPath, `SELECT 1 AS x FROM market_reports WHERE report_type=${sqlStr(reportType)} AND scope='global' AND as_of_date=${sqlStr(asOf)} LIMIT 1;`).trim()
  return out ? (JSON.parse(out) as unknown[]).length > 0 : false
}

function upsertReport(dbPath: string, reportType: string, asOf: string, contentMd: string, structuredJson: string, runId: string): void {
  const sql = `INSERT INTO market_reports (report_type, as_of_date, scope, content_md, structured_json, agent_run_id) `
    + `VALUES (${sqlStr(reportType)}, ${sqlStr(asOf)}, 'global', ${sqlStr(contentMd)}, ${sqlStr(structuredJson)}, ${sqlStr(runId)}) `
    + `ON CONFLICT(report_type, as_of_date, scope) DO UPDATE SET `
    + `content_md=excluded.content_md, structured_json=excluded.structured_json, `
    + `agent_run_id=excluded.agent_run_id, generated_at=datetime('now');`
  runSqlite(dbPath, sql)
}

// ── 引擎输出类型（与 mainline_daily_plan.py 的 plan dict 对齐）────────────────

type Counters = { in_top5: number; in_top3: number; out_top5: number; out_top15: number; below_ma60: number }
type Holding = {
  code: string; name: string; role: string; rank: number | null; above_ma60: boolean
  since: string; held_days: number; min_hold_left: number; counters: Counters
}
type FundSel = { code?: string; name?: string; index_name?: string; verdict?: string; overlap?: number; corr?: number; beta?: number; scale_yi?: number }
type FundMatch = { board_code?: string; board_name?: string; role?: string; selected?: FundSel | null; candidates?: FundSel[] }
type Candidate = { code: string; name: string; rank: number; above_ma60: boolean; cooldown_left: number; counters: Counters }
type DailyPlan = {
  as_of_date: string
  decision_trade_date: string
  regime: {
    name: string; raw_name: string; days: number
    pending: { name: string; days: number } | null
    hs300_vs_ma120: number | null
    concentration: { dominant_group: string; dominant_count: number; counts: Record<string, number> }
  }
  mainline_theme: string | null
  top15: Array<{ code: string; name: string; rank: number; group: string; above_ma60: boolean; ret20: number }>
  holdings: Holding[]
  candidates?: Candidate[]
  portfolio: Array<Record<string, unknown>>
  actions: Array<{ type: string; code: string; name: string; role: string; reason: string }>
  today_action: string
  cooldowns: Array<{ code: string; days_left: number }>
  fund_matches?: FundMatch[]
}

function loadPlans(from: string, to: string, includeFunds: boolean): DailyPlan[] {
  const args = [ENGINE, '--from', from, '--to', to, ...(includeFunds ? ['--include-funds'] : [])]
  const out = execFileSync('python3', args, { encoding: 'utf8', maxBuffer: 512 * 1024 * 1024 })
  return out.split('\n').filter(l => l.trim()).map(l => JSON.parse(l) as DailyPlan)
}

// ── 渲染 ────────────────────────────────────────────────────────────────────

const NOTE = '> 本报告由日度主线确定性引擎（mainline-daily，skill 日度纪律阈值状态机）生成：'
  + '逐交易日推进核心/卫星状态机与 regime 确认，**与月度 market_mainline（v5 月度状态机）并行观察、互不影响**，无 LLM 叙事。'

function fmtPct(v: unknown, digits = 1): string {
  return typeof v === 'number' && Number.isFinite(v) ? (v * 100).toFixed(digits) + '%' : '—'
}
function fmtNum(v: unknown, digits = 2): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—'
}

function renderBody(p: DailyPlan): string {
  const rg = p.regime
  const conc = rg.concentration
  const isMainline = rg.name.startsWith('抱主线')
  const fm = new Map((p.fund_matches ?? []).map(m => [m.board_code ?? '', m]))
  const lines: string[] = []
  lines.push(NOTE, '')

  lines.push('## ① 当日主线结论')
  if (isMainline) {
    lines.push(`- 主线主题：**${p.mainline_theme ?? '—'}**（top15 集中度 ${conc.dominant_count} 个）`)
    lines.push(`- 主线板块：${p.holdings.map(h => `${h.code} ${h.name}[${h.role}]`).join('、') || '—'}`)
  } else if (rg.name.startsWith('防御')) {
    lines.push('- **无可投主线（防御态）**：沪深300 跌破年线迟滞带下沿，全退红利。')
  } else {
    lines.push('- **无主线**：主线发散（top15 最大一类 <4 个），方向 = 全市场宽基。')
  }

  lines.push('', '## ② regime（日度判定：确认窗口 + 迟滞带）')
  lines.push(`- regime：**${rg.name}**（已连续维持 ${rg.days} 个交易日）`)
  if (rg.pending) lines.push(`- ⚠️ 待确认切换：原始信号「${rg.pending.name}」已连续 ${rg.pending.days}/5 日，确认满 5 日才切换`)
  lines.push(`- HS300 距 MA120(年线)：${fmtPct(rg.hs300_vs_ma120, 2)}（迟滞带：破 -3% 转防御 / 回 -1% 上方解除）`)
  lines.push(`- top15 分组计数：${Object.entries(conc.counts).map(([g, n]) => `${g}=${n}`).join('、') || '—'}`)

  lines.push('', '## ③ 组合状态机（核心/卫星 + 日度计数器）')
  if (p.holdings.length) {
    lines.push('| 板块 | 角色 | rank | 站MA60 | 已持 | 最小持有余 | 连续in_top5 | 连续out_top5 | 连续out_top15 | 连续破MA60 |',
               '|---|---|---|---|---|---|---|---|---|---|')
    for (const h of p.holdings) {
      const c = h.counters
      lines.push(`| ${h.code} ${h.name} | ${h.role} | ${h.rank ?? '—'} | ${h.above_ma60 ? '√' : '✗'} | ${h.held_days}日 | ${h.min_hold_left > 0 ? h.min_hold_left + '日' : '—'} | ${c.in_top5} | ${c.out_top5} | ${c.out_top15} | ${c.below_ma60} |`)
    }
  } else {
    lines.push('- （状态机当前无在管板块）')
  }
  if (!isMainline && p.portfolio.length) {
    const p0 = p.portfolio[0]
    lines.push(`- 非抱主线态，底线产品：**${p0.code} ${p0.name}**（${p0.role}）`)
  }
  if (p.cooldowns.length) {
    lines.push(`- 冷却中（剔除后 15 日内不纳回）：${p.cooldowns.map(c => `${c.code}(余${c.days_left}日)`).join('、')}`)
  }
  lines.push(`> 纪律口径：连续≥40日top5且站MA60→晋核心；连续≥3日破MA60（硬）或核心连续≥20日出top15/卫星连续≥5日出top5（受15日最小持有期约束）→剔除；卫星连续≥5日top3纳入。`)

  lines.push('', '## ④ 今日动作', `- ${p.today_action}`)
  if (p.today_action === '维持不动') {
    lines.push('- 说明：未触发任何确认型换仓条件（默认动作 = hold，换手靠确认窗口+迟滞+最小持有期压到月级）。')
  }

  lines.push('', '## ⑤ 可投基金池（board_fund_match 双测度）')
  if (isMainline && p.holdings.length) {
    lines.push('| 主线板块 | 角色 | 基金 | 档位 | 重叠 | 相关 | β | 规模(亿) |', '|---|---|---|---|---|---|---|---|')
    for (const h of p.holdings) {
      const s = (fm.get(h.code)?.selected ?? {}) as FundSel
      lines.push(`| ${h.code} ${h.name} | ${h.role} | ${s.code ? `${s.code} ${s.name ?? ''}` : '（无匹配）'} | ${s.verdict ?? '—'} | ${fmtPct(s.overlap)} | ${fmtNum(s.corr)} | ${fmtNum(s.beta)} | ${fmtNum(s.scale_yi, 1)} |`)
    }
  } else {
    const p0 = p.portfolio[0] as Record<string, unknown> | undefined
    lines.push(`- 非抱主线态，底线产品：${p0 ? `${p0.code} ${p0.name}（${p0.role}）` : '—'}`)
  }
  return lines.join('\n')
}

function renderStructured(p: DailyPlan, runId: string): string {
  const fm = new Map((p.fund_matches ?? []).map(m => [m.board_code ?? '', m.selected ?? null]))
  return JSON.stringify({
    mainline_theme: p.mainline_theme,
    regime: p.regime.name,
    regime_days: p.regime.days,
    pending_regime: p.regime.pending,
    hs300_vs_ma120: p.regime.hs300_vs_ma120,
    concentration: p.regime.concentration,
    mainline_sectors: p.holdings.map(h => `${h.code} ${h.name}[${h.role}]`).join('、') || null,
    holdings: p.holdings.map(h => ({
      code: h.code, name: h.name, role: h.role, rank: h.rank, above_ma60: h.above_ma60,
      since: h.since, held_days: h.held_days, min_hold_left: h.min_hold_left,
      fund_code: (fm.get(h.code) as FundSel | null)?.code ?? null,
      fund_name: (fm.get(h.code) as FundSel | null)?.name ?? null,
      counters: h.counters,
    })),
    portfolio: p.portfolio,
    today_action: p.today_action,
    actions: p.actions,
    cooldowns: p.cooldowns,
    source: 'mainline-daily-skill-v1',
    generated_by: runId,
    decision_trade_date: p.decision_trade_date,
  })
}

// ── mainline_rotation_daily 渲染（组合/换仓视角，reporter-rotation ①~⑤ 结构的日度版）──

const ROTATION_NOTE = '> 本报告由日度主线确定性引擎（mainline-daily，skill 日度纪律阈值状态机）生成：'
  + '核心/卫星组合与「今日动作」是日度状态机的**真实换仓输出**（月度 mainline_rotation 的计数器仅是刻度展示），'
  + '**与月度版并行观察、互不影响**，无 LLM 叙事。'

const FALSIFICATION = '核心：连续≥3日破MA60（硬）或连续≥20日出top15→剔除；卫星：连续≥5日跌出top5→剔除（均受15日最小持有期约束，破MA60除外）；'
  + '剔除后15日冷却不纳回；regime 信号连续≥5日维持新状态才切打法（年线迟滞带：破-3%转防御/回-1%上方解除）。'

function renderRotationBody(p: DailyPlan): string {
  const rg = p.regime
  const isMainline = rg.name.startsWith('抱主线')
  const fm = new Map((p.fund_matches ?? []).map(m => [m.board_code ?? '', (m.selected ?? null) as FundSel | null]))
  const dist = rg.hs300_vs_ma120
  const lines: string[] = []
  lines.push(ROTATION_NOTE, '')

  lines.push('## ① regime（环境判定，日度确认）')
  lines.push(`- **${rg.name}**（已连续维持 ${rg.days} 个交易日）`)
  if (rg.pending) lines.push(`- ⚠️ 待确认切换：原始信号「${rg.pending.name}」已连续 ${rg.pending.days}/5 日`)
  const trendDesc = dist === null ? '年线数据不足'
    : dist >= 0 ? `站年线上方 ${fmtPct(dist, 2)}`
    : `跌破年线 ${fmtPct(Math.abs(dist), 2)}${dist <= -0.03 ? '（破 -3% 迟滞带下沿）' : '（在迟滞带内）'}`
  lines.push(`- 大盘趋势：HS300 距 MA120(年线) ${fmtPct(dist, 2)} → ${trendDesc}`)
  lines.push(`- 主线集中度：top15 中「${rg.concentration.dominant_group}」占 ${rg.concentration.dominant_count} 个（≥4 = 有主线土壤）`)
  lines.push(`- 打法：${isMainline ? '构建核心+卫星组合，精选主线载体' : rg.name.startsWith('防御') ? '全退红利，不碰主线' : '退全市场宽基，等主线明朗'}`)

  lines.push('', '## ② 组合（等权）与持有理由')
  if (isMainline && p.holdings.length) {
    for (const h of p.holdings) {
      const sel = fm.get(h.code)
      const roleDesc = h.role === '核心'
        ? '大主线·惯性持有（回调不割，仍站MA60则不动）'
        : '新兴主线·卫星（连续top3纳入，最小持有15日）'
      lines.push(`### [${h.role}] ${h.code} ${h.name}`)
      lines.push(`- 定位：${roleDesc}`)
      lines.push(`- 现状：动量 rank ${h.rank ?? '—'}，${h.above_ma60 ? '站MA60 √' : '破MA60 ✗'}，已持 ${h.held_days} 交易日`
        + (h.min_hold_left > 0 ? `（最小持有期余 ${h.min_hold_left} 日）` : ''))
      if (sel?.code) {
        lines.push(`- 载体：**${sel.code} ${sel.name ?? ''}**（双测度：重叠 ${fmtPct(sel.overlap)}、相关 ${fmtNum(sel.corr)} → ${sel.verdict ?? '—'}）`)
      } else {
        lines.push('- 载体：（双测度无合格匹配基金，待人工兜底）')
      }
      if (h.role === '卫星') {
        const gap = 40 - h.counters.in_top5
        lines.push(`- 晋升距离：连续 in_top5 ${h.counters.in_top5} 日，距核心晋升(40日)${h.counters.in_top5 > 0 && gap > 0 ? `还差 ${gap} 日` : gap <= 0 ? '已达标（待当日站MA60确认）' : '未起算'}`)
      }
    }
  } else {
    for (const pf of p.portfolio) {
      lines.push(`- **[${pf.role}] ${pf.code} ${pf.name ?? ''}**：${rg.name.startsWith('防御') ? '防御态底线产品（退红利）' : '无主线态底线产品（退宽基）'}`)
    }
  }

  lines.push('', '## ③ 今日动作', `- ${p.today_action}`)
  if (p.today_action === '维持不动') {
    lines.push('- 说明：未触发任何确认型换仓条件（默认动作 = hold，换手靠确认窗口+迟滞+最小持有期压到月级）。')
  }

  lines.push('', '## ④ 计数器与触发距离（在管 + 当日 top5 候选）')
  lines.push('| 板块 | 状态 | 连续in_top5 | 连续in_top3 | 连续out_top5 | 连续out_top15 | 连续破MA60 | 距触发 |',
             '|---|---|---|---|---|---|---|---|')
  for (const h of p.holdings) {
    const c = h.counters
    // 只对已起算的剔除时钟求最近距离（未起算的时钟不参与，避免误报）
    const gaps: number[] = []
    if (c.below_ma60 > 0) gaps.push(3 - c.below_ma60)
    if (h.role === '核心' && c.out_top15 > 0) gaps.push(20 - c.out_top15)
    if (h.role === '卫星' && c.out_top5 > 0) gaps.push(5 - c.out_top5)
    const trigger = gaps.length
      ? `距剔除差${Math.max(0, Math.min(...gaps))}日${h.min_hold_left > 0 ? '（最小持有期保护中）' : ''}` : '安全'
    lines.push(`| ${h.code} ${h.name} | 在管·${h.role} | ${c.in_top5} | ${c.in_top3} | ${c.out_top5} | ${c.out_top15} | ${c.below_ma60} | ${trigger} |`)
  }
  for (const cd of p.candidates ?? []) {
    const c = cd.counters
    const note = cd.cooldown_left > 0 ? `冷却余${cd.cooldown_left}日`
      : c.in_top3 > 0 ? `距卫星纳入(5日top3)差${Math.max(0, 5 - c.in_top3)}日`
      : c.in_top5 > 0 ? `距核心晋升(40日top5)差${Math.max(0, 40 - c.in_top5)}日` : '—'
    lines.push(`| ${cd.code} ${cd.name} | 候选·rank${cd.rank} | ${c.in_top5} | ${c.in_top3} | ${c.out_top5} | ${c.out_top15} | ${c.below_ma60} | ${note} |`)
  }
  if (p.cooldowns.length) {
    lines.push(`> 冷却名单（剔除后 15 日不纳回）：${p.cooldowns.map(c => `${c.code}(余${c.days_left}日)`).join('、')}`)
  }

  lines.push('', '## ⑤ 再平衡 / 证伪', `- ${FALSIFICATION}`)
  return lines.join('\n')
}

function renderRotationStructured(p: DailyPlan, runId: string): string {
  const fm = new Map((p.fund_matches ?? []).map(m => [m.board_code ?? '', (m.selected ?? null) as FundSel | null]))
  const isMainline = p.regime.name.startsWith('抱主线')
  const portfolio = isMainline
    ? p.holdings.map(h => ({
        role: h.role === '核心' ? 'core' : 'satellite',
        sector: `${h.code} ${h.name}`,
        fund_code: fm.get(h.code)?.code ?? null,
        fund_name: fm.get(h.code)?.name ?? null,
        held_days: h.held_days,
        above_ma60: h.above_ma60,
        min_hold_left: h.min_hold_left,
      }))
    : p.portfolio.map(pf => ({
        role: String(pf.role ?? ''), sector: null,
        fund_code: String(pf.code ?? ''), fund_name: String(pf.name ?? ''),
        held_days: 0, above_ma60: null, min_hold_left: 0,
      }))
  return JSON.stringify({
    regime: p.regime.name,
    regime_days: p.regime.days,
    pending_regime: p.regime.pending,
    portfolio,
    today_action: p.today_action,
    actions: p.actions,
    counters: [...p.holdings.map(h => ({ sector: `${h.code} ${h.name}`, held: h.role, ...h.counters })),
               ...(p.candidates ?? []).map(c => ({ sector: `${c.code} ${c.name}`, held: '候选', ...c.counters }))],
    cooldowns: p.cooldowns,
    rebalance_falsification: FALSIFICATION,
    source: 'mainline-daily-skill-v1',
    generated_by: runId,
    decision_trade_date: p.decision_trade_date,
  })
}

// ── 主流程 ──────────────────────────────────────────────────────────────────

async function main(argv = process.argv.slice(2)): Promise<number> {
  if (argv.includes('-h') || argv.includes('--help')) {
    process.stdout.write(
      'market_mainline_daily + mainline_rotation_daily 确定性生成/回补（skill 日度纪律状态机，仅看板观察）\n' +
      '  --from / --to    窗口（YYYY-MM-DD），逐交易日生成\n' +
      '  --fund-db <p>    缺省 <repo>/data/fund.db\n' +
      '  --run-id <id>    缺省 mainline-daily-<from>-<to>\n' +
      '  --force          覆盖已存在的 as_of_date（缺省跳过）\n' +
      '  --no-funds       不跑 board_fund_match 基金匹配\n')
    return 0
  }
  const from = argVal(argv, '--from') ?? '2025-01-02'
  const to = argVal(argv, '--to') ?? from
  const fundDb = resolve(argVal(argv, '--fund-db') ?? join(REPO_ROOT, 'data', 'fund.db'))
  const runId = argVal(argv, '--run-id') ?? `mainline-daily-${from.replace(/-/g, '')}-${to.replace(/-/g, '')}`
  const force = argv.includes('--force')
  const includeFunds = !argv.includes('--no-funds')

  ensureMarketReportsTable(fundDb)
  log(`run=${runId} db=${fundDb} 窗口 ${from}..${to}（引擎逐日重放，含基金匹配=${includeFunds}）`)
  const plans = loadPlans(from, to, includeFunds)
  log(`引擎输出 ${plans.length} 个交易日`)

  let written = 0, skipped = 0, failed = 0
  const jobs: Array<[string, (p: DailyPlan) => string, (p: DailyPlan) => string]> = [
    [TYPE_MAINLINE, renderBody, p => renderStructured(p, runId)],
    [TYPE_ROTATION, renderRotationBody, p => renderRotationStructured(p, runId)],
  ]
  for (const p of plans) {
    const date = p.as_of_date
    for (const [type, body, structured] of jobs) {
      try {
        if (!force && reportExists(fundDb, type, date)) { skipped++; continue }
        upsertReport(fundDb, type, date, body(p), structured(p), runId)
        if (!reportExists(fundDb, type, date)) throw new Error('写后校验未命中')
        written++
        log(`✓ ${date} ${type} 落库（regime=${p.regime.name}，action=${p.today_action.slice(0, 60)}）`)
      } catch (e) {
        failed++
        log(`✗ ${date} ${type} 失败：${e instanceof Error ? e.message : String(e)}`)
      }
    }
  }
  log(`done: written=${written} skipped=${skipped} failed=${failed}`)
  return failed ? 1 : 0
}

main().then(c => process.exit(c)).catch(e => { console.error(e); process.exit(1) })
