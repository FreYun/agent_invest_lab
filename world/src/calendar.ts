import { readFileSync } from 'node:fs'

export interface Calendar {
  tradingDays: string[]
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export function loadCalendar(path: string): Calendar {
  const raw = JSON.parse(readFileSync(path, 'utf8')) as unknown
  const days = (raw as { trading_days?: unknown })?.trading_days
  if (!Array.isArray(days)) throw new Error(`calendar ${path}: missing array field "trading_days"`)
  const tradingDays: string[] = []
  for (const d of days) {
    if (typeof d !== 'string' || !ISO_DATE.test(d)) throw new Error(`calendar ${path}: bad date ${JSON.stringify(d)} (expected YYYY-MM-DD)`)
    if (tradingDays.length && d <= tradingDays[tradingDays.length - 1]) throw new Error(`calendar ${path}: trading_days must be strictly sorted ascending (offender: ${d})`)
    tradingDays.push(d)
  }
  if (tradingDays.length === 0) throw new Error(`calendar ${path}: trading_days is empty`)
  return { tradingDays }
}

export function computeTradingDates(cal: Calendar, from: string, to: string): string[] {
  if (!ISO_DATE.test(from) || !ISO_DATE.test(to)) throw new Error(`replay range must be YYYY-MM-DD (got ${from} .. ${to})`)
  if (from > to) throw new Error(`replay range start ${from} is after end ${to}`)
  const out = cal.tradingDays.filter(d => d >= from && d <= to)
  if (out.length === 0) throw new Error(`no trading days in calendar within range ${from} .. ${to}`)
  return out
}
