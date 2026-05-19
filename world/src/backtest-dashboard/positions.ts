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
