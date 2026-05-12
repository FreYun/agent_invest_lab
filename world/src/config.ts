import { readFileSync } from 'node:fs'
import { dirname, isAbsolute, resolve } from 'node:path'
import { parse as parseYaml } from 'yaml'

export const DEFAULT_SHADOW_INCLUDE = [
  'IDENTITY.md', 'SOUL.md', 'AGENTS.md', 'USER.md',
  'METHODOLOGY.md', 'RESEARCH.md', 'MEMORY.md',
  'EQUIPPED_SKILLS.md', 'TOOLS.md', 'skills', 'config',
]

export interface WorldConfig {
  researchLoopTs: string
  workspaceRoot: string
  bots: string[]
  replay: { from: string; to: string }
  calendar: string
  concurrency: number
  perBotTimeoutSeconds: number
  rlConfigBase: string
  rlOpenclawDir: string
  shadowInclude: string[]
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

function reqString(obj: Record<string, unknown>, key: string): string {
  const v = obj[key]
  if (typeof v !== 'string' || v.trim() === '') throw new Error(`world config: "${key}" is required and must be a non-empty string`)
  return v
}

function validIsoDate(s: string, label: string): string {
  if (!ISO_DATE.test(s)) throw new Error(`world config: "${label}" must be YYYY-MM-DD (got ${JSON.stringify(s)})`)
  const d = new Date(s + 'T00:00:00Z')
  if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== s) throw new Error(`world config: "${label}" is not a real date (${s})`)
  return s
}

function resolveMaybe(base: string, p: string): string {
  return isAbsolute(p) ? p : resolve(base, p)
}

export function loadWorldConfig(path: string): WorldConfig {
  const text = readFileSync(path, 'utf8')
  const raw = (parseYaml(text) ?? {}) as Record<string, unknown>
  const baseDir = dirname(resolve(path))

  const researchLoopTs = reqString(raw, 'research_loop_ts')
  const workspaceRoot = reqString(raw, 'workspace_root')

  const botsRaw = raw.bots
  if (!Array.isArray(botsRaw) || botsRaw.length === 0 || !botsRaw.every(b => typeof b === 'string' && b.trim())) {
    throw new Error('world config: "bots" must be a non-empty array of bot ids (e.g. [bot1, bot7])')
  }
  const bots = botsRaw as string[]

  const replayRaw = (raw.replay ?? {}) as Record<string, unknown>
  const from = validIsoDate(reqString(replayRaw, 'from'), 'replay.from')
  const to = validIsoDate(reqString(replayRaw, 'to'), 'replay.to')
  if (from > to) throw new Error(`world config: replay.from (${from}) is after replay.to (${to})`)

  const calendar = typeof raw.calendar === 'string' && raw.calendar.trim()
    ? resolveMaybe(baseDir, raw.calendar)
    : resolveMaybe(baseDir, 'calendar.json')
  const concurrency = typeof raw.concurrency === 'number' && raw.concurrency >= 1 ? Math.floor(raw.concurrency) : 4
  const perBotTimeoutSeconds = typeof raw.per_bot_timeout_seconds === 'number' && raw.per_bot_timeout_seconds > 0 ? Math.floor(raw.per_bot_timeout_seconds) : 1200
  const rlConfigBase = typeof raw.rl_config_base === 'string' && raw.rl_config_base.trim()
    ? resolveMaybe(baseDir, raw.rl_config_base)
    : resolveMaybe(baseDir, '../config/trading-rl-config.base.json')
  const rlOpenclawDir = typeof raw.rl_openclaw_dir === 'string' && raw.rl_openclaw_dir.trim() ? raw.rl_openclaw_dir : '/home/rooot/.openclaw'
  const shadowInclude = Array.isArray(raw.shadow_include) && raw.shadow_include.every(x => typeof x === 'string')
    ? (raw.shadow_include as string[])
    : DEFAULT_SHADOW_INCLUDE

  return { researchLoopTs, workspaceRoot, bots, replay: { from, to }, calendar, concurrency, perBotTimeoutSeconds, rlConfigBase, rlOpenclawDir, shadowInclude }
}
