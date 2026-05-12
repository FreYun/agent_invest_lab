import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runWorld } from '../src/run.ts'
import { BotServer } from '../src/botServer.ts'
import * as P from '../src/paths.ts'
import { readState } from '../src/state.ts'
import type { WorldConfig } from '../src/config.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const STUB = join(HERE, 'helpers', 'stubBotServer.ts')

// 用 stub 替换真实 research-loop-ts：忽略真实 argv，只用 --bot-id 起 stub。
function stubStartBotServer(botId: string, _argv: string[]): Promise<BotServer> {
  return BotServer.start(botId, { argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`], readyTimeoutMs: 5000 })
}

function setupWorldDir(opts: { bots: string[]; dates: string[]; withSrcWorkspaces?: boolean }): { worldRoot: string; config: WorldConfig; cleanup: () => void } {
  const root = mkdtempSync(join(tmpdir(), 'world-e2e-'))
  const worldRoot = join(root, 'world')
  // calendar
  mkdirSync(worldRoot, { recursive: true })
  writeFileSync(P.calendarFile(worldRoot), JSON.stringify({ trading_days: opts.dates }))
  // days/<date>/quotes.json (+ overview.md)
  for (const d of opts.dates) {
    mkdirSync(P.dayDir(worldRoot, d), { recursive: true })
    writeFileSync(P.quotesFile(worldRoot, d), JSON.stringify({ summary: `行情快照 ${d}` }))
    writeFileSync(P.overviewFile(worldRoot, d), `概览：${d} 上证小涨`)
  }
  // 源 workspace（research_loop_ts 路径在 e2e 里不会被真的执行，但 buildShadowWorkspace 需要它存在）
  const wsRoot = join(root, 'workspaces')
  for (const b of opts.bots) {
    const ws = join(wsRoot, `workspace-${b}`)
    mkdirSync(ws, { recursive: true })
    writeFileSync(join(ws, 'SOUL.md'), `# soul ${b}`)
  }
  // rl-config base
  const cfgBase = join(root, 'trading-rl-config.base.json')
  writeFileSync(cfgBase, JSON.stringify({ model: { primary: {} }, mcp: { servers: {} }, plugins: { 'memory-mem0': { enabled: true } } }))
  const config: WorldConfig = {
    researchLoopTs: '/nonexistent/research-loop-ts',
    workspaceRoot: wsRoot,
    bots: opts.bots,
    replay: { from: opts.dates[0], to: opts.dates[opts.dates.length - 1] },
    calendar: P.calendarFile(worldRoot),
    concurrency: 4,
    perBotTimeoutSeconds: 30,
    rlConfigBase: cfgBase,
    rlOpenclawDir: join(root, 'fake-openclaw'),
    shadowInclude: ['SOUL.md'],
  }
  return { worldRoot, config, cleanup: () => rmSync(root, { recursive: true, force: true }) }
}

test('runWorld replays 2 trading days for 2 bots: artifacts written, status done, generated rl-config has mem0 url', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1', 'bot7'], dates: ['2024-03-14', '2024-03-15'] })
  await runWorld({ worldRoot, config, runId: 'r1', startBotServer: stubStartBotServer })

  const st = readState(worldRoot)
  assert.equal(st.status, 'done')
  assert.equal(st.cursor, 2)
  assert.equal(st.run_id, 'r1')
  assert.ok(st.memory_port > 0)

  for (const d of ['2024-03-14', '2024-03-15']) {
    for (const b of ['bot1', 'bot7']) {
      assert.ok(existsSync(P.sentFile(worldRoot, 'r1', d, b)), `sent ${d} ${b}`)
      assert.match(readFileSync(P.sentFile(worldRoot, 'r1', d, b), 'utf8'), new RegExp(`当前世界日期：${d}`))
      const reply = JSON.parse(readFileSync(P.replyFile(worldRoot, 'r1', d, b), 'utf8'))
      assert.equal(reply.reply, 'stub reply')
      assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r1', d, b), 'utf8')).status, 'ok')
    }
  }
  // 首日发完整规则，次日发精简规则
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-14', 'bot1'), 'utf8'), /回放历史行情的沙盘/)
  assert.match(readFileSync(P.sentFile(worldRoot, 'r1', '2024-03-15', 'bot1'), 'utf8'), /规则同前/)
  // 影子 workspace + journal
  assert.match(readFileSync(join(P.shadowWorkspaceDir(worldRoot, 'r1', 'bot7'), 'SOUL.md'), 'utf8'), /soul bot7/)
  assert.ok(existsSync(join(P.shadowWorkspaceDir(worldRoot, 'r1', 'bot7'), 'memory', 'trading', 'journal.md')))
  // 生成的 rl-config 写入了 mem0 url
  const genCfg = JSON.parse(readFileSync(P.runConfigFile(worldRoot, 'r1'), 'utf8'))
  assert.match(genCfg.mcp.mem0, /^http:\/\/127\.0\.0\.1:\d+$/)
  // summary.json
  const summary = JSON.parse(readFileSync(P.summaryFile(worldRoot, 'r1'), 'utf8'))
  assert.equal(summary.run_id, 'r1')
  assert.equal(summary.days.length, 2)
  cleanup()
})

test('runWorld: a hanging bot is recorded as timeout but does not block the other bot or the day advance', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1', 'bot7'], dates: ['2024-03-14'] })
  config.perBotTimeoutSeconds = 1 // 1s 超时
  const start = (botId: string, _argv: string[]) => BotServer.start(botId, {
    argv: [process.execPath, '--experimental-strip-types', STUB, '--bot-id', botId, '--workspace', `/shadow/${botId}`],
    readyTimeoutMs: 5000,
    env: botId === 'bot7' ? { STUB_CHAT_MODE: 'hang' } : {},
  })
  await runWorld({ worldRoot, config, runId: 'r2', startBotServer: start })
  assert.equal(readState(worldRoot).status, 'done')
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r2', '2024-03-14', 'bot1'), 'utf8')).status, 'ok')
  assert.equal(JSON.parse(readFileSync(P.statusFile(worldRoot, 'r2', '2024-03-14', 'bot7'), 'utf8')).status, 'timeout')
  cleanup()
})

test('runWorld fails fast when a quotes.json is missing', async () => {
  const { worldRoot, config, cleanup } = setupWorldDir({ bots: ['bot1'], dates: ['2024-03-14', '2024-03-15'] })
  rmSync(P.quotesFile(worldRoot, '2024-03-15'), { force: true })
  await assert.rejects(() => runWorld({ worldRoot, config, runId: 'r3', startBotServer: stubStartBotServer }), /quotes\.json|missing/i)
  // state 应为 failed（若已写过）或不存在
  if (existsSync(P.stateFile(worldRoot))) assert.equal(readState(worldRoot).status, 'failed')
  cleanup()
})
