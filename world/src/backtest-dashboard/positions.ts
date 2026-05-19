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
 * Look up the per-fund weight on `action_date` and on the latest snapshot date
 * strictly before it. If `action_date` itself has no snapshot row, fall back to
 * the latest snapshot ≤ action_date (handles weekend / between-snapshot actions).
 *
 * Missing fund on a given day → weight=0 (covers first-buy / sell-all).
 */
export function computeActionWeights(
  holdingsByDate: Record<string, { fund_code: string; weight: number | null }[]>,
  action_date: string,
  fund_code: string,
): ActionWeights {
  const dates = Object.keys(holdingsByDate).sort()
  // anchor: latest snapshot ≤ action_date
  let anchorIdx = -1
  for (let i = 0; i < dates.length; i++) {
    if (dates[i] <= action_date) anchorIdx = i
    else break
  }
  if (anchorIdx < 0) {
    // action happens before any snapshot — treat both sides as zero
    return { weight_before: 0, weight_after: 0, weight_delta: 0 }
  }
  const afterDay = holdingsByDate[dates[anchorIdx]]
  const beforeDay = anchorIdx > 0 ? holdingsByDate[dates[anchorIdx - 1]] : []
  const w_after = Number(afterDay.find(h => h.fund_code === fund_code)?.weight ?? 0)
  const w_before = Number(beforeDay.find(h => h.fund_code === fund_code)?.weight ?? 0)
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
