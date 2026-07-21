#!/usr/bin/env -S node --experimental-strip-types
import { resolve, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadWorldConfig, type WorldConfig } from './config.ts'

import { loadCalendar, computeTradingDates } from './calendar.ts'
import { runWorld } from './run.ts'

function argVal(argv: string[], flag: string): string | undefined {
  const i = argv.indexOf(flag)
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined
}

function splitList(raw: string | undefined): string[] {
  return (raw ?? '')
    .split(/[\s,]+/)
    .map(s => s.trim())
    .filter(Boolean)
}

function latestTradingDay(calendarPath: string): string {
  const cal = loadCalendar(calendarPath)
  const today = process.env.TODAY ?? new Date().toISOString().slice(0, 10)
  for (let i = cal.tradingDays.length - 1; i >= 0; i--) {
    if (cal.tradingDays[i] <= today) return cal.tradingDays[i]
  }
  throw new Error(`no trading day <= ${today} in ${calendarPath}`)
}

function defaultRunIdFor(botIds: string[]): string {
  if (botIds.length === 1 && /^bot\d+$/.test(botIds[0])) return `oos-${botIds[0]}-daily`
  return process.env.OOS_BOT101_RUN_ID ?? 'oos-bot101-daily'
}

export function phaseToFlags(phase: string | undefined): { skipClose: boolean; skipChat: boolean } {
  if (phase === 'decide') return { skipClose: true, skipChat: false }
  if (phase === 'settle') return { skipClose: false, skipChat: true }
  return { skipClose: false, skipChat: false }
}

function pickRecords<T>(record: Record<string, T> | undefined, keys: string[]): Record<string, T> | undefined {
  if (!record) return undefined
  const out: Record<string, T> = {}
  for (const key of keys) {
    if (key in record) out[key] = record[key]
  }
  return Object.keys(out).length ? out : undefined
}

async function main(argv = process.argv.slice(2)): Promise<number> {
  if (argv.includes('-h') || argv.includes('--help')) {
    process.stdout.write([
      'oos-daily-driver: run one or more OOS bots for exactly one trading day without resetting fund accounts',
      '  --date YYYY-MM-DD       default: latest trading day <= today in the config calendar',
      '  --bot-id ID             bot to run; may repeat. default: OOS_BOTS or bot101',
      '  --bots A,B              comma/space-separated bots; alternative to repeated --bot-id',
      '  --run-id ID             default: oos-<bot>-daily for a single bot, else OOS_BOT101_RUN_ID/oos-bot101-daily',
      '  --config PATH           default: config/world-multi-fund-backtest.yaml',
      '  --world-dir PATH        default: <cwd>/runtime',
      '  --phase decide|settle   live 两阶段：decide=盘中决策跳过收盘核算；settle=盘后纯系统结算',
    ].join('\n') + '\n')
    return 0
  }

  const configPath = argVal(argv, '--config') ?? 'config/world-multi-fund-backtest.yaml'
  const base = loadWorldConfig(configPath)
  const date = argVal(argv, '--date') ?? process.env.TRADE_DATE ?? latestTradingDay(base.calendar)
  computeTradingDates(loadCalendar(base.calendar), date, date)

  const repeatedBotIds = argv.flatMap((arg, i) => arg === '--bot-id' && argv[i + 1] ? [argv[i + 1]] : [])
  const botIds = [...new Set([...repeatedBotIds, ...splitList(argVal(argv, '--bots') ?? process.env.OOS_BOTS)])]
  const bots = botIds.length ? botIds : ['bot101']
  const runId = argVal(argv, '--run-id') ?? defaultRunIdFor(bots)
  const worldRoot = resolve(argVal(argv, '--world-dir') ?? join(process.cwd(), 'runtime'))
  const phase = argVal(argv, '--phase')
  const { skipClose, skipChat } = phaseToFlags(phase)
  const config: WorldConfig = {
    ...base,
    bots,
    replay: { from: date, to: date },
    concurrency: 1,
    fundInitReset: false,
    chatStepMode: 'trading_days',
    chatStepDays: 1,
    researchDayEvery: 0,
    botAssignments: pickRecords(base.botAssignments, bots),
    botModels: pickRecords(base.botModels, bots),
    skipClose,
    skipChat,
  }

  process.stdout.write(`[oos-daily-driver] date=${date} run_id=${runId} bots=${bots.join(',')} world_root=${worldRoot} fund_init_reset=false
`)
  await runWorld({ worldRoot, config, runId })
  return 0
}

const isEntry = process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])
if (isEntry) {
  main().then(code => { process.exitCode = code }).catch(err => {
    process.stderr.write(`[oos-daily-driver] fatal: ${err instanceof Error ? err.stack ?? err.message : String(err)}
`)
    process.exitCode = 1
  })
}
