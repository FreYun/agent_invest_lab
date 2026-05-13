import { existsSync, readFileSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { parse as parseYaml } from 'yaml'

export const DEFAULT_SHADOW_INCLUDE = [
  'IDENTITY.md', 'SOUL.md', 'AGENTS.md', 'USER.md',
  'METHODOLOGY.md', 'RESEARCH.md', 'MEMORY.md',
  'EQUIPPED_SKILLS.md', 'TOOLS.md', 'skills',
]

export interface WorldConfig {
  researchLoop: string
  botsRoot: string
  openclawJson: string
  skillsRoot: string
  bots: string[]
  replay: { from: string; to: string }
  calendar: string
  concurrency: number
  perBotTimeoutSeconds: number
  rlConfigBase: string
  rlOpenclawDir?: string
  shadowInclude: string[]
  loop: 'research-loop' | 'openclaw-pi'
  openclawRoot?: string
  piServerEntry?: string
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

  const researchLoop = reqString(raw, 'research_loop')

  // world.yaml lives at <world>/config/world.yaml, so defaults resolve into the world tree:
  //   bots_root      → ../bots            (i.e. <world>/bots/<botId>/)
  //   skills_root    → ../skills          (i.e. <world>/skills/, injected as extra_roots)
  //   openclaw_json  → ./openclaw.json    (i.e. <world>/config/openclaw.json, credentials)
  const botsRoot = typeof raw.bots_root === 'string' && raw.bots_root.trim()
    ? resolveMaybe(baseDir, raw.bots_root)
    : resolveMaybe(baseDir, '../bots')
  const skillsRoot = typeof raw.skills_root === 'string' && raw.skills_root.trim()
    ? resolveMaybe(baseDir, raw.skills_root)
    : resolveMaybe(baseDir, '../skills')
  const openclawJson = typeof raw.openclaw_json === 'string' && raw.openclaw_json.trim()
    ? resolveMaybe(baseDir, raw.openclaw_json)
    : resolveMaybe(baseDir, 'openclaw.json')

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
  const rlOpenclawDir = typeof raw.rl_openclaw_dir === 'string' && raw.rl_openclaw_dir.trim() ? raw.rl_openclaw_dir : undefined
  const shadowInclude = Array.isArray(raw.shadow_include) && raw.shadow_include.every(x => typeof x === 'string')
    ? (raw.shadow_include as string[])
    : DEFAULT_SHADOW_INCLUDE

  const loopRaw = raw.loop
  let loop: 'research-loop' | 'openclaw-pi' = 'research-loop'
  if (loopRaw !== undefined) {
    if (loopRaw !== 'research-loop' && loopRaw !== 'openclaw-pi') {
      throw new Error(`world config: "loop" must be "research-loop" or "openclaw-pi" (got ${JSON.stringify(loopRaw)})`)
    }
    loop = loopRaw
  }

  let openclawRoot: string | undefined
  let piServerEntry: string | undefined
  if (loop === 'openclaw-pi') {
    if (!existsSync(openclawJson)) {
      throw new Error(`world config: openclaw_json not found (required for openclaw-pi loop): ${openclawJson}`)
    }
    openclawRoot = typeof raw.openclaw_root === 'string' && raw.openclaw_root.trim()
      ? resolveMaybe(baseDir, raw.openclaw_root)
      : '/home/rooot/.openclaw/openclaw'
    piServerEntry = typeof raw.pi_server_entry === 'string' && raw.pi_server_entry.trim()
      ? resolveMaybe(baseDir, raw.pi_server_entry)
      : join(openclawRoot, 'src/agents/agent_invest_pi_stdio_server.ts')
    if (!existsSync(piServerEntry)) {
      throw new Error(`world config: pi_server_entry not found: ${piServerEntry}`)
    }
  }

  return { researchLoop, botsRoot, openclawJson, skillsRoot, bots, replay: { from, to }, calendar, concurrency, perBotTimeoutSeconds, rlConfigBase, rlOpenclawDir, shadowInclude, loop, openclawRoot, piServerEntry }
}
