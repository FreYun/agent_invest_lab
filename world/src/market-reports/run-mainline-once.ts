// Direct one-shot runner for reporter-mainline.
//
// This bypasses the market-reports prepass loop. It starts only the services
// reporter-mainline needs, deletes market_mainline for the target date, lets the
// agent submit a fresh report, then exits.

import { join, resolve } from 'node:path'
import { mkdirSync, writeFileSync, copyFileSync, existsSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { loadWorldConfig } from '../config.ts'
import { MemoryStore } from '../memory-server/store.ts'
import { createMemoryServer } from '../memory-server/server.ts'
import { createSimworldProxy } from '../simworld-proxy/server.ts'
import { createStrategyServer } from '../strategy-server/server.ts'
import { buildShadowWorkspace } from '../shadowWorkspace.ts'
import { BotServer } from '../botServer.ts'
import {
  botServerArgv, loopConfigPath, proxyEnvSupplement, openclawJsonSource,
  generateRlConfig, writeResearchLoopYaml,
} from '../run.ts'
import * as P from '../paths.ts'

function argVal(argv: string[], flag: string): string | undefined {
  const i = argv.indexOf(flag)
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined
}

function sqlStr(s: string): string { return `'${s.replace(/'/g, "''")}'` }

function sqlite(dbPath: string, sql: string): string {
  return execFileSync('sqlite3', ['-json', '-cmd', '.timeout 5000', dbPath], {
    input: sql,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  })
}

function reportExists(dbPath: string, asOf: string): boolean {
  const out = sqlite(dbPath, `SELECT 1 AS ok FROM market_reports WHERE report_type='market_mainline' AND scope='global' AND as_of_date=${sqlStr(asOf)} LIMIT 1;`).trim()
  return out ? JSON.parse(out).length > 0 : false
}

async function main(argv = process.argv.slice(2)): Promise<number> {
  const repoRoot = resolve(join(import.meta.dirname, '..', '..', '..'))
  const worldDir = join(repoRoot, 'world')
  const date = argVal(argv, '--date') ?? '2025-01-02'
  const configPath = argVal(argv, '--config') ?? join(worldDir, 'config', 'world-market-reports.yaml')
  const runId = argVal(argv, '--run-id') ?? `mainline-once-${date}-${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}`
  const worldRoot = resolve(argVal(argv, '--out-dir') ?? join(worldDir, 'runtime-mainline-once'))
  const fundDbPath = resolve(argVal(argv, '--fund-db') ?? join(repoRoot, 'data', 'fund.db'))
  const botId = 'reporter-mainline'

  const config = loadWorldConfig(configPath)
  const currentDateRef = { value: date }
  const getCurrentDate = (): string => currentDateRef.value

  mkdirSync(P.runDir(worldRoot, runId), { recursive: true })
  mkdirSync(P.memoryDir(worldRoot, runId), { recursive: true })
  mkdirSync(P.workspacesDir(worldRoot, runId), { recursive: true })
  const rlOpenclawDir = P.rlOpenclawDir(worldRoot, runId)
  mkdirSync(rlOpenclawDir, { recursive: true })

  const srcOc = openclawJsonSource(config)
  const dstOc = join(rlOpenclawDir, 'openclaw.json')
  if (existsSync(srcOc) && !existsSync(dstOc)) copyFileSync(srcOc, dstOc)

  const store = new MemoryStore(P.memoryStoreFile(worldRoot, runId))
  const memory = await createMemoryServer({ store, getCurrentDate })
  const simworldProxy = await createSimworldProxy({ upstreamUrl: config.simworldUpstreamUrl, getCurrentDate, clientId: runId })
  const strategyServer = await createStrategyServer({ worldRoot, runId, getCurrentDate, fundDbPath })

  const templateVars: Record<string, string> = { SIMWORLD_PROXY_URL: simworldProxy.url, STRATEGY_SERVER_URL: strategyServer.url }
  generateRlConfig(config, worldRoot, runId, memory.url, rlOpenclawDir)
  writeFileSync(P.worldDateOverrideFile(worldRoot, runId), date)

  const sourceDir = join(config.botsRoot, botId)
  const shadow = P.shadowWorkspaceDir(worldRoot, runId, botId)
  buildShadowWorkspace({ sourceDir, destDir: shadow, include: config.shadowInclude, templateVars })
  writeResearchLoopYaml(config, botId, shadow, memory.url, rlOpenclawDir)

  // Explicitly overwrite only this report type/date. Keep market_context intact
  // so reporter-mainline can read the upstream context report for the same date.
  sqlite(fundDbPath, `DELETE FROM market_reports WHERE report_type='market_mainline' AND scope='global' AND as_of_date=${sqlStr(date)};`)

  const server = await BotServer.start(botId, {
    argv: botServerArgv(config, botId, shadow, loopConfigPath(config, worldRoot, runId)),
    env: { WORLD_DATE_OVERRIDE_FILE: P.worldDateOverrideFile(worldRoot, runId), ...proxyEnvSupplement() },
    readyTimeoutMs: 60_000,
    onLog: line => process.stderr.write(line + '\n'),
    onNotification: method => { if (method === 'tool.call') process.stdout.write('.') },
  })

  try {
    const message = '直接重跑 2025-01 market_mainline。必须先调用 strategy-mcp.get_v5_mainline_plan，以 v5 scout 数据源为唯一主线与基金映射真值；fund_pool 必须直接来自 fund_matches.selected，禁止调用 sector_constituents/sector_index_match 自行匹配；提交 submit_market_report 后结束。'
    const result = await server.chat({
      message,
      session_key: `agent:${botId}:${runId}-${date}-${Date.now()}`,
      history: [],
    }, { timeoutMs: 900_000 })
    process.stdout.write(`\nchat_error=${result.chat_error ?? ''}\n`)
    if (!reportExists(fundDbPath, date)) {
      process.stderr.write(`market_mainline@${date} was not written\n`)
      return 1
    }
    const out = sqlite(fundDbPath, `SELECT as_of_date, content_md, structured_json FROM market_reports WHERE report_type='market_mainline' AND scope='global' AND as_of_date=${sqlStr(date)} LIMIT 1;`).trim()
    process.stdout.write(out + '\n')
    return 0
  } finally {
    await Promise.allSettled([
      server.shutdown({ timeoutMs: 5000 }),
      memory.close(), simworldProxy.close(), strategyServer.close(),
    ])
  }
}

main().then(code => { process.exitCode = code }).catch(err => {
  process.stderr.write((err instanceof Error ? err.stack ?? err.message : String(err)) + '\n')
  process.exitCode = 1
})
