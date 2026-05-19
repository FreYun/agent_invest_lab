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
