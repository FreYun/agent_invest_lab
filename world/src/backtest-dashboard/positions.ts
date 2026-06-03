export interface HoldingSnapshotRow {
  trade_date: string
  fund_code: string
  fund_name: string
  theme: string
  asset_class: string | null
  role: string | null
  shares: number | null
  nav: number | null
  market_value: number | null
  weight: number | null
  daily_pnl: number | null
  cumulative_return_pct: number | null
  holding_days: number | null
}

export type HoldingRowSansDate = Omit<HoldingSnapshotRow, 'trade_date'>

export function buildHoldingsByDate(rows: HoldingSnapshotRow[]): Record<string, HoldingRowSansDate[]> {
  const idx: Record<string, HoldingRowSansDate[]> = {}
  for (const row of rows) {
    const { trade_date, ...rest } = row
    ;(idx[trade_date] ??= []).push(rest)
  }
  return idx
}

export interface ActionWeights {
  weight_before: number
  weight_after: number
  weight_delta: number
}

/**
 * 估算一笔动作前后的该基金权重。注意基金申赎是 T+1 结算：动作记在下单日（T，
 * = action_date），但这笔成交要到 T+1 的快照才反映出来。因此：
 *   weight_before = action_date 当日及之前最近一张快照的权重（结算前 / 下单前的仓位）
 *   weight_after  = 严格晚于 action_date 的第一张快照的权重（T+1 结算后的真实仓位）
 * 若取锚点在 ≤ action_date（旧逻辑），买入行会显示“下单前”的仓位而非调仓后的仓位
 * （例如空仓→买入却显示成买入前某个旧仓权重）。数据末尾没有更晚快照时，after 回退
 * 到 before，避免误显 0。某基金在某日无快照行 → 权重按 0（覆盖首次建仓 / 清仓）。
 */
export function computeActionWeights(
  holdingsByDate: Record<string, { fund_code: string; weight: number | null }[]>,
  action_date: string,
  fund_code: string,
): ActionWeights {
  const dates = Object.keys(holdingsByDate).sort()
  const weightOn = (d: string | undefined): number =>
    d == null ? 0 : Number((holdingsByDate[d] || []).find(h => h.fund_code === fund_code)?.weight ?? 0)
  // beforeIdx: 最近一张 ≤ action_date 的快照
  let beforeIdx = -1
  for (let i = 0; i < dates.length; i++) {
    if (dates[i] <= action_date) beforeIdx = i
    else break
  }
  // afterIdx: 严格晚于 action_date 的第一张快照（T+1 结算后）；没有则回退到 before
  const afterIdx = beforeIdx + 1 < dates.length ? beforeIdx + 1 : beforeIdx
  const w_before = weightOn(dates[beforeIdx])
  const w_after = weightOn(dates[afterIdx])
  return {
    weight_before: w_before,
    weight_after: w_after,
    weight_delta: w_after - w_before,
  }
}

export interface StackBand {
  fund_code: string
  fund_name: string
  lower: number[]   // per-date cumulative weight at band bottom
  upper: number[]   // per-date cumulative weight at band top
}

/**
 * For a stacked-area chart: produce one band per fund across the date range.
 * Funds are ordered by their max weight (desc) within the range, so the heaviest
 * fund sits at the bottom of the stack and the visual order is stable for the
 * given range. Funds missing from a given date contribute a zero-width segment
 * that day (lower == upper).
 */
export function buildStackBands(
  holdingsByDate: Record<string, { fund_code: string; fund_name: string; weight: number | null }[]>,
  dates: string[],
): StackBand[] {
  // Collect funds present anywhere in `dates`, tracking max weight + display name
  const meta = new Map<string, { fund_name: string; maxWeight: number }>()
  for (const d of dates) {
    for (const h of holdingsByDate[d] ?? []) {
      const cur = meta.get(h.fund_code)
      const w = Number(h.weight ?? 0)
      if (!cur) meta.set(h.fund_code, { fund_name: h.fund_name, maxWeight: w })
      else if (w > cur.maxWeight) cur.maxWeight = w
    }
  }
  const ordered = [...meta.entries()].sort((a, b) => b[1].maxWeight - a[1].maxWeight || a[0].localeCompare(b[0]))
  // Build per-date cumulative stack
  const bands: StackBand[] = ordered.map(([fund_code, m]) => ({
    fund_code,
    fund_name: m.fund_name,
    lower: new Array(dates.length).fill(0),
    upper: new Array(dates.length).fill(0),
  }))
  for (let i = 0; i < dates.length; i++) {
    const day = holdingsByDate[dates[i]] ?? []
    const byCode = new Map<string, number>()
    for (const h of day) byCode.set(h.fund_code, Number(h.weight ?? 0))
    let cum = 0
    for (const band of bands) {
      band.lower[i] = cum
      cum += byCode.get(band.fund_code) ?? 0
      band.upper[i] = cum
    }
  }
  return bands
}
