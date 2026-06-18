#!/usr/bin/env -S node --experimental-strip-types
import { resolve, join } from 'node:path'
import { loadWorldConfig, type WorldConfig } from './config.ts'
import { loadCalendar, computeTradingDates } from './calendar.ts'
import { runWorld } from './run.ts'

function argVal(argv: string[], flag: string): string | undefined {
  const i = argv.indexOf(flag)
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined
}

function latestTradingDay(calendarPath: string): string {
  const cal = loadCalendar(calendarPath)
  const today = process.env.TODAY ?? new Date().toISOString().slice(0, 10)
  for (let i = cal.tradingDays.length - 1; i >= 0; i--) {
    if (cal.tradingDays[i] <= today) return cal.tradingDays[i]
  }
  throw new Error(`no trading day <= ${today} in ${calendarPath}`)
}

function pickRecord<T>(record: Record<string, T> | undefined, key: string): Record<string, T> | undefined {
  if (!record || !(key in record)) return undefined
  return { [key]: record[key] }
}

async function main(argv = process.argv.slice(2)): Promise<number> {
  if (argv.includes('-h') || argv.includes('--help')) {
    process.stdout.write(
      'oos-daily-driver: run bot101 for exactly one trading day without resetting the fund account\n' +
      '  --date YYYY-MM-DD       default: latest trading day <= today in the config calendar\n' +
      '  --run-id ID             default: OOS_BOT101_RUN_ID or oos-bot101-daily\n' +
      '  --config PATH           default: config/world-multi-fund-backtest.yaml\n' +
      '  --world-dir PATH        default: <cwd>/runtime\n',
    )
    return 0
  }

  const configPath = argVal(argv, '--config') ?? 'config/world-multi-fund-backtest.yaml'
  const base = loadWorldConfig(configPath)
  const date = argVal(argv, '--date') ?? process.env.TRADE_DATE ?? latestTradingDay(base.calendar)
  computeTradingDates(loadCalendar(base.calendar), date, date)

  const runId = argVal(argv, '--run-id') ?? process.env.OOS_BOT101_RUN_ID ?? 'oos-bot101-daily'
  const worldRoot = resolve(argVal(argv, '--world-dir') ?? join(process.cwd(), 'runtime'))
  const config: WorldConfig = {
    ...base,
    bots: ['bot101'],
    replay: { from: date, to: date },
    concurrency: 1,
    fundInitReset: false,
    chatStepMode: 'trading_days',
    chatStepDays: 1,
    researchDayEvery: 0,
    botAssignments: pickRecord(base.botAssignments, 'bot101'),
    botModels: pickRecord(base.botModels, 'bot101'),
  }

  process.stdout.write(`[oos-daily-driver] date=${date} run_id=${runId} world_root=${worldRoot} fund_init_reset=false\n`)
  await runWorld({ worldRoot, config, runId })
  return 0
}

main().then(code => { process.exitCode = code }).catch(err => {
  process.stderr.write(`[oos-daily-driver] fatal: ${err instanceof Error ? err.stack ?? err.message : String(err)}\n`)
  process.exitCode = 1
})
