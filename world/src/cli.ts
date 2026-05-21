import { existsSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { loadWorldConfig } from './config.ts'
import { runWorld, resumeWorld, requestStop, requestPause } from './run.ts'
import { readState, stateExists } from './state.ts'
import * as P from './paths.ts'

export interface CliArgs {
  command: 'run' | 'resume' | 'status' | 'stop' | 'pause' | 'help' | 'unknown'
  config?: string
  runId?: string
  worldDir?: string
}

const HELP = `world — agent_invest_lab 日度滚动金融世界系统

用法:
  world run    --config <world.yaml> [--run-id <id>] [--world-dir <dir>]   开新 run
  world resume --config <world.yaml> --run-id <id> [--world-dir <dir>]     续跑指定 run（从 state.json.cursor）
  world status --run-id <id> [--world-dir <dir>]                            查看某 run 的进度
  world stop   --run-id <id> [--world-dir <dir>]                            请求优雅终止某 run（当前交易日跑完后 abort，不可 resume）
  world pause  --run-id <id> [--world-dir <dir>]                            请求暂停某 run（立即终止当前交易日，可 resume，从该日重跑）
  world --help

注：--run-id 现在是 resume/status/stop 的必填项。dashboard 通过扫描
runtime/runs/*/state.json 拿到活动 run 列表。
`

export function parseCliArgs(argv: string[]): CliArgs {
  if (argv.length === 0 || argv[0] === '-h' || argv[0] === '--help') return { command: 'help', config: undefined, runId: undefined, worldDir: undefined }
  const cmd = argv[0]
  const valueOf = (name: string): string | undefined => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined }
  const base: CliArgs = { command: 'unknown', config: valueOf('--config'), runId: valueOf('--run-id'), worldDir: valueOf('--world-dir') }
  if (cmd === 'run' || cmd === 'resume' || cmd === 'status' || cmd === 'stop' || cmd === 'pause') return { ...base, command: cmd }
  return base
}

function resolveWorldRoot(args: CliArgs): string {
  return resolve(args.worldDir ?? join(process.cwd(), 'runtime'))
}

function deriveRunId(): string {
  return `run-${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}`
}

async function cmdRun(args: CliArgs): Promise<number> {
  if (!args.config) { process.stderr.write('run: --config <world.yaml> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  mkdirSync(worldRoot, { recursive: true })
  // 不再读/写 worldRoot/state.json；每个 run 自管 runDir/state.json，
  // dashboard 通过 listActiveRuns 扫描得到活动列表。
  const config = loadWorldConfig(args.config)
  const runId = args.runId ?? deriveRunId()
  await runWorld({ worldRoot, config, runId })
  return 0
}

async function cmdResume(args: CliArgs): Promise<number> {
  if (!args.config) { process.stderr.write('resume: --config <world.yaml> is required\n'); return 2 }
  if (!args.runId) { process.stderr.write('resume: --run-id <id> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  if (!stateExists(worldRoot, args.runId)) { process.stderr.write(`resume: no state.json for run "${args.runId}" under ${worldRoot}\n`); return 2 }
  const config = loadWorldConfig(args.config)
  await resumeWorld({ worldRoot, config, runId: args.runId })
  return 0
}

function cmdStatus(args: CliArgs): number {
  if (!args.runId) { process.stderr.write('status: --run-id <id> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  if (!stateExists(worldRoot, args.runId)) { process.stdout.write(`(no run "${args.runId}" under ${worldRoot})\n`); return 0 }
  const st = readState(worldRoot, args.runId)
  process.stdout.write(`run_id:       ${st.run_id}\nstatus:       ${st.status}\ncurrent_date: ${st.current_date}\nprogress:     ${st.cursor}/${st.trading_dates.length} trading days\nbots:         ${st.bots.join(', ')}\nmemory_port:  ${st.memory_port}\nstarted_at:   ${st.started_at}\nupdated_at:   ${st.updated_at}\n`)
  if (existsSync(P.summaryFile(worldRoot, st.run_id))) process.stdout.write(`summary:      ${P.summaryFile(worldRoot, st.run_id)}\n`)
  return 0
}

function cmdStop(args: CliArgs): number {
  if (!args.runId) { process.stderr.write('stop: --run-id <id> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  const r = requestStop(worldRoot, args.runId)
  if (!r.ok) { process.stderr.write(`stop: ${r.reason}\n`); return 2 }
  process.stdout.write(`stop requested — run "${args.runId}" will abort after the current trading day.\n`)
  return 0
}

function cmdPause(args: CliArgs): number {
  if (!args.runId) { process.stderr.write('pause: --run-id <id> is required\n'); return 2 }
  const worldRoot = resolveWorldRoot(args)
  const r = requestPause(worldRoot, args.runId)
  if (!r.ok) { process.stderr.write(`pause: ${r.reason}\n`); return 2 }
  process.stdout.write(`pause requested — run "${args.runId}" will halt the current trading day (resume re-runs it).\n`)
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
    case 'pause': return cmdPause(args)
    default: process.stderr.write(`unknown command: ${argv.join(' ')}\n\n${HELP}`); return 2
  }
}
