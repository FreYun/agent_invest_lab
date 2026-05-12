import { existsSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { loadWorldConfig } from './config.ts'
import { runWorld, resumeWorld, requestStop } from './run.ts'
import { readState, stateExists } from './state.ts'
import * as P from './paths.ts'

export interface CliArgs {
  command: 'run' | 'resume' | 'status' | 'stop' | 'help' | 'unknown'
  config?: string
  runId?: string
  worldDir?: string
}

const HELP = `world — agent_invest_lab 日度滚动金融世界系统

用法:
  world run    --config <world.yaml> [--run-id <id>] [--world-dir <dir>]   开新 run（默认 --world-dir ./world）
  world resume --config <world.yaml> [--world-dir <dir>]                   从 state.json.cursor 续跑
  world status [--world-dir <dir>]                                        查看进度
  world stop   [--world-dir <dir>]                                        请求优雅终止当前 run
  world --help
`

export function parseCliArgs(argv: string[]): CliArgs {
  if (argv.length === 0 || argv[0] === '-h' || argv[0] === '--help') return { command: 'help', config: undefined, runId: undefined, worldDir: undefined }
  const cmd = argv[0]
  const valueOf = (name: string): string | undefined => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined }
  const base: CliArgs = { command: 'unknown', config: valueOf('--config'), runId: valueOf('--run-id'), worldDir: valueOf('--world-dir') }
  if (cmd === 'run' || cmd === 'resume' || cmd === 'status' || cmd === 'stop') return { ...base, command: cmd }
  return base
}

function resolveWorldRoot(args: CliArgs): string {
  return resolve(args.worldDir ?? join(process.cwd(), 'world'))
}

function deriveRunId(): string {
  return `run-${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}`
}

async function cmdRun(args: CliArgs): Promise<number> {
  if (!args.config) { process.stderr.write('run: --config <world.yaml> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  mkdirSync(worldRoot, { recursive: true })
  if (stateExists(worldRoot)) {
    const st = readState(worldRoot)
    if (st.status === 'running') { process.stderr.write(`run: a run "${st.run_id}" is already running (status=running). Use "world resume" or "world stop" first.\n`); return 2 }
  }
  const config = loadWorldConfig(args.config)
  const runId = args.runId ?? deriveRunId()
  await runWorld({ worldRoot, config, runId })
  return 0
}

async function cmdResume(args: CliArgs): Promise<number> {
  if (!args.config) { process.stderr.write('resume: --config <world.yaml> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  if (!stateExists(worldRoot)) { process.stderr.write(`resume: no state.json under ${worldRoot}\n`); return 2 }
  const config = loadWorldConfig(args.config)
  await resumeWorld({ worldRoot, config })
  return 0
}

function cmdStatus(args: CliArgs): number {
  const worldRoot = resolveWorldRoot(args)
  if (!stateExists(worldRoot)) { process.stdout.write(`(no run under ${worldRoot})\n`); return 0 }
  const st = readState(worldRoot)
  process.stdout.write(`run_id:       ${st.run_id}\nstatus:       ${st.status}\ncurrent_date: ${st.current_date}\nprogress:     ${st.cursor}/${st.trading_dates.length} trading days\nbots:         ${st.bots.join(', ')}\nmemory_port:  ${st.memory_port}\nstarted_at:   ${st.started_at}\nupdated_at:   ${st.updated_at}\n`)
  if (existsSync(P.summaryFile(worldRoot, st.run_id))) process.stdout.write(`summary:      ${P.summaryFile(worldRoot, st.run_id)}\n`)
  return 0
}

function cmdStop(args: CliArgs): number {
  const worldRoot = resolveWorldRoot(args)
  const r = requestStop(worldRoot)
  if (!r.ok) { process.stderr.write(`stop: ${r.reason}\n`); return 2 }
  process.stdout.write('stop requested — the running process will abort after the current trading day.\n')
  return 0
}

export async function main(argv = process.argv.slice(2)): Promise<number> {
  const args = parseCliArgs(argv)
  switch (args.command) {
    case 'help': process.stdout.write(HELP); return 0
    case 'run': return cmdRun(args)
    case 'resume': return cmdResume(args)
    case 'status': return cmdStatus(args)
    case 'stop': return cmdStop(args)
    default: process.stderr.write(`unknown command: ${argv.join(' ')}\n\n${HELP}`); return 2
  }
}
