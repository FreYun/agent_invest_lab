import { existsSync } from 'node:fs'
import { DatabaseSync } from 'node:sqlite'

/**
 * 处境层只读数据源。服务于 /situation.html。
 *
 * 三条纪律直接编码在这个文件里，不是注释承诺：
 *
 * 一、**PIT 闸门是必填参数，没有默认值。** `baseRates()` 的 asof 若为空直接抛错。
 *     一个能省略的 as-of，迟早会被省略，然后就是未来信息。
 * 二、**样本不足不出数字。** 独立窗口数 < MIN_N 的格子返回 `enough:false` 且
 *     分位数全部为 null，前端只能显示「你在地图外」。不给近似值、不给灰色小字。
 * 三、**只出分布不出均值。** 返回 p10/p25/p50/p75/p90 + 胜率。均值会被一两个
 *     极端值拖着走，而在 n=7 的格子里一个极端值就是全部。
 */

const MIN_N = 5
export const STRESS_ORDER = ['平静', '偏紧', '紧张', '极端']
export const DD_ORDER = ['无', '浅', '中', '深']

type SqlRow = Record<string, any>

function openReadOnly(dbPath: string): DatabaseSync | null {
  if (!existsSync(dbPath)) return null
  return new DatabaseSync(dbPath, { readOnly: true })
}

function q(db: DatabaseSync, sql: string, ...params: any[]): SqlRow[] {
  return db.prepare(sql).all(...params) as SqlRow[]
}

function q1(db: DatabaseSync, sql: string, ...params: any[]): SqlRow {
  return (db.prepare(sql).get(...params) ?? {}) as SqlRow
}

function jparse(s: unknown): any {
  if (typeof s !== 'string') return s
  try { return JSON.parse(s) } catch { return s }
}

function unavailable(dbPath: string, reason: string) {
  return { available: false, reason, source: { filename: dbPath.split('/').pop() ?? dbPath, readOnly: true } }
}

/** 线性插值分位数。n<2 时不抛异常——这一层经常遇到很小的格子，那时该返回值不该崩。 */
function quantiles(xs: number[]): Record<string, number | null> {
  const qs = [0.1, 0.25, 0.5, 0.75, 0.9]
  const out: Record<string, number | null> = {}
  if (xs.length === 0) { for (const k of qs) out[String(k)] = null; return out }
  const v = [...xs].sort((a, b) => a - b)
  for (const k of qs) {
    if (v.length === 1) { out[String(k)] = v[0]; continue }
    const pos = k * (v.length - 1)
    const lo = Math.floor(pos)
    const hi = Math.min(lo + 1, v.length - 1)
    out[String(k)] = v[lo] + (v[hi] - v[lo]) * (pos - lo)
  }
  return out
}

/** 格子下钻表最多列多少行。截断本身没问题，把截断后的行数当总数报出去才是问题。 */
const CELL_ROW_CAP = 400

function shiftDays(d: string, n: number): string {
  const t = new Date(d + 'T00:00:00Z')
  t.setUTCDate(t.getUTCDate() + n)
  return t.toISOString().slice(0, 10)
}

/**
 * 同一 (bot,run) 内贪心取互不重叠的窗口数。
 *
 * 为什么必须算：20 日窗口在相邻交易日之间重合 19/20 天。把 1,246 个连续交易日
 * 当成 1,246 个独立观测，等于把同一段行情数了 20 遍。晋升判据只认这个数。
 *
 * 窗口末日直接取 `outcome_available_at`——那是这条先例的结果真正落地的那天，
 * 也是 PIT 闸门用的同一列。以前这里拿 `h * 7 / 5`（20 交易日 ≈ 28 自然日）估，
 * 并在注释里自称「保守下界」。第七轮实测把这句话证伪了：20 个交易日的真实自然日
 * 跨度 min 28 / 中位 29 / max 38，**52.4%（193/368）严格大于 28**，所以
 * 「跳过 28 天」会放进一个窗口仍然重叠的样本——方向是**高估独立性**，不是保守。
 * 全库口径下这让 211 比真值 206 多了 5 个（无格子跨过 n≥5 判据，但分母必须算准）。
 */
function independentWindows(rows: SqlRow[], h: number): number {
  const byRun = new Map<string, { d: string; end: string }[]>()
  for (const r of rows) {
    const k = String(r.bot_id) + '|' + String(r.run_id)
    if (!byRun.has(k)) byRun.set(k, [])
    byRun.get(k)!.push({
      d: String(r.trade_date),
      // 兜底只在 avail 缺失时才用；底率查询已强制 outcome_available_at IS NOT NULL。
      end: r.avail ? String(r.avail) : shiftDays(String(r.trade_date), Math.round(h * 7 / 5)),
    })
  }
  let n = 0
  for (const xs of byRun.values()) {
    xs.sort((a, b) => (a.d < b.d ? -1 : a.d > b.d ? 1 : 0))
    let chosenEnd = ''
    for (const x of xs) {
      if (x.d > chosenEnd) { n += 1; chosenEnd = x.end }
    }
  }
  return n
}

// ---------------------------------------------------------------- 概览

export function loadSituationOverview(dbPath: string): Record<string, unknown> {
  const db = openReadOnly(dbPath)
  if (!db) return unavailable(dbPath, '处境库文件不存在：' + dbPath)
  try {
    const cells = q(db, `SELECT cell_id, subject_type, count(*) n FROM situation_vectors GROUP BY 1,2`)
    const grid = new Map<string, any>()
    for (const s of STRESS_ORDER) for (const d of DD_ORDER) {
      grid.set(s + '|' + d, { cellId: s + '|' + d, stress: s, dd: d, nDay: 0, nCase: 0 })
    }
    for (const r of cells) {
      const g = grid.get(String(r.cell_id))
      if (!g) { grid.set(String(r.cell_id), { cellId: r.cell_id, stress: '?', dd: '?', nDay: 0, nCase: 0 }) }
      const t = grid.get(String(r.cell_id))!
      if (r.subject_type === 'case') t.nCase = Number(r.n); else t.nDay = Number(r.n)
    }

    const tiers = q(db, `SELECT card_type, status, count(*) n FROM experience_card_versions GROUP BY 1,2 ORDER BY n DESC`)
    const edgeTypes = q(db, `SELECT edge_type, basis, role, count(*) n FROM experience_edges GROUP BY 1,2,3 ORDER BY n DESC`)
    const withPredicate = q(db, `SELECT DISTINCT card_id FROM experience_card_verdicts`)
    const verdictSplit = q(db, `SELECT subject_kind,
        sum(CASE WHEN verdict=1 THEN 1 ELSE 0 END) n_true,
        sum(CASE WHEN verdict=0 THEN 1 ELSE 0 END) n_false,
        sum(CASE WHEN verdict IS NULL THEN 1 ELSE 0 END) n_null,
        count(*) n FROM experience_card_verdicts GROUP BY 1`)
    const outcomes = q(db, `SELECT horizon_days, status, count(*) n FROM situation_outcomes GROUP BY 1,2`)
    const cohort = q1(db, `SELECT count(*) n, max(n_bot) mx, sum(n_subject) subs FROM v_cohort`)
    const vintage = q1(db, `SELECT max(data_vintage) v, max(computed_at) c, max(evaluator_version) e FROM situation_vectors`)
    const predVer = q1(db, `SELECT max(predicate_version) p FROM experience_card_verdicts`)

    const nDay = Number(q1(db, `SELECT count(*) c FROM situation_vectors WHERE subject_type='backtest_day'`).c ?? 0)
    const nCase = Number(q1(db, `SELECT count(*) c FROM situation_vectors WHERE subject_type='case'`).c ?? 0)

    return {
      available: true,
      source: {
        filename: dbPath.split('/').pop() ?? dbPath,
        readOnly: true,
        note: '这是生产库 experience-library-v7.db 的 P1 副本。处境层/底率层/关系层只写副本，生产库一个字节没动。',
        evaluatorVersion: vintage.e ?? null,
        predicateVersion: predVer.p ?? null,
        dataVintage: vintage.v ?? null,
        computedAt: vintage.c ?? null,
      },
      headline: {
        cards: Number(q1(db, `SELECT count(*) c FROM experience_card_versions`).c ?? 0),
        cases: Number(q1(db, `SELECT count(*) c FROM experience_cases`).c ?? 0),
        dayRows: nDay,
        caseRows: nCase,
        cardsWithPredicate: withPredicate.length,
        verdicts: Number(q1(db, `SELECT count(*) c FROM experience_card_verdicts`).c ?? 0),
        edges: Number(q1(db, `SELECT count(*) c FROM experience_edges`).c ?? 0),
        outcomes: Number(q1(db, `SELECT count(*) c FROM situation_outcomes`).c ?? 0),
        cohortDays: Number(cohort.n ?? 0),
        cohortMaxBots: Number(cohort.mx ?? 0),
      },
      stressOrder: STRESS_ORDER,
      ddOrder: DD_ORDER,
      grid: [...grid.values()],
      cardTiers: tiers.map(r => ({ cardType: r.card_type, status: r.status, n: Number(r.n) })),
      edgeTypes: edgeTypes.map(r => ({ edgeType: r.edge_type, basis: r.basis, role: r.role, n: Number(r.n) })),
      verdictSplit: verdictSplit.map(r => ({
        subjectKind: r.subject_kind, nTrue: Number(r.n_true), nFalse: Number(r.n_false),
        nNull: Number(r.n_null), n: Number(r.n),
      })),
      outcomeStatus: outcomes.map(r => ({ horizon: Number(r.horizon_days), status: r.status, n: Number(r.n) })),
      minN: MIN_N,
    }
  } finally { db.close() }
}

// ---------------------------------------------------------------- 底率

/** PIT 闸门写在 SQL 里。调用方不允许省略 asof——省略即抛错，不是取默认值。 */
export function loadBaseRates(
  dbPath: string, asof: string, horizon: number, kind: string, decisionType: string,
): Record<string, unknown> {
  if (!asof) throw new Error('asof 必填：这一层没有默认 as-of 是故意的')
  const db = openReadOnly(dbPath)
  if (!db) return unavailable(dbPath, '处境库文件不存在：' + dbPath)
  try {
    let sql = `SELECT s.cell_id, s.subject_id, s.trade_date,
        o.fwd_return_pct r, o.fwd_excess_pct e, o.fwd_min_nav_return_pct w,
        o.outcome_available_at avail,
        json_extract(s.coords_json,'$.__bot_id') bot_id,
        json_extract(s.coords_json,'$.__run_id') run_id,
        json_extract(s.coords_json,'$.decision_type') dt
      FROM situation_outcomes o
      JOIN situation_vectors s
        ON s.subject_id = o.subject_id AND s.subject_type = o.subject_kind
      WHERE o.outcome_available_at IS NOT NULL
        AND o.outcome_available_at <= ?
        AND o.status = 'ok'
        AND o.horizon_days = ?
        AND s.subject_type = ?
        -- 结果表以 (subject, horizon, outcome_version) 为主键。不锁版本的话，
        -- 将来落第二个 outcome_version 会让每条先例被数两遍，n 悄悄翻倍而不报错。
        AND o.outcome_version = (SELECT max(outcome_version) FROM situation_outcomes)`
    const params: any[] = [asof, horizon, kind]
    if (decisionType) { sql += ` AND json_extract(s.coords_json,'$.decision_type') = ?`; params.push(decisionType) }
    const rows = q(db, sql, ...params)

    const byCell = new Map<string, SqlRow[]>()
    for (const r of rows) {
      const c = String(r.cell_id)
      if (!byCell.has(c)) byCell.set(c, [])
      byCell.get(c)!.push(r)
    }
    const cells: any[] = []
    for (const [cell, rs] of byCell) {
      const rets = rs.map(r => r.r).filter(x => x !== null && x !== undefined).map(Number)
      const exs = rs.map(r => r.e).filter(x => x !== null && x !== undefined).map(Number)
      const worst = rs.map(r => r.w).filter(x => x !== null && x !== undefined).map(Number)
      const nInd = independentWindows(rs, horizon)
      const bots = new Set(rs.map(r => String(r.bot_id)))
      const enough = nInd >= MIN_N
      cells.push({
        cellId: cell,
        stress: cell.split('|')[0], dd: cell.split('|')[1],
        nRows: rs.length, nInd, nBot: bots.size, bots: [...bots].sort(),
        nRet: rets.length,
        win: rets.filter(x => x > 0).length,
        // 胜率也是**导出统计量**，样本不足时同样不给。只留 win / nRet 两个原始计数，
        // 想自己算的人算得出来，但接口不会替他把 n=2 的 100% 说出口。
        winRate: enough && rets.length ? rets.filter(x => x > 0).length / rets.length * 100 : null,
        q: enough ? quantiles(rets) : quantiles([]),
        excessMedian: enough && exs.length ? quantiles(exs)['0.5'] : null,
        worstMedian: enough && worst.length ? quantiles(worst)['0.5'] : null,
        nExcess: exs.length,
        enough,
        // 样本不足时**不返回**分位数，前端就算想显示也没有东西可显示。
        outOfMap: enough ? null : `独立窗口 ${nInd} < ${MIN_N}，你在地图外，本格不出数字`,
      })
    }
    cells.sort((a, b) => b.nRows - a.nRows)

    const totalOk = Number(q1(db,
      `SELECT count(*) c FROM situation_outcomes o JOIN situation_vectors s
         ON s.subject_id=o.subject_id AND s.subject_type=o.subject_kind
       WHERE o.status='ok' AND o.horizon_days=? AND s.subject_type=?`, horizon, kind).c ?? 0)

    return {
      available: true, asof, horizon, kind, decisionType: decisionType || null,
      minN: MIN_N,
      caliber: '「后来怎么样」= 该 bot 自己净值在窗口内的走势，不是这次动作的归因。仓位、行情、其它持仓、当天没做的事全混在里面。',
      cells,
      totals: {
        nRows: cells.reduce((a, c) => a + c.nRows, 0),
        nInd: cells.reduce((a, c) => a + c.nInd, 0),
        nCells: cells.length,
        nCellsOutOfMap: cells.filter(c => !c.enough).length,
        visibleOfAllOk: totalOk,
      },
    }
  } finally { db.close() }
}

/**
 * 闸门实证：同一条查询只改 as-of，样本数必须单调不减。
 * 「我在 SQL 里写了 PIT」是一句断言；这张表是证据。
 */
export function loadPitProof(dbPath: string, horizon: number, kind: string): Record<string, unknown> {
  const db = openReadOnly(dbPath)
  if (!db) return unavailable(dbPath, '处境库文件不存在：' + dbPath)
  try {
    const span = q1(db, `SELECT min(outcome_available_at) a, max(outcome_available_at) b
      FROM situation_outcomes WHERE status='ok' AND horizon_days=?`, horizon)
    const full = Number(q1(db, `SELECT count(*) c FROM situation_outcomes o JOIN situation_vectors s
      ON s.subject_id=o.subject_id AND s.subject_type=o.subject_kind
      WHERE o.status='ok' AND o.horizon_days=? AND s.subject_type=?
        AND o.outcome_version = (SELECT max(outcome_version) FROM situation_outcomes)`, horizon, kind).c ?? 0)
    const points = ['2025-03-31', '2025-06-30', '2025-12-31', '2026-03-31', '2026-06-30', '2026-08-11', '2099-01-01']
    let prev = -1
    let monotone = true
    const rows = points.map(asof => {
      const n = Number(q1(db, `SELECT count(*) c FROM situation_outcomes o JOIN situation_vectors s
        ON s.subject_id=o.subject_id AND s.subject_type=o.subject_kind
        WHERE o.outcome_available_at IS NOT NULL AND o.outcome_available_at <= ?
          AND o.status='ok' AND o.horizon_days=? AND s.subject_type=?
          AND o.outcome_version = (SELECT max(outcome_version) FROM situation_outcomes)`,
        asof, horizon, kind).c ?? 0)
      const ok = n >= prev
      monotone = monotone && ok
      prev = n
      return { asof, n, share: full ? n / full * 100 : 0, monotone: ok }
    })
    return {
      available: true, horizon, kind, full, monotone,
      reachesFull: prev === full,
      span: { from: span.a ?? null, to: span.b ?? null },
      rows,
    }
  } finally { db.close() }
}

// ---------------------------------------------------------------- 卡片

/** 只有有机械谓词的卡才进这里。hit_means 从 INSTANTIATES 边的 role 反查——它是声明的，不是推断的。 */
export function loadPredicateCards(dbPath: string): Record<string, unknown> {
  const db = openReadOnly(dbPath)
  if (!db) return unavailable(dbPath, '处境库文件不存在：' + dbPath)
  try {
    const meta = new Map<string, any>()
    for (const r of q(db, `SELECT card_id, version, title, claim, status, card_type
        FROM experience_card_versions ORDER BY card_id, version`)) {
      meta.set(String(r.card_id), r)
    }
    const roles = new Map<string, string>()
    for (const r of q(db, `SELECT dst_id, role, count(*) n FROM experience_edges
        WHERE edge_type='INSTANTIATES' GROUP BY 1,2`)) {
      roles.set(String(r.dst_id), String(r.role))
    }
    const stats = q(db, `SELECT card_id, subject_kind,
        sum(CASE WHEN verdict=1 THEN 1 ELSE 0 END) n_true,
        sum(CASE WHEN verdict=0 THEN 1 ELSE 0 END) n_false,
        sum(CASE WHEN verdict IS NULL THEN 1 ELSE 0 END) n_null,
        count(*) n FROM experience_card_verdicts GROUP BY 1,2`)
    const byCard = new Map<string, any>()
    for (const r of stats) {
      const cid = String(r.card_id)
      if (!byCard.has(cid)) {
        const m = meta.get(cid) ?? {}
        byCard.set(cid, {
          cardId: cid, title: m.title ?? cid, claim: m.claim ?? '', status: m.status ?? '',
          cardType: m.card_type ?? '', hitMeans: roles.get(cid) ?? '（无实例，方向未知）',
          byKind: [], scope: [], evidence: { corroborates: 0, refutes: 0 },
        })
      }
      byCard.get(cid).byKind.push({
        subjectKind: r.subject_kind, nTrue: Number(r.n_true), nFalse: Number(r.n_false),
        nNull: Number(r.n_null), n: Number(r.n),
        truePct: Number(r.n) ? Number(r.n_true) / Number(r.n) * 100 : 0,
      })
    }
    for (const r of q(db, `SELECT src_id, dst_id, role, basis_json FROM experience_edges
        WHERE edge_type='SCOPED_TO' ORDER BY src_id`)) {
      const c = byCard.get(String(r.src_id))
      if (!c) continue
      const b = jparse(r.basis_json)
      c.scope.push({ cellId: r.dst_id, subjectKind: r.role, n: b['实例数'], total: b['该卡实例总数'], share: b['占比'] })
    }
    for (const r of q(db, `SELECT dst_id, edge_type, count(*) n FROM experience_edges
        WHERE edge_type IN ('CORROBORATES','REFUTES') GROUP BY 1,2`)) {
      const c = byCard.get(String(r.dst_id))
      if (!c) continue
      if (r.edge_type === 'CORROBORATES') c.evidence.corroborates = Number(r.n)
      else c.evidence.refutes = Number(r.n)
    }
    const cards = [...byCard.values()]
    for (const c of cards) {
      const day = c.byKind.find((k: any) => k.subjectKind === 'backtest_day')
      // 判别力：命中率接近 100% 的谓词，命中这件事几乎不携带信息。
      // 这个读数是**降级信号**，不是重要性排名——刻意写成一句话，免得被读成"覆盖广=重要"。
      c.power = day ? {
        truePct: day.truePct,
        reading: day.truePct >= 70
          ? `${day.truePct.toFixed(0)}% 的日子都为真——这是常态处境，命中几乎不携带信息`
          : day.truePct <= 2
            ? `${day.truePct.toFixed(1)}% 的日子为真——极稀有，样本会很小`
            : `${day.truePct.toFixed(1)}% 的日子为真——有判别力`,
      } : null
      // 实测范围集中度：先例是否挤在一个格里
      const dayScope = c.scope.filter((s: any) => s.subjectKind === 'backtest_day')
      const top = dayScope.reduce((a: any, b: any) => (!a || Number(b.share) > Number(a.share) ? b : a), null)
      c.scopeConcentration = top ? {
        topCell: top.cellId, share: Number(top.share) * 100, nCells: dayScope.length,
        flag: Number(top.share) > 0.6 ? '先例高度集中于单格，实测范围远窄于自称范围' : null,
      } : null
    }
    cards.sort((a, b) => {
      const ad = a.byKind.find((k: any) => k.subjectKind === 'backtest_day')?.nTrue ?? 0
      const bd = b.byKind.find((k: any) => k.subjectKind === 'backtest_day')?.nTrue ?? 0
      return bd - ad
    })

    const noPredicate = Number(q1(db, `SELECT count(*) c FROM experience_card_versions
      WHERE card_id NOT IN (SELECT DISTINCT card_id FROM experience_card_verdicts)`).c ?? 0)

    return {
      available: true, cards,
      noPredicateCount: noPredicate,
      note: '库里 216 张卡，只有下面这几张写得出机械谓词、能被证伪。其余 ' + noPredicate +
        ' 张目前只能靠人读——不是它们不好，是它们还没有可复算的判定式，因此不进判定表、不产生边、也不出现在处境格里。',
    }
  } finally { db.close() }
}

// ---------------------------------------------------------------- 格子下钻

export function loadCellDetail(
  dbPath: string, cellId: string, horizon: number, asof: string,
): Record<string, unknown> {
  const db = openReadOnly(dbPath)
  if (!db) return unavailable(dbPath, '处境库文件不存在：' + dbPath)
  try {
    const subjects = q(db, `SELECT s.subject_type, s.subject_id, s.trade_date,
        json_extract(s.coords_json,'$.__bot_id') bot_id,
        json_extract(s.coords_json,'$.__run_id') run_id,
        json_extract(s.coords_json,'$.decision_type') dt,
        json_extract(s.coords_json,'$.action_count') ac,
        o.fwd_return_pct r, o.fwd_excess_pct e, o.status ostatus,
        o.outcome_available_at avail, o.note onote
      FROM situation_vectors s
      LEFT JOIN situation_outcomes o
        ON o.subject_id = s.subject_id AND o.subject_kind = s.subject_type
        AND o.horizon_days = ?
        AND o.outcome_version = (SELECT max(outcome_version) FROM situation_outcomes)
      WHERE s.cell_id = ?
      ORDER BY s.trade_date DESC LIMIT ?`, horizon, cellId, CELL_ROW_CAP)

    // 真实条数必须单独数。以前直接把截断后的 subjects.length 当 total 返回，
    // 于是最大的那个格子在下钻页显示「共 400 条先例」，而底率页对同一格说 722 行，
    // 库里实为 740——同一个系统对同一件事给了三个数。截断可以有，谎报不行。
    const totals = q(db, `SELECT s.subject_type k, count(*) n,
        sum(CASE WHEN o.outcome_available_at IS NOT NULL AND o.outcome_available_at <= ? THEN 1 ELSE 0 END) vis
      FROM situation_vectors s
      LEFT JOIN situation_outcomes o
        ON o.subject_id = s.subject_id AND o.subject_kind = s.subject_type
        AND o.horizon_days = ?
        AND o.outcome_version = (SELECT max(outcome_version) FROM situation_outcomes)
      WHERE s.cell_id = ? GROUP BY 1`, asof || '', horizon, cellId)
    const tot = (k: string) => Number(totals.find(r => r.k === k)?.n ?? 0)
    const trueTotal = totals.reduce((a, r) => a + Number(r.n), 0)
    const trueVisible = totals.reduce((a, r) => a + Number(r.vis ?? 0), 0)

    const cards = q(db, `SELECT v.card_id, c.title, count(*) n FROM experience_card_verdicts v
      LEFT JOIN experience_card_versions c ON c.card_id = v.card_id
      WHERE v.cell_id = ? AND v.verdict = 1 GROUP BY 1,2 ORDER BY n DESC`, cellId)

    const cohabit = q(db, `SELECT src_id, dst_id, basis_json FROM experience_edges
      WHERE edge_type='CO_OCCURS_IN_CELL'`)
      .filter(r => String(jparse(r.basis_json)['格'] ? JSON.stringify(jparse(r.basis_json)['格']) : '').includes(cellId))
      .map(r => ({ left: r.src_id, right: r.dst_id, detail: jparse(r.basis_json) }))

    return {
      available: true, cellId, horizon, asof,
      stress: cellId.split('|')[0], dd: cellId.split('|')[1],
      subjects: subjects.map(s => {
        const vis = Boolean(s.avail && asof && String(s.avail) <= asof)
        return {
          subjectKind: s.subject_type, subjectId: s.subject_id, tradeDate: s.trade_date,
          botId: s.bot_id, runId: s.run_id, decisionType: s.dt, actionCount: s.ac,
          // as-of 那天还看不到的收益值，**在服务端就置空，不下发**。
          // 以前这两个值照发，只在前端 JS 里盖住——打开 devtools 就能读到 D+20 的收益。
          // 闸门必须在数据出库那一步闭合，不能靠渲染层遮。
          fwdReturnPct: vis ? s.r : null,
          fwdExcessPct: vis ? s.e : null,
          outcomeStatus: s.ostatus,
          availableAt: s.avail, note: s.onote,
          visibleAtAsof: vis,
        }
      }),
      cards: cards.map(r => ({ cardId: r.card_id, title: r.title ?? r.card_id, n: Number(r.n) })),
      coOccurs: cohabit,
      counts: {
        total: trueTotal,
        day: tot('backtest_day'),
        case: tot('case'),
        outcomeVisible: trueVisible,
        // 表里实际列出多少行，以及是不是被截断了。前端必须把这两个数说出来。
        shown: subjects.length,
        cap: CELL_ROW_CAP,
        truncated: trueTotal > subjects.length,
      },
    }
  } finally { db.close() }
}

// ---------------------------------------------------------------- 边

export function loadEdges(dbPath: string, edgeType: string, limit: number): Record<string, unknown> {
  const db = openReadOnly(dbPath)
  if (!db) return unavailable(dbPath, '处境库文件不存在：' + dbPath)
  try {
    // 认不出的 edge_type 以前被静默忽略，于是筛选框看着生效、实际把 1453 条全发了回去。
    // 「筛选没生效」比「筛选报错」危险得多——报错看得见，全量伪装成子集看不见。
    const knownTypes = q(db, `SELECT DISTINCT edge_type t FROM experience_edges ORDER BY 1`)
      .map(r => String(r.t))
    if (edgeType && !knownTypes.includes(edgeType)) {
      return { available: true, unknownType: true, edgeType, knownTypes, edges: [], total: 0 }
    }

    const titles = new Map<string, string>()
    for (const r of q(db, `SELECT card_id, title FROM experience_card_versions`)) {
      titles.set(String(r.card_id), String(r.title))
    }
    let sql = `SELECT edge_id, edge_type, src_kind, src_id, dst_kind, dst_id, role, basis,
        basis_json, trade_date, cell_id, predicate_version, evaluator_version, computed_at
      FROM experience_edges`
    const params: any[] = []
    if (edgeType) { sql += ` WHERE edge_type = ?`; params.push(edgeType) }
    sql += ` ORDER BY edge_type, src_id LIMIT ?`
    params.push(Math.max(1, Math.min(2000, limit || 200)))
    const rows = q(db, sql, ...params)
    return {
      available: true, edgeType: edgeType || null,
      // basis 是这张表的**主键级信息**：mechanical_predicate 可复算可证伪；
      // card_provenance 是卡自己写的出处，机器没验过。两者绝不能在同一列里混着读。
      basisLegend: {
        mechanical_predicate: '机械谓词判定——可复算、可证伪。改一行代码结论就会变，这是好事。',
        card_provenance: '卡片自报的出处——机器没有验证过，只是把卡里写的原话搬过来。',
        observed_instances: '实测处境格分布——不是作者声明的适用范围。',
        forward_nav_not_attribution: '后续净值走势，不是归因。样本小于 5 的整组标弱证据。',
        keyword_signature_superseded: '按关键词重叠检出的旧判法，已保留但被取代。「出现相同关键词」不等于「条件真的重叠」。',
        observed_cell_overlap: '两张卡的先例落在同一格。这只是共现，不是冲突。',
      },
      edges: rows.map(r => ({
        edgeId: r.edge_id, edgeType: r.edge_type,
        src: { kind: r.src_kind, id: r.src_id, title: titles.get(String(r.src_id)) ?? null },
        dst: { kind: r.dst_kind, id: r.dst_id, title: titles.get(String(r.dst_id)) ?? null },
        role: r.role, basis: r.basis, detail: jparse(r.basis_json),
        tradeDate: r.trade_date, cellId: r.cell_id,
        predicateVersion: r.predicate_version, evaluatorVersion: r.evaluator_version,
      })),
      total: Number((edgeType
        ? q1(db, `SELECT count(*) c FROM experience_edges WHERE edge_type = ?`, edgeType)
        : q1(db, `SELECT count(*) c FROM experience_edges`)).c ?? 0),
      knownTypes,
    }
  } finally { db.close() }
}

// ---------------------------------------------------------------- 结果覆盖率

/**
 * 「有多少主体根本没有后续结果」——这个数必须显示，否则底率会看起来比实际扎实。
 * 案例写在 run 的活边缘上，71% 的案例没有 20 日后果，这是结构性的，不是数据缺口。
 */
export function loadCoverage(dbPath: string, horizon: number): Record<string, unknown> {
  const db = openReadOnly(dbPath)
  if (!db) return unavailable(dbPath, '处境库文件不存在：' + dbPath)
  try {
    const rows = q(db, `SELECT subject_kind, status, count(*) n FROM situation_outcomes
      WHERE horizon_days = ?
        AND outcome_version = (SELECT max(outcome_version) FROM situation_outcomes)
      GROUP BY 1,2`, horizon)
    const byKind = new Map<string, any>()
    for (const r of rows) {
      const k = String(r.subject_kind)
      if (!byKind.has(k)) byKind.set(k, { subjectKind: k, ok: 0, truncated: 0, no_nav: 0, total: 0 })
      const t = byKind.get(k)
      t[String(r.status)] = Number(r.n)
      t.total += Number(r.n)
    }
    const out = [...byKind.values()].map(t => ({ ...t, okPct: t.total ? t.ok / t.total * 100 : 0 }))
    const gaps = q(db, `SELECT end_date, horizon_days, note, count(*) n FROM situation_outcomes
      WHERE status='ok' AND bench_return_pct IS NULL AND horizon_days=? GROUP BY 1,2,3`, horizon)
    return {
      available: true, horizon, byKind: out,
      excessGaps: gaps.map(r => ({ endDate: r.end_date, horizon: Number(r.horizon_days), note: r.note, n: Number(r.n) })),
      note: 'truncated = run 末尾窗口不够，收益列留空而不是拿最后一天顶数。案例写在 run 的活边缘上，所以案例线的留白比例天然远高于日级线。',
    }
  } finally { db.close() }
}
